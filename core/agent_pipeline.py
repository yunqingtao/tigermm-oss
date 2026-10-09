"""
YAML-driven multi-agent pipeline — define agents + tasks + dependencies in YAML.

Usage:
    from core.agent_pipeline import AgentPipeline
    results = await AgentPipeline("flow.yaml").run()

YAML format:
    agents:
      coder: {model: deepseek, role: "Python expert"}
      reviewer: {model: mimo, role: "Code reviewer"}
    
    tasks:
      - id: write_code
        agent: coder
        prompt: "Write a function that..."
        output: code_file
    
      - id: review
        agent: reviewer
        prompt: "Review this code: ${write_code}"
        depends: [write_code]
"""
import asyncio, json, os, re, time, logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("core.agent_pipeline")

# Lightweight YAML parser (no PyYAML dependency, handles our subset)
def _parse_yaml(text: str) -> dict:
    """Parse TMM's agent-pipeline YAML subset: agents map + tasks list."""
    import json
    
    result = {"agents": {}, "tasks": []}
    current_section = None
    current_task = None
    current_agent = None
    
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        
        if line == "agents:":
            current_section = "agents"
            continue
        if line == "tasks:":
            current_section = "tasks"
            continue
        
        # Agent entry: "  name:" starts a new agent
        if current_section == "agents" and line.endswith(":") and not line.startswith("-"):
            current_agent = line[:-1].strip()
            result["agents"][current_agent] = {"model": "deepseek", "role": current_agent}
            continue
        
        # Agent key: value
        if current_section == "agents" and current_agent and ":" in line and not line.endswith(":"):
            k, v = line.split(":", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            result["agents"][current_agent][k] = v
            continue
        
        # Task list item: "- id: x" starts new task
        if current_section == "tasks" and line.startswith("-"):
            current_task = {}
            result["tasks"].append(current_task)
            if ":" in line[1:]:
                inner = line[1:].strip()
                k, v = inner.split(":", 1)
                current_task[k.strip()] = v.strip().strip('"').strip("'")
            continue
        
        # Task key: value
        if current_section == "tasks" and current_task is not None and ":" in line:
            k, v = line.split(":", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k == "depends":
                # Parse [a, b] or a
                v_stripped = v.strip("[] ")
                current_task[k] = [x.strip().strip('"').strip("'") for x in v_stripped.split(",")] if v_stripped else []
            else:
                current_task[k] = v
            continue
    
    return result


@dataclass
class TaskResult:
    task_id: str
    agent: str
    success: bool = False
    output: str = ""
    elapsed: float = 0
    error: str = ""


class AgentPipeline:
    """Multi-agent pipeline driven by YAML config."""
    
    def __init__(self, config_path_or_text: str):
        """
        Args:
            config_path_or_text: path to .yaml file, or inline YAML string
        """
        if os.path.isfile(config_path_or_text):
            text = Path(config_path_or_text).read_text(encoding="utf-8")
        else:
            text = config_path_or_text
        
        self.config = _parse_yaml(text) if isinstance(_parse_yaml(text), dict) else {}
        self.agents = self.config.get("agents", {})
        self.tasks = self.config.get("tasks", [])
        self.results: dict[str, TaskResult] = {}
        self._model_client = None
    
    def set_model_client(self, client):
        """Inject the pipeline's model client for API calls."""
        self._model_client = client
    
    def _resolve_vars(self, text: str) -> str:
        """Replace ${task_id} with that task's output."""
        def replacer(match):
            task_id = match.group(1)
            if task_id in self.results:
                r = self.results[task_id]
                return r.output[:2000] if r.success else f"[FAILED: {r.error[:200]}]"
            return match.group(0)
        return re.sub(r'\$\{(\w+)\}', replacer, text)
    
    def _topo_sort(self) -> list[list[dict]]:
        """Topological sort tasks into parallel-ready groups. Returns [[task, ...], ...]"""
        task_map = {t["id"]: t for t in self.tasks}
        done = set()
        groups = []
        
        while len(done) < len(self.tasks):
            ready = []
            for task in self.tasks:
                if task["id"] in done:
                    continue
                deps = task.get("depends", [])
                if isinstance(deps, str):
                    deps = [deps]
                if all(d in done for d in deps):
                    ready.append(task)
            
            if not ready:
                remaining = [t["id"] for t in self.tasks if t["id"] not in done]
                logger.error("AgentPipeline: cycle or missing dependency among: %s", remaining)
                break
            
            groups.append(ready)
            for t in ready:
                done.add(t["id"])
        
        return groups
    
    async def _run_one_task(self, task: dict) -> TaskResult:
        """Execute a single agent task via the model client."""
        t0 = time.time()
        result = TaskResult(task_id=task["id"], agent=task.get("agent", "general"))
        
        try:
            prompt = self._resolve_vars(task.get("prompt", task.get("task", "")))
            agent_name = task.get("agent", "general")
            agent_config = self.agents.get(agent_name, {})
            model = agent_config.get("model", "deepseek")
            
            if self._model_client:
                # Use pipeline's model client
                try:
                    response = await self._model_client.chat(
                        messages=[{"role": "user", "content": prompt}],
                        model_override=model,
                        temperature=0.3,
                    )
                    result.output = response[:5000]
                    result.success = True
                except Exception as e:
                    result.error = str(e)
                    result.success = False
            else:
                result.output = f"[dry-run] Agent {agent_name} ({model}) would process: {prompt[:200]}"
                result.success = True
            
        except Exception as e:
            result.error = str(e)
            result.success = False
        
        result.elapsed = round(time.time() - t0, 2)
        self.results[task["id"]] = result
        logger.info("AgentPipeline: %s %s → %s (%.2fs)",
                    result.task_id, "OK" if result.success else "FAIL",
                    result.output[:60] if result.success else result.error[:60],
                    result.elapsed)
        return result
    
    async def run(self) -> dict:
        """Execute all tasks respecting dependencies. Returns summary."""
        if not self.tasks:
            return {"ok": False, "output": "No tasks defined", "results": {}}
        
        groups = self._topo_sort()
        all_results = []
        
        for group in groups:
            tasks = [self._run_one_task(t) for t in group]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    results[i] = TaskResult(
                        task_id=group[i]["id"],
                        agent=group[i].get("agent", ""),
                        success=False, error=str(r)
                    )
            all_results.extend(results)
        
        success_count = sum(1 for r in all_results if r.success)
        total = len(all_results)
        
        summary_lines = [f"Pipeline: {success_count}/{total} tasks passed"]
        for r in all_results:
            status = "✓" if r.success else "✗"
            preview = (r.output[:80] if r.success else r.error[:80])
            summary_lines.append(f"  {status} {r.task_id} ({r.agent}, {r.elapsed}s): {preview}")
        
        return {
            "ok": success_count == total,
            "output": "\n".join(summary_lines),
            "success": success_count,
            "total": total,
            "results": {r.task_id: r.__dict__ for r in all_results},
        }


# ── Quick config builder ──
def quick_config(tasks: list[dict], default_model: str = "deepseek") -> str:
    """Build a YAML config string from a simple task list.
    
    Example:
        tasks = [
            {"id": "step1", "agent": "coder", "prompt": "Write a hello world"},
            {"id": "step2", "agent": "reviewer", "prompt": "Review: ${step1}", "depends": ["step1"]},
        ]
    """
    agents = {}
    for t in tasks:
        agent = t.get("agent", "general")
        if agent not in agents:
            agents[agent] = {"model": default_model, "role": f"{agent} agent"}
    
    lines = ["agents:"]
    for name, cfg in agents.items():
        lines.append(f"  {name}:")
        for k, v in cfg.items():
            lines.append(f"    {k}: {v}")
    
    lines.append("\ntasks:")
    for t in tasks:
        lines.append(f"  - id: {t['id']}")
        lines.append(f"    agent: {t.get('agent', 'general')}")
        lines.append(f"    prompt: \"{t.get('prompt', '')}\"")
        if t.get("depends"):
            deps = t["depends"] if isinstance(t["depends"], list) else [t["depends"]]
            lines.append(f"    depends: {json.dumps(deps)}")
    
    return "\n".join(lines)
