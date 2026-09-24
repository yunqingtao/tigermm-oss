"""
Log sanitization — mask API keys, tokens, phone numbers, ID numbers.
"""
import re

_PATTERNS = [
    (r'sk-[A-Za-z0-9]{20,}', '[API_KEY]'),
    (r'Bearer\s+[A-Za-z0-9_\-]+', 'Bearer [TOKEN]'),
    (r'1[3-9]\d{9}', '[PHONE]'),
    (r'\d{15,18}', '[ID_NUM]'),
    (r'api_key[=:]\s*[^\s,}]+', 'api_key=[MASKED]'),
    (r'token[=:]\s*[^\s,}]+', 'token=[MASKED]'),
]


def sanitize(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


def safe_log(logger, level: str, msg: str, *args, **kwargs):
    """Log with automatic sanitization."""
    sanitized = sanitize(msg)
    log_func = getattr(logger, level, logger.info)
    log_func(sanitized, *args, **kwargs)
