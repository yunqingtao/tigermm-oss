"""
KB Search — 知识库检索
基于用户问题的语义检索，返回最相关的文档片段（供模型引用回答）
"""
import os
import sys
from typing import Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOL = {
    "name": "kb_search",
    "keywords": ["查手册", "查文档", "手册里", "文档里", "说明书", "资料里"],
    "description": "知识库语义检索——传query=问题，返回最相关的文档片段和来源。回答文档类问题前先调这个",
    "params": [
        {"name": "query", "type": "string", "required": True, "description": "要查的问题"},
        {"name": "top_k", "type": "int", "required": False, "default": 5, "description": "返回条数"},
    ]
}


async def run(query: Optional[str] = None, top_k: int = 5, **kwargs) -> Dict[str, Any]:
    from core.kb_store import MemoryVectorStore

    if not query:
        # /tool gateway passes query="xxx" style — extract
        q = kwargs.get("query", "")
        if not q:
            return {"success": False, "output": "缺少问题", "error": "需要query参数"}
        query = q

    store = MemoryVectorStore()
    if store.count() == 0:
        return {"success": True, "result": "知识库为空，请先导入文档（kb_import）"}

    hits = store.search(query, top_k=top_k)
    if not hits:
        return {"success": True, "result": "知识库中未找到相关内容"}

    # 返回检索片段 + 上下文格式（模型可直接引用）
    context = store.format_context(hits)
    sources = sorted({h["source"] for h in hits})
    result = f"{context}\n\n[来源: {', '.join(sources)}]"
    return {"success": True, "result": result}
