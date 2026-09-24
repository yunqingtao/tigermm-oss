"""Bridge inbox/outbox protocol. TMM CLI polls inbox, writes outbox."""
import json, os, time
from pathlib import Path

INBOX_FILE = None
OUTBOX_FILE = None
_last_hash = ""

def init(project_root: str):
    global INBOX_FILE, OUTBOX_FILE
    d = os.path.join(project_root, "data")
    os.makedirs(d, exist_ok=True)
    INBOX_FILE = os.path.join(d, "bridge_inbox.json")
    OUTBOX_FILE = os.path.join(d, "bridge_outbox.json")

def poll() -> list:
    """Called by CLI background thread. Returns list of (sender, text, msg_id) tuples."""
    global _last_hash
    if not INBOX_FILE:
        return []
    try:
        with open(INBOX_FILE, "r", encoding="utf-8") as f:
            raw = f.read()
        import hashlib
        h = hashlib.md5(raw.encode()).hexdigest()
        if h == _last_hash:
            return []
        _last_hash = h
        data = json.loads(raw)
        if not data:
            return []
        # Clear inbox
        with open(INBOX_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        _last_hash = hashlib.md5("[]".encode()).hexdigest()
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    
    results = []
    for item in data:
        sender = item.get("sender", "")
        text = item.get("text", "")
        msg_id = item.get("msg_id", "")
        if sender and text:
            results.append((sender, text, msg_id))
    return results

def write_response(sender: str, text: str, msg_id: str = ""):
    """Called after pipeline processes a message."""
    if not OUTBOX_FILE:
        return
    try:
        existing = []
        try:
            with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        existing.append({"sender": sender, "text": text[:1000], "msg_id": msg_id})
        with open(OUTBOX_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
    except Exception:
        pass
