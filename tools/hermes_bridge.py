"""
Hermes Bridge v3.0 — local-only, zero RPC dependency.
Tool registry in hermes_bridge/tool_adapter.py handles all tools.
This plugin is a no-op compatibility shim.
"""
PLUGIN = {
    "name": "hermes_bridge",
    "description": "Hermes Bridge v3.0 (local mode, no RPC)",
    "version": "3.0",
    "requires": [],
    "trigger": [],
    "permission": ["safe"],
    "category": "bridge",
    "expose": False,
    "expose_reason": "与 Hermes 互调的桥: 模型看得见会造成跨 agent 递归调用, 只允许技能/计划按名调用",
}

async def run(**kwargs) -> dict:
    """All tools are now local. See hermes_bridge/tool_adapter.py."""
    return {
        "success": True,
        "output": "Hermes Bridge v3.0: all 9 tools run locally. Use /tool <name> to call."
    }
