"""
TaskManager — Multi-agent task decomposition and delegation.
Splits complex tasks into subtasks, dispatches to parallel agents, aggregates results.
"""
import asyncio, json, time, logging, uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("core.task_manager")


@dataclass
class SubTask:
    id: str
    goal: str
    agent: str = "deepseek"      # which model to use
    status: str = "pending"      # pending / running / done / failed / timeout
    result: str = ""
    error: str = ""
    elapsed: float = 0.0
    parent_id: str = ""
    depends_on: list = field(default_factory=list)  # subtask IDs this depends on


@dataclass
class Task:
    id: str
    goal: str
    subtasks: list = field(default_factory=list)
    status: str = "pending"
    created_at: float = 0.0
    finished_at: float = 0.0
    aggregated_result: str = ""


class TaskManager:
    MAX_SUBTASKS = 5
    SUBTASK_TIMEOUT = 60  # seconds per subtask

    def __init__(self, pipeline=None):
        self.pipeline = pipeline
        self._tasks: dict[str, Task] = {}

    async def decompose(self, goal: str) -> Task:
        """Ask the model to break a complex task into subtasks."""
        task = Task(
            id=str(uuid.uuid4())[:8],
            goal=goal,
            created_at=time.time(),
        )

        if not self.pipeline:
            task.subtasks = [SubTask(id="s1", goal=goal)]
            task.status = "decomposed"
            self._tasks[task.id] = task
            return task

        # Use DeepSeek to decompose
        prompt = (
            "Break this task into independent subtasks. "
            "Each subtask should be self-contained. "
            f"Maximum {self.MAX_SUBTASKS} subtasks. "
            "Return JSON array: [{\"goal\": \"...\"}, ...]\n\n"
            f"Task: {goal}"
        )
        result = await self.pipeline._process_external(prompt, "deepseek", time.time())

        try:
            import re
            text = result.get("response", "")
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                items = json.loads(m.group())
                for i, item in enumerate(items[:self.MAX_SUBTASKS]):
                    st = SubTask(
                        id=f"{task.id}-{i+1}",
                        goal=item.get("goal", item.get("task", str(item))),
                        parent_id=task.id,
                    )
                    task.subtasks.append(st)
        except Exception as e:
            logger.warning("TaskManager decompose: %s", e)

        if not task.subtasks:
            task.subtasks = [SubTask(id=f"{task.id}-1", goal=goal)]

        task.status = "decomposed"
        self._tasks[task.id] = task
        logger.info("TaskManager: '%s' -> %d subtasks", task.id, len(task.subtasks))
        return task

    async def dispatch(self, task: Task) -> Task:
        """Execute all subtasks, respecting dependencies."""
        task.status = "running"

        # Build execution order respecting dependencies
        done: set = set()
        remaining = list(task.subtasks)

        while remaining:
            # Find subtasks whose dependencies are all done
            ready = [
                st for st in remaining
                if all(d in done for d in st.depends_on)
            ]
            if not ready:
                # Deadlock or all blocked
                for st in remaining:
                    st.status = "failed"
                    st.error = "Dependency deadlock"
                break

            # Execute ready subtasks in parallel
            async def _run_subtask(st: SubTask):
                st.status = "running"
                t0 = time.time()
                try:
                    if self.pipeline:
                        result = await asyncio.wait_for(
                            self.pipeline._process_external(st.goal, st.agent, t0),
                            timeout=self.SUBTASK_TIMEOUT
                        )
                        st.result = result.get("response", "")[:3000]
                        st.status = "done"
                    else:
                        st.status = "failed"
                        st.error = "No pipeline"
                except asyncio.TimeoutError:
                    st.status = "timeout"
                    st.error = f"Timeout ({self.SUBTASK_TIMEOUT}s)"
                except Exception as e:
                    st.status = "failed"
                    st.error = str(e)[:200]
                st.elapsed = round(time.time() - t0, 2)

            await asyncio.gather(*[_run_subtask(st) for st in ready])

            for st in ready:
                done.add(st.id)
                remaining.remove(st)

        task.finished_at = time.time()
        task.status = "done"
        return task

    async def aggregate(self, task: Task) -> str:
        """Merge subtask results into a final answer."""
        if len(task.subtasks) == 1:
            return task.subtasks[0].result or "(no result)"

        # Build context for aggregation
        ctx_parts = []
        for st in task.subtasks:
            icon = "OK" if st.status == "done" else st.status.upper()
            ctx_parts.append(f"[{icon}] {st.goal}\n{st.result or st.error}")

        if self.pipeline:
            prompt = (
                "Merge these subtask results into a coherent final answer.\n\n"
                + "\n\n---\n\n".join(ctx_parts)
                + "\n\nMerged answer:"
            )
            result = await self.pipeline._process_external(prompt, "deepseek", time.time())
            task.aggregated_result = result.get("response", "")[:5000]
        else:
            task.aggregated_result = "\n\n".join(ctx_parts)

        return task.aggregated_result

    async def run(self, goal: str) -> dict:
        """Full pipeline: decompose → dispatch → aggregate."""
        task = await self.decompose(goal)
        task = await self.dispatch(task)
        result = await self.aggregate(task)
        return {
            "task_id": task.id,
            "status": task.status,
            "subtask_count": len(task.subtasks),
            "completed": sum(1 for st in task.subtasks if st.status == "done"),
            "failed": sum(1 for st in task.subtasks if st.status == "failed"),
            "result": result,
        }

    def stats(self) -> dict:
        return {
            "total_tasks": len(self._tasks),
            "running": sum(1 for t in self._tasks.values() if t.status == "running"),
            "done": sum(1 for t in self._tasks.values() if t.status == "done"),
            "failed": sum(1 for t in self._tasks.values() if t.status == "failed"),
        }


_manager: Optional[TaskManager] = None

def get_task_manager(pipeline=None) -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager(pipeline=pipeline)
    elif pipeline and not _manager.pipeline:
        _manager.pipeline = pipeline
    return _manager
