"""
Health check and system status.
"""
from fastapi import APIRouter, Request, Depends
from api.middlewares.auth import verify_token
import re

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {
        "status": "online",
        "version": "4.2.0",
        "level": 4,
    }


@router.get("/resource")
async def resource(request: Request, user: dict = Depends(verify_token)):
    import psutil
    try:
        proc = psutil.Process()
        mem = proc.memory_info().rss / 1024 / 1024
        cpu = proc.cpu_percent(interval=0.1)
        alert = "normal"
        if mem > 500: alert = "high"
        elif mem > 300: alert = "warning"
        result = {"memory_mb": round(mem, 1), "cpu_percent": cpu, "alert": alert}
        # Add storage stats
        pipeline = request.app.state.pipeline
        if pipeline:
            result["learned_rules"] = len(pipeline.auto_guide._learned)
            result["gateway_tools"] = len(pipeline.gateway._tool_cache) if hasattr(pipeline.gateway, '_tool_cache') else 0
        return result
    except Exception as e:
        return {"error": str(e), "memory_mb": 0, "cpu_percent": 0, "alert": "unknown"}


@router.get("/status")
async def status(user: dict = Depends(verify_token)):
    return {"status": "online", "version": "4.2.0"}


@router.get("/models")
async def list_models(request: Request, user: dict = Depends(verify_token)):
    """Return models actually available from loaded config + env."""
    models = []
    try:
        config = request.app.state.config if hasattr(request.app.state, 'config') else {}
        for name, cfg in config.items():
            has_key = bool(cfg.get("key") or cfg.get("api_key"))
            models.append({
                "id": name,
                "name": name.title(),
                "type": "local" if name == "ollama" else "api",
                "available": has_key,
            })
    except Exception:
        pass
    if not models:
        models = [
            {"id": "deepseek", "name": "DeepSeek", "type": "api", "available": False},
            {"id": "ollama", "name": "Ollama (local)", "type": "local", "available": False},
        ]
    return {"models": models}
