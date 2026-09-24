"""Dedicated tests for retry + CircuitBreaker."""
import pytest, asyncio
from core.retry import retry, async_retry, safe_call, CircuitBreaker


class TestRetrySync:
    def test_succeeds_first_try(self):
        @retry(max_attempts=3, delay=0.01)
        def ok(): return 42
        assert ok() == 42

    def test_retries_and_succeeds(self):
        calls = [0]
        @retry(max_attempts=3, delay=0.01)
        def flaky():
            calls[0] += 1
            if calls[0] < 3: raise ValueError("not yet")
            return "ok"
        assert flaky() == "ok"
        assert calls[0] == 3

    def test_exhausts_retries(self):
        @retry(max_attempts=2, delay=0.01)
        def always_fails(): raise RuntimeError("boom")
        with pytest.raises(RuntimeError):
            always_fails()

    def test_bare_decorator(self):
        @retry
        def fine(): return True
        assert fine()


class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_async_succeeds(self):
        async def ok(): return 42
        r = await async_retry(ok, max_attempts=3, delay=0.01)
        assert r == 42

    @pytest.mark.asyncio
    async def test_async_retries(self):
        calls = [0]
        async def flaky():
            calls[0] += 1
            if calls[0] < 2: raise RuntimeError("flaky")
            return "done"
        r = await async_retry(flaky, max_attempts=3, delay=0.01)
        assert r == "done"


class TestSafeCall:
    def test_returns_result(self):
        r = asyncio.run(safe_call(lambda: "ok"))
        assert r == "ok"

    def test_fallback_on_error(self):
        r = asyncio.run(safe_call(lambda: 1/0, fallback="safe"))
        assert r == "safe"


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker("test")
        assert not cb.is_open

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", threshold=2, cooldown=60)
        cb.record_failure(); cb.record_failure()
        assert cb.is_open

    def test_record_success(self):
        cb = CircuitBreaker("test", threshold=5, cooldown=60)
        cb.record_failure()
        cb.record_success()
        cb.record_success()
        assert not cb.is_open

    @pytest.mark.asyncio
    async def test_call_rejected_when_open(self):
        cb = CircuitBreaker("test", threshold=1, cooldown=60)
        cb.record_failure()
        r = await cb.call(lambda: 42, fallback="rejected")
        assert r == "rejected"
