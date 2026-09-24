"""
KB List — 知识库文档清单
"""
import os
import sys
from typing import Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOL = {
    "name": "kb_list",
    "keywords": ["知识库里有什么", "知识库有什么", "知识库有哪些", "知识库都有什么",
                 "库里有啥", "知识库列表", "知识库里有啥", "知识库里有几个"],
    "description": "列出知识库中的全部文档、切片数和入库时间",
    "params": []
}


async def run(query: str = "", **kwargs) -> Dict[str, Any]:
    from core.kb_store import MemoryVectorStore

    store = MemoryVectorStore()
    sources = store.list_sources()
    total = store.count()

    if not sources:
        return {"success": True, "result": "知识库是空的，先用 kb_import 导入文档（说：把 XX.txt 加进知识库）"}

    lines = [f"📚 知识库共 {total} 个切片，{len(sources)} 个文档："]
    for s in sources:
        t = s.get("time") or 0
        import time as _t
        ts = _t.strftime("%m-%d %H:%M", _t.localtime(t)) if t else "?"
        lines.append(f"  • {s['source']}（{s['chunks']} 切片，{ts}）")
    return {"success": True, "result": "\n".join(lines)}
