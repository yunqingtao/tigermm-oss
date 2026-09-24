"""
产物/交付清单工具 — 回答"今天产出了什么、在哪、多大"。

为什么要有 (2026-09-20, 借鉴通用 Agent 平台架构图的 "Files/产物面板"):
    引擎每天产出成片/报告/图/表格/音频, 但散落在桌面与项目目录里,
    "今天产出了什么"只能人肉翻目录。用户长期习惯是 **问进度要查盘直答**,
    所以这里提供: 登记过的产物清单 + 目录盘点 (两者都只读, 不动任何文件)。

★ 铁律:
   ① **只读** —— 不移动/不删除/不改名任何用户文件; 登记表只追加
   ② 登记一个**不存在**的路径 = 制造假信息 → 直接拒绝 (谎报比报错贵)
   ③ 已不在盘上的产物**不报** (alive_only), 免得给出过期结论

★ 元数据两处都写 (TOOL 与 PLUGIN):
   实测 (2026-09-20) 22 个工具只写 PLUGIN、20 个只写 TOOL, 而**关键词路由只读 TOOL**
   (core/pipeline.py:216 _match_keywords)。只写 PLUGIN 的工具无法被关键词命中。
   为稳妥, 本工具 TOOL/PLUGIN 都定义 (与另外 8 个工具同样做法)。
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ★ 2026-09-24 扩: 用户说"今天**干**了什么"(不是"做") —— 差一个字就命不中。
_KEYWORDS = ["产物", "产出了什么", "交付物", "成品在哪", "输出文件", "今天做了什么",
             "今天干了什么", "今天做了啥", "今天干了啥", "今天都干了啥", "今天都做了什么",
             "总结今天", "今天的总结", "生成了什么", "导出清单", "文件清单"]
_DESC = ("产物/交付清单: 列出登记过的成品与近期目录盘点 (只读, 不移动文件) — "
         "\"今天产出了什么 / 片子在哪个路径 / 最近生成的报告\"")

TOOL = {
    "name": "artifacts",
    "description": _DESC,
    "keywords": _KEYWORDS,
    "parameters": {
        "action": {"type": "string",
                   "description": "list/recent (默认) 列登记产物 · scan 盘点目录 · stats 按类统计 · add 登记一个文件",
                   "required": False},
        "path": {"type": "string", "description": "add 时的文件路径", "required": False},
        "title": {"type": "string", "description": "add 时的标题 (可选)", "required": False},
        "kind": {"type": "string", "description": "筛选: video/audio/image/doc/sheet/slide/code", "required": False},
        "days": {"type": "integer", "description": "scan 时看最近几天 (默认 1)", "required": False},
        "limit": {"type": "integer", "description": "最多几条 (默认 20)", "required": False},
    },
}

# 兼容只读 PLUGIN 的旧代码路径 (tool_schema.py 两处都认)
PLUGIN = {
    "name": "artifacts",
    "description": _DESC,
    "version": "1.0",
    "requires": [],
    "trigger": _KEYWORDS,
    "permission": ["read", "write"],
    "category": "productivity",
}


async def run(action: str = "", path: str = "", title: str = "", kind: str = "",
              days: int = 1, limit: int = 20, **kwargs) -> dict:
    """产物清单。action: list/recent | scan | stats | add"""
    from core import artifacts as A

    act = (action or "list").strip().lower()
    if act in ("recent", "ls", "all"):
        act = "list"
    if not path:                      # 参数抽取的常见变体: 路径可能落在别的字段
        for k in ("file", "target", "name", "text"):
            v = kwargs.get(k)
            if isinstance(v, str) and v.strip():
                path = v.strip()
                break
    try:
        limit = max(1, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        limit = 20
    try:
        days = max(0, int(days or 1))
    except (TypeError, ValueError):
        days = 1

    try:
        if act == "add":
            if not path:
                return {"success": False, "error": "add 需要 path (要登记的文件路径)"}
            r = A.add(path, title=title, kind=kind, source="tool")
            if not r.get("ok"):
                return {"success": False, "error": r.get("error", "登记失败")}
            rec = r["record"]
            return {"success": True,
                    "output": f"已登记产物: {rec['name']} [{rec['kind']}] {rec['size_h']}\n  {rec['path']}",
                    "record": rec}

        if act == "stats":
            st = A.stats()
            lines = [f"产物登记表: {st['store']}",
                     f"  在盘上的产物: {st['count']} 个, 合计 {st['total_size_h']}"]
            for k, v in sorted((st["by_kind"] or {}).items(), key=lambda x: -x[1]):
                lines.append(f"    {k:<8} {v}")
            return {"success": True, "output": "\n".join(lines), "stats": st}

        if act == "scan":
            found = A.scan_dirs(days=days, limit=limit)
            if not found:
                return {"success": True, "output": f"最近 {days} 天没在常见目录发现产物 (桌面/项目 reports,tmp)",
                        "count": 0, "items": []}
            lines = [f"最近 {days} 天目录盘点 ({len(found)} 个, 未登记, 只读发现):"]
            for r in found:
                import time as _t
                ts = _t.strftime("%m-%d %H:%M", _t.localtime(r["mtime"]))
                lines.append(f"  {ts}  [{r['kind']:<6}] {r['size_h']:>8}  {r['path']}")
            return {"success": True, "output": "\n".join(lines), "count": len(found), "items": found}

        if act != "list":
            return {"success": False,
                    "error": f"未知 action: {action} (可用: list / scan / stats / add)"}

        recs = A.list_recent(limit=limit, kind=kind)
        if not recs:
            return {"success": True,
                    "output": ("产物登记表还是空的 (没有任何登记记录)。\n"
                               "  · 想登记某个文件: action=add path=<路径>\n"
                               "  · 想直接盘点目录: action=scan (看桌面/项目 reports,tmp 最近产物)"),
                    "count": 0, "items": []}
        return {"success": True, "output": A.render_list(recs, "登记过的产物"),
                "count": len(recs), "items": [A.public_rec(r) for r in recs]}
    except Exception as e:
        logger.warning("artifacts tool failed: %s", e)
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
