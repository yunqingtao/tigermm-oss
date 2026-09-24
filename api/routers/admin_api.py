"""
Admin API — user management, audit log, system config.
"""
import json, logging
from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin_api"])


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


@router.get("/users")
async def list_users(request: Request):
    from core.auth import get_auth
    auth = get_auth()
    users = auth.db.list_users()
    return {
        "success": True, "code": 0,
        "users": [{
            "username": u.username,
            "role": u.role,
            "locked_until": u.locked_until,
            "login_attempts": u.login_attempts,
            "last_login_ip": u.last_login_ip,
            "last_login_time": u.last_login_time,
            "created_at": u.created_at,
            "token": u.token[:15] + "..." if u.token else "",
        } for u in users]
    }


@router.post("/users")
async def create_user(req: CreateUserRequest):
    from core.auth import get_auth
    auth = get_auth()
    try:
        u = auth.db.create_user(req.username, req.password, req.role)
        return {"success": True, "code": 0, "message": f"User {u.username} created",
                "user": {"username": u.username, "role": u.role, "token": u.token}}
    except Exception as e:
        return {"success": False, "code": 1009, "message": str(e)}


@router.delete("/users/{username}")
async def delete_user(username: str):
    from core.auth import get_auth
    auth = get_auth()
    # Regenerate token (effectively disabling)
    u = auth.db.get_user(username)
    if u:
        import secrets
        u.token = "disabled-" + secrets.token_hex(8)
        u.locked_until = float("inf")
        auth.db.update_user(u)
        return {"success": True, "code": 0, "message": f"User {username} disabled"}
    return {"success": False, "code": 1001, "message": "User not found"}


@router.get("/audit")
async def audit_log(request: Request, username: str = "", limit: int = 100):
    from core.auth import get_auth
    auth = get_auth()
    entries = auth.db.get_audit_log(username, limit)
    return {
        "success": True, "code": 0,
        "entries": [{
            "timestamp": e.timestamp,
            "username": e.username,
            "action": e.action,
            "ip": e.ip,
            "detail": e.detail
        } for e in entries]
    }


@router.get("/config")
async def get_config():
    from config.settings import RUN_MODE
    import json
    prefs_path = None
    try:
        from config.settings import DATA_DIR
        prefs_path = DATA_DIR / "prefs.json"
    except Exception:
        pass
    prefs = {}
    if prefs_path and prefs_path.exists():
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    return {
        "success": True, "code": 0,
        "config": {
            "run_mode": RUN_MODE,
            "regex_passthrough": prefs.get("regex_passthrough", True),
        }
    }


@router.post("/config")
async def update_config(request: Request):
    body = await request.json()
    from config.settings import DATA_DIR
    import json as _json
    prefs_path = DATA_DIR / "prefs.json"
    prefs = _json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    for k, v in body.items():
        prefs[k] = v
    prefs_path.write_text(_json.dumps(prefs, ensure_ascii=False, indent=2))
    return {"success": True, "code": 0, "message": "Config updated"}
