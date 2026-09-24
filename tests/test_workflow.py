"""Tests for WorkflowEngine."""
import pytest
from core.workflow import WorkflowEngine


class TestWorkflowEngine:
    @pytest.fixture
    def engine(self):
        return WorkflowEngine.__new__(WorkflowEngine)

    def test_class_exists(self):
        assert WorkflowEngine is not None
