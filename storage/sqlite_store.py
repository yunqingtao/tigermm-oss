"""
SQLite connection pool with WAL mode and auto-cleanup.
"""
import sqlite3
import threading
import queue
import logging
import time
from pathlib import Path
from config.settings import DB_POOL_SIZE
import os
import re

logger = logging.getLogger(__name__)


class SQLitePool:
    """Thread-safe SQLite connection pool."""

    def __init__(self, db_path: Path, pool_size: int = None):
        self.db_path = str(db_path)
        self.pool = queue.Queue(maxsize=pool_size or DB_POOL_SIZE)
        for _ in range(pool_size or DB_POOL_SIZE):
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self.pool.put(conn)

    def get_conn(self):
        try:
            return self.pool.get(timeout=5)
        except queue.Empty:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            return conn

    def release_conn(self, conn):
        try:
            self.pool.put_nowait(conn)
        except queue.Full:
            conn.close()

    def close_all(self):
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
                conn.close()
            except queue.Empty:
                break


class MemoryStore:
    """High-level memory DB operations with auto-cleanup."""

    def __init__(self, db_path: Path):
        self.pool = SQLitePool(db_path)
        self._init_schema()

    def _init_schema(self):
        conn = self.pool.get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE,
                    value TEXT,
                    created_at REAL,
                    accessed_at REAL,
                    ttl_days INTEGER DEFAULT 30
                )
            """)
            conn.commit()
        finally:
            self.pool.release_conn(conn)

    def remember(self, key: str, value: str, ttl_days: int = 30):
        conn = self.pool.get_conn()
        try:
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO memories (key, value, created_at, accessed_at, ttl_days) VALUES (?, ?, ?, ?, ?)",
                (key, value, now, now, ttl_days)
            )
            conn.commit()
        finally:
            self.pool.release_conn(conn)

    def recall(self, key: str) -> str | None:
        conn = self.pool.get_conn()
        try:
            row = conn.execute(
                "SELECT value FROM memories WHERE key = ? AND (accessed_at + ttl_days * 86400) > ?",
                (key, time.time())
            ).fetchone()
            if row:
                conn.execute("UPDATE memories SET accessed_at = ? WHERE key = ?",
                             (time.time(), key))
                conn.commit()
                return row[0]
            return None
        finally:
            self.pool.release_conn(conn)

    def forget(self, key: str):
        conn = self.pool.get_conn()
        try:
            conn.execute("DELETE FROM memories WHERE key = ?", (key,))
            conn.commit()
        finally:
            self.pool.release_conn(conn)

    def cleanup_expired(self):
        conn = self.pool.get_conn()
        try:
            conn.execute(
                "DELETE FROM memories WHERE accessed_at + ttl_days * 86400 < ?",
                (time.time(),)
            )
            conn.commit()
        finally:
            self.pool.release_conn(conn)

    def search(self, query: str = "", limit: int = 20) -> list[dict]:
        """Full-text search across all memories. Empty query = all entries."""
        conn = self.pool.get_conn()
        try:
            if query:
                rows = conn.execute(
                    "SELECT key, value, created_at, accessed_at, ttl_days FROM memories "
                    "WHERE key LIKE ? OR value LIKE ? ORDER BY accessed_at DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value, created_at, accessed_at, ttl_days FROM memories "
                    "ORDER BY accessed_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [{
                "key": r[0], "value": r[1][:200] if r[1] else "",
                "created": time.strftime("%m-%d %H:%M", time.localtime(r[2])),
                "accessed": time.strftime("%m-%d %H:%M", time.localtime(r[3])),
                "ttl_days": r[4]
            } for r in rows]
        finally:
            self.pool.release_conn(conn)

    def stats(self) -> dict:
        """Memory statistics: count, total size, oldest, newest."""
        conn = self.pool.get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(value)),0), "
                "MIN(created_at), MAX(accessed_at) FROM memories"
            ).fetchone()
            return {
                "count": row[0], "total_bytes": row[1],
                "oldest": time.strftime("%Y-%m-%d", time.localtime(row[2])) if row[2] else "N/A",
                "newest": time.strftime("%Y-%m-%d %H:%M", time.localtime(row[3])) if row[3] else "N/A",
            }
        finally:
            self.pool.release_conn(conn)

    def close(self):
        self.pool.close_all()
