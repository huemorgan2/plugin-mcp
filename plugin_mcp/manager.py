"""ServerManager — orchestrates MCP servers + their wrapped tools (Phase 010).

State machine (per server):

  not-exists  -- add()     --> exists, disabled
  disabled    -- enable()  --> enabled (client connected, tools registered)
  enabled     -- disable() --> disabled (tools unregistered, client closed)
  any         -- remove()  --> not-exists

`refresh(name)` re-discovers tools on the live client and re-registers.
`boot()` enumerates `enabled=true` rows and connects them concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from luna_sdk import PluginContext, ToolRegistry

from .client import MCPClient, StdioConfig
from .models import MCPServerRow, MCPToolRow
from .wrapper import build_wrapped_tool, plugin_owner_name

log = logging.getLogger("plugin-mcp.manager")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MCPManagerError(Exception):
    pass


class ServerManager:
    """Owns the live MCPClient objects for all currently-enabled servers."""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._clients: dict[str, MCPClient] = {}
        # Snapshot of last-discovered tools per server, kept in memory to
        # power refresh + the built-in `mcp_list_tools`.
        self._tool_snapshot: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._ctx.tool_registry

    def client_for(self, name: str) -> MCPClient | None:
        return self._clients.get(name)

    # ---------- CRUD ----------

    async def list_servers(self) -> list[dict[str, Any]]:
        async with self._ctx.db_session_factory() as s:
            rows = (await s.execute(select(MCPServerRow).order_by(MCPServerRow.name))).scalars().all()
            out: list[dict[str, Any]] = []
            for r in rows:
                client = self._clients.get(r.name)
                tool_count = len(self._tool_snapshot.get(r.name, []))
                out.append(
                    {
                        "name": r.name,
                        "transport_type": r.transport_type,
                        "config": r.config,
                        "enabled": r.enabled,
                        "connected": client is not None and client.connected,
                        "tool_count": tool_count,
                        "last_connected_at": r.last_connected_at.isoformat()
                        if r.last_connected_at
                        else None,
                        "last_error": r.last_error,
                    }
                )
            return out

    async def get(self, name: str) -> dict[str, Any]:
        async with self._ctx.db_session_factory() as s:
            row = (await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))).scalar_one_or_none()
            if row is None:
                raise MCPManagerError(f"server '{name}' not found")
            return {
                "name": row.name,
                "transport_type": row.transport_type,
                "config": row.config,
                "enabled": row.enabled,
                "connected": (c := self._clients.get(name)) is not None and c.connected,
                "tool_count": len(self._tool_snapshot.get(name, [])),
                "last_connected_at": row.last_connected_at.isoformat()
                if row.last_connected_at
                else None,
                "last_error": row.last_error,
            }

    async def get_tools(self, name: str) -> list[dict[str, Any]]:
        async with self._ctx.db_session_factory() as s:
            row = (await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))).scalar_one_or_none()
            if row is None:
                raise MCPManagerError(f"server '{name}' not found")
            tools = (
                await s.execute(
                    select(MCPToolRow)
                    .where(MCPToolRow.server_id == row.id)
                    .order_by(MCPToolRow.tool_name)
                )
            ).scalars().all()
            return [
                {
                    "name": t.tool_name,
                    "description": t.description or "",
                    "destructive": t.destructive,
                    "schema": t.schema,
                }
                for t in tools
            ]

    async def add(
        self,
        name: str,
        transport_type: str,
        config: dict[str, Any],
        *,
        enable: bool = False,
        author: str = "user",
    ) -> dict[str, Any]:
        if transport_type != "stdio":
            raise MCPManagerError("only stdio transport is supported in this phase")
        if not str(config.get("command", "")).strip():
            raise MCPManagerError("config.command is required")
        async with self._ctx.db_session_factory() as s:
            existing = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if existing is not None:
                raise MCPManagerError(f"server '{name}' already exists")
            row = MCPServerRow(
                name=name,
                transport_type=transport_type,
                config=config,
                enabled=False,
            )
            s.add(row)
            await self._ctx.record_version(
                "mcp_server",
                name,
                before=None,
                after={"transport_type": transport_type, "config": config, "enabled": False},
                author=author,
                reason="added",
                session=s,
            )
            await s.commit()
        await self._ctx.events.emit("mcp.server_added", {"name": name})
        if enable:
            await self.enable(name, author=author)
        return await self.get(name)

    async def update_config(
        self, name: str, *, config: dict[str, Any], author: str = "user"
    ) -> dict[str, Any]:
        async with self._ctx.db_session_factory() as s:
            row = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise MCPManagerError(f"server '{name}' not found")
            before = dict(row.config or {})
            row.config = config
            row.updated_at = _utcnow()
            await self._ctx.record_version(
                "mcp_server",
                name,
                before={"config": before},
                after={"config": config},
                author=author,
                reason="config-updated",
                session=s,
            )
            await s.commit()
        # If enabled, reconnect to pick up the new config.
        if name in self._clients:
            await self.disable(name, author="system")
            await self.enable(name, author="system")
        return await self.get(name)

    async def remove(self, name: str, *, author: str = "user") -> None:
        if name in self._clients:
            await self.disable(name, author=author, reason="removing")
        async with self._ctx.db_session_factory() as s:
            row = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise MCPManagerError(f"server '{name}' not found")
            before = {
                "transport_type": row.transport_type,
                "config": row.config,
                "enabled": row.enabled,
            }
            await s.execute(delete(MCPToolRow).where(MCPToolRow.server_id == row.id))
            await s.delete(row)
            await self._ctx.record_version(
                "mcp_server",
                name,
                before=before,
                after={},
                author=author,
                reason="removed",
                session=s,
            )
            await s.commit()
        self._tool_snapshot.pop(name, None)
        await self._ctx.events.emit("mcp.server_removed", {"name": name})

    # ---------- enable / disable ----------

    async def enable(self, name: str, *, author: str = "user") -> dict[str, Any]:
        async with self._lock:
            if name in self._clients and self._clients[name].connected:
                return await self.get(name)
            row_id, config = await self._load_for_connect(name)
            config = await self._resolve_vault_env(config, server=name)
            client = MCPClient(StdioConfig.from_dict(config))
            try:
                await client.connect()
                tools = await client.list_tools()
            except Exception as e:  # noqa: BLE001
                await client.close()
                await self._record_error(name, str(e))
                raise MCPManagerError(f"failed to connect to '{name}': {e}") from e

            self._clients[name] = client
            self._tool_snapshot[name] = tools
            await self._persist_tools(row_id, tools)
            self._register_tools(name, tools)
            await self._mark_enabled(name, ok=True)
            s_after = await self.get(name)
            await self._ctx.record_version(
                "mcp_server",
                name,
                before={"enabled": False},
                after={"enabled": True, "tool_count": len(tools)},
                author=author,
                reason="enabled",
            )
            await self._ctx.events.emit(
                "mcp.server_enabled", {"name": name, "tool_count": len(tools)}
            )
            return s_after

    async def disable(self, name: str, *, author: str = "user", reason: str = "disabled") -> dict[str, Any]:
        async with self._lock:
            client = self._clients.pop(name, None)
            self._unregister_tools(name)
            if client is not None:
                try:
                    await client.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("mcp close error (server=%s): %s", name, e)
            await self._mark_enabled(name, ok=False, clear_error=False)
            await self._ctx.record_version(
                "mcp_server",
                name,
                before={"enabled": True},
                after={"enabled": False},
                author=author,
                reason=reason,
            )
            await self._ctx.events.emit("mcp.server_disabled", {"name": name})
            return await self.get(name)

    async def refresh(self, name: str) -> dict[str, Any]:
        async with self._lock:
            client = self._clients.get(name)
            if client is None or not client.connected:
                raise MCPManagerError(f"server '{name}' is not enabled")
            tools = await client.list_tools()
            self._tool_snapshot[name] = tools
            row_id = await self._lookup_row_id(name)
            await self._persist_tools(row_id, tools)
            # Re-register: drop old wrappers for this server, register new ones.
            self._unregister_tools(name)
            self._register_tools(name, tools)
        return await self.get(name)

    # ---------- test (ad-hoc) ----------

    async def test_config(self, transport_type: str, config: dict[str, Any]) -> dict[str, Any]:
        if transport_type != "stdio":
            raise MCPManagerError("only stdio transport is supported in this phase")
        config = await self._resolve_vault_env(config, server=config.get("name", "test"))
        client = MCPClient(StdioConfig.from_dict(config))
        try:
            await client.connect()
            tools = await client.list_tools()
            return {"ok": True, "tool_count": len(tools), "tools": [t["name"] for t in tools]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            await client.close()

    # ---------- boot/shutdown ----------

    async def boot(self) -> None:
        """Connect to every enabled server on plugin load."""
        async with self._ctx.db_session_factory() as s:
            enabled_rows = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.enabled.is_(True)))
            ).scalars().all()
            names = [r.name for r in enabled_rows]

        if not names:
            return

        async def _try_enable(n: str) -> None:
            try:
                await self.enable(n, author="system")
            except Exception as e:  # noqa: BLE001
                log.warning("mcp boot enable failed (server=%s): %s", n, e)

        await asyncio.gather(*[_try_enable(n) for n in names])

    async def shutdown(self) -> None:
        names = list(self._clients.keys())
        for n in names:
            try:
                await self.disable(n, author="system", reason="shutdown")
            except Exception as e:  # noqa: BLE001
                log.warning("mcp shutdown error (server=%s): %s", n, e)

    # ---------- internals ----------

    async def _resolve_vault_env(
        self, config: dict[str, Any], *, server: str
    ) -> dict[str, Any]:
        """007.015: replace `vault:<name>` env values with the resolved secret.

        Resolution goes through the plugin's SCOPED vault (requester=plugin-mcp),
        so the ACL applies — plugin-mcp must be granted access to the credential.
        The resolved value enters ONLY the spawned subprocess env; the DB keeps
        the `vault:` reference, never the secret. On a missing credential or a
        denied grant the spawn fails with a clear, actionable message.
        """
        env = config.get("env")
        if not env or not isinstance(env, dict):
            return config
        refs = {k: v for k, v in env.items() if isinstance(v, str) and v.startswith("vault:")}
        if not refs:
            return config
        vault = self._ctx.vault
        if vault is None:
            raise MCPManagerError(
                f"server '{server}' references vault credentials but no vault is loaded"
            )
        resolved_env = dict(env)
        for key, ref in refs.items():
            cred_name = ref[len("vault:"):].strip()
            try:
                cred = await vault.get_credential(cred_name)
            except PermissionError:
                raise MCPManagerError(
                    f"MCP server '{server}' needs credential '{cred_name}', but "
                    f"plugin-mcp isn't granted access. Grant it in Settings → Key "
                    f"Vault (Shared), or have the agent call grant_credential_access."
                ) from None
            except KeyError:
                raise MCPManagerError(
                    f"MCP server '{server}' references credential '{cred_name}' "
                    f"which is not in the vault. Store it first (Settings → Key Vault)."
                ) from None
            resolved_env[key] = cred.value
        out = dict(config)
        out["env"] = resolved_env
        return out

    async def _load_for_connect(self, name: str) -> tuple[Any, dict[str, Any]]:
        async with self._ctx.db_session_factory() as s:
            row = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise MCPManagerError(f"server '{name}' not found")
            return row.id, dict(row.config)

    async def _lookup_row_id(self, name: str) -> Any:
        async with self._ctx.db_session_factory() as s:
            row = (
                await s.execute(select(MCPServerRow.id).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                raise MCPManagerError(f"server '{name}' not found")
            return row

    async def _persist_tools(self, server_id: Any, tools: list[dict[str, Any]]) -> None:
        async with self._ctx.db_session_factory() as s:
            await s.execute(delete(MCPToolRow).where(MCPToolRow.server_id == server_id))
            for t in tools:
                s.add(
                    MCPToolRow(
                        server_id=server_id,
                        tool_name=t["name"],
                        description=t.get("description") or None,
                        schema=t.get("input_schema") or {"type": "object", "properties": {}},
                        destructive=bool(t.get("destructive")),
                    )
                )
            await s.commit()

    async def _mark_enabled(self, name: str, *, ok: bool, clear_error: bool = True) -> None:
        async with self._ctx.db_session_factory() as s:
            row = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                return
            row.enabled = ok
            if ok:
                row.last_connected_at = _utcnow()
                if clear_error:
                    row.last_error = None
            await s.commit()

    async def _record_error(self, name: str, msg: str) -> None:
        async with self._ctx.db_session_factory() as s:
            row = (
                await s.execute(select(MCPServerRow).where(MCPServerRow.name == name))
            ).scalar_one_or_none()
            if row is None:
                return
            row.enabled = False
            row.last_error = msg[:2000]
            await s.commit()
        await self._ctx.events.emit("mcp.connect_failed", {"name": name, "error": msg})

    def _register_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None:
        owner = plugin_owner_name(server_name)
        # Defensive: clear any existing tools for this server first.
        self.tool_registry.unregister_plugin(owner)

        def _getter(name: str = server_name):
            return self._clients.get(name)

        for tool in tools:
            try:
                defn, handler = build_wrapped_tool(server_name, tool, _getter)
                self.tool_registry.register(owner, defn, handler)
            except ValueError as e:
                # Name collision with another tool — log and skip.
                log.warning(
                    "mcp tool name collision (server=%s, tool=%s): %s",
                    server_name,
                    tool.get("name"),
                    e,
                )

    def _unregister_tools(self, server_name: str) -> None:
        owner = plugin_owner_name(server_name)
        self.tool_registry.unregister_plugin(owner)
