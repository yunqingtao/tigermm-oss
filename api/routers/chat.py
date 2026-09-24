"""
Chat and SSE streaming endpoints.
"""
import json
import traceback
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse
from api.middlewares.auth import verify_token
from pydantic import BaseModel
from typing import Optional
import os
import re

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    ext_model: Optional[str] = None
    model: Optional[str] = None


@router.post("/chat")
async def chat(req: ChatRequest, request: Request, user: dict = Depends(verify_token)):
    pipeline = request.app.state.pipeline
    if pipeline is None:
        raise HTTPException(503, "Pipeline not initialized")
    try:
        result = await pipeline.process(req.message, req.ext_model or req.model)
        response_text = result.get("response", "")
        # Sanitize: strip model name and key-status from error responses
        if "Error:" in response_text and ("API key" in response_text or "No API key" in response_text):
            response_text = "Service temporarily unavailable"
        return {
            "response": response_text,
            "intent": result.get("intent", "general"),
            "elapsed": result.get("elapsed", 0),
            "code_rounds": result.get("code_rounds", 0),
        }
    except Exception as e:
        logger.error("Chat error: %s", str(e)[:200])
        # Never expose model names, key status, or stack traces to client
        raise HTTPException(500, "Service temporarily unavailable")


@router.get("/chat/stream")
async def chat_stream(message: str = "", model: str = "auto", request: Request = None,
                      user: dict = Depends(verify_token)):
    pipeline = request.app.state.pipeline
    async def gen():
        result = await pipeline.process(message, model)
        yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/history")
async def get_history(request: Request):
    return {"history": []}


@router.delete("/history")
async def clear_history(request: Request):
    return {"cleared": True}
