"""
SessionRecovery — save/restore last conversation context across restarts.
"""
import json, os, time
from pathlib import Path

SESSION_FILE = "last_session.json"
MAX_TURNS = 3


def save(project_root: str, messages: list):
    """Save last N messages before exit."""
    if not messages:
        return
    data = {
        "ts": time.time(),
        "turns": messages[-MAX_TURNS:],  # last 3 exchanges
    }
    path = Path(project_root) / "data" / SESSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load(project_root: str) -> str | None:
    """Load last session context. Returns formatted string or None."""
    path = Path(project_root) / "data" / SESSION_FILE
    if not path.exists():
        return None
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, KeyError):
        return None

    turns = data.get("turns", [])
    if not turns:
        return None

    # Age check — only show if less than 24h old
    age = time.time() - data.get("ts", 0)
    if age > 86400:
        return None

    lines = ["\n上次会话:"]
    for i, turn in enumerate(turns[-MAX_TURNS:], 1):
        if isinstance(turn, dict):
            role = turn.get("role", "?")
            content = turn.get("content", "")[:120]
            prefix = "你" if role == "user" else "TMM"
            lines.append(f"  {prefix}: {content}")
        else:
            lines.append(f"  {turn}")
    
    return "\n".join(lines)


def clear(project_root: str):
    """Delete session file."""
    path = Path(project_root) / "data" / SESSION_FILE
    if path.exists():
        path.unlink()
