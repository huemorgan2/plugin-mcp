"""Registry of well-known MCP servers and their setup requirements.

Used by mcp_add_server to auto-fill config and tell the agent which
env vars are needed, so it can ask the owner for them proactively.

Only verified packages are included here. When you add a new entry:
1. Verify the npm package name resolves on registry.npmjs.org
2. Verify the env var name from the project's README
3. Test the full add → enable flow once with a valid token
"""

from __future__ import annotations

from typing import Any

KNOWN_SERVERS: dict[str, dict[str, Any]] = {
    # monday.com — official MCP server. Token via env or -t flag.
    # Source: https://github.com/mondaycom/mcp
    "monday": {
        "command": "npx",
        "args": ["-y", "@mondaydotcomorg/monday-api-mcp@latest"],
        "required_env": ["MONDAY_TOKEN"],
        "setup_hint": (
            "Get your API token at monday.com → Avatar (top right) → "
            "Developers → My Access Tokens."
        ),
    },
    # GitHub — official @modelcontextprotocol/server-github
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "required_env": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "setup_hint": (
            "Create a personal access token at github.com/settings/tokens. "
            "For private repos, give it the `repo` scope; for public, `public_repo` is enough."
        ),
    },
    # Filesystem — official; pass allowed directory as extra arg.
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "required_env": [],
        "setup_hint": (
            "Filesystem needs an allowed directory passed as an extra arg "
            "(e.g. /Users/you/projects). Update the server's args with that path "
            "appended after the package name."
        ),
    },
    # Slack — official server
    "slack": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "required_env": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
        "setup_hint": (
            "Create a Slack app at api.slack.com/apps with the required bot scopes, "
            "install it to your workspace, then copy the Bot User OAuth Token (xoxb-…) "
            "and your team ID."
        ),
    },
    # Postgres — official; connection string passed as extra arg, not env.
    "postgres": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "required_env": [],
        "setup_hint": (
            "Append your connection string as an extra arg, e.g. "
            "postgresql://user:pass@host:5432/dbname. For safety, use a read-only DB user."
        ),
    },
    # Brave Search — official
    "brave-search": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "required_env": ["BRAVE_API_KEY"],
        "setup_hint": "Get a free API key (2k queries/month) at brave.com/search/api.",
    },
    # Notion — community-standard
    "notion": {
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "required_env": ["NOTION_API_KEY"],
        "setup_hint": (
            "Create an internal integration at notion.so/my-integrations, "
            "then share the relevant pages with it."
        ),
    },
}

# Aliases: common names people might use instead of the canonical key
ALIASES: dict[str, str] = {
    "monday.com": "monday",
    "gh": "github",
    "fs": "filesystem",
    "files": "filesystem",
    "pg": "postgres",
    "postgresql": "postgres",
    "brave": "brave-search",
    "web-search": "brave-search",
    "search": "brave-search",
}


def lookup(name: str) -> dict[str, Any] | None:
    """Look up a known server by name or alias. Returns None if unknown."""
    canonical = ALIASES.get(name.lower(), name.lower())
    return KNOWN_SERVERS.get(canonical)


def canonical_name(name: str) -> str:
    """Resolve aliases to canonical server name."""
    return ALIASES.get(name.lower(), name.lower())
