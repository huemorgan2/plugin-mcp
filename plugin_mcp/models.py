"""ORM rows owned by plugin-mcp (Phase 010)."""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from luna_sdk import JSONB, UUID, declarative_base

# 008.5/phase12 (E4): plugin-owned tables bind to their OWN metadata, isolated
# from core's Base, so create_all/drop never touch core tables.
Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid_default() -> _uuid.UUID:
    return _uuid.uuid4()


class MCPServerRow(Base):
    """A configured MCP server. `enabled` is the runtime gate — until the
    user explicitly enables it, no tools are registered."""

    __tablename__ = "plugin_mcp_servers"

    id: Mapped[_uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid_default)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    transport_type: Mapped[str] = mapped_column(String(16), nullable=False, default="stdio")
    # stdio: {"command": "...", "args": [...], "env": {...}, "cwd": "..."}
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (Index("ix_plugin_mcp_servers_enabled", "enabled"),)


class MCPToolRow(Base):
    """Cached MCP tools per server. Truth-source is the live server while
    enabled; this lets the UI list tools when the server is offline."""

    __tablename__ = "plugin_mcp_tools"

    id: Mapped[_uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid_default)
    server_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(), ForeignKey("plugin_mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    destructive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("server_id", "tool_name", name="uq_plugin_mcp_tools_server_name"),
    )
