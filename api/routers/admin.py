"""
Admin endpoints — config, stats, management.
All require admin token.
"""
from fastapi import APIRouter, Request, HTTPException, Depends
from api.middlewares.auth import verify_token

router = APIRouter(tags=["admin"], prefix="/admin")


def _require_admin(user: dict = Depends(verify_token)):
    """Dependency: only admin role can access."""
    if user is None:
        raise HTTPException(401, "Token required")
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access only")
    return user


@router.get("/config")
async def admin_config(request: Request, user: dict = Depends(_require_admin)):
    config = request.app.state.config if hasattr(request.app.state, 'config') else {}
    safe_config = {}
    for k, v in config.items():
        safe_config[k] = {
            "model": v.get("model", k),
            "has_key": bool(v.get("key") or v.get("api_key")),
        }
    return {"config": safe_config, "models": list(safe_config.keys())}


@router.post("/config/test")
async def test_models(request: Request, user: dict = Depends(_require_admin)):
    return {"results": [], "message": "Test endpoint — implement model connectivity checks here"}
