"""
Context Builder — Hermes-style context pre-injection.
Assembles entities, environment, skills, and learned rules
into a system prompt section injected BEFORE every process() call.

The agent doesn't search for context — context finds the agent.
"""

import json
from pathlib import Path


class ContextBuilder:
    """Builds the context block injected into system prompt before every turn."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self._cache = {}
        self._cache_mtime = {}

    def build(self, message="", user=None):
        """Assemble full context block. Returns string for system prompt injection."""
        parts = []

        # 1. Entities — who the agent knows
        entities = self._load("entities.json")
        if entities:
            lines = ["Known contacts:"]
            for name, info in entities.items():
                if info.get("type") == "person":
                    email = info.get("email", "")
                    phone = info.get("phone", "")
                    city = info.get("city", "")
                    extras = []
                    if email:
                        extras.append(f"email={email}")
                    if phone:
                        extras.append(f"phone={phone}")
                    if city:
                        extras.append(f"city={city}")
                    aliases = info.get("aliases", [])
                    alias_str = f" (aka {', '.join(aliases)})" if aliases else ""
                    extra_str = f" [{', '.join(extras)}]" if extras else ""
                    lines.append(f"  {name}{alias_str}{extra_str}")
            parts.append("\n".join(lines))

        # 2. Environment — known paths
        env = self._load("environment.json")
        if env:
            lines = ["Known paths:"]
            for name, info in env.items():
                path_val = info.get("value", "")
                aliases = info.get("aliases", [])
                alias_str = f" ({', '.join(aliases)})" if aliases else ""
                lines.append(f"  {name}{alias_str} = {path_val}")
            parts.append("\n".join(lines))

        # 3. Skills — what tools are available
        skills = self._load("skills.json")
        if skills:
            lines = ["Available skills (use proactively when relevant):"]
            for name, info in list(skills.items())[:12]:
                desc = info.get("desc", "")
                tool = info.get("tool", "?")
                examples = info.get("say", [])[:2]
                ex_str = f" e.g. {'; '.join(examples)}" if examples else ""
                lines.append(f"  {name} -> {tool}: {desc}{ex_str}")
            parts.append("\n".join(lines))

        # 4. Learned rules — what the agent has learned
        learned = self._load("learned_rules.json")
        if learned:
            recent = list(learned.items())[-5:]  # last 5
            if recent:
                lines = ["Recently learned:"]
                for key, val in recent:
                    if isinstance(val, dict):
                        t = val.get("_t", "?")
                        urls = len(val.get("urls", []))
                        cmds = len(val.get("cmds", []))
                        lines.append(f"  {key} (type={t}, urls={urls}, cmds={cmds})")
                    else:
                        lines.append(f"  {key}: {str(val)[:60]}")
                parts.append("\n".join(lines))

        # 5. Task context — what we're working on now (persists across sessions)
        task = self._load("task_context.json")
        if task:
            lines = ["CURRENT PROJECT STATUS:"]
            lines.append(f"  Project: {task.get('project', 'N/A')}")
            lines.append(f"  Phase: {task.get('phase', 'N/A')}")
            done = task.get('done', [])
            if done:
                lines.append("  Completed:")
                for d in done[-8:]:
                    lines.append(f"    - {d}")
            next_items = task.get('next', [])
            if next_items:
                lines.append("  Up next:")
                for n in next_items[:5]:
                    lines.append(f"    - {n}")
            parts.append("\n".join(lines))

        # 8. Daily progress (PROGRESS.md)
        progress_path = Path(__file__).parent.parent / "PROGRESS.md"
        if progress_path.exists():
            try:
                progress_text = progress_path.read_text(encoding="utf-8")
                # Extract today's section
                today_header = "## " + __import__('datetime').datetime.now().strftime("%Y-%m-%d")
                start = progress_text.find(today_header)
                if start >= 0:
                    end = progress_text.find("\n## ", start + len(today_header))
                    if end < 0:
                        end = len(progress_text)
                    parts.append(f"# Today's Progress\n{progress_text[start:end].strip()}")
            except Exception:
                pass

        return "\n\n".join(parts) if parts else ""

    def _load(self, filename):
        """Load JSON with mtime-based caching."""
        fpath = self.data_dir / filename
        if not fpath.exists():
            return {}

        mtime = fpath.stat().st_mtime
        if filename in self._cache and self._cache_mtime.get(filename) == mtime:
            return self._cache[filename]

        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
            self._cache[filename] = data
            self._cache_mtime[filename] = mtime
            return data
        except Exception:
            return self._cache.get(filename, {})

    def build_behavioral_rules(self):
        """Return behavioral directives for the system prompt.
        These are rules the agent follows PROACTIVELY — not reactive code."""
        return """PROACTIVE RULES (follow these without being asked):

1. ENTITY RULE: After showing contact info, check if desktop files could be relevant.
   e.g. User asks "who is X" -> after answering, check desktop for files and mention "I see N files on desktop you could send to X".

2. VERIFY RULE: After writing/creating a file, immediately verify it exists and show its content.
   e.g. User says "write X to desktop" -> after writing, read the file back and confirm.

3. SYSTEM RULE: After showing system info, automatically check disk space.
   e.g. User says "system status" -> after status, run disk check and report.

4. CONTINUITY RULE: When user greets after a task session, recall what was being worked on.
   e.g. User says "hello" after working on files -> mention "continuing from: we were working on X".

5. DEEPEN RULE: After answering knowledge questions, offer to search for more detail.
   e.g. User asks "what is X" -> after answering, suggest "I can search for more details on X".

6. CODE VERIFY RULE: After generating code, run syntax check automatically.
   e.g. User asks "write a Python script" -> after writing, py_compile to verify.

Apply these rules silently. Don't ask permission for verification steps — just do them."""

    def build_full_system_prompt(self, base_prompt, message="", user=None):
        """Build complete system prompt with injected context and behavioral rules."""
        context = self.build(message, user)
        rules = self.build_behavioral_rules()
        return f"{base_prompt}\n\n# Context (pre-loaded for this session)\n{context}\n\n{rules}"
