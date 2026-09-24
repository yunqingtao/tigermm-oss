"""
Tiger.M.M Inbox - Async approval queue (distilled from OpenWorker PermissionEngine + InboxStore)

Core design:
  1. State machine: pending -> resolved (first-responder-wins, idempotent)
  2. asyncio.Event suspends agent -> any surface resolves -> agent wakes
  3. SQLite persistence (reuses mary3/storage SQLitePool pattern)
  4. Auto-deny on timeout (default 300s)

Unlike OpenWorker's per-tool approval granularity, Tiger.M.M uses batch command approval:
  - One InboxItem = one batch of commands
  - Resolution: allow / deny / always (session memory)
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import datetime
from core.risk import RiskClass, classify_batch
from core.logger import log as _log

STATE_PENDING = "pending"
STATE_RESOLVED = "resolved"

RESOLVE_ALLOW = "allow"
RESOLVE_DENY = "deny"
RESOLVE_ALWAYS = "always"

DEFAULT_TIMEOUT = 300


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _now_ts() -> float:
    return time.time()


@dataclass
class InboxItem:
    id: str
    session_id: str
    title: str
    body: str = ""
    state: str = STATE_PENDING
    resolution: Optional[str] = None
    cmds: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    resolved_at: Optional[str] = None
    timeout_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "resolution": self.resolution,
            "cmds": self.cmds,
            "urls": self.urls,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_row(cls, row: tuple) -> "InboxItem":
        return cls(
            id=row[0],
            session_id=row[1],
            title=row[2],
            body=row[3] or "",
            state=row[4],
            resolution=row[5],
            cmds=json.loads(row[6] or "[]"),
            urls=json.loads(row[7] or "[]"),
            created_at=row[8] or "",
            resolved_at=row[9],
            timeout_at=float(row[10] or 0),
        )


class InboxStore:
    """SQLite-backed approval queue with asyncio.Event wait mechanism"""

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "inbox.db"
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._waiters: dict[str, asyncio.Event] = {}
        self._session_allow: set[str] = set()
        self._conn = None  # Reusable connection
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        # 每次新建独立连接: SQLite 连接非线程安全, 复用单连接会被
        # FastAPI/anyio 线程池并发 execute 触发 native access violation (SIGSEGV)。
        # 连接用完即 close, 各调用点均已有 finally: conn.close()。
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT DEFAULT '',
                    state TEXT DEFAULT 'pending',
                    resolution TEXT,
                    cmds_json TEXT DEFAULT '[]',
                    urls_json TEXT DEFAULT '[]',
                    created_at TEXT,
                    resolved_at TEXT,
                    timeout_at REAL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_inbox_session_state
                ON inbox(session_id, state)
            """)
            conn.commit()
        finally:
            conn.close()

    def create_approval(
        self,
        session_id: str,
        cmds: list[str],
        urls: list[str] = None,
        summary: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> InboxItem:
        item_id = uuid.uuid4().hex[:12]
        cmd_list = [c[:200] for c in cmds[:50]]
        url_list = (urls or [])[:20]

        title = f"Execute {len(cmd_list)} command(s)"
        body_lines = []
        for i, c in enumerate(cmd_list, 1):
            body_lines.append(f"  {i}. {c[:120]}")
        if url_list:
            body_lines.append(f"\nOpen {len(url_list)} link(s) after completion")
        if summary:
            body_lines.insert(0, summary)

        item = InboxItem(
            id=item_id,
            session_id=session_id,
            title=title,
            body="\n".join(body_lines),
            cmds=cmd_list,
            urls=url_list,
            timeout_at=_now_ts() + timeout,
        )

        # Auto-approve READ-only batches
        _log("inbox", "auto_approve", session_id=session_id, risk="READ", cmds=len(cmd_list))
        if cmd_list and classify_batch(cmd_list) == RiskClass.READ:
            item.state = STATE_RESOLVED
            item.resolution = RESOLVE_ALLOW
            item.resolved_at = _now_iso()

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO inbox (id, session_id, title, body, state, resolution, cmds_json, urls_json, created_at, resolved_at, timeout_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.id, item.session_id, item.title, item.body,
                    item.state, item.resolution,
                    json.dumps(item.cmds, ensure_ascii=False),
                    json.dumps(item.urls, ensure_ascii=False),
                    item.created_at, item.resolved_at, item.timeout_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return item

    def batch_create(self, items: list[tuple[str, list[str]]]) -> list[InboxItem]:
        """Batch create multiple approvals in one transaction.
        items: [(session_id, [cmds]), ...]
        """
        results = []
        conn = self._get_conn()
        try:
            for session_id, cmds in items:
                item_id = uuid.uuid4().hex[:12]
                cmd_list = [c[:200] for c in cmds[:50]]
                item = InboxItem(
                    id=item_id, session_id=session_id,
                    title=f"Execute {len(cmd_list)} command(s)",
                    body="\n".join(f"  {i}. {c[:120]}" for i, c in enumerate(cmd_list, 1)),
                    cmds=cmd_list, timeout_at=_now_ts() + DEFAULT_TIMEOUT,
                )
                if cmd_list and classify_batch(cmd_list) == RiskClass.READ:
                    item.state = STATE_RESOLVED
                    item.resolution = RESOLVE_ALLOW
                    item.resolved_at = _now_iso()
                conn.execute(
                    """INSERT INTO inbox (id, session_id, title, body, state, resolution, cmds_json, urls_json, created_at, resolved_at, timeout_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (item.id, item.session_id, item.title, item.body,
                     item.state, item.resolution,
                     json.dumps(item.cmds, ensure_ascii=False),
                     json.dumps(item.urls, ensure_ascii=False),
                     item.created_at, item.resolved_at, item.timeout_at),
                )
                # item persisted to DB, accessible via get()
                results.append(item)
            conn.commit()
        finally:
            conn.close()
        return results

    def get(self, item_id: str) -> Optional[InboxItem]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM inbox WHERE id = ?", (item_id,)
            ).fetchone()
            return InboxItem.from_row(row) if row else None
        finally:
            conn.close()

    def list_pending(self, session_id: Optional[str] = None) -> list[InboxItem]:
        conn = self._get_conn()
        try:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE state = ? AND session_id = ? ORDER BY created_at",
                    (STATE_PENDING, session_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE state = ? ORDER BY created_at",
                    (STATE_PENDING,),
                ).fetchall()
            return [InboxItem.from_row(r) for r in rows]
        finally:
            conn.close()

    def list_all(self, session_id: Optional[str] = None, limit: int = 50) -> list[InboxItem]:
        conn = self._get_conn()
        try:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM inbox WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM inbox ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [InboxItem.from_row(r) for r in rows]
        finally:
            conn.close()

    def resolve(self, item_id: str, resolution: str) -> bool:
        """Resolve an approval item. First-responder-wins. Wakes the waiting agent."""
        _log("inbox", "resolve", item_id=item_id, resolution=resolution)
        if resolution not in (RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS):
            return False

        with self._lock:
            item = self.get(item_id)
            if item is None:
                return False
            if item.state != STATE_PENDING:
                return False

            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE inbox SET state = ?, resolution = ?, resolved_at = ? WHERE id = ?",
                    (STATE_RESOLVED, resolution, _now_iso(), item_id),
                )
                conn.commit()
            finally:
                conn.close()

        waiter = self._waiters.get(item_id)
        if waiter is not None:
            waiter.set()
        return True

    def handle_timeouts(self) -> int:
        now = _now_ts()
        closed = 0
        for item in self.list_pending():
            if item.timeout_at > 0 and now >= item.timeout_at:
                if self.resolve(item.id, RESOLVE_DENY):
                    closed += 1
        return closed

    async def wait_for(self, item_id: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """Suspend and wait for approval. Returns 'allow' | 'deny' | 'always'."""
        item = self.get(item_id)
        if item is None:
            return RESOLVE_DENY
        if item.state == STATE_RESOLVED:
            return item.resolution or RESOLVE_DENY

        ev = asyncio.Event()
        self._waiters[item_id] = ev

        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.resolve(item_id, RESOLVE_DENY)
            return RESOLVE_DENY
        finally:
            self._waiters.pop(item_id, None)

        resolved = self.get(item_id)
        return (resolved.resolution if resolved else RESOLVE_DENY) or RESOLVE_DENY

    def allow_session(self, session_id: str):
        self._session_allow.add(session_id)

    def is_session_allowed(self, session_id: str) -> bool:
        return session_id in self._session_allow

    def revoke_session(self, session_id: str):
        self._session_allow.discard(session_id)

    def cleanup(self, older_than_hours: int = 24):
        conn = self._get_conn()
        try:
            cutoff = _now_ts() - older_than_hours * 3600
            conn.execute(
                "DELETE FROM inbox WHERE state = ? AND timeout_at < ? AND timeout_at > 0",
                (STATE_RESOLVED, cutoff),
            )
            conn.commit()
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self._get_conn()
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE state = ?", (STATE_PENDING,)
            ).fetchone()[0]
            resolved = conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE state = ?", (STATE_RESOLVED,)
            ).fetchone()[0]
            return {"pending": pending, "resolved": resolved}
        finally:
            conn.close()


# Global singleton
_inbox_instance: Optional[InboxStore] = None


def get_inbox(db_path: Optional[str | Path] = None) -> InboxStore:
    global _inbox_instance
    if _inbox_instance is None:
        _inbox_instance = InboxStore(db_path)
    return _inbox_instance
