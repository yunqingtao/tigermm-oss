"""
API authentication middleware — business error codes.
All auth failures return 200 + business code (1001-1006).
"""
import json, logging
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

PUBLIC_PATHS = {"/", "/health", "/docs", "/openapi.json", "/auth/login"}


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict | None:
    """FastAPI dependency: returns user dict or raises with business error code."""
    path = request.url.path.rstrip("/") or "/"

    if path in PUBLIC_PATHS or request.method == "OPTIONS":
        return None

    token = request.query_params.get("token")
    if not token and credentials:
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=200, detail=json.dumps({
            "success": False, "code": 1005, "message": "Token 缺失"
        }))

    try:
        from core.auth import get_auth
        auth = get_auth()
    except RuntimeError:
        # Auth not initialized yet — fall back to old users.enc
        from config.settings import USERS_FILE
        from config.crypto import secure_load
        try:
            data = secure_load(USERS_FILE, force_encrypt=True)
            users = data.get("users", [])
        except Exception:
            users = []
        for u in users:
            if u.get("token") == token:
                return u
        raise HTTPException(status_code=200, detail=json.dumps({
            "success": False, "code": 1005, "message": "Token 无效"
        }))

    u = auth.verify_token(token)
    if not u:
        raise HTTPException(status_code=200, detail=json.dumps({
            "success": False, "code": 1005, "message": "Token 无效"
        }))

    return {
        "username": u.username,
        "role": u.role,
        "token": u.token,
    }
