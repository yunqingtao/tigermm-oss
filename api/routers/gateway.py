"""
Tool gateway and plugin endpoints.
"""
import webbrowser
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from api.middlewares.auth import verify_token
import os
import re

logger = logging.getLogger(__name__)
router = APIRouter(tags=["gateway"])


@router.get("/gateway/tools")
async def list_tools(request: Request, user: dict = Depends(verify_token)):
    """List all available tools."""
    gw = request.app.state.gateway
    if gw is None:
        return {"tools": [], "count": 0}
    try:
        tools = gw.get_all_tool_defs()
        return {"tools": tools, "count": len(tools)}
    except Exception as e:
        return {"error": str(e)}


@router.get("/gateway/open")
async def gateway_open(url: str = "", user: dict = Depends(verify_token)):
    """Open URL in browser. Only http/https allowed."""
    if not url:
        raise HTTPException(400, "Missing 'url' parameter")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Only http/https URLs allowed")
    try:
        webbrowser.open(url)
        return {"opened": url}
    except Exception as e:
        return {"error": str(e), "url": url}


@router.get("/plugins")
async def list_plugins(request: Request):
    plugins = request.app.state.plugins
    if plugins is None:
        return {"plugins": []}
    return {"plugins": [
        {"name": k, "info": v.get("info", {})} 
        for k, v in plugins._plugins.items()
    ]}


@router.post("/plugins")
async def call_plugin(payload: dict, request: Request):
    return {"error": "not implemented"}
