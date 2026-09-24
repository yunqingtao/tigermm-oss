"""
KB Import — 文档入库 (txt/md/docx/pdf) → 切块 → 向量化 → 存 kb_chunks
"""
import os
import sys
from typing import Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOOL = {
    "name": "kb_import",
    "keywords": ["入库", "导入文档", "知识库导入", "喂文档", "加进知识库"],
    "description": "导入文档到知识库——传file=文件路径或text=文本内容，自动切块向量化。支持txt/md/docx/pdf",
    "params": [
        {"name": "file", "type": "string", "required": False, "description": "文件绝对路径，如 D:/docs/产品手册.pdf"},
        {"name": "text", "type": "string", "required": False, "description": "直接传入文本内容"},
        {"name": "source", "type": "string", "required": False, "description": "来源名（text模式时必填）"},
    ]
}


def _read_text(filepath: str) -> str:
    """按扩展名读取文本。txt/md 直接读；docx/pdf 尽力解析。"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".txt", ".md", ".csv", ".json"):
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                with open(filepath, "r", encoding=enc) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        raise ValueError(f"无法解码文件: {filepath}")
    elif ext == ".docx":
        import zipfile
        from xml.etree import ElementTree as ET
        with zipfile.ZipFile(filepath) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        texts = []
        for p in root.iter():
            if p.tag.endswith("}p"):
                texts.append("".join(t.text or "" for t in p.iter() if t.tag.endswith("}t")))
        return "\n".join(texts)
    elif ext == ".pdf":
        raise ValueError("PDF 请先用 ocr 工具转文本再入库，或提供 txt/md/docx")
    else:
        raise ValueError(f"不支持的文件格式: {ext}")


# 常见文档目录: 桌面/文档/下载/项目根
def _candidate_dirs() -> list:
    home = os.path.expanduser("~")
    dirs = [home, os.path.join(home, "Desktop"), os.path.join(home, "桌面"),
            os.path.join(home, "Documents"), os.path.join(home, "文档"),
            os.path.join(home, "Downloads"), os.path.join(home, "下载")]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # mary3/
    dirs.append(root)
    dirs.append(os.getcwd())
    seen = set()
    out = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _locate_file(raw: str) -> str | None:
    """从用户描述中找文件: 绝对路径直接用, 否则在常见目录搜索文件名。"""
    import re as _re
    raw = raw.strip().strip("'\"")
    # 1. 绝对路径
    if os.path.isfile(raw):
        return raw
    # 2. 提取文件名 (桌面上的 XX.txt / 文档里的 XX.md)
    m = _re.search(r"([\w\u4e00-\u9fff\-. ]+\.(?:txt|md|docx|doc|csv|json))", raw, _re.I)
    fname = m.group(1).strip() if m else raw
    fname = _re.sub(r"^(把|将|给|读|打开|传|发|找|查|搜|看)?(桌面|桌面上|文档|文档里|下载|下载里|我的)?[的里]?", "", fname).strip()
    if not fname:
        return None
    # 3. 常见目录搜索
    for d in _candidate_dirs():
        p = os.path.join(d, fname)
        if os.path.isfile(p):
            return p
        # 宽松匹配: 文件名至少2字符前缀 (防 startswith('') 永远真误匹配目录第一个文件)
        base = os.path.splitext(fname)[0]
        if base and len(base) >= 2:
            try:
                for f in os.listdir(d):
                    if f.lower() == fname.lower() or f.lower().startswith(base.lower()):
                        return os.path.join(d, f)
            except OSError:
                pass
    return None


async def run(file: Optional[str] = None, text: Optional[str] = None,
              source: Optional[str] = None, query: str = "", **kwargs) -> Dict[str, Any]:
    from core.kb_store import MemoryVectorStore

    store = MemoryVectorStore()

    # query 模式: 关键词路由传 "把桌面XX.txt" — 提取文件名并定位
    if not file and not text and query:
        located = _locate_file(query)
        if located:
            file = located
        else:
            return {"success": False, "output": "找不到文件",
                    "error": f"在桌面/文档/下载/项目目录都没找到: {query}。请给完整路径，如 D:/xx/手册.txt"}

    if file:
        if not os.path.exists(file):
            return {"success": False, "output": "文件不存在", "error": f"找不到文件: {file}"}
        try:
            content = _read_text(file)
        except Exception as e:
            return {"success": False, "output": "文件解析失败", "error": str(e)}
        src_name = os.path.basename(file)

    elif text:
        if not source:
            return {"success": False, "output": "缺少来源名", "error": "text模式需提供source参数"}
        content = text
        src_name = source

    else:
        return {"success": False, "output": "参数缺失", "error": "需要 file 或 text"}

    if len(content.strip()) < 5:
        return {"success": False, "output": "内容过短", "error": "提取到的文本不足5字符"}

    n = store.add_document(src_name, content)
    if n == 0:
        return {"success": False, "output": "入库失败", "error": "切片数为0（embedding服务是否正常？）"}

    total = store.count()
    return {"success": True, "result": f"✅ {src_name} 已入库：{n} 个切片，知识库共 {total} 个切片"}
