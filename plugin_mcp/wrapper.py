"""Wrap an MCP tool as a Luna tool (Phase 010)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from luna_sdk import ToolDef, ToolHandler

from .client import MCPClient, MCPClientError

# Anthropic tool names must match ^[a-zA-Z0-9_-]+$. We sanitise to that.
_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")

NAMESPACE_SEP = "__"


def safe_segment(s: str) -> str:
    cleaned = _NAME_SAFE.sub("_", s.strip())
    return cleaned or "x"


def wrapped_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp{NAMESPACE_SEP}{safe_segment(server_name)}{NAMESPACE_SEP}{safe_segment(tool_name)}"


def plugin_owner_name(server_name: str) -> str:
    """The plugin-owner string used in the ToolRegistry. Lets us unregister
    one server's tools cleanly via `unregister_plugin(...)`."""
    return f"plugin-mcp:server:{safe_segment(server_name)}"


def _wrap_description(server_name: str, tool: dict[str, Any]) -> str:
    desc = tool.get("description") or tool.get("name") or "MCP tool"
    base = f"[via mcp:{server_name}] {desc}"
    if tool.get("destructive"):
        base = f"[destructive] {base}"
    return base[:1024]


def build_wrapped_tool(
    server_name: str,
    tool: dict[str, Any],
    client_getter: Callable[[], MCPClient | None],
) -> tuple[ToolDef, ToolHandler]:
    """Return (ToolDef, handler) for a single MCP tool.

    `client_getter` returns the current live client for the server (or None
    if the server is no longer enabled). We resolve lazily so a reconnect or
    refresh doesn't leave a wrapper pointing at a dead client.
    """

    name = wrapped_tool_name(server_name, tool["name"])
    schema = tool.get("input_schema") or {"type": "object", "properties": {}}
    description = _wrap_description(server_name, tool)
    original_name = tool["name"]

    # MCP annotation hints → approval policy. Destructive tools MUST go
    # through the approval engine — they're exactly why it exists. Read-only
    # tools stay auto-approved (no side effects). Anything else defaults to
    # auto_approve too; owners can override per-tool via /api/approvals/policy.
    destructive = bool(tool.get("destructive"))
    read_only = bool(tool.get("read_only"))
    if destructive:
        policy = "prompt_always"
        risk_level = "high"
    elif read_only:
        policy = "auto_approve"
        risk_level = "low"
    else:
        policy = "auto_approve"
        risk_level = "medium"

    async def handler(**kwargs: Any) -> dict[str, Any]:
        client = client_getter()
        if client is None or not client.connected:
            # 005.82-fixes2 item D: return a clear, structured offline result the
            # agent can relay ("reconnect it") instead of raising a raw exception
            # that surfaces as "Tool X raised: MCPClientError: …".
            return {
                "server": server_name,
                "tool": original_name,
                "error": "server_offline",
                "detail": (
                    f"The '{server_name}' MCP server is not connected, so '{original_name}' "
                    f"could not run. Reconnect it under Settings → MCP and try again."
                ),
            }
        try:
            text = await client.call_tool(original_name, kwargs)
        except MCPClientError as e:
            return {"server": server_name, "tool": original_name, "error": "tool_failed", "detail": str(e)}
        return {"server": server_name, "tool": original_name, "result": text}

    definition = ToolDef(
        name=name,
        description=description,
        parameters=schema,
        policy=policy,
        risk_level=risk_level,
    )
    return definition, handler
