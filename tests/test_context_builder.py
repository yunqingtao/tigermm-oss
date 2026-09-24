"""Tests for ContextBuilder."""
import pytest, os, tempfile
from core.context_builder import ContextBuilder


class TestContextBuilder:
    @pytest.fixture
    def cb(self):
        d = tempfile.mkdtemp()
        cb = ContextBuilder(data_dir=d)
        yield cb
        import shutil; shutil.rmtree(d, ignore_errors=True)

    def test_init(self, cb):
        assert cb is not None

    def test_build_returns_string(self, cb):
        result = cb.build("test query")
        assert isinstance(result, str)

    def test_build_behavioral_rules(self, cb):
        rules = cb.build_behavioral_rules()
        assert isinstance(rules, str)

    def test_build_full_system_prompt(self, cb):
        prompt = cb.build_full_system_prompt("base prompt", "user message")
        assert isinstance(prompt, str)
        assert "base prompt" in prompt
