"""Tests for structured logger."""
import pytest, os, tempfile
from core.logger import log, flush, read_logs, stats, set_session


class TestLogger:
    def test_log_and_flush(self):
        log("test", "event1", key="val")
        log("test", "event2", count=42)
        flush()
        entries = read_logs(module="test")
        assert len(entries) >= 2

    def test_session(self):
        set_session("session-abc")
        log("test", "session_event")
        flush()
        entries = read_logs()
        assert any(e.get("session") == "session-abc" for e in entries)

    def test_stats(self):
        log("alpha", "a1"); log("alpha", "a2"); log("beta", "b1")
        flush()
        s = stats()
        assert s["total"] >= 3

    def test_empty_stats(self):
        s = stats()
        if s["total"] > 0:
            assert "modules" in s

    def test_level(self):
        log("test", "info_event", level="info")
        log("test", "err_event", level="error")
        flush()
        entries = read_logs(module="test")
        levels = [e.get("level") for e in entries[-2:]]
        assert "info" in levels or "error" in levels
