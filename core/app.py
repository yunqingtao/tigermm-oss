"""
Tiger.M.M FastAPI application factory.
Extracted from pipeline.py for maintainability.
"""
import sys, os, asyncio, json, logging
from pathlib import Path

logger = logging.getLogger("core.app")

try:
    from fastapi import FastAPI, HTTPException, Request as FR
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from config.settings import DATA_DIR, CORS_ORIGINS, PROJECT_ROOT
from core.pipeline import Level4Pipeline, FRAMEWORK_TEXT

def create_app(config: dict):
    """Build FastAPI app with all routers and middleware."""
    if not HAS_FASTAPI:
        logger.error("FastAPI not installed")
        return None

    from config.settings import CORS_ORIGINS

    app = FastAPI(title="MARY III", version="4.2.0")

    from starlette.middleware.base import BaseHTTPMiddleware
    class _BodyLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > 102400:
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "Request too large (max 100KB)"}, status_code=413)
            return await call_next(request)
    app.add_middleware(_BodyLimitMiddleware)

    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                        allow_methods=["*"], allow_headers=["*"])

    from core.model_client import ModelClient

    model_client = ModelClient(config)
    pipeline = Level4Pipeline(model_client, config)

    app.state.pipeline = pipeline
    app.state.auto_guide = pipeline.auto_guide
    app.state.plugins = pipeline.plugins
    app.state.gateway = pipeline.gateway
    app.state.model_client = model_client
    app.state.config = config

    from storage.sqlite_store import MemoryStore
    from config.settings import MEMORY_DB
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    app.state.memory = MemoryStore(MEMORY_DB)

    from api.routers import health, chat, code, gateway, admin, inbox
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(code.router)
    app.include_router(gateway.router)
    app.include_router(admin.router)
    app.include_router(inbox.router)

    return app


async def main(port=8800):
    """API server entry point."""
    from core.model_client import ModelClient
    import json as _json

    config = {}
    keys_file = DATA_DIR / "keys.json"
    if keys_file.exists():
        try:
            config = _json.loads(keys_file.read_text(encoding='utf-8'))
        except Exception:
            pass

    if "deepseek" not in config:
        config["deepseek"] = {
            "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat"
        }

    logger.info("Config: %d models loaded", len(config))
    model_client = ModelClient(config)
    pipeline = Level4Pipeline(model_client, config)

    app = create_app(config)
    if app is None:
        print("FastAPI not installed. Run: pip install fastapi uvicorn")
        return

    import uvicorn
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(cfg)
    print(FRAMEWORK_TEXT)
    logger.info("MARY III v4.2 on http://0.0.0.0:%d", port)
    try:
        await server.serve()
    except asyncio.CancelledError:
        logger.info("Shutdown")
    finally:
        logger.info("Server stopped")
