"""
HiveMind v2 - Multi-agent collaboration core
Distilled from orca (parallel worktrees) + herdr (socket API)

Architecture:
  HiveMind
  ├── agents: {name: AgentSpec}
  ├── broadcast(task) -> parallel fan-out
  ├── delegate(task, agent) -> single dispatch
  └── channels: inter-agent message passing

Minimum viable: broadcast + delegate + result aggregation.
"""
import asyncio, time, uuid
from core.logger import log as _log
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class AgentSpec:
    """Agent registration in the hive."""
    name: str
    model: str = "auto"
    capabilities: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "model": self.model,
            "capabilities": self.capabilities,
            "description": self.description,
        }


@dataclass
class TaskResult:
    agent: str
    model: str
    response: str
    elapsed: float
    success: bool
    error: str = ""

    def to_dict(self):
        return {
            "agent": self.agent,
            "model": self.model,
            "response": self.response[:500],
            "elapsed": round(self.elapsed, 2),
            "success": self.success,
            "error": self.error[:200],
        }


class HiveMind:
    """Multi-agent coordinator. Registers agents, dispatches tasks, collects results."""

    def __init__(self, pipeline=None):
        self.pipeline = pipeline
        self._agents: dict[str, AgentSpec] = {}
        self._channels: dict[str, list[dict]] = {}

    def register(self, name: str, model: str = "auto",
                 capabilities: list[str] = None, description: str = "",
                 max_agents: int = 1000):
        """Register an agent. Auto-cleanup if over max_agents."""
        if len(self._agents) >= max_agents:
            oldest = min(self._agents.keys(), key=lambda k: self._agents[k].to_dict().get('name', ''))
            _log("hive", "auto_cleanup", removed=oldest, reason="max_agents")
            del self._agents[oldest]
        _log("hive", "register", agent=name, model=model)
        self._agents[name] = AgentSpec(
            name=name, model=model,
            capabilities=capabilities or [],
            description=description,
        )

    def unregister(self, name: str):
        self._agents.pop(name, None)

    def cleanup(self, max_age_seconds: float = 3600):
        """Remove agents that haven't sent messages in max_age_seconds.
        This is a best-effort cleanup based on channel activity."""
        # Simple: clear all empty channels
        empty = [ch for ch, msgs in self._channels.items() if not msgs]
        for ch in empty:
            del self._channels[ch]

    def list_agents(self) -> list[dict]:
        return [a.to_dict() for a in self._agents.values()]

    def get_agent(self, name: str) -> Optional[AgentSpec]:
        return self._agents.get(name)

    async def broadcast(self, task: str, agents: list[str] = None) -> list[TaskResult]:
        """Fan-out task to multiple agents in parallel, collect results."""
        targets = agents or list(self._agents.keys())
        if not targets:
            return []

        _log("hive", "broadcast", task=task[:60], agents=len(targets))
        async def run_one(name: str) -> TaskResult:
            agent = self._agents.get(name)
            if agent is None or self.pipeline is None:
                return TaskResult(agent=name, model="?", response="",
                                  elapsed=0, success=False, error="Agent not found")
            t0 = time.time()
            try:
                result = await self.pipeline.process(task, agent.model)
                elapsed = time.time() - t0
                resp = result.get("response", "") if isinstance(result, dict) else str(result)
                return TaskResult(agent=name, model=agent.model,
                                  response=resp, elapsed=elapsed, success=True)
            except Exception as e:
                return TaskResult(agent=name, model=agent.model,
                                  response="", elapsed=time.time() - t0,
                                  success=False, error=str(e)[:500])

        tasks = [run_one(name) for name in targets]
        return await asyncio.gather(*tasks)

    async def delegate(self, task: str, agent_name: str) -> TaskResult:
        """Send task to a single specific agent."""
        results = await self.broadcast(task, agents=[agent_name])
        return results[0] if results else TaskResult(
            agent=agent_name, model="?", response="",
            elapsed=0, success=False, error="No result")

    async def sequential(self, tasks: list[tuple[str, str]]) -> list[TaskResult]:
        """Run tasks in sequence: [(agent_name, task), ...].
        Each agent receives output of previous as context.
        """
        results = []
        context = ""
        for agent_name, task in tasks:
            full_task = task
            if context:
                full_task = f"Previous result:\n{context[:1000]}\n\nNext task: {task}"
            r = await self.delegate(full_task, agent_name)
            results.append(r)
            if r.success:
                context = r.response
        return results

    def send_message(self, channel: str, sender: str, content: str):
        """Inter-agent message passing via named channels."""
        if channel not in self._channels:
            self._channels[channel] = []
        self._channels[channel].append({
            "sender": sender,
            "content": content,
            "time": time.time(),
        })

    def read_channel(self, channel: str, limit: int = 20) -> list[dict]:
        """Read messages from a channel (latest first)."""
        msgs = self._channels.get(channel, [])
        return msgs[-limit:]

    def stats(self) -> dict:
        return {
            "agents": len(self._agents),
            "channels": len(self._channels),
            "total_messages": sum(len(v) for v in self._channels.values()),
        }


# Global singleton
_hive: Optional[HiveMind] = None


def get_hive(pipeline=None) -> HiveMind:
    global _hive
    if _hive is None:
        _hive = HiveMind(pipeline)
    elif pipeline is not None and _hive.pipeline is None:
        _hive.pipeline = pipeline
    return _hive
