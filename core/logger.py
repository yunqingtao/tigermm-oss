"""
Tiger.M.M Structured Logger — lightweight, no external deps.
Usage: from core.logger import log
       log("inbox", "approval_created", item_id="abc", session="s1")
"""
import json, time, threading, os
from pathlib import Path
from typing import Any, Optional

LOG_DIR = Path(__file__).parent.parent / "data" / "logs"
_LOG_LOCK = threading.Lock()
_BUFFER: list[dict] = []
_BUFFER_SIZE = 20
_CURRENT_SESSION = "default"


def set_session(session_id: str):
    global _CURRENT_SESSION
    _CURRENT_SESSION = session_id


def log(module: str, event: str, level: str = "info", **kwargs):
    """Write a structured log entry. Auto-flushes buffer every N entries."""
    entry = {
        "ts": time.time(),
        "session": _CURRENT_SESSION,
        "module": module,
        "event": event,
        "level": level,
        **kwargs,
    }
    with _LOG_LOCK:
        _BUFFER.append(entry)
        if len(_BUFFER) >= _BUFFER_SIZE:
            _flush()


def _flush():
    if not _BUFFER:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    path = LOG_DIR / f"tiger-{today}.jsonl"
    # Rotate if > 10MB
    if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
        rotated = LOG_DIR / f"tiger-{today}-{int(time.time())}.jsonl"
        path.rename(rotated)
    with open(path, "a", encoding="utf-8") as f:
        for entry in _BUFFER:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    _BUFFER.clear()


def flush():
    with _LOG_LOCK:
        _flush()


def read_logs(date: str = None, module: str = None, limit: int = 100) -> list[dict]:
    """Read log entries, optionally filtered."""
    if date is None:
        date = time.strftime("%Y-%m-%d")
    path = LOG_DIR / f"tiger-{date}.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if module and entry.get("module") != module:
                    continue
                entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries[-limit:]


def stats() -> dict:
    """Log statistics for current session."""
    entries = read_logs()
    if not entries:
        return {"total": 0}
    modules = {}
    levels = {}
    for e in entries:
        m = e.get("module", "?")
        modules[m] = modules.get(m, 0) + 1
        lv = e.get("level", "info")
        levels[lv] = levels.get(lv, 0) + 1
    return {
        "total": len(entries),
        "modules": modules,
        "levels": levels,
    }
