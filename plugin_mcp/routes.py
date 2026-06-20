"""plugin-mcp API routes (008.5/phase12).

NOTE: `from __future__ import annotations` is intentionally absent. It
stringifies Pydantic model hints in FastAPI handler signatures, which
prevents the body-parameter resolution needed by MCPAddRequest et al.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .schemas import (
    MCPAddRequest,
    MCPServerInfo,
    MCPTestRequest,
    MCPTestResult,
    MCPToolInfo,
    MCPUpdateRequest,
)
from .state import get_manager

_SETTINGS_DIR = Path(__file__).parent / "interface" / "webui" / "settings"


def register_routes(app, ctx):
    from luna_sdk import get_current_user

    router = APIRouter(prefix="/api/p/plugin-mcp", tags=["mcp"])

    def _mgr():
        return get_manager()

    @router.get("/servers", response_model=list[MCPServerInfo])
    async def list_servers(user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            return []
        return await mgr.list_servers()

    @router.post("/servers", response_model=MCPServerInfo)
    async def add_server(req: MCPAddRequest, user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(503, "plugin-mcp not loaded")
        try:
            return await mgr.add(req.name, req.transport_type, req.config, enable=req.enable)
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @router.put("/servers/{name}", response_model=MCPServerInfo)
    async def update_server(name: str, req: MCPUpdateRequest, user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(503, "plugin-mcp not loaded")
        try:
            if req.config is not None:
                await mgr.update_config(name, config=req.config)
            if req.enabled is True:
                await mgr.enable(name)
            elif req.enabled is False:
                await mgr.disable(name)
            return await mgr.get(name)
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @router.delete("/servers/{name}")
    async def delete_server(name: str, user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(503, "plugin-mcp not loaded")
        try:
            await mgr.remove(name)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

    @router.post("/servers/{name}/refresh", response_model=MCPServerInfo)
    async def refresh_server(name: str, user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(503, "plugin-mcp not loaded")
        try:
            return await mgr.refresh(name)
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/test", response_model=MCPTestResult)
    async def test_config(req: MCPTestRequest, user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(503, "plugin-mcp not loaded")
        return await mgr.test_config(req.transport_type, req.config)

    @router.get("/servers/{name}/tools", response_model=list[MCPToolInfo])
    async def get_tools(name: str, user=Depends(get_current_user)):
        mgr = _mgr()
        if mgr is None:
            raise HTTPException(503, "plugin-mcp not loaded")
        try:
            return await mgr.get_tools(name)
        except Exception as e:
            raise HTTPException(404, str(e)) from e

    # --- Settings UI (served as a themed iframe by the host) ---

    @router.get("/ui/settings/")
    async def settings_index():
        index = _SETTINGS_DIR / "index.html"
        if not index.exists():
            raise HTTPException(404, "settings UI not found")
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})

    @router.get("/ui/settings/{path:path}")
    async def settings_asset(path: str):
        target = (_SETTINGS_DIR / path).resolve()
        if not str(target).startswith(str(_SETTINGS_DIR.resolve())):
            raise HTTPException(403, "forbidden")
        if not target.exists() or target.is_dir():
            return FileResponse(str(_SETTINGS_DIR / "index.html"), headers={"Cache-Control": "no-cache"})
        return FileResponse(str(target), headers={"Cache-Control": "no-cache"})

    app.include_router(router)
