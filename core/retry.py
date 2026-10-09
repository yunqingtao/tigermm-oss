"""
Tiger.M.M Error Recovery — retry + fallback patterns.
Usage:
    from core.retry import retry, safe_call
    result = await retry(lambda: risky_operation(), max_attempts=3)
"""
import asyncio, time, functools
from typing import Callable, Any, Optional
from core.logger import log as _log


def retry(func=None, *, max_attempts: int = 3, delay: float = 1.0,
          backoff: float = 2.0, module: str = "retry"):
    """Synchronous retry with exponential backoff.
    Can be used as @retry or @retry(max_attempts=3)."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return f(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts:
                        _log(module, "retry", attempt=attempt, error=str(e)[:100])
                        time.sleep(delay * (backoff ** (attempt - 1)))
            _log(module, "retry_exhausted", attempts=max_attempts, error=str(last_error)[:100])
            raise last_error
        return wrapper
    if func is None:
        return decorator
    return decorator(func)


async def async_retry(func: Callable, max_attempts: int = 3, delay: float = 1.0,
                      backoff: float = 2.0, module: str = "retry"):
    """Async retry with exponential backoff."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func()
            return func()
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                _log(module, "async_retry", attempt=attempt, error=str(e)[:100])
                await asyncio.sleep(delay * (backoff ** (attempt - 1)))
    _log(module, "async_retry_exhausted", attempts=max_attempts, error=str(last_error)[:100])
    raise last_error


async def safe_call(func: Callable, fallback: Any = None, module: str = "safe"):
    """Call a function, return fallback on any error. Never raises."""
    try:
        if asyncio.iscoroutinefunction(func):
            return await func()
        return func()
    except Exception as e:
        _log(module, "safe_call_failed", error=str(e)[:100])
        return fallback


class CircuitBreaker:
    """Simple circuit breaker: fail N times -> open for cooldown period."""

    def __init__(self, name: str, threshold: int = 5, cooldown: float = 30.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._last_failure = 0.0
        self._open = False

    @property
    def is_open(self) -> bool:
        if not self._open:
            return False
        if time.time() - self._last_failure > self.cooldown:
            self._open = False
            self._failures = 0
            _log("circuit", "half_open", name=self.name)
            return False
        return True

    def record_failure(self):
        self._failures += 1
        self._last_failure = time.time()
        if self._failures >= self.threshold:
            self._open = True
            _log("circuit", "open", name=self.name, failures=self._failures)

    def record_success(self):
        if not self._open:
            self._failures = 0

    async def call(self, func: Callable, fallback: Any = None):
        """Call through the circuit breaker."""
        if self.is_open:
            _log("circuit", "rejected", name=self.name)
            return fallback
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func()
            else:
                result = func()
            self.record_success()
            return result
        except Exception as e:
            self.record_failure()
            return fallback
