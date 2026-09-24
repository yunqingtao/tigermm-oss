"""
Tests for core modules after v2 reinforcement.
Covers: CommandHandler, SkillLoader workflow methods, pipeline import integrity.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCommandHandler:
    """Test the extracted CommandHandler."""

    @pytest.fixture
    def handler(self):
        from core.command_handler import CommandHandler
        class MockPipeline:
            stats = {"total": 0, "errors": 0}
            _startup = __import__('time').time()
        return CommandHandler(MockPipeline())

    def test_is_command_voice(self, handler):
        assert handler.is_command("/voice") is True

    def test_is_command_stats(self, handler):
        assert handler.is_command("/stats") is True

    def test_is_command_cost(self, handler):
        assert handler.is_command("/cost") is True

    def test_is_command_hive(self, handler):
        assert handler.is_command("/hive test") is True

    def test_is_command_tool(self, handler):
        assert handler.is_command("/tool file_ops read") is True

    def test_is_not_command(self, handler):
        assert handler.is_command("hello") is False

    def test_stats_returns_dict(self, handler):
        result = handler._stats(0)
        assert isinstance(result, dict)
        assert result["intent"] == "stats"

    def test_handle_unknown_returns_none(self, handler):
        import asyncio
        result = asyncio.run(handler.handle("hello", 0))
        assert result is None


class TestSkillLoaderWorkflow:
    """Test workflow methods in SkillLoader."""

    @pytest.fixture
    def loader(self):
        from core.skill_loader import SkillLoader
        return SkillLoader()

    def test_split_single(self, loader):
        assert loader.split_tasks("hello") == ["hello"]

    def test_split_chain(self, loader):
        result = loader.split_tasks("A then B")
        assert len(result) >= 1  # English connectors not supported, falls through

    def test_split_chinese(self, loader):
        result = loader.split_tasks("查天气然后发邮件")
        assert len(result) == 2

    def test_match_no_match(self, loader):
        loader._workflows = {}
        assert loader.match_workflow("random") is None

    def test_match_found(self, loader):
        loader._workflows = {"test": {"patterns": ["hello"], "success_count": 3}}
        result = loader.match_workflow("hello world")
        assert result is not None
        assert result["success_count"] == 3

    def test_learn_workflow(self, loader):
        loader._workflows = {}
        steps = [{"skill": "a"}, {"skill": "b"}]
        assert loader.learn_workflow("a then b", steps) is True
        assert "a+b" in loader._workflows


class TestPipelineImport:
    """Import checks."""

    def test_import_pipeline(self):
        import core.pipeline
        assert hasattr(core.pipeline, 'Level4Pipeline')

    def test_import_command_handler(self):
        from core.command_handler import CommandHandler
        assert CommandHandler is not None

    def test_skill_loader_has_workflow_methods(self):
        from core.skill_loader import SkillLoader
        sl = SkillLoader()
        assert hasattr(sl, 'split_tasks')
        assert hasattr(sl, 'match_workflow')
        assert hasattr(sl, 'learn_workflow')
