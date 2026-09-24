"""Tests for IntentReasoner."""
import pytest
from core.intent_reasoner import IntentReasoner


class TestIntentReasoner:
    @pytest.fixture
    def reasoner(self):
        return IntentReasoner(entities={"test": {}}, environment={})

    def test_init(self, reasoner):
        assert reasoner is not None
        assert reasoner.entities is not None

    def test_reason_returns_result(self, reasoner):
        result = reasoner.reason("hello world")
        assert isinstance(result, dict)
