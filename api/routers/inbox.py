"""
Inbox API endpoints - async approval queue surface.

GET  /inbox/pending  - list pending approvals
POST /inbox/resolve  - resolve an approval item
GET  /inbox/stats    - queue statistics
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from api.middlewares.auth import verify_token
from pydantic import BaseModel
from typing import Optional
from core.inbox import get_inbox, RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS

router = APIRouter(prefix="/inbox", tags=["inbox"])


class ResolveRequest(BaseModel):
    item_id: str
    resolution: str  # "allow" | "deny" | "always"


@router.get("/pending")
async def list_pending(request: Request, user: dict = Depends(verify_token)):
    """List all pending approval items for this session."""
    inbox = get_inbox()
    session_id = user.get("session_id", "default")
    items = inbox.list_pending(session_id=session_id)
    return {
        "count": len(items),
        "items": [item.to_dict() for item in items],
    }


@router.post("/resolve")
async def resolve_item(req: ResolveRequest, request: Request, user: dict = Depends(verify_token)):
    """Resolve an approval item. Resolution must be one of: allow, deny, always."""
    inbox = get_inbox()

    if req.resolution not in (RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS):
        raise HTTPException(400, f"Invalid resolution: {req.resolution}. Use: allow, deny, always")

    ok = inbox.resolve(req.item_id, req.resolution)
    if not ok:
        raise HTTPException(404, "Item not found or already resolved")

    item = inbox.get(req.item_id)
    return {
        "resolved": True,
        "item": item.to_dict() if item else None,
    }


@router.get("/stats")
async def get_stats(request: Request, user: dict = Depends(verify_token)):
    """Get inbox queue statistics."""
    inbox = get_inbox()
    return inbox.stats()


@router.get("/all")
async def list_all(request: Request, user: dict = Depends(verify_token), limit: int = 20):
    """List all items (pending + resolved) for this session."""
    inbox = get_inbox()
    session_id = user.get("session_id", "default")
    items = inbox.list_all(session_id=session_id, limit=limit)
    return {
        "count": len(items),
        "items": [item.to_dict() for item in items],
    }
