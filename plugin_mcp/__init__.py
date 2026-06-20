"""plugin-mcp — universal tool adapter via the Model Context Protocol.

Connects Luna to any MCP server (stdio transport, Phase 010 scope).
Discovered tools are wrapped into the Luna tool registry under the name
`mcp__<server>__<tool>`. Servers are disabled by default; the user (or CLI)
must enable each one explicitly — that's the runtime safety gate while the
approval engine (Phase 005) isn't built yet.
"""

from __future__ import annotations

import logging
from typing import Any

from luna_sdk import LunaPlugin, PluginContext, PluginManifest, SettingsTab, ToolDef

from . import state
from .manager import MCPManagerError, ServerManager
from .models import MCPServerRow, MCPToolRow

log = logging.getLogger("plugin-mcp")


class MCPPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-mcp",
        version="0.1.0",
        description="Universal MCP (Model Context Protocol) tool adapter.",
        category="connectors",
        system_app=False,
        critical=False,
        license="MIT",
        depends_on=["plugin-vault"],
        db_tables=["plugin_mcp_servers", "plugin_mcp_tools"],
        routes_module="routes",
        settings_tabs=[
            SettingsTab(
                id="mcp",
                label="MCP Servers",
                icon="server",
                sort_order=60,
                iframe_src="/api/p/plugin-mcp/ui/settings/",
            ),
        ],
        interfaces={"webui": "interface/webui"},
    )

    def __init__(self) -> None:
        self._manager: ServerManager | None = None

    @property
    def manager(self) -> ServerManager:
        if self._manager is None:
            raise RuntimeError("plugin-mcp not loaded")
        return self._manager

    async def on_load(self, ctx: PluginContext) -> None:
        # Idempotent table create so plugin works on fresh SQLite databases
        # (Postgres in dev uses the Alembic migration 0005_mcp).
        async with ctx.engine.begin() as conn:
            await conn.run_sync(MCPServerRow.__table__.create, checkfirst=True)
            await conn.run_sync(MCPToolRow.__table__.create, checkfirst=True)

        self._manager = ServerManager(ctx)
        # Expose the manager to the routes via the module singleton (decoupled
        # from the core plugin registry).
        state.set_manager(self._manager)

        # Register the always-on built-in tools BEFORE booting so they're
        # immediately available even if servers fail to come up.
        self._register_builtins(ctx)

        # Connect previously-enabled servers (non-fatal on failure).
        try:
            await self._manager.boot()
        except Exception as e:  # noqa: BLE001
            log.warning("mcp boot error: %s", e)

        wrapped = sum(
            1 for t in ctx.tool_registry.all() if t.plugin.startswith("plugin-mcp:")
        )
        log.info(
            "plugin-mcp loaded (tables=2, wrapped_tools=%d)", wrapped
        )

    async def on_unload(self) -> None:
        if self._manager is not None:
            await self._manager.shutdown()
        self._manager = None
        state.set_manager(None)

    async def prompt_sections(self) -> list[str]:
        """007.010: surface ENABLED MCP servers (and their tool counts) so the
        agent knows what MCP already provides and doesn't re-add a server it
        already has. Reads local rows via the manager — no live probe. Returns
        [] when nothing is enabled, so no empty header is emitted."""
        if self._manager is None:
            return []

        servers = [s for s in await self._manager.list_servers() if s.get("enabled")]
        if not servers:
            return []

        lines = [
            "## MCP servers (enabled)",
            "Tools from these are available as `mcp__<server>__<tool>`. Check "
            "here and the connected apps above before adding a new integration.",
            "",
        ]
        for s in servers:
            name = s.get("name", "?")
            if s.get("connected"):
                lines.append(f"- `{name}` (connected, {s.get('tool_count', 0)} tools)")
            else:
                err = s.get("last_error")
                suffix = f" — error: {err}" if err else ""
                lines.append(f"- `{name}` (enabled but not connected{suffix})")

        return ["\n".join(lines)]

    # ---------- built-in tools ----------

    def _register_builtins(self, ctx: PluginContext) -> None:
        mgr = self.manager

        async def _list_servers() -> dict[str, Any]:
            return {"servers": await mgr.list_servers()}

        async def _list_tools(server: str) -> dict[str, Any]:
            try:
                tools = await mgr.get_tools(server)
            except MCPManagerError as e:
                return {"error": str(e), "tools": []}
            return {"server": server, "tools": tools}

        async def _call_tool(server: str, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            client = mgr.client_for(server)
            if client is None or not client.connected:
                return {"error": f"server '{server}' is not enabled"}
            try:
                text = await client.call_tool(tool, args or {})
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}
            return {"server": server, "tool": tool, "result": text}

        async def _refresh(server: str) -> dict[str, Any]:
            try:
                status = await mgr.refresh(server)
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}
            return {"ok": True, "tool_count": status.get("tool_count", 0)}

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_list_servers",
                description=(
                    "List configured MCP servers and their status (enabled, "
                    "connected, tool_count, last_error). Use when the user "
                    "asks what MCP integrations are available."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            _list_servers,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_list_tools",
                description=(
                    "List the tools exposed by a specific MCP server. Use when "
                    "deciding which mcp_* tool to call, or when the user asks "
                    "what a particular MCP server can do."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "server": {"type": "string", "description": "The MCP server name."},
                    },
                    "required": ["server"],
                },
            ),
            _list_tools,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_call_tool",
                description=(
                    "Call an MCP tool by server + tool name. Prefer the wrapped "
                    "tool (mcp__<server>__<tool>) when available; use this only "
                    "when the wrapped name is unknown."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "args": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["server", "tool"],
                },
            ),
            _call_tool,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_refresh",
                description="Re-discover tools on an enabled MCP server.",
                parameters={
                    "type": "object",
                    "properties": {"server": {"type": "string"}},
                    "required": ["server"],
                },
            ),
            _refresh,
        )

        # ---------- self-extension tools (010.2) + management tools (010.3) ----------

        async def _add_server(
            name: str,
            command: str | None = None,
            args: list[str] | None = None,
            env: dict[str, str] | None = None,
            cwd: str | None = None,
        ) -> dict[str, Any]:
            import json as _json

            from .known_servers import canonical_name, lookup

            name = canonical_name(name)
            known = lookup(name)

            if command is None and known:
                command = known["command"]
                if args is None:
                    args = known["args"]

            if not command:
                return {"error": "command is required (or use a known server name like 'monday', 'github', 'slack')"}

            config: dict[str, Any] = {"command": command}
            if args:
                if isinstance(args, str):
                    try:
                        args = _json.loads(args)
                    except (ValueError, TypeError):
                        args = [a.strip() for a in args.split(",") if a.strip()]
                if not isinstance(args, list):
                    return {"error": f"args must be a list of strings, got {type(args).__name__}"}
                config["args"] = [str(a) for a in args]
            if env:
                config["env"] = env
            if cwd:
                config["cwd"] = cwd
            try:
                status = await mgr.add(name, "stdio", config, enable=False, author="luna")
            except MCPManagerError as e:
                return {"error": str(e)}

            result: dict[str, Any] = {
                "ok": True,
                "server": status,
            }
            if known and known.get("required_env"):
                missing = [k for k in known["required_env"] if k not in (env or {})]
                if missing:
                    result["required_env"] = missing
                    result["setup_hint"] = known.get("setup_hint", "")
                    result["note"] = (
                        f"Server staged but needs environment variables: {', '.join(missing)}. "
                        "Ask the owner for these values, then call mcp_update_server to set them."
                    )
                else:
                    result["note"] = (
                        "Server staged with all required env vars. "
                        "Call mcp_enable_server to connect and discover tools."
                    )
            else:
                result["note"] = (
                    "Server staged in DISABLED state. "
                    "Call mcp_enable_server to connect, or mcp_update_server to add env vars first."
                )
            return result

        async def _test_config(
            command: str,
            args: list[str] | None = None,
            env: dict[str, str] | None = None,
            cwd: str | None = None,
        ) -> dict[str, Any]:
            config: dict[str, Any] = {"command": command}
            if args:
                config["args"] = args
            if env:
                config["env"] = env
            if cwd:
                config["cwd"] = cwd
            try:
                result = await mgr.test_config("stdio", config)
            except MCPManagerError as e:
                return {"ok": False, "error": str(e)}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            return {"ok": True, **result}

        async def _update_server(
            name: str,
            command: str | None = None,
            args: list[str] | None = None,
            env: dict[str, str] | None = None,
            replace_env: bool = False,
            cwd: str | None = None,
        ) -> dict[str, Any]:
            try:
                current = await mgr.get(name)
            except MCPManagerError as e:
                return {"error": str(e)}

            config = dict(current["config"])
            if command is not None:
                config["command"] = command
            if args is not None:
                config["args"] = args
            if env is not None:
                if replace_env:
                    config["env"] = env
                else:
                    existing_env = dict(config.get("env") or {})
                    existing_env.update(env)
                    config["env"] = existing_env
            if cwd is not None:
                config["cwd"] = cwd

            try:
                status = await mgr.update_config(name, config=config, author="luna")
            except MCPManagerError as e:
                return {"error": str(e)}
            return {"ok": True, "server": status}

        async def _enable_server(name: str) -> dict[str, Any]:
            try:
                status = await mgr.enable(name, author="luna")
            except MCPManagerError as e:
                return {
                    "ok": False,
                    "error": str(e),
                    "hint": (
                        "Read the error above. Common causes: missing API key (add via "
                        "mcp_update_server with env), wrong command/package name, or the "
                        "server process crashed on startup. Use mcp_get_server_status to "
                        "see the full last_error."
                    ),
                }
            return {"ok": True, "server": status}

        async def _disable_server(name: str) -> dict[str, Any]:
            try:
                status = await mgr.disable(name, author="luna")
            except MCPManagerError as e:
                return {"error": str(e)}
            return {"ok": True, "server": status}

        async def _remove_server(name: str) -> dict[str, Any]:
            try:
                await mgr.remove(name, author="luna")
            except MCPManagerError as e:
                return {"error": str(e)}
            return {"ok": True, "removed": name}

        async def _get_server_status(name: str | None = None) -> dict[str, Any]:
            if name:
                try:
                    return await mgr.get(name)
                except MCPManagerError as e:
                    return {"error": str(e)}
            return {"servers": await mgr.list_servers()}

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_add_server",
                description=(
                    "Before adding a server, FIRST check the connected apps in your "
                    "prompt (and connector_list_connected) and the enabled MCP "
                    "servers — only add a new MCP server if no existing connector or "
                    "server already covers the request. "
                    "USE THIS WHEN THE OWNER ASKS FOR AN INTEGRATION YOU DON'T HAVE. "
                    "Adds a new MCP server in DISABLED state. For well-known servers "
                    "(monday, github, slack, notion, linear, postgres, filesystem, "
                    "brave-search, firecrawl, google-drive), just pass the name — "
                    "command and args are auto-filled. After adding, check the response "
                    "for required_env — if present, get the API key into the VAULT "
                    "(request_credential secure form), grant plugin-mcp access "
                    "(grant_credential_access), set the env value to 'vault:<name>' "
                    "via mcp_update_server, then mcp_enable_server. NEVER paste a raw "
                    "secret into env — use a 'vault:<name>' reference so the key is "
                    "resolved server-side and never stored in plaintext or seen by you."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Server name (use a known name like 'monday' or a custom short name)."},
                        "command": {"type": "string", "description": "Executable to spawn. Optional for known servers."},
                        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments. Optional for known servers."},
                        "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Env vars. For secrets use a vault reference: 'vault:<credential_name>' (e.g. 'vault:brave_api_key') — never a raw key."},
                        "cwd": {"type": "string", "description": "Optional working directory."},
                    },
                    "required": ["name"],
                },
            ),
            _add_server,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_test_server_config",
                description=(
                    "Dry-run an MCP stdio server config without persisting it. Connects, "
                    "lists tools, then disconnects. Use to verify a config works before "
                    "calling mcp_add_server."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "env": {"type": "object", "additionalProperties": {"type": "string"}},
                        "cwd": {"type": "string"},
                    },
                    "required": ["command"],
                },
            ),
            _test_config,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_update_server",
                description=(
                    "Update the config of an existing MCP server. Use to set API keys "
                    "(via env), change the command/args, or fix a broken config. "
                    "Env vars are MERGED by default (existing keys preserved, new keys "
                    "added/updated). Set replace_env=true to replace all env vars. "
                    "If the server is currently enabled, it will auto-reconnect with "
                    "the new config. FOR SECRETS: set the env value to a vault "
                    "reference 'vault:<credential_name>' (e.g. 'vault:brave_api_key') "
                    "— never the raw key. The key must be in the vault and plugin-mcp "
                    "granted access (grant_credential_access). The DB stores the "
                    "reference, not the secret."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Server name to update."},
                        "command": {"type": "string", "description": "New command (optional)."},
                        "args": {"type": "array", "items": {"type": "string"}, "description": "New args (optional)."},
                        "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Env vars to set/update. For secrets use 'vault:<credential_name>' (resolved server-side), never a raw key."},
                        "replace_env": {"type": "boolean", "description": "If true, replace all env vars instead of merging."},
                        "cwd": {"type": "string", "description": "New working directory (optional)."},
                    },
                    "required": ["name"],
                },
            ),
            _update_server,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_enable_server",
                description=(
                    "Enable a server: connects to the process, discovers tools, and "
                    "makes them available for use. If connection fails, read the error "
                    "in the response — common issues: missing API key → use "
                    "mcp_update_server to add env vars; wrong package → check the "
                    "command/args; process crash → use mcp_get_server_status for details."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Server name to enable."},
                    },
                    "required": ["name"],
                },
            ),
            _enable_server,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_disable_server",
                description=(
                    "Disable a server: disconnects and unregisters its tools. "
                    "Config is preserved — can be re-enabled later."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Server name to disable."},
                    },
                    "required": ["name"],
                },
            ),
            _disable_server,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_remove_server",
                description=(
                    "Remove a server entirely (config + tools deleted). "
                    "Use only when the owner wants to completely remove an integration."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Server name to remove."},
                    },
                    "required": ["name"],
                },
            ),
            _remove_server,
        )

        ctx.tool_registry.register(
            "plugin-mcp",
            ToolDef(
                name="mcp_get_server_status",
                description=(
                    "Get detailed status of an MCP server (or all servers if name is "
                    "omitted). Returns config, enabled, connected, tool_count, and "
                    "last_error. Use to diagnose connection problems — the last_error "
                    "field contains the actual error message from the failed connection."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Server name (optional — omit for all servers)."},
                    },
                },
            ),
            _get_server_status,
        )


__all__ = ["MCPPlugin", "ServerManager", "MCPManagerError"]
