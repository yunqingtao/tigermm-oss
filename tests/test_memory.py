"""Tests for TigerMemory."""
import pytest, tempfile, os
from core.memory import TigerMemory


class TestTigerMemory:
    @pytest.fixture
    def mem(self):
        db = os.path.join(tempfile.gettempdir(), "test_mem.json")
        m = TigerMemory(db_path=db)
        yield m
        try:
            os.unlink(db)
        except OSError:
            pass

    def test_add_and_recent(self, mem):
        mem.add_turn("hello", "hi there")
        recent = mem.recent(1)
        assert len(recent) >= 1
        assert recent[0]["text"] == "hi there"

    def test_recent_multiple(self, mem):
        for i in range(5):
            mem.add_turn(f"q{i}", f"a{i}")
        recent = mem.recent(3)
        assert len(recent) == 3

    def test_context_set_get(self, mem):
        mem.set_ctx("key1", "value1")
        assert mem.get_ctx("key1") == "value1"
        assert mem.has_ctx("key1")

    def test_clear_ctx(self, mem):
        mem.set_ctx("temp", "val")
        mem.clear_ctx()
        assert not mem.has_ctx("temp")

    def test_remember_recall(self, mem):
        mem.remember("fact1", "The sky is blue")
        result = mem.recall("fact1")
        assert result is not None
