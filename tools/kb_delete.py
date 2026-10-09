"""
KB Delete — 从知识库删除文档 / 清空知识库
删除的是知识库索引（可重新导入恢复），不碰原文件。
"""
import os
import re
import sys
from typing import Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOL = {
    "name": "kb_delete",
    "keywords": ["清空知识库", "删掉知识库", "删除知识库", "从知识库删",
                 "知识库删掉", "知识库删除", "移除知识库", "知识库清空"],
    "description": "删除知识库中的文档（说删掉哪份），或清空整个知识库。删除的是索引，原文件不受影响",
    "params": [
        {"name": "source", "type": "string", "required": False, "description": "要删除的文档名"},
    ]
}


async def run(source: Optional[str] = None, query: str = "", **kwargs) -> Dict[str, Any]:
    from core.kb_store import MemoryVectorStore

    store = MemoryVectorStore()
    sources = store.list_sources()
    if not sources:
        return {"success": True, "result": "知识库本来就是空的"}

    # ── 清空模式: source 为 __all__，或 query 含"清空"且没有具体文件名 ──
    want_all = source == "__all__" or (
        "清空" in (query or "") and not re.search(r"[\w\u4e00-\u9fff\-. ]+\.(?:txt|md|docx|doc|csv|json)", query or "")
    )
    if want_all:
        total = 0
        for s in sources:
            total += store.delete_source(s["source"])
        return {"success": True, "result": f"🧹 知识库已清空：删除 {len(sources)} 个文档、{total} 个切片"}

    # ── 指定删除: source 参数 or 从 query 提取 ──
    target = (source or "").strip()
    if not target:
        # 从 query 提取文件名/关键词: "里的保养手册" → "保养手册"
        m = re.search(r"([\w\u4e00-\u9fff\-. ]+\.(?:txt|md|docx|doc|csv|json))", query or "", re.I)
        target = m.group(1).strip() if m else (query or "").strip()
        target = re.sub(r"^(把|将|里|的|中|从|移除|删掉|删除|清空|知识库)", "", target).strip()
        target = re.sub(r"[的里]$", "", target).strip()
    if not target:
        return {"success": False, "output": "要删哪个文档？", "error": "请说清文档名，如：删掉知识库里的保养手册"}

    # 精确匹配 → 唯一模糊匹配 → 多个列出
    exact = [s for s in sources if s["source"] == target]
    fuzzy = [s for s in sources if target in s["source"]]
    candidates = exact or fuzzy
    if not candidates:
        names = "、".join(s["source"] for s in sources)
        return {"success": False, "output": "没找到这份文档",
                "error": f"知识库里有：{names}"}
    if len(candidates) > 1:
        names = "、".join(s["source"] for s in candidates)
        return {"success": False, "output": f"匹配到多个文档：{names}",
                "error": "请说得更具体一点"}

    n = store.delete_source(candidates[0]["source"])
    remain = store.count()
    return {"success": True,
            "result": f"🗑️ 已从知识库删除 {candidates[0]['source']}（{n} 个切片），剩余 {remain} 个切片"}
