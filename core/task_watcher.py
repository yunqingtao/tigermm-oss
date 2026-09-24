"""
TaskWatcher — persistent background task monitor.
===================================================
Survives restart, checks conditions on interval,
notifies user when triggered.

Usage:
  /watch add 监控Agent邮件，有新邮件通知我
  /watch list
  /watch remove <id>
"""
import asyncio, json, time, logging, sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger("core.task_watcher")

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    check_tool TEXT NOT NULL,
    check_action TEXT NOT NULL,
    check_params TEXT NOT NULL,  -- JSON
    interval_sec INTEGER DEFAULT 300,
    last_checked REAL DEFAULT 0,
    last_result TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


class TaskWatcher:
    """Background watcher that polls tools and triggers alerts."""

    def __init__(self, data_dir: Path, gateway, model_client=None):
        self.db_path = data_dir / "watchers.db"
        self.gateway = gateway
        self.model_client = model_client
        self._alerts = []  # pending alerts for the current session
        self._task = None
        self._running = False
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(SCHEMA)
        conn.commit()
        conn.close()

    def add(self, description: str, check_tool: str, check_action: str,
            check_params: dict, interval_sec: int = 300) -> int:
        """Add a new watcher. Returns watcher id."""
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.execute(
            "INSERT INTO watchers (description, check_tool, check_action, check_params, interval_sec) VALUES (?,?,?,?,?)",
            (description, check_tool, check_action, json.dumps(check_params, ensure_ascii=False), interval_sec)
        )
        wid = cur.lastrowid
        conn.commit()
        conn.close()
        logger.info("TaskWatcher: added #%d — %s", wid, description)
        return wid

    def remove(self, wid: int) -> bool:
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.execute("DELETE FROM watchers WHERE id = ?", (wid,))
        ok = cur.rowcount > 0
        conn.commit()
        conn.close()
        return ok

    def list_all(self) -> list:
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT id, description, check_tool, interval_sec, last_checked, enabled FROM watchers ORDER BY id"
        ).fetchall()
        conn.close()
        return [
            {"id": r[0], "description": r[1], "tool": r[2],
             "interval_sec": r[3], "last_checked": r[4], "enabled": r[5]}
            for r in rows
        ]

    def get_alerts(self) -> list:
        """Get and clear pending alerts."""
        alerts = list(self._alerts)
        self._alerts.clear()
        return alerts

    async def check_all(self):
        """Check all enabled watchers. Called by background loop."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT id, check_tool, check_action, check_params, last_result FROM watchers WHERE enabled = 1"
        ).fetchall()
        conn.close()

        now = time.time()
        for row in rows:
            wid, tool, action, params_json, last_result = row
            try:
                params = json.loads(params_json) if params_json else {}
            except json.JSONDecodeError:
                params = {}

            try:
                if action:
                    result = await self.gateway.call(tool, action=action, **params)
                else:
                    result = await self.gateway.call(tool, **params)
            except Exception as e:
                result = {"success": False, "error": str(e)}

            result_str = json.dumps(result, ensure_ascii=False, default=str)

            # Compare with last_result to detect changes
            if result_str != last_result and result.get("success"):
                last_data = json.loads(last_result) if last_result else {}
                # Check if something NEW appeared
                new_count = result.get("count", 0)
                old_count = last_data.get("count", 0) if isinstance(last_data, dict) else 0
                if new_count > old_count:
                    self._alerts.append({
                        "watcher_id": wid,
                        "result": result,
                        "time": now,
                    })
                    logger.info("TaskWatcher: #%d triggered — new items", wid)

            # Update last checked + result
            conn = sqlite3.connect(str(self.db_path))
            conn.execute(
                "UPDATE watchers SET last_checked = ?, last_result = ? WHERE id = ?",
                (now, result_str, wid)
            )
            conn.commit()
            conn.close()

    async def _loop(self):
        """Background loop — runs every 60 seconds."""
        while self._running:
            try:
                await self.check_all()
            except Exception as e:
                logger.error("TaskWatcher loop error: %s", e)
            await asyncio.sleep(60)

    def start(self):
        """Start background loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("TaskWatcher: started")

    def stop(self):
        """Stop background loop."""
        self._running = False
        if self._task:
            self._task.cancel()


# Singleton
_watcher: Optional[TaskWatcher] = None

def get_watcher(data_dir: Path, gateway, model_client=None) -> TaskWatcher:
    global _watcher
    if _watcher is None:
        _watcher = TaskWatcher(data_dir, gateway, model_client)
    return _watcher
