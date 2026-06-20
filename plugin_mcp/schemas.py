"""Request/response models for plugin-mcp routes (008.5/phase12).

Inlined from core's ``luna.schemas.api`` during extraction — these are
MCP-specific and have no reason to live in core. Plain pydantic; no `import luna.*`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MCPServerInfo(BaseModel):
    name: str
    transport_type: str
    config: dict
    enabled: bool
    connected: bool
    tool_count: int
    last_connected_at: str | None = None
    last_error: str | None = None


class MCPToolInfo(BaseModel):
    name: str
    description: str = ""
    destructive: bool = False
    input_schema: dict = Field(default_factory=dict, alias="schema")
    model_config = {"populate_by_name": True}


class MCPAddRequest(BaseModel):
    name: str
    transport_type: str = "stdio"
    config: dict
    enable: bool = False


class MCPUpdateRequest(BaseModel):
    config: dict | None = None
    enabled: bool | None = None


class MCPTestRequest(BaseModel):
    transport_type: str = "stdio"
    config: dict


class MCPTestResult(BaseModel):
    ok: bool
    tool_count: int | None = None
    tools: list[str] = Field(default_factory=list)
    error: str | None = None
