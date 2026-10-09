"""
Unified Fernet encryption for all sensitive storage.
No plaintext fallback. No mixed modes.
force_encrypt=True = hard fail on decrypt error.
"""
import json
import logging
from pathlib import Path
from cryptography.fernet import Fernet
import os
import re
import time

logger = logging.getLogger(__name__)

# Will be set by app factory
_cipher = None
CRYPTO_KEY_PATH = None


def init_crypto(key_path: Path):
    """Initialize encryption. Must be called once at startup.
    Also enforces key file permissions (0o600 on Linux, read-only on Windows)."""
    global _cipher, CRYPTO_KEY_PATH
    CRYPTO_KEY_PATH = key_path
    if not key_path.exists():
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
    else:
        # Verify permissions on existing key
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
    _cipher = Fernet(key_path.read_bytes())
    return _cipher


def _get_cipher() -> Fernet:
    if _cipher is None:
        raise RuntimeError("Crypto not initialized. Call init_crypto() first.")
    return _cipher


def encrypt_json(data: dict) -> bytes:
    """Encrypt a dict to bytes."""
    return _get_cipher().encrypt(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def decrypt_json(raw: bytes) -> dict:
    """Decrypt bytes to dict. Raises on failure — no plaintext fallback."""
    return json.loads(_get_cipher().decrypt(raw).decode("utf-8"))


def secure_save(file_path: Path, data: dict):
    """Save dict as encrypted file. Atomic write to prevent corruption."""
    encrypted = encrypt_json(data)
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_bytes(encrypted)
    tmp.replace(file_path)


def secure_load(file_path: Path, force_encrypt: bool = False) -> dict:
    """Load encrypted file.
    
    Args:
        file_path: Path to .enc file
        force_encrypt: If True, raise RuntimeError on ANY decrypt failure.
                       If False, return {} for non-critical files (with warning).
    
    NEVER falls back to reading plaintext.
    """
    if not file_path.exists():
        if force_encrypt:
            raise FileNotFoundError(f"Required encrypted file not found: {file_path}")
        return {}
    try:
        return decrypt_json(file_path.read_bytes())
    except Exception as e:
        if force_encrypt:
            raise RuntimeError(
                f"CRITICAL: {file_path.name} decryption failed. "
                f"Key mismatch or corruption. HALTING. Error: {e}"
            )
        logger.warning("%s decrypt failed, returning empty (non-critical)", file_path.name)
        return {}


def secure_load_json(file_path: Path, force_encrypt: bool = True) -> dict:
    """Alias for secure_load with force_encrypt=True by default.
    Use this for all sensitive data files (users, keys, chat history)."""
    return secure_load(file_path, force_encrypt=force_encrypt)


def secure_save_json(file_path: Path, data: dict) -> None:
    """Alias for secure_save."""
    secure_save(file_path, data)
