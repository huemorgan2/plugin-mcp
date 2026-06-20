# plugin-mcp

Universal **Model Context Protocol** tool adapter for the
[Luna](https://github.com/huemorgan2) agent.

Connect Luna to any MCP server. Discovered tools are wrapped into Luna's tool
registry as `mcp__<server>__<tool>` and registered **dynamically at runtime** —
enable a server and its tools come online; disable it and they're gone. Servers
are disabled by default (the runtime safety gate).

## Managing servers

**Settings → MCP Servers** (a themed iframe served from the plugin): add, test,
enable/disable, edit env, inspect discovered tools. The agent can also manage
servers via tools (`mcp_add_server`, `mcp_enable_server`, …).

For secrets, set an env value to `vault:<credential_name>` so the key is resolved
server-side through the vault and never stored in plaintext.

## 11 built-in tools

`mcp_list_servers`, `mcp_list_tools`, `mcp_call_tool`, `mcp_refresh`,
`mcp_add_server`, `mcp_test_server_config`, `mcp_update_server`,
`mcp_enable_server`, `mcp_disable_server`, `mcp_remove_server`,
`mcp_get_server_status` — plus the dynamic `mcp__<server>__<tool>` wrappers.

## Built on `luna_sdk` v0

No `import luna.*`:

- **E4** — plugin-owned tables via `declarative_base()` (isolated metadata;
  `plugin_mcp_servers`, `plugin_mcp_tools`).
- **E4.1** — `ToolHandler` / `ToolRegistry` for the dynamic wrapped-tool
  registration through `ctx.tool_registry`.
- **E7** — `ctx.record_version` so server changes appear in core's version /
  rollback log.
- Routes auth via `luna_sdk.get_current_user`; secrets via `ctx.vault`.

## Requirements

Needs **Node** on the host to spawn stdio servers (`npx` / `node`). Depends on
`plugin-vault`.

## License

MIT — see [LICENSE](./LICENSE).
