"""
Encrypted JSON file storage — atomic writes, file locking, memory cache.
Includes desensitization and chat history management with auto-cleanup.
"""
import json
import re
import threading
import logging
import time
from pathlib import Path
from config.crypto import secure_save, secure_load
from config.settings import CHAT_RETENTION_DAYS

logger = logging.getLogger(__name__)

# Patterns for auto-desensitization
_SENSITIVE_PATTERNS = [
    (re.compile(r'sk-[a-zA-Z0-9]{20,}', re.IGNORECASE), 'sk-***REDACTED***'),
    (re.compile(r'api[_-]?key[=:]\s*["\x27][a-zA-Z0-9_\-]{16,}', re.IGNORECASE), 'api_key=***REDACTED***'),
    (re.compile(r'token[=:]\s*["\x27][a-zA-Z0-9_\-]{16,}', re.IGNORECASE), 'token=***REDACTED***'),
    (re.compile(r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}', re.IGNORECASE), 'Bearer ***REDACTED***'),
    (re.compile(r'\b1[3-9]\d{9}\b'), '***PHONE***'),
    (re.compile(r'\b\d{15,19}\b'), '***CARD***'),
]


def desensitize_text(text: str) -> str:
    """Strip API keys, tokens, phone numbers, card numbers from text."""
    if not isinstance(text, str):
        return str(text)
    result = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def desensitize_dict(data: dict) -> dict:
    """Recursively desensitize all string values in a dict."""
    cleaned = {}
    for k, v in data.items():
        if isinstance(v, str):
            cleaned[k] = desensitize_text(v)
        elif isinstance(v, dict):
            cleaned[k] = desensitize_dict(v)
        elif isinstance(v, list):
            cleaned[k] = [
                desensitize_text(item) if isinstance(item, str) else item
                for item in v
            ]
        else:
            cleaned[k] = v
    return cleaned


class JsonStore:
    """Thread-safe encrypted JSON file store with in-memory cache."""

    def __init__(self, file_path: Path, force_encrypt: bool = False):
        self.file_path = Path(file_path)
        self.force_encrypt = force_encrypt
        self._lock = threading.Lock()
        self._cache = None
        self._dirty = False

    def load(self) -> dict:
        with self._lock:
            if self._cache is None:
                self._cache = secure_load(self.file_path, force_encrypt=self.force_encrypt)
                self._dirty = False
            return dict(self._cache)

    def save(self, data: dict = None):
        with self._lock:
            if data is not None:
                self._cache = dict(data)
            if self._cache is not None:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                secure_save(self.file_path, self._cache)
                self._dirty = False

    def update(self, key: str, value):
        with self._lock:
            if self._cache is None:
                self.load()
            self._cache[key] = value
            self._dirty = True

    def get(self, key: str, default=None):
        with self._lock:
            if self._cache is None:
                self.load()
            return self._cache.get(key, default)

    def delete(self, key: str):
        with self._lock:
            if self._cache is None:
                self.load()
            self._cache.pop(key, None)
            self._dirty = True

    def flush(self):
        """Persist if dirty."""
        if self._dirty:
            self.save()

    def clear(self):
        with self._lock:
            self._cache = {}
            self._dirty = True

    def __len__(self):
        return len(self._cache or {})

    def items(self):
        return (self._cache or {}).items()

    def keys(self):
        return (self._cache or {}).keys()

    def values(self):
        return (self._cache or {}).values()


# === Chat history management ===

def save_chat_record(session_id: str, chat_list: list):
    """Save chat history encrypted, with auto-desensitization."""
    from config.settings import CHAT_HISTORY_FILE
    
    store = JsonStore(CHAT_HISTORY_FILE, force_encrypt=True)
    all_records = store.load()
    
    # Desensitize before storing
    safe_chat = []
    for item in chat_list:
        if isinstance(item, dict):
            safe_item = dict(item)
            if "content" in safe_item:
                safe_item["content"] = desensitize_text(safe_item["content"])
            safe_chat.append(safe_item)
        else:
            safe_chat.append(item)
    
    all_records[session_id] = {
        "messages": safe_chat,
        "updated_at": time.time(),
    }
    store.save(all_records)


def load_chat_record(session_id: str) -> list:
    """Load encrypted chat history for a session."""
    from config.settings import CHAT_HISTORY_FILE
    
    store = JsonStore(CHAT_HISTORY_FILE, force_encrypt=True)
    all_records = store.load()
    record = all_records.get(session_id, {})
    return record.get("messages", [])


def cleanup_expired_chat(max_days: int = None):
    """Remove chat sessions older than max_days."""
    from config.settings import CHAT_HISTORY_FILE
    
    if max_days is None:
        max_days = CHAT_RETENTION_DAYS
    
    store = JsonStore(CHAT_HISTORY_FILE, force_encrypt=True)
    all_records = store.load()
    cutoff = time.time() - (max_days * 86400)
    
    removed = 0
    for sid in list(all_records.keys()):
        record = all_records[sid]
        if isinstance(record, dict) and record.get("updated_at", 0) < cutoff:
            del all_records[sid]
            removed += 1
    
    if removed > 0:
        store.save(all_records)
        logger.info("Cleaned up %d expired chat sessions (retention: %d days)", removed, max_days)
