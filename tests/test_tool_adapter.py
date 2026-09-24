"""tool_adapter: broken tools excluded, run() validation."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from mcp.tool_adapter import HermesToolBridge


@pytest.fixture
def bridge():
    """Bridge without PluginManager — tools populated via _sync."""
    return HermesToolBridge()


class TestBrokenToolsExcluded:
    """Bug #4: broken/offline tools not exposed to LLM."""

    def test_libretranslate_excluded(self, bridge):
        defs = bridge.tool_defs_for_llm()
        names = {d["function"]["name"] for d in defs}
        assert "libretranslate" not in names

    def test_nominatim_excluded(self, bridge):
        defs = bridge.tool_defs_for_llm()
        names = {d["function"]["name"] for d in defs}
        assert "nominatim" not in names

    def test_sms_send_excluded(self, bridge):
        defs = bridge.tool_defs_for_llm()
        names = {d["function"]["name"] for d in defs}
        assert "sms_send" not in names

    def test_hermes_bridge_excluded(self, bridge):
        defs = bridge.tool_defs_for_llm()
        names = {d["function"]["name"] for d in defs}
        assert "hermes_bridge" not in names


class TestToolDefsFormat:
    """Exported tool defs must be valid OpenAI function-calling format."""

    def test_all_have_function_key(self, bridge):
        defs = bridge.tool_defs_for_llm()
        for d in defs:
            assert "type" in d
            assert "function" in d
            assert "name" in d["function"]
            assert "parameters" in d["function"]
