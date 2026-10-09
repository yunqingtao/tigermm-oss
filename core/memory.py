
"""
TigerMemory — 虎哥三层记忆库
============================
会话层: 最近N轮对话，内存存储，进程活多久记多久
工作层: 当前任务上下文，步骤间传递数据
情节层: SQLite FTS5 全文搜索，永久存储，跨进程可查
"""

import time
import json
import sqlite3
from pathlib import Path
from collections import deque


class TigerMemory:
    """三层记忆: 会话(短) + 工作(中) + 情节(长)"""

    def __init__(self, db_path=None, max_turns=50):
        # 会话层
        self._turns = deque(maxlen=max_turns)
        self._session_id = str(int(time.time()))

        # 工作层
        self._context = {}

        # 情节层
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "memory.db"
        self._db_path = Path(db_path)
        self._init_fts()

    # ═══════════════════════════════════════════
    # 会话层 — 刚才说了什么
    # ═══════════════════════════════════════════

    def add_turn(self, role, text):
        """记录一轮对话"""
        self._turns.append({
            "role": role,
            "text": text[:2000],
            "time": time.time()
        })

    def recent(self, n=10):
        """最近n轮对话"""
        return list(self._turns)[-n:]

    def last_user_message(self):
        """上一句用户说了什么"""
        for t in reversed(self._turns):
            if t["role"] == "user":
                return t["text"]
        return ""

    def last_assistant_message(self):
        """上一句虎哥说了什么"""
        for t in reversed(self._turns):
            if t["role"] == "assistant":
                return t["text"]
        return ""

    # ═══════════════════════════════════════════
    # 工作层 — 当前任务做到哪了
    # ═══════════════════════════════════════════

    def set_ctx(self, key, value):
        self._context[key] = value

    def get_ctx(self, key, default=None):
        return self._context.get(key, default)

    def clear_ctx(self):
        self._context.clear()

    def has_ctx(self, key):
        return key in self._context

    # ═══════════════════════════════════════════
    # 情节层 — 以前聊过什么 (SQLite FTS5)
    # ═══════════════════════════════════════════

    def _init_fts(self):
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS episodic "
            "USING fts5(content, role, timestamp, tokenize='unicode61')"
        )
        conn.commit()
        conn.close()

    def remember(self, text, role="user"):
        """记住一件事，以后能搜到"""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(
            "INSERT INTO episodic (content, role, timestamp) VALUES (?, ?, ?)",
            (text[:5000], role, time.time())
        )
        conn.commit()
        conn.close()

    def recall(self, query, limit=5):
        """模糊搜索历史记忆"""
        conn = sqlite3.connect(str(self._db_path))
        try:
            cur = conn.execute(
                "SELECT content, role, timestamp FROM episodic "
                "WHERE episodic MATCH ? ORDER BY rank LIMIT ?",
                (query, limit)
            )
            results = []
            for content, role, ts in cur.fetchall():
                results.append({
                    "content": content[:300],
                    "role": role,
                    "time": ts,
                    "ago": self._time_ago(ts)
                })
            return results
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def recall_context(self, limit=20):
        """获取最近的情节记忆作为上下文"""
        conn = sqlite3.connect(str(self._db_path))
        cur = conn.execute(
            "SELECT content, role FROM episodic "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        results = []
        for content, role in cur.fetchall():
            results.append({"role": role, "content": content[:500]})
        conn.close()
        return list(reversed(results))

    def _time_ago(self, ts):
        diff = time.time() - ts
        if diff < 60:
            return "刚刚"
        elif diff < 3600:
            return f"{int(diff/60)}分钟前"
        elif diff < 86400:
            return f"{int(diff/3600)}小时前"
        else:
            return f"{int(diff/86400)}天前"

    # ═══════════════════════════════════════════
    # 综合接口
    # ═══════════════════════════════════════════

    def build_context_for_model(self, message):
        """为模型调用构建完整上下文"""
        parts = []

        # 情节: 有没有相关的历史记忆
        episodic = self.recall(message, limit=3)
        if episodic:
            parts.append("相关历史:")
            for e in episodic:
                parts.append(f"  [{e['ago']}] {e['content'][:200]}")

        # 会话: 最近几轮
        recent = self.recent(6)
        if recent:
            parts.append("最近对话:")
            for t in recent:
                role_name = "用户" if t["role"] == "user" else "虎哥"
                parts.append(f"  {role_name}: {t['text'][:200]}")

        return "".join(parts) if parts else ""
