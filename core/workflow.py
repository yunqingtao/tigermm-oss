"""
[DEPRECATED] WorkflowEngine absorbed into SkillLoader (core/skill_loader.py).
This module kept for backward compatibility only — all new code should use
SkillLoader.split_tasks(), match_workflow(), learn_workflow(), execute_workflow().
"""
import json, re, time, logging
from pathlib import Path

logger = logging.getLogger("mary3.workflow")

_CONNECTORS = [
    (r'(.+?)(?:然后|接着|再|之后|再然后|完了)(.+)', 2),
    (r'先(.+?)(?:再|然后|接着)(.+)', 2),
    (r'(.+?)(?:并且|同时|顺便|外加)(.+)', 2),
]


class WorkflowEngine:
    """[DEPRECATED] Use SkillLoader instead."""

    def __init__(self, data_dir: Path, knowledge_engine):
        import warnings
        warnings.warn("WorkflowEngine is deprecated, use SkillLoader", DeprecationWarning, stacklevel=2)
        self.data_dir = data_dir
        self.ke = knowledge_engine
        self.workflows = {}

    def _load(self): pass
    def _save(self): pass

    def split(self, text):
        for pat, n in _CONNECTORS:
            m = re.search(pat, text)
            if m:
                tasks = [g.strip() for g in m.groups() if g.strip()]
                if len(tasks) >= 2:
                    return tasks
        return [text]

    def parse(self, message):
        tasks_raw = self.split(message)
        return [{"text": t, "parsed": self.ke.parse(t)} for t in tasks_raw]

    async def execute(self, workflow, plugin_mgr):
        ctx = {"_steps": [], "_success": True}
        for task in workflow:
            parsed = task["parsed"]
            sk, sd, ss = parsed["skill"]
            if not sk or ss < 0.02:
                task["result"] = {"success": False, "error": f"No skill for: {task['text']}"}
                ctx["_success"] = False
                break
            result = await self.ke.execute(parsed, plugin_mgr)
            task["result"] = result
            ctx["_steps"].append({"skill": sk, "tool": sd.get("tool"), "result": result})
            if isinstance(result, dict):
                ctx["prev"] = result
            if not result.get("success", False):
                ctx["_success"] = False
                break
        return ctx

    def learn(self, message, steps):
        if not steps or len(steps) < 2:
            return False
        first = steps[0].get("skill", "?")
        last = steps[-1].get("skill", "?")
        key = f"{first}+{last}"
        words = re.findall(r'[\u4e00-\u9fff]{2,6}', message)
        patterns = [message[:30]]
        if len(words) >= 2:
            patterns.append(words[0] + words[-1])
        self.workflows[key] = {
            "patterns": patterns,
            "steps": [{"skill": s["skill"], "tool": s.get("tool", s["skill"])} for s in steps],
            "learned_at": time.time(),
            "success_count": 1,
            "source_message": message[:200],
        }
        return True

    def _find_similar(self, key):
        for k in self.workflows:
            if key in k or k in key:
                return k
        return None

    def match(self, message):
        msg_lower = message.lower()
        best_key, best_score = None, 0
        for key, wf in self.workflows.items():
            for pat in wf.get("patterns", []):
                if pat.lower() in msg_lower:
                    score = len(pat) / max(len(msg_lower), 1)
                    if score > best_score:
                        best_score = score
                        best_key = key
        if best_key and best_score > 0.1:
            return self.workflows[best_key]
        return None
