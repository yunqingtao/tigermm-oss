
"""
HiveMind — multi-agent cluster dispatcher.
============================================
Main agent decomposes complex tasks, dispatches to
sub-agents (isolated pipelines with tool allowlists),
aggregates results.

Usage:
  /hive 查天气，同时查Agent邮件，再画架构图
"""
import asyncio, json, time, logging, re
from typing import Optional

logger = logging.getLogger("core.hive_mind")

DECOMPOSE_PROMPT = """Break this task into independent sub-tasks that can run in parallel.
Each sub-task gets ONE specific tool group.

Available tool groups:
- weather: openmeteo (weather queries)
- mail: check_mail, send_email (email operations)
- files: file_ops (read/write/list files)
- system: system_info (CPU, disk, uptime)
- diagram: plantuml (architecture diagrams, flowcharts)
- desktop: windows_desktop (screenshots, desktop control)
- search: web_search (internet search)

Rules:
- Weather queries default to Beijing
- Split complex tasks into parallel independent sub-tasks
- Each sub-task uses ONLY ONE tool group
- Sub-tasks must NOT depend on each other
- Output ONLY a JSON array

Example:
User: "查天气，查邮件，截图保存"
[
  {{"agent": "weather", "goal": "查北京天气"}},
  {{"agent": "mail", "goal": "查最近5封邮件"}},
  {{"agent": "desktop", "goal": "截图保存到桌面"}}
]

User request: {message}

JSON array only:"""



class SubAgent:
    """Isolated agent with restricted tools for one sub-task."""

    def __init__(self, name: str, model_client, tool_gateway, plugin_manager, allowlist: set):
        self.name = name
        self.model_client = model_client
        self.gateway = tool_gateway
        self.plugins = plugin_manager
        self.allowlist = allowlist

    async def run(self, goal: str) -> dict:
        """Execute with think→act→verify→fix loop. Max 5 rounds."""
        t0 = time.time()
        try:
            tool_descs = []
            for tname in sorted(self.allowlist):
                if tname in self.plugins._plugins:
                    plugin = self.plugins._plugins[tname]
                    mod = plugin.get("module")
                    desc = mod.__doc__.strip().split("\n")[0] if mod and mod.__doc__ else tname
                    tool_descs.append(f"  {tname}: {desc}")
            
            tools_str = "\n".join(tool_descs)

            system = f"""You are a focused sub-agent with Think→Act→Verify loop.

AVAILABLE TOOLS:
{tools_str}

WORKFLOW:
1. THINK: what do I need to do?
2. ACT: call ONE tool with JSON: {{"tool":"name","arguments":{{"param":"value"}}}}
3. VERIFY: did the tool return what I expected?
4. If wrong → try different params or tool
5. When done → write FINAL: <your answer>

Max 5 tool calls. Be precise. Use Chinese for weather/mail answers."""

            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": goal},
            ]

            final_answer = ""
            for round_num in range(5):
                raw = await self.model_client.generate(
                    "deepseek", messages,
                    temperature=0.1, max_tokens=800,
                )
                text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
                if not text:
                    break

                # Check for FINAL answer
                if text.strip().upper().startswith("FINAL:") or "FINAL:" in text[:50]:
                    final_answer = text.split("FINAL:", 1)[1].strip()
                    break

                # Try to extract tool call
                m = re.search(r'"tool"\s*:\s*"(\w+)"', text)
                if m:
                    tool_name = m.group(1)
                    if tool_name not in self.allowlist:
                        messages.append({"role": "assistant", "content": text})
                        messages.append({"role": "user", "content": f"Tool '{tool_name}' not allowed. Use: {', '.join(sorted(self.allowlist))}"})
                        continue

                    args = {}
                    am = re.search(r'"arguments"\s*:\s*(\{[^}]+\})', text)
                    if am:
                        try:
                            args = json.loads(am.group(1))
                        except json.JSONDecodeError:
                            args = {}

                    try:
                        result = await self.gateway.call(tool_name, **args)
                        result_str = json.dumps(result, ensure_ascii=False, default=str)[:600]
                    except Exception as e:
                        result_str = f"Error: {e}"

                    # Feed result, ask to verify
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": f"Result: {result_str}\n\nVerify: did this give you what you need? If yes, write FINAL: <answer>. If no, try again."})
                else:
                    # No tool call, treat as final
                    final_answer = text.strip()
                    break
            else:
                final_answer = text.strip() if text else "(reached max rounds)"

            elapsed = round(time.time() - t0, 2)
            return {
                "agent": self.name,
                "goal": goal,
                "success": bool(final_answer),
                "result": final_answer[:500] if final_answer else "(empty)",
                "rounds": round_num + 1,
                "elapsed": elapsed,
            }

        except Exception as e:
            return {
                "agent": self.name,
                "goal": goal,
                "success": False,
                "result": "",
                "error": str(e),
                "elapsed": round(time.time() - t0, 2),
            }


# ═══════════════════════════════════════════════
# MessageBus — inter-agent communication
# ═══════════════════════════════════════════════

class MessageBus:
    """Async message queue for agent-to-agent communication."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._history: list[dict] = []

    def register(self, agent_name: str):
        """Register an agent to receive messages."""
        if agent_name not in self._queues:
            self._queues[agent_name] = asyncio.Queue()

    def unregister(self, agent_name: str):
        self._queues.pop(agent_name, None)

    async def send(self, sender: str, recipient: str, msg_type: str, payload: dict) -> bool:
        """Send a message to an agent. Returns False if recipient not registered."""
        if recipient not in self._queues:
            return False
        msg = {
            "sender": sender,
            "recipient": recipient,
            "type": msg_type,
            "payload": payload,
            "time": time.time(),
        }
        self._history.append(msg)
        await self._queues[recipient].put(msg)
        return True

    async def receive(self, agent_name: str, timeout: float = 1.0) -> dict | None:
        """Receive next message for agent. Returns None on timeout."""
        if agent_name not in self._queues:
            return None
        try:
            return await asyncio.wait_for(self._queues[agent_name].get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def broadcast(self, sender: str, msg_type: str, payload: dict) -> list:
        """Send to all registered agents except sender. Returns list of recipients."""
        import asyncio as _aio
        tasks = []
        recipients = []
        for name in self._queues:
            if name != sender:
                recipients.append(name)
                tasks.append(self.send(sender, name, msg_type, payload))
        return recipients  # fire-and-forget; use gather if waiting needed

    def history(self, limit: int = 50) -> list[dict]:
        return self._history[-limit:]


# ═══════════════════════════════════════════════
# DelegationTask — pass work between agents
# ═══════════════════════════════════════════════

DELEGATE_REQUEST = "delegate_request"
DELEGATE_RESULT = "delegate_result"




class HiveMind:
    """Orchestrates sub-agents for complex parallel tasks."""

    def __init__(self, model_client, gateway, plugins):
        self.model_client = model_client
        self.gateway = gateway
        self.plugins = plugins
        self.bus = MessageBus()
        self._agent_registry: dict[str, SubAgent] = {}

    async def decompose(self, message: str) -> dict:
        """Decompose complex task into sub-tasks."""
        prompt = DECOMPOSE_PROMPT.format(message=message)

        try:
            raw = await self.model_client.generate(
                "deepseek",
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2000,
            )
            text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
        except Exception as e:
            return {"error": f"Decomposition failed: {e}", "sub_tasks": []}

        if not text:
            return {"error": "Empty response", "sub_tasks": []}

        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            return {"error": "No JSON array found", "raw": text[:300], "sub_tasks": []}

        try:
            sub_tasks = json.loads(m.group())
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse: {e}", "raw": text[:300], "sub_tasks": []}

        if not isinstance(sub_tasks, list) or len(sub_tasks) == 0:
            return {"error": "Empty task list", "sub_tasks": []}

        return {"error": None, "sub_tasks": sub_tasks}

    async def execute(self, sub_tasks: list) -> dict:
        """Run all sub-tasks in parallel with inter-agent messaging."""
        tool_map = {
            "weather": {"openmeteo"},
            "mail": {"check_mail", "send_email"},
            "files": {"file_ops"},
            "system": {"system_info"},
            "diagram": {"plantuml"},
            "desktop": {"windows_desktop"},
            "search": {"web_search"},
        }

        agents = []
        t0 = time.time()
        for task in sub_tasks:
            agent_name = task.get("agent", "unknown")
            goal = task.get("goal", "")
            allowlist = tool_map.get(agent_name, set())
            
            sub = SubAgent(
                name=agent_name,
                model_client=self.model_client,
                tool_gateway=self.gateway,
                plugin_manager=self.plugins,
                allowlist=allowlist,
            )
            sub.bus = self.bus
            sub.registry = self._agent_registry
            self.bus.register(agent_name)
            self._agent_registry[agent_name] = sub
            agents.append(sub.run(goal))

        results = await asyncio.gather(*agents)
        # Cleanup: unregister agents from bus
        for task in sub_tasks:
            self.bus.unregister(task.get("agent", ""))
        elapsed = round(time.time() - t0, 2)

        ok = sum(1 for r in results if r.get("success"))
        total = len(results)

        return {
            "success": ok == total,
            "total": total,
            "ok": ok,
            "failed": total - ok,
            "results": results,
            "elapsed": elapsed,
        }

    async def delegate(self, from_agent: str, to_agent: str, goal: str,
                      tool_set: set = None) -> dict:
        """Delegate a subtask from one agent to another."""
        logger.info("HiveMind: %s delegates to %s: %s", from_agent, to_agent, goal[:60])
        if to_agent not in self._agent_registry:
            return {"success": False, "error": f"Agent '{to_agent}' not found"}

        # Send delegation request via message bus
        ok = await self.bus.send(from_agent, to_agent, DELEGATE_REQUEST, {
            "goal": goal,
            "from": from_agent,
        })
        if not ok:
            return {"success": False, "error": f"Cannot reach {to_agent}"}

        # Execute directly
        sub = self._agent_registry[to_agent]
        result = await sub.run(goal)

        # Send result back
        await self.bus.send(to_agent, from_agent, DELEGATE_RESULT, {
            "goal": goal,
            "result": result.get("result", ""),
            "success": result.get("success", False),
        })

        return result

    def list_agents(self) -> list[str]:
        """List registered agents."""
        return list(self._agent_registry.keys())

    def format_summary(self, result: dict) -> str:
        """Format hive execution as readable text."""
        lines = [f"\n=== Hive: {result['ok']}/{result['total']} agents ==="]
        for r in result.get("results", []):
            icon = "✅" if r["success"] else "❌"
            res = r.get("result", "")[:100]
            lines.append(f"  {icon} [{r['agent']}] {r['goal']}")
            if res:
                lines.append(f"     {res}")
        if result.get("elapsed"):
            lines.append(f"\n总耗时: {result['elapsed']}s")
        return "\n".join(lines)


# Singleton
_hive_mind: Optional[HiveMind] = None

def get_hive_mind(model_client, gateway, plugins) -> HiveMind:
    global _hive_mind
    if _hive_mind is None:
        _hive_mind = HiveMind(model_client, gateway, plugins)
    return _hive_mind
