"""
Session Store — SQLite + FTS5 for searchable message history.
Replaces chat_history.enc (encrypted blob) with queryable storage.

Hermes pattern: agent can self-discover context by searching history.
"""

import sqlite3
import threading
import time
import json
from pathlib import Path


# ── 对话库路径解析 (纯函数, 便于测试; 隔离规则见下) ──────────────────
_REAL_DB = Path(__file__).parent.parent / "data" / "chat_sessions.db"


def resolve_db_path(db_path=None):
    """决定用哪个对话库文件。

    ★ 隔离规则 (2026-09-19, 两轮门禁实测踩出来的):
      命中条件 = "这个库就是用户的真实对话库" —— 不管是默认取值 **还是显式传入**
      (core/pipeline.py 就是显式传 DATA_DIR/"chat_sessions.db")。
      显式传入**别的**路径 (测试临时库) 一律不动。
    坑史: ① 无条件覆盖 → 劫持了测试临时库, 套件互相串数据
          ② 只认"默认取值" → 漏掉 pipeline 的显式路径 → verify_learn_loop 的
             8 句 probe=False 学习测试仍写进用户库 (门禁 +16 行)
    不设 TMM_SESSION_DB 时行为完全不变 (正常使用/活服务不受影响)。
    """
    import os as _os
    if db_path is None:
        db_path = _REAL_DB
    _env_db = _os.environ.get("TMM_SESSION_DB")
    if _env_db:
        try:
            if Path(db_path).resolve() == _REAL_DB.resolve():
                return Path(_env_db)
        except Exception:
            pass
    return Path(db_path)


class SessionStore:
    """FTS5-backed session store. Agent can search past conversations."""

    def __init__(self, db_path=None):
        # 2026-08-20 v3: 默认改用独立文件 chat_sessions.db — 原 sessions.db 被
        # core/session_manager.py (TaskManager) 抢占建了 task 表, SessionStore 的
        # CREATE TABLE IF NOT EXISTS 不重建 → 历史读写全错 → ollama 失忆
        # 库路径解析 (含验证/门禁隔离) 见 resolve_db_path()
        self.db_path = resolve_db_path(db_path)
        self._conn = None
        # ★ 2026-09-19: Web 服务里 SessionStore 是**跨线程**用的 —— uvicorn 把同步
        #   endpoint (web_server.history) 丢进线程池, 而落库发生在另一个线程。
        #   sqlite3 默认 check_same_thread=True → 抛
        #   "SQLite objects created in a thread can only be used in that same thread"
        #   实测后果: /history 读库静默失败退回内存; _persist_turn 静默不落库
        #   (web_ui.err.log 里那句 "history from db failed, fallback to memory" 就是它)。
        #   修法: 允许跨线程 + 自己用锁串行化 (本项目访问是短事务、低频)。
        self._lock = threading.RLock()

    @property
    def conn(self):
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._init_schema()
            return self._conn

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                message_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                intent TEXT,
                model TEXT,
                elapsed REAL,
                timestamp REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content, role, intent,
                content='messages', content_rowid='id'
            );
            CREATE TABLE IF NOT EXISTS state_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.commit()

    # --- Session management ---

    def start_session(self):
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO sessions (started_at) VALUES (?)", (now,)
        )
        self.conn.commit()
        return cur.lastrowid

    @property
    def current_session_id(self):
        row = self.conn.execute(
            "SELECT value FROM state_meta WHERE key='current_session'"
        ).fetchone()
        if row:
            return int(row[0])
        sid = self.start_session()
        self.conn.execute(
            "INSERT OR REPLACE INTO state_meta (key, value) VALUES ('current_session', ?)",
            (str(sid),)
        )
        self.conn.commit()
        return sid

    # --- Message storage ---

    def add_message(self, role, content, intent="", model="", elapsed=0):
        with self._lock:            # ★ 跨线程共享连接 → "写 + commit" 必须串行
            sid = self.current_session_id
            self.conn.execute(
                """INSERT INTO messages (session_id, role, content, intent, model, elapsed, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sid, role, content, intent, model, elapsed, time.time())
            )
            self.conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (sid,)
            )
            # Rebuild FTS index periodically
            count = self.conn.execute(
                "SELECT message_count FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if count and count[0] % 20 == 0:
                self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            self.conn.commit()

    # --- Search ---

    def search(self, query, limit=5):
        """Search message history. Uses LIKE for reliability with CJK."""
        try:
            # Force FTS rebuild to sync content
            self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            self.conn.commit()
        except Exception:
            pass
        try:
            rows = self.conn.execute(
                """SELECT m.content, m.role, m.intent, m.timestamp, s.started_at
                   FROM messages m
                   JOIN sessions s ON m.session_id = s.id
                   WHERE m.content LIKE ?
                   ORDER BY m.timestamp DESC
                   LIMIT ?""",
                (f"%{query}%", limit)
            ).fetchall()
            results = [
                {"content": r[0][:200], "role": r[1], "intent": r[2],
                 "timestamp": r[3], "session_start": r[4]}
                for r in rows
            ]
            if results:
                return results
        except Exception:
            pass
        # Try FTS5 as fallback
        try:
            rows = self.conn.execute(
                """SELECT m.content, m.role, m.intent, m.timestamp, s.started_at
                   FROM messages_fts f
                   JOIN messages m ON f.rowid = m.id
                   JOIN sessions s ON m.session_id = s.id
                   WHERE messages_fts MATCH ?
                   ORDER BY m.timestamp DESC
                   LIMIT ?""",
                (query, limit)
            ).fetchall()
            return [
                {"content": r[0][:200], "role": r[1], "intent": r[2],
                 "timestamp": r[3], "session_start": r[4]}
                for r in rows
            ]
        except sqlite3.OperationalError:
            return []

    def recent(self, limit=10):
        """Get recent messages from current session."""
        sid = self.current_session_id
        rows = self.conn.execute(
            """SELECT role, content, intent, model, elapsed
               FROM messages WHERE session_id = ?
               ORDER BY id DESC LIMIT ?""",
            (sid, limit)
        ).fetchall()
        return [
            {"role": r[0], "content": r[1][:200], "intent": r[2],
             "model": r[3], "elapsed": r[4]}
            for r in reversed(rows)
        ]

    def recent_all(self, limit=50):
        """最近 N 条消息 —— **跨会话**读取。

        ★ 2026-09-19 新增: 界面的"恢复历史"不能只用 recent() —— 那个只看
          current_session_id, 而每次重启引擎都会开一个新会话(空) → 重启后
          界面永远恢复不出历史 (实测: 库里明明有消息, /history 仍返回 0 条)。
        截断口径与内存版 TigerMemory.add_turn (2000 字) 对齐。
        """
        try:
            with self._lock:
                rows = self.conn.execute(
                    """SELECT role, content, intent, model, elapsed
                       FROM messages ORDER BY id DESC LIMIT ?""",
                    (int(limit),)
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {"role": r[0], "content": (r[1] or "")[:2000], "intent": r[2],
             "model": r[3], "elapsed": r[4]}
            for r in reversed(rows)
        ]

    def session_summary(self):
        """Summary of current session."""
        sid = self.current_session_id
        row = self.conn.execute(
            "SELECT started_at, message_count FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        if not row:
            return {"messages": 0, "uptime": 0}
        return {
            "started_at": row[0],
            "messages": row[1],
            "uptime": time.time() - row[0],
        }

    def list_sessions(self, limit=10):
        """List recent sessions."""
        rows = self.conn.execute(
            """SELECT id, started_at, message_count
               FROM sessions ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        results = []
        for r in rows:
            # Get first user message as title
            first = self.conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
                (r[0],)
            ).fetchone()
            title = first[0][:60] if first else "(empty)"
            results.append({
                "id": r[0], "title": title,
                "started": r[1], "messages": r[2]
            })
        return results

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
