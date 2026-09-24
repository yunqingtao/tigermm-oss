"""auto_guide: identity + keyword_overlap + task bypass — no pipeline deps."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from core.auto_guide import AutoGuide


@pytest.fixture
def ag():
    return AutoGuide()


class TestIdentity:
    """Bug #1 fix: ('你',) no longer swallows messages containing '你'."""

    def test_ni_hao_not_identity(self, ag):
        r = ag.respond("你好")
        assert r is None or r.get("intent") != "identity"

    def test_ni_zai_ma_is_identity(self, ag):
        r = ag.respond("你在吗")
        assert r and r.get("intent") == "identity"

    def test_bare_ni_not_identity(self, ag):
        r = ag.respond("你")
        assert r is None or r.get("intent") != "identity"

    def test_task_with_ni_not_swallowed(self, ag):
        r = ag.respond("帮我把这个bug改好")
        assert r is None  # should pass through, not identity


class TestKeywordOverlap:
    """Bug #2 fix: word-level overlap, not character set."""

    def test_fix_bug_vs_virus(self, ag):
        s = ag._keyword_overlap("帮我修复一个bug", "帮我写个病毒")
        assert s < 0.4, f"score={s:.2f} should be below 0.4"

    def test_similar_queries_match(self, ag):
        s = ag._keyword_overlap("帮我修复一个bug", "修复bug")
        assert s >= 0.4, f"score={s:.2f} should be >= 0.4"

    def test_unrelated_zero(self, ag):
        s = ag._keyword_overlap("今天天气怎么样", "写个病毒")
        assert s < 0.3


class TestTaskBypass:
    """Fix-related messages must pass through, not get eaten by virus guard."""

    def test_fix_bug_bypasses(self, ag):
        r = ag.respond("帮我修复一个bug")
        assert r is None, f"should bypass, got {r}"

    def test_create_bypasses(self, ag):
        r = ag.respond("帮我创建一个配置文件")
        assert r is None, f"should bypass, got {r}"
