"""Tests for SessionManager."""
import pytest
from core.session_manager import SessionManager, STATUS_RUNNING, STATUS_KILLED


class TestSessionManager:
    def test_spawn(self, session_mgr):
        sid = session_mgr.spawn("test")
        assert sid and session_mgr.get(sid).status == STATUS_RUNNING

    def test_list(self, session_mgr):
        session_mgr.spawn("a"); session_mgr.spawn("b")
        assert len(session_mgr.list_sessions()) == 2

    def test_kill(self, session_mgr):
        sid = session_mgr.spawn("kill me")
        assert session_mgr.kill(sid)
        assert session_mgr.get(sid).status == STATUS_KILLED

    def test_stats(self, session_mgr):
        session_mgr.spawn("a")
        s = session_mgr.stats()
        assert s["running"] == 1 and s["total"] == 1
