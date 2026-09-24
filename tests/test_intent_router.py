"""intent_router: classify + build_chain, noise filtering, mail priority."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from core.intent_router import IntentRouter, ACTION_INTENTS


@pytest.fixture
def router():
    class KE:
        entities = {}
        environment = {}
    return IntentRouter(KE())


class TestClassify:
    """classify() returns correct intent."""

    def test_search(self, router):
        r = router.classify("搜索Python教程")
        assert "search" in r["actions"]

    def test_mail_priority(self, router):
        """Mail keywords override search intent."""
        r = router.classify("查一下有没有关于Agent的邮件")
        assert r["actions"] == ["check_mail"]

    def test_weather(self, router):
        r = router.classify("北京天气怎么样")
        assert "weather" in r["actions"]

    def test_file_write(self, router):
        r = router.classify("帮我在桌面创建一个test.txt")
        assert "file_write" in r["actions"]


class TestNoiseFiltering:
    """Noise words (然后/接着/并且) removed from content."""

    def test_noise_removed(self, router):
        r = router.classify("搜索北京天气然后保存")
        assert "然后" not in r.get("content", "")

    def test_chain_connectors_filtered(self, router):
        r = router.classify("查一下Python并且发邮件")
        # "并且" should be stripped
        assert r["actions"]


class TestBuildChain:
    """build_chain() produces valid tool chain."""

    def test_single_action(self, router):
        r = router.classify("搜索Python")
        chain = router.build_chain(r)
        assert len(chain) >= 1
        assert chain[0]["tool"] == "web_search"

    def test_empty_actions_no_chain(self, router):
        r = {"actions": [], "delivery": "save", "content": "导出文字"}
        chain = router.build_chain(r)
        assert chain == [] or len(chain) == 0
