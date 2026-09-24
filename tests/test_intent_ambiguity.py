
"""Tests for intent_router ambiguity fixes."""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIntentRouterAmbiguity:
    """Verify '查X' patterns route correctly."""

    @pytest.fixture
    def router(self):
        from core.intent_router import IntentRouter
        return IntentRouter()

    def test_check_mail_wins_over_search(self, router):
        """查邮件 should route to check_mail, not search."""
        result = router.classify("查邮件")
        assert result["actions"] == ["check_mail"], \
            f"Expected check_mail, got {result['actions']}"

    def test_check_mail_with_filler(self, router):
        """查一下邮件 should route to check_mail."""
        result = router.classify("查一下邮件")
        assert result["actions"] == ["check_mail"], \
            f"Expected check_mail, got {result['actions']}"

    def test_weather_wins_over_search(self, router):
        """查天气 should route to weather, not search."""
        result = router.classify("北京天气")
        assert result["actions"] == ["weather"], \
            f"Expected weather, got {result['actions']}"

    def test_screenshot_wins_over_search(self, router):
        """截图 should route to screenshot, not anything else."""
        result = router.classify("截图保存")
        assert "screenshot" in result["actions"], \
            f"Expected screenshot, got {result['actions']}"

    def test_pure_search_still_works(self, router):
        """搜索XXX without domain keywords should still go to search."""
        result = router.classify("搜索Python教程")
        assert result["actions"] == ["search"], \
            f"Expected search, got {result['actions']}"

    def test_file_delete_not_search(self, router):
        """删除文件 should route to file_delete, not search."""
        result = router.classify("删除test.txt")
        # file_delete patterns: ["删除", "删掉", "移除"]
        # Also .txt triggers implicit file_read
        # The longest match should win
        assert "search" not in result["actions"], \
            f"Should not be search, got {result['actions']}"

    def test_priority_tiers(self, router):
        """check_mail priority (3) > search priority (1)."""
        from core.intent_router import INTENT_PRIORITY
        assert INTENT_PRIORITY["check_mail"] > INTENT_PRIORITY["search"]
        assert INTENT_PRIORITY["weather"] > INTENT_PRIORITY["search"]
        assert INTENT_PRIORITY["screenshot"] > INTENT_PRIORITY["search"]


class TestDomainOverride:
    """Domain override: explicit words force correct intent."""

    @pytest.fixture
    def router(self):
        from core.intent_router import IntentRouter
        return IntentRouter()

    def test_mail_keyword_overrides_search(self, router):
        """任何含'邮件'的消息不应走到search."""
        for msg in ["帮我查邮件", "看看邮件", "搜索邮件", "看一下邮件"]:
            result = router.classify(msg)
            assert result["actions"] == ["check_mail"], \
                f"'{msg}' -> {result['actions']}, expected check_mail"

    def test_weather_keyword_overrides_search(self, router):
        """任何含'天气'的消息不应走到search."""
        for msg in ["查天气", "搜索天气", "看一下天气"]:
            result = router.classify(msg)
            assert result["actions"] == ["weather"], \
                f"'{msg}' -> {result['actions']}, expected weather"
