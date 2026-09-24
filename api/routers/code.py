"""
Code sandbox execution endpoint.
"""
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from api.middlewares.auth import verify_token
from pydantic import BaseModel
import os
import re
import time

logger = logging.getLogger(__name__)
router = APIRouter(tags=["code"])


class CodeRequest(BaseModel):
    code: str
    timeout: int = 30


@router.post("/code/exec")
async def exec_code(req: CodeRequest, request: Request, user: dict = Depends(verify_token)):
    """Execute code in sandbox."""
    try:
        from sandbox.code_sandbox import CodeSandbox
        result = CodeSandbox.execute(req.code)
        return result
    except Exception as e:
        logger.error("Code exec error: %s", e)
        raise HTTPException(500, str(e))
