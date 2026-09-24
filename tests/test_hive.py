"""Tests for HiveMind multi-agent collaboration."""
import pytest
from core.hive import HiveMind, AgentSpec, TaskResult, get_hive

class TestHiveMind:
    @pytest.fixture
    def hive(self):
        return HiveMind(pipeline=None)

    def test_register(self, hive):
        hive.register("alpha", model="deepseek", capabilities=["code", "search"])
        assert "alpha" in hive._agents
        agent = hive.get_agent("alpha")
        assert agent.model == "deepseek"
        assert "code" in agent.capabilities

    def test_list_agents(self, hive):
        hive.register("alpha")
        hive.register("beta", model="mimo")
        agents = hive.list_agents()
        assert len(agents) == 2

    def test_unregister(self, hive):
        hive.register("temp")
        hive.unregister("temp")
        assert hive.get_agent("temp") is None

    def test_broadcast_no_agents(self, hive):
        # Without pipeline, broadcast returns error results
        import asyncio
        results = asyncio.run(hive.broadcast("test"))
        assert results == []

    def test_send_and_read_channel(self, hive):
        hive.send_message("general", "alpha", "hello world")
        hive.send_message("general", "beta", "ack")
        msgs = hive.read_channel("general")
        assert len(msgs) == 2
        assert msgs[0]["sender"] == "alpha"
        assert msgs[1]["sender"] == "beta"

    def test_stats(self, hive):
        hive.register("a")
        hive.register("b")
        hive.send_message("ch1", "a", "hi")
        s = hive.stats()
        assert s["agents"] == 2
        assert s["channels"] == 1

    def test_agent_spec_to_dict(self):
        spec = AgentSpec(name="test", model="mimo", capabilities=["web"])
        d = spec.to_dict()
        assert d["name"] == "test"
        assert d["model"] == "mimo"

    def test_task_result(self):
        r = TaskResult(agent="a", model="m", response="ok", elapsed=1.0, success=True)
        d = r.to_dict()
        assert d["success"]
        assert d["elapsed"] == 1.0
