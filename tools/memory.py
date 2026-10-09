# -*- coding: utf-8 -*-
r"""长期记忆工具 —— 包一层 core/memory.py 的 TigerMemory, 给它一个**可调用入口**。

为什么必须有这个文件 (真缺陷, 2026-09-25 实测抓到)
═══════════════════════════════════════════════════════════════════
`data/skills.json` 里早就有两条技能指着工具 `memory`:
    记忆管理 → tool=memory action=save   patterns=[记住 记忆 备忘 保存这个 记一下 …]
    记忆查询 → tool=memory action=search patterns=[回忆 查记忆 我之前 说过什么 …]
但仓库里**只有 core/memory.py (类 TigerMemory), 从来没有 tools/memory.py**。
工具派发的规矩是"按 tools/<名>.py 加载插件 + 调它的 run()/execute()":

    call(memory save)   → {"success": false, "error": "未知工具: memory"}
    call(memory search) → {"success": false, "error": "未知工具: memory"}

于是这两条技能一命中就**必然失败** —— 用户说"记住我的收件人是老王", 系统回一句
"未知工具"。不是缺功能, 是**已经挂上去的路是断的**。

★ 只用**持久层** (TigerMemory 的 SQLite 那三层)
    remember(text)         → 写 episodic 表 (落盘, 跨进程可见)
    recall(query, limit)   → FTS 检索
    recall_context(limit)  → 最近 N 条
  刻意**不用** set_ctx/get_ctx —— 那不是持久的: `self._context` 是实例内存里的 dict,
  每次 `TigerMemory()` 都新建实例, 写进去的东西下一个进程就没了。用它做"记住"
  会变成"假装记住了"(最坏的一种成功)。

★ 与已有的实时路径不冲突 (别重复抢路由)
  · 句首教学词 (`以后…/默认…/记住…`) 已被 pipeline 的 `_match_learn_teach` (370 层)
    接走 → `learn_teach` → `learn_loop.observe` 落库。那是**偏好/规矩**路, 保持不动;
    本工具负责**事实记忆** (存一条 / 按关键词翻)。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("tool.memory")

TOOL = {
    "name": "memory",
    "description": "长期记忆的存取 (落盘, 重启还在)。action: save=存一条(给 value, 可选 key) · "
                   "search=按关键词翻(query) · list=最近记了什么 · forget=删一条(给 key/内容片段)。"
                   "用户说'记一下…''记住…''我之前说过什么'时用。"
                   "★ '以后都这样/默认用X'这类**规矩**由学习闭环处理, 不要用本工具存规矩。",
    # ★★ keywords 精心收窄 (2026-09-25, verify_learn_loop 抓到的**真回归**):
    #   这里**绝不能**放 "记住 / 记一下 / 备忘 / 默认 / 以后" ——
    #   路由优先级 P_PLUGIN=500 **早于** P_LEARN_TEACH=370, 放了这些词,
    #   "记住我的收件人默认是老王" 会被本工具抢走, 教学句不再走 learn.teach
    #   (实测 route 从 learn.teach 变成 plugin.keyword, 学习闭环收不到偏好)。
    #   本工具只负责**翻** (检索侧), **教**归 learn_loop。
    #   存的那一侧由 data/skills.json 的「记忆管理」技能 (P_SKILL=800, 晚于 370) 接,
    #   不会抢教学句。
    "keywords": ["查记忆", "翻记忆", "记忆里找", "回忆一下", "找找记忆"],
    "params": [
        {"name": "action", "type": "string", "required": False,
         "enum": ["save", "search", "list", "forget"],
         "description": "save 存一条 · search 按关键词翻 · list 最近记的 · forget 删一条"},
        {"name": "value", "type": "string", "required": False,
         "description": "要记住的内容 (action=save)"},
        {"name": "key", "type": "string", "required": False,
         "description": "这条记忆的名字/标签 (action=save 可选; forget 用它定位)"},
        {"name": "query", "type": "string", "required": False,
         "description": "搜索词 (action=search)"},
        {"name": "limit", "type": "integer", "required": False,
         "description": "最多几条, 默认 8"},
    ],
}

#: 兼容只认 PLUGIN 的旧代码路径 (core/tool_schema.py 两种都认)
PLUGIN = {
    "name": "memory",
    "description": "长期记忆存取 (save/search/list/forget, 落盘)",
    "version": "1.0",
    "requires": [],
    "trigger": TOOL["keywords"],
    "permission": ["memory"],
    "category": "memory",
}


def _mem():
    """拿 TigerMemory。取不到就**诚实报错**。

    库路径可被 `TMM_MEMORY_DB` 覆盖 —— 门禁/探针必须指到临时库, 不许污染
    真 `data/memory.db` (与 tools/session_logs.py 的 TMM_SESSION_DB 同一约定)。
    """
    from core.memory import TigerMemory
    import os
    db = (os.environ.get("TMM_MEMORY_DB") or "").strip()
    return TigerMemory(db_path=db) if db else TigerMemory()


def _do_save(key: str, value: str) -> dict:
    key = (key or "").strip()
    value = (value or "").strip()
    if not value:
        return {"success": False, "output": "",
                "error": "没拿到要记的内容 (value)。请说清要记什么"}
    text = ("%s: %s" % (key, value)) if key else value
    m = _mem()
    m.remember(text)                      # ← 只有这条是落盘的
    # 落盘对账 (09-24 复盘: 回执"已生成"必须和磁盘对上)
    got = m.recall_context(1) or []
    if not got or str(got[-1].get("content") or "")[:40] not in text[:40]:
        return {"success": False, "output": "",
                "error": "写入后回读没对上, 这条记忆可能没落盘。请重试"}
    return {"success": True, "output": "记住了: %s" % text[:120],
            "key": key or None, "text": text}


def _do_search(query: str, limit: int) -> dict:
    q = (query or "").strip()
    if not q:
        return {"success": False, "output": "", "error": "搜索要给 query"}
    m = _mem()
    hits = m.recall(q, limit=int(limit or 8)) or []
    if not hits:
        # ★ 中文子串兜底 (2026-09-25 实测): 底层 FTS5 用 unicode61 分词, **一整串
        #   中文是一个 token** —— 存了「收件人: 老王」能搜到「收件人」(分词点),
        #   但存了「跨进程也要能搜到这句话」搜「跨进程」**搜不到** (不在分词点上)。
        #   用户说"我之前说过什么"时搜的恰恰是这种词中片段 ⇒ 退回 LIKE 子串查。
        try:
            import sqlite3
            con = sqlite3.connect(str(_mem()._db_path))
            try:
                cur = con.execute(
                    "SELECT content, role, timestamp FROM episodic "
                    "WHERE content LIKE ? ORDER BY timestamp DESC LIMIT ?",
                    ("%" + q + "%", int(limit or 8)))
                hits = [{"content": c[:300], "role": r, "time": t}
                        for c, r, t in cur.fetchall()]
            finally:
                con.close()
        except Exception as e:
            logger.warning("memory LIKE fallback failed: %s: %s", type(e).__name__, e)
    if not hits:
        return {"success": True, "output": "没翻到跟「%s」有关的记忆" % q, "hits": []}
    lines = ["翻到 %d 条:" % len(hits)]
    for h in hits:
        lines.append("  %s%s" % (("[%s] " % h["ago"] if h.get("ago") else ""),
                                 str(h.get("content") or "")[:140]))
    return {"success": True, "output": "\n".join(lines), "hits": hits}


def _do_list(limit: int) -> dict:
    m = _mem()
    rows = m.recall_context(int(limit or 8)) or []
    if not rows:
        return {"success": True, "output": "记忆库还是空的 (还没存过东西)", "recent": []}
    lines = ["最近 %d 条:" % len(rows)]
    for r in rows:
        lines.append("  %s: %s" % (r.get("role", "?"), str(r.get("content") or "")[:120]))
    return {"success": True, "output": "\n".join(lines), "recent": rows}


def _do_forget(key: str) -> dict:
    """删一条 —— 底层**没有**按 key 删的接口, 所以诚实说清, 不假装删了。

    能做的: 在 episodic 表里按内容片段精确删掉匹配行 (FTS 表支持 DELETE by rowid)。
    """
    k = (key or "").strip()
    if not k:
        return {"success": False, "output": "", "error": "删记忆要给 key 或内容片段"}
    try:
        import sqlite3
        db = _mem()._db_path
        con = sqlite3.connect(str(db))
        try:
            cur = con.execute(
                "SELECT rowid, content FROM episodic WHERE content LIKE ? LIMIT 5",
                ("%" + k + "%",))
            rows = cur.fetchall()
            if not rows:
                return {"success": False, "output": "",
                        "error": "没找到含「%s」的记忆, 没删任何东西" % k}
            if len(rows) > 1:
                return {"success": False, "output": "",
                        "error": "「%s」匹配到 %d 条, 说不清删哪条。请给更长的片段"
                                 % (k, len(rows)),
                        "matches": [str(r[1])[:80] for r in rows]}
            con.execute("DELETE FROM episodic WHERE rowid = ?", (rows[0][0],))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        logger.warning("memory forget failed: %s: %s", type(e).__name__, e)
        return {"success": False, "output": "",
                "error": "删除失败: %s: %s" % (type(e).__name__, e)}
    return {"success": True, "output": "删了: %s" % str(rows[0][1])[:100]}


async def run(action: str = "save", key: str = "", value: str = "",
              query: str = "", limit: int = 8, **kwargs) -> dict:
    """长期记忆存取。action: save / search / list / forget。"""
    act = (action or "save").strip().lower()
    try:
        if act in ("save", "remember", "存", "记住", "记一下"):
            return _do_save(key or kwargs.get("name") or "",
                            value or kwargs.get("content") or kwargs.get("text") or "")
        if act in ("search", "query", "recall", "查", "翻"):
            return _do_search(query or kwargs.get("q") or value or key, limit)
        if act in ("list", "ls", "all", "recent", "清单", "最近"):
            return _do_list(limit)
        if act in ("forget", "delete", "删除", "忘掉"):
            return _do_forget(key or value or query)
    except Exception as e:
        # 诚实报错 —— 底层炸了不许回报"已记住", 那是最坏的一种成功
        logger.warning("memory tool failed: %s: %s", type(e).__name__, e)
        return {"success": False, "output": "",
                "error": "记忆操作失败: %s: %s" % (type(e).__name__, e)}
    return {"success": False, "output": "",
            "error": "Unknown action: %s; 可用: save / search / list / forget"
                     % (action or "(空)")}
