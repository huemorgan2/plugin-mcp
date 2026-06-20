"""Module-level handle to the live ServerManager (008.5/phase12).

Decoupling: routes used to find the manager by walking the core plugin registry
(`luna.plugins.loader.get_plugin_registry`). A managed-dir plugin can't import
that. Instead the plugin sets the manager here in ``on_load`` and the routes read
it — same pattern as the other extracted plugins' ``state.py`` singletons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import ServerManager

_manager: ServerManager | None = None


def set_manager(manager: ServerManager | None) -> None:
    global _manager
    _manager = manager


def get_manager() -> ServerManager | None:
    return _manager
