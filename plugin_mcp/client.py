"""Long-lived async wrapper around the official `mcp` SDK (Phase 010).

The SDK exposes `stdio_client` and `ClientSession` as anyio context managers
that internally start task groups. Opening them in one task and using them
from another runs into anyio's "cancel scope must exit on the same task"
rule. To stay safe, we own the connection in a dedicated *owner task* and
dispatch operations to it via an asyncio.Queue.

Callers (from any task) get a normal async API:

    client = MCPClient(StdioConfig(command=..., args=...))
    await client.connect()
    tools = await client.list_tools()
    text = await client.call_tool("echo", {"text": "hi"})
    await client.close()
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import logging

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("plugin-mcp.client")

DEFAULT_CONNECT_TIMEOUT = 90.0  # npx -y downloads the package on first run; give it room
DEFAULT_CALL_TIMEOUT = 30.0


class MCPClientError(Exception):
    """Wraps any MCP / transport error so callers can catch one type."""


@dataclass
class StdioConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StdioConfig:
        return cls(
            command=str(d.get("command", "")),
            args=list(d.get("args") or []),
            env=dict(d["env"]) if d.get("env") else None,
            cwd=str(d["cwd"]) if d.get("cwd") else None,
        )


# An owner-task job: a coroutine taking the live session, returning whatever.
Job = Callable[[ClientSession], Awaitable[Any]]


class MCPClient:
    """One live connection to one MCP server, owned by an internal task.

    All public methods are safe to call from any asyncio task. Internally,
    operations are funneled through a queue to the owner task, which is the
    only task that ever touches the SDK's context managers / session.
    """

    def __init__(
        self,
        config: StdioConfig,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._config = config
        self._connect_timeout = connect_timeout
        self._queue: asyncio.Queue[tuple[Job, asyncio.Future[Any]]] | None = None
        self._owner: asyncio.Task[None] | None = None
        self._ready: asyncio.Event = asyncio.Event()
        self._stopped: asyncio.Event = asyncio.Event()
        self._connect_error: BaseException | None = None

    @property
    def connected(self) -> bool:
        return (
            self._owner is not None
            and not self._owner.done()
            and self._ready.is_set()
            and not self._stopped.is_set()
        )

    async def connect(self) -> None:
        """Spawn the owner task and wait until it has the session up."""
        if self.connected:
            return
        if self._owner is not None and not self._owner.done():
            # Already connecting — wait for ready.
            await self._ready.wait()
            if self._connect_error:
                raise MCPClientError(str(self._connect_error)) from self._connect_error
            return

        self._queue = asyncio.Queue()
        self._ready.clear()
        self._stopped.clear()
        self._connect_error = None
        self._owner = asyncio.create_task(self._run(), name="mcp-owner")

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._connect_timeout)
        except TimeoutError as e:
            await self._kill_owner()
            raise MCPClientError(f"MCP connect timed out after {self._connect_timeout}s") from e

        if self._connect_error is not None:
            err = self._connect_error
            await self._kill_owner()
            raise MCPClientError(f"MCP connect failed: {err}") from err

    async def close(self) -> None:
        if self._owner is None:
            return
        if not self._stopped.is_set():
            self._stopped.set()
            # Wake the owner so it can exit its wait().
            if self._queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait((self._noop, asyncio.get_running_loop().create_future()))
        try:
            await asyncio.wait_for(self._owner, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            await self._kill_owner()
        finally:
            self._owner = None
            self._queue = None

    async def list_tools(self) -> list[dict[str, Any]]:
        async def job(session: ClientSession) -> list[dict[str, Any]]:
            resp = await session.list_tools()
            out: list[dict[str, Any]] = []
            for t in resp.tools:
                destructive = bool(
                    getattr(t.annotations, "destructiveHint", False)
                    if t.annotations
                    else False
                )
                read_only = bool(
                    getattr(t.annotations, "readOnlyHint", False)
                    if t.annotations
                    else False
                )
                out.append(
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                        "destructive": destructive,
                        "read_only": read_only,
                    }
                )
            return out

        return await self._dispatch(job, label="list_tools")

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> str:
        async def job(session: ClientSession) -> str:
            result = await asyncio.wait_for(session.call_tool(name, args or {}), timeout=timeout)
            text_parts: list[str] = []
            for block in result.content or []:
                text = getattr(block, "text", None)
                if text:
                    text_parts.append(text)
            joined = "\n".join(text_parts).strip()
            if result.isError:
                raise MCPClientError(joined or f"tool '{name}' returned isError without content")
            return joined

        return await self._dispatch(job, label=f"call_tool:{name}")

    # ---------- internal ----------

    @staticmethod
    async def _noop(_: ClientSession) -> None:
        return None

    async def _dispatch(self, job: Job, *, label: str) -> Any:
        if not self.connected or self._queue is None:
            raise MCPClientError(f"Not connected (cannot dispatch {label})")
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((job, fut))
        return await fut

    async def _kill_owner(self) -> None:
        if self._owner is not None and not self._owner.done():
            self._owner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._owner
        self._owner = None
        self._queue = None
        self._stopped.set()
        self._ready.set()

    async def _run(self) -> None:
        """Owner task: open the session, then loop processing jobs until close."""
        assert self._queue is not None

        # Resolve the command to an absolute path. The MCP SDK passes
        # `env` directly to the child process, which means a bare command
        # like `npx` only works if PATH is inherited. Resolving the
        # command ourselves (using the parent's PATH) makes this robust
        # to env replacement and to PATHs that aren't set in subprocesses
        # spawned by GUI-launched servers.
        resolved_command = shutil.which(self._config.command) or self._config.command

        # Merge custom env into the parent process env (don't REPLACE it).
        # Without this, the subprocess loses PATH, HOME, NODE_PATH, etc.
        # and basic tools like `npx` can't find their dependencies.
        merged_env: dict[str, str] | None
        if self._config.env:
            merged_env = {**os.environ, **self._config.env}
        else:
            merged_env = None  # mcp SDK falls back to os.environ

        params = StdioServerParameters(
            command=resolved_command,
            args=list(self._config.args),
            env=merged_env,
            cwd=self._config.cwd,
        )

        # Capture the subprocess's stderr instead of letting it dump to the
        # parent's stderr. When connection fails, the captured text usually
        # contains the real cause (npm 404, missing module, auth rejection,
        # etc.) — and we surface it to the agent so it can diagnose instead
        # of guessing from a generic "TaskGroup error".
        #
        # We must use a real tempfile (not StringIO) because the MCP SDK
        # passes errlog directly as `stderr=` to `anyio.open_process`,
        # which needs a real file descriptor (fileno()).
        errlog = tempfile.TemporaryFile(mode="w+b")

        def _read_stderr() -> str:
            try:
                errlog.flush()
                errlog.seek(0)
                data = errlog.read()
                return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
            except Exception:  # noqa: BLE001
                return ""

        try:
            async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                self._ready.set()
                while not self._stopped.is_set():
                    try:
                        job, fut = await self._queue.get()
                    except asyncio.CancelledError:
                        break
                    if self._stopped.is_set():
                        if not fut.done():
                            fut.cancel()
                        break
                    try:
                        result = await job(session)
                        if not fut.done():
                            fut.set_result(result)
                    except Exception as e:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(e)
        except BaseException as e:  # noqa: BLE001
            stderr_text = _read_stderr().strip()
            if stderr_text:
                tail = stderr_text[-600:]
                self._connect_error = MCPClientError(
                    f"{e}\n--- subprocess stderr ---\n{tail}"
                )
            else:
                self._connect_error = e
            log.warning("mcp owner task exit: %s | stderr=%s", e, stderr_text[:400])
        finally:
            with contextlib.suppress(Exception):
                errlog.close()
            self._stopped.set()
            self._ready.set()
            # Drain any pending futures with an error.
            if self._queue is not None:
                while not self._queue.empty():
                    try:
                        _, fut = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if not fut.done():
                        fut.set_exception(MCPClientError("MCP session ended"))
