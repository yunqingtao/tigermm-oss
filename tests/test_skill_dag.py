
"""Tests for SkillWorkflowEngine: DAG, validation, caching, boundary guards."""
import pytest
import sys, os, tempfile, json, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSkillDAG:
    """DAG structure and validation."""

    @pytest.fixture
    def engine(self):
        from core.skill_loader import SkillWorkflowEngine, SkillDAG
        return SkillWorkflowEngine()

    def test_dag_valid_basic(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(
            name="test", steps=[
                {"id": "s1", "tool": "file_ops", "action": "read"}
            ]
        )
        engine._validate_dag(dag)
        assert len(dag.parse_errors) == 0
        assert dag.is_valid()

    def test_dag_missing_steps(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(name="test", steps=[])
        engine._validate_dag(dag)
        assert "No steps defined" in dag.parse_errors
        assert not dag.is_valid()

    def test_dag_unknown_tool(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(name="test", steps=[
            {"id": "s1", "tool": "nonexistent_tool_xyz", "action": "read"}
        ])
        engine._validate_dag(dag)
        assert any("unknown tool" in e for e in dag.parse_errors)

    def test_dag_duplicate_step_ids(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(name="test", steps=[
            {"id": "s1", "tool": "file_ops", "action": "read"},
            {"id": "s1", "tool": "send_email", "action": "send"},
        ])
        engine._validate_dag(dag)
        assert any("duplicate id" in e for e in dag.parse_errors)

    def test_dag_invalid_param_ref(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(
            name="test",
            params_schema={"file": {"type": "path", "required": True}},
            steps=[{"id": "s1", "tool": "file_ops", "action": "read",
                    "input": {"path": "$params.nonexistent"}}]
        )
        engine._validate_dag(dag)
        assert any("not in params schema" in e for e in dag.parse_errors)

    def test_dag_valid_param_ref(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(
            name="test",
            params_schema={"file": {"type": "path", "required": True}},
            steps=[{"id": "s1", "tool": "file_ops", "action": "read",
                    "input": {"path": "$params.file"}}]
        )
        engine._validate_dag(dag)
        assert len(dag.parse_errors) == 0

    def test_dag_unknown_param_type(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(
            name="test",
            params_schema={"x": {"type": "weird_type_xyz"}},
            steps=[{"id": "s1", "tool": "file_ops", "action": "read"}]
        )
        engine._validate_dag(dag)
        assert any("unknown type" in e for e in dag.parse_errors)

    def test_dag_depends_on_unknown(self, engine):
        from core.skill_loader import SkillDAG
        dag = SkillDAG(name="test", steps=[
            {"id": "s1", "tool": "file_ops", "action": "read",
             "depends_on": ["nonexistent"]}
        ])
        engine._validate_dag(dag)
        assert any("depends_on" in e for e in dag.parse_errors)


class TestSkillParser:
    """Parser: scan, parse, validate SKILL.md/skill.json."""

    @pytest.fixture
    def tmp_skills_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def engine(self, tmp_skills_dir):
        from core.skill_loader import SkillWorkflowEngine
        return SkillWorkflowEngine(tmp_skills_dir)

    def test_scan_empty_dir(self, engine):
        names = engine.scan()
        assert names == []

    def test_parse_valid_skill_json(self, engine, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "skill.json").write_text(json.dumps({
            "name": "test-skill",
            "version": "1.0",
            "description": "A test skill",
            "requires": ["file_ops"],
            "permission": "read",
            "timeout": 30,
            "trigger": {"patterns": ["test"], "match": "any"},
            "params": {"file": {"type": "path", "required": True}},
            "steps": [{"id": "read_file", "tool": "file_ops", "action": "read",
                       "input": {"path": "$params.file"}}]
        }), encoding="utf-8")

        names = engine.scan()
        assert "test-skill" in names

        dag = engine.get_dag("test-skill")
        assert dag is not None
        assert dag.is_valid()
        assert dag.name == "test-skill"
        assert len(dag.steps) == 1

    def test_parse_bad_json(self, engine, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "skill.json").write_text("{not valid json", encoding="utf-8")
        names = engine.scan()
        assert "bad-skill" not in names

    def test_validate_skill_returns_errors(self, engine, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "bad-step-skill"
        skill_dir.mkdir()
        (skill_dir / "skill.json").write_text(json.dumps({
            "name": "bad-step-skill",
            "steps": [{"id": "s1", "tool": "no_such_tool", "action": "x"}]
        }), encoding="utf-8")
        engine.scan()
        result = engine.validate_skill("bad-step-skill")
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_dag_cache(self, engine, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "cached-skill"
        skill_dir.mkdir()
        (skill_dir / "skill.json").write_text(json.dumps({
            "name": "cached-skill",
            "steps": [{"id": "s1", "tool": "file_ops", "action": "read"}]
        }), encoding="utf-8")
        engine.scan()

        dag1 = engine.get_dag("cached-skill")
        dag2 = engine.get_dag("cached-skill")
        assert dag1 is dag2  # Same object from cache

    def test_dag_cache_expiry(self, engine, tmp_skills_dir):
        from core.skill_loader import CACHE_TTL
        skill_dir = tmp_skills_dir / "expiry-skill"
        skill_dir.mkdir()
        (skill_dir / "skill.json").write_text(json.dumps({
            "name": "expiry-skill",
            "steps": [{"id": "s1", "tool": "file_ops", "action": "read"}]
        }), encoding="utf-8")
        engine.scan()

        # Force cache expiry
        engine._cache_time["expiry-skill"] = time.time() - CACHE_TTL - 10
        dag = engine.get_dag("expiry-skill")
        assert dag is not None
        # Should have re-parsed (cache was expired)


class TestBoundaryGuard:
    """Parser boundary enforcement."""

    @pytest.fixture
    def engine(self):
        from core.skill_loader import SkillWorkflowEngine
        return SkillWorkflowEngine()

    def test_parser_context_blocks_runner(self, engine):
        engine._enter_parser()
        try:
            with pytest.raises(RuntimeError, match="BOUNDARY VIOLATION"):
                engine.split_tasks("test")
        finally:
            engine._exit_parser()

    def test_parser_context_blocks_execute(self, engine):
        engine._enter_parser()
        try:
            with pytest.raises(RuntimeError, match="BOUNDARY VIOLATION"):
                import asyncio
                asyncio.run(engine.execute_workflow([], None))
        finally:
            engine._exit_parser()

    def test_runner_works_outside_parser(self, engine):
        result = engine.split_tasks("a then b")
        assert result is not None

    def test_gateway_not_required_for_split(self, engine):
        """split_tasks should work even without gateway."""
        result = engine.split_tasks("hello")
        assert result == ["hello"]


class TestBackwardCompat:
    """Backward compatibility aliases."""

    def test_skill_loader_alias(self):
        from core.skill_loader import SkillLoader, SkillWorkflowEngine
        assert SkillLoader is SkillWorkflowEngine

    def test_get_skill_index_alias(self):
        from core.skill_loader import get_skill_index, get_skill_engine
        eng1 = get_skill_index()
        eng2 = get_skill_engine()
        assert eng1 is eng2  # Same singleton
