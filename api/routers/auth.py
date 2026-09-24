"""
Auth API endpoints — login, logout, token refresh.
All endpoints return 200 + business error code.
"""
import json, logging
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str

class RefreshRequest(BaseModel):
    token: str

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _get_ua(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:200]


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    from core.auth import get_auth
    auth = get_auth()
    result = auth.login(req.username, req.password, _get_ip(request), _get_ua(request))
    return result


@router.post("/logout")
async def logout(req: RefreshRequest, request: Request):
    from core.auth import get_auth
    auth = get_auth()
    result = auth.logout(req.token, _get_ip(request))
    return result


@router.post("/refresh")
async def refresh(req: RefreshRequest, request: Request):
    from core.auth import get_auth
    auth = get_auth()
    result = auth.refresh_token(req.token, _get_ip(request))
    return result


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    from core.auth import get_auth
    auth = get_auth()

    # Extract username from token
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.query_params.get("token", "")
    u = auth.verify_token(token)
    if not u:
        return {"success": False, "code": 1005, "message": "Token 无效"}

    result = auth.change_password(u.username, req.old_password, req.new_password)
    return result


@router.get("/audit-log")
async def audit_log(request: Request, username: str = "", limit: int = 100):
    from core.auth import get_auth
    auth = get_auth()
    entries = auth.db.get_audit_log(username, limit)
    return {
        "success": True,
        "code": 0,
        "entries": [
            {"timestamp": e.timestamp, "username": e.username,
             "action": e.action, "ip": e.ip, "detail": e.detail}
            for e in entries
        ]
    }
