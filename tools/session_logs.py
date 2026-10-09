"""
对话史查询工具 — 让 TMM 能"记得"你跟它聊过什么。

为什么要有 (2026-09-20 对照 OpenClaw 的 session-logs 补):
    chat_sessions.db 里躺着全部历史, 但**没有任何工具能读它** ——
    所以 TMM 每次都得说"我不知道我们之前聊过什么"。

★ 铁律: 本工具**只读**, 一律用 `file:...?mode=ro` 打开。
   为什么不复用 storage/session_store.py: 它的 search() 会执行
   `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` —— 那是**写操作**
   (索引重建)。查询历史这种事不该动用户的库, 所以这里自己写只读 SQL。
   库不存在时**不创建** (mode=ro 会直接报错 → 我们如实转述)。
"""
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PLUGIN = {
    "name": "session_logs",
    "description": "查询对话历史 (只读): 最近聊了什么 / 搜关键词 / 统计概览",
    "version": "1.0",
    "requires": [],
    "trigger": ["聊过什么", "聊天记录", "历史记录", "对话历史", "之前说过", "以前聊过"],
    "permission": ["read"],
    "category": "memory",
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "chat_sessions.db")


def db_path() -> str:
    """库位置: 环境变量优先 (测试/门禁隔离), 否则项目 data/。"""
    return os.environ.get("TMM_SESSION_DB") or DEFAULT_DB


def _connect():
    """只读连接。库不存在 → 抛 FileNotFoundError (由 run 转成诚实报错)。"""
    p = db_path()
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _fmt(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _preview(text: str, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _overview(conn) -> dict:
    """整体概览 —— 一句话统计头, 让上层模型有数字可依据 (不用它自己数)。"""
    q = conn.execute
    total = q("SELECT COUNT(*) FROM messages").fetchone()[0]
    sessions = q("SELECT COUNT(*) FROM sessions").fetchone()[0]
    first = q("SELECT MIN(timestamp) FROM messages").fetchone()[0]
    last = q("SELECT MAX(timestamp) FROM messages").fetchone()[0]
    users = q("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
    return {"total": total, "sessions": sessions, "user_turns": users,
            "first": _fmt(first) if first else None, "last": _fmt(last) if last else None}


def _query(keyword=None, limit=20, days=None):
    """只读查询: 最近 N 条 (可按关键词/天数过滤)。"""
    conn = _connect()
    try:
        ov = _overview(conn)
        sql = ("SELECT role, content, intent, model, timestamp FROM messages")
        where, args = [], []
        if keyword:
            where.append("content LIKE ?")
            args.append(f"%{keyword}%")
        if days:
            where.append("timestamp >= ?")
            args.append(time.time() - float(days) * 86400)
        if where:
            sql += " WHERE " + " AND ".join(where)
        # ★ 按**时间**倒序取最近 N 条, 不用 id —— id 只是插入序, 而"最近聊了什么"
        #   问的是时间。两者通常一致, 但库经过清理/迁移后可能不同, 以时间说话更诚实。
        #   加 id 作次序键, 保证同秒消息的次序稳定 (测试要可复现)。
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
        return ov, list(reversed(rows))
    finally:
        conn.close()


def _lines(ov, rows, keyword=None, days=None):
    head = (f"对话史概览: 共 {ov['total']} 条消息 · {ov['sessions']} 个会话 · "
            f"你的提问 {ov['user_turns']} 次 · 时间跨度 {ov['first']} ~ {ov['last']}")
    if keyword:
        head += f"\n筛选: 含「{keyword}」"
    if days:
        head += f"\n筛选: 最近 {days} 天"
    out = [head, ""]
    if not rows:
        out.append("(没有符合条件的记录)")
        return "\n".join(out)
    for role, content, intent, model, ts in rows:
        who = {"user": "你", "assistant": "虎哥", "system": "系统"}.get(role, role)
        tag = f"[{intent}]" if intent else ""
        out.append(f"{_fmt(ts)} {who}{tag}: {_preview(content)}")
    return "\n".join(out)


def _stats_lines(ov, conn):
    out = [f"对话史统计: 共 {ov['total']} 条 · {ov['sessions']} 个会话 · "
           f"时间跨度 {ov['first']} ~ {ov['last']}"]
    rows = conn.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role ORDER BY COUNT(*) DESC").fetchall()
    out.append("\n按角色: " + " · ".join(f"{r} {c}" for r, c in rows))
    rows = conn.execute(
        "SELECT intent, COUNT(*) FROM messages WHERE intent IS NOT NULL AND intent != '' "
        "GROUP BY intent ORDER BY COUNT(*) DESC LIMIT 12").fetchall()
    if rows:
        out.append("按意图 (前 12): " + " · ".join(f"{i} {c}" for i, c in rows))
    rows = conn.execute(
        "SELECT model, COUNT(*) FROM messages WHERE model IS NOT NULL AND model != '' "
        "GROUP BY model ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
    if rows:
        out.append("按模型 (前 10): " + " · ".join(f"{m} {c}" for m, c in rows))
    day = datetime.now().strftime("%Y-%m-%d")
    today = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE timestamp >= ?",
        (datetime.strptime(day, "%Y-%m-%d").timestamp(),)).fetchone()[0]
    out.append(f"今天 ({day}): {today} 条")
    return "\n".join(out)


async def run(action: str = "", keyword: str = "", limit: int = 20, days: int = 0, **kwargs) -> dict:
    """读对话史。

    action:
      recent (默认) — 最近消息 + 概览头 (可带 keyword / days 过滤)
      stats         — 统计概览 (按角色/意图/模型/今天)
    """
    action = (action or "recent").strip().lower()
    try:
        limit = int(limit or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0

    kw = (keyword or kwargs.get("q") or "").strip()
    # 用户可能把关键词写在别的字段里 (技能参数抽取的常见变体)
    if not kw:
        for k in ("text", "query", "content", "name"):
            v = kwargs.get(k)
            if isinstance(v, str) and v.strip():
                kw = v.strip()
                break

    try:
        if action == "stats":
            conn = _connect()
            try:
                return {"success": True, "output": _stats_lines(_overview(conn), conn),
                        "stats": _overview(conn)}
            finally:
                conn.close()
        if action not in ("recent", "list", "search", "auto"):
            return {"success": False, "error": f"未知 action: {action} (可用: recent / stats)"}
        ov, rows = _query(kw or None, limit, days or None)
        return {"success": True, "output": _lines(ov, rows, kw or None, days or None),
                "count": len(rows), "stats": ov,
                "messages": [{"role": r[0], "content": r[1], "intent": r[2],
                              "model": r[3], "timestamp": r[4]} for r in rows]}
    except FileNotFoundError as e:
        return {"success": False, "error": f"对话记录库不存在: {e}",
                "hint": "还没聊过任何内容, 或库位置不对 (TMM_SESSION_DB / data/chat_sessions.db)"}
    except sqlite3.Error as e:
        return {"success": False, "error": f"读对话库失败: {e}"}
    except Exception as e:
        return {"success": False, "error": f"对话史查询失败: {e}"}
