"""
Workflow Engine — save and replay task workflows.
Triggers when a session has >= 5 tool calls.
"""
import json, time, os
from pathlib import Path
import logging

logger = logging.getLogger("core.workflow")


class WorkflowEngine:
    """Lightweight workflow manager: save/load/replay task patterns."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.workflows_dir = self.data_dir / "workflows"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self._load_index()

    def _load_index(self):
        index_path = self.workflows_dir / "_index.json"
        if index_path.exists():
            try:
                self.index = json.loads(index_path.read_text(encoding='utf-8'))
            except Exception:
                self.index = {}
        else:
            self.index = {}

    def _save_index(self):
        (self.workflows_dir / "_index.json").write_text(
            json.dumps(self.index, ensure_ascii=False, indent=2), encoding='utf-8')

    def should_prompt(self, tool_call_count: int, threshold: int = 5) -> bool:
        """Check if we should prompt user to save this as a workflow."""
        return tool_call_count >= threshold

    def save_workflow(self, name: str, description: str, turns: list,
                      tool_calls: list, result: str = "") -> str:
        """Save a workflow snapshot. Returns workflow ID."""
        wf_id = f"wf_{int(time.time())}"
        wf = {
            "id": wf_id,
            "name": name,
            "description": description,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "turns": len(turns),
            "tool_calls": tool_calls,
            "summary": turns[-1].get("content", "")[:200] if turns else "",
            "result": result[:500],
        }
        fpath = self.workflows_dir / f"{wf_id}.json"
        fpath.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding='utf-8')

        self.index[wf_id] = {"name": name, "description": description, "created_at": wf["created_at"]}
        self._save_index()
        logger.info("Workflow saved: %s", wf_id)
        return wf_id

    def list_workflows(self) -> list:
        return [
            {"id": k, **v}
            for k, v in sorted(self.index.items(),
                               key=lambda x: x[1].get("created_at", ""),
                               reverse=True)
        ]

    def load_workflow(self, wf_id: str) -> dict | None:
        fpath = self.workflows_dir / f"{wf_id}.json"
        if fpath.exists():
            return json.loads(fpath.read_text(encoding='utf-8'))
        return None


# ═══ Singleton ═══
_we: WorkflowEngine | None = None


def get_workflow_engine(data_dir: Path = None) -> WorkflowEngine:
    global _we
    if _we is None and data_dir is not None:
        _we = WorkflowEngine(data_dir)
    return _we
