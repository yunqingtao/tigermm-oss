"""
ProactiveThinker - TMM active thinking engine
Predict user's next intent, pre-execute, deliver before asked.

Three confidence tiers:
  >0.8 HIGH: auto-execute, deliver silently
  0.4-0.8 MEDIUM: pre-execute + lightweight confirm
  <0.4 LOW: hint only
"""

import os, re, time
from pathlib import Path


class ProactiveThinker:
    """Predicts next user intent and pre-emptively executes."""

    def __init__(self, gateway, knowledge_engine, data_dir):
        self.gateway = gateway
        self.ke = knowledge_engine
        self.data_dir = Path(data_dir)
        self.session = {"history": []}

    # Intent chains: (current_intent -> [(next_intent, confidence, method), ...])
    CHAINS = {
        "entity_query": [
            ("entity_operate", 0.85, "_preempt_entity_operate"),
        ],
        "file_write": [
            ("verify_file", 0.9, "_preempt_verify_file"),
        ],
        "system_info": [
            ("disk_check", 0.85, "_preempt_disk_check"),
        ],
        "search": [
            ("deep_dive", 0.75, "_preempt_deep_dive"),
        ],
        "error": [
            ("check_log", 0.7, "_preempt_check_log"),
        ],
        "knowledge": [
            ("deep_question", 0.6, "_preempt_deep_question"),
        ],
        "greeting": [
            ("resume_context", 0.6, "_preempt_resume_context"),
        ],
        "code_gen": [
            ("syntax_check", 0.9, "_preempt_syntax_check"),
        ],
        "list_dir": [
            ("read_recent_file", 0.75, "_preempt_read_recent"),
        ],
        "email_send": [
            ("verify_send", 0.8, "_preempt_verify_send"),
        ],
    }

    async def predict_and_act(self, current_intent, message, result):
        """Main entry: predict next step, execute if confident, return proactive results."""
        self.session["history"].append((current_intent, message, str(result)[:200]))
        if len(self.session["history"]) > 10:
            self.session["history"] = self.session["history"][-10:]

        chains = self.CHAINS.get(current_intent, [])
        if not chains:
            return []

        proactive_results = []
        for next_intent, confidence, method_name in chains:
            # Boost confidence if this intent appears in recent history
            if self._recent_intent_count(current_intent, lookback=3) >= 2:
                confidence = min(1.0, confidence + 0.1)

            method = getattr(self, method_name, None) if method_name else None
            level = "auto" if confidence >= 0.8 else ("suggest" if confidence >= 0.4 else "hint")

            try:
                if method and confidence >= 0.4:
                    preempt = await method(current_intent, message, result)
                    if preempt:
                        proactive_results.append({
                            "level": level,
                            "label": preempt.get("label", ""),
                            "content": preempt.get("content", ""),
                            "confidence": confidence,
                        })
            except Exception:
                pass

        proactive_results.sort(key=lambda x: x["confidence"], reverse=True)
        return proactive_results[:2]

    def _recent_intent_count(self, intent, lookback=3):
        return sum(1 for h_intent, _, _ in self.session["history"][-lookback:] if h_intent == intent)

    # ---- Preempt methods ----

    async def _preempt_entity_operate(self, intent, message, result):
        """Entity query -> list files available for sending."""
        try:
            entities = self.ke.entities
            who = next((name for name in entities if name in message), None)
            if not who:
                return None
            desktop = Path.home() / "Desktop"
            if not desktop.exists():
                return None
            files = sorted(
                [f for f in desktop.iterdir() if f.is_file()],
                key=lambda p: p.stat().st_mtime, reverse=True
            )[:8]
            if not files:
                return None
            lines = [f"  {f.name} ({f.stat().st_size:,}B)" for f in files[:5]]
            content = f"桌面 {len(files)} 个文件:\n" + "\n".join(lines)
            content += f"\n说 发 X文件 给{who}"
            return {"label": f"可发给{who}", "content": content}
        except Exception:
            return None

    async def _preempt_verify_file(self, intent, message, result):
        """File written -> verify it exists + show content."""
        try:
            result_str = str(result.get("response", ""))
            m = re.search(r'([A-Za-z]:[^\s,]+\.\w{2,10})', result_str)
            fpath = None
            if m:
                fpath = Path(m.group(1))
            else:
                desktop = Path.home() / "Desktop"
                recent = sorted(desktop.glob("doc_*.txt"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
                fpath = recent[0] if recent else None
            if fpath and fpath.exists():
                size = fpath.stat().st_size
                text = fpath.read_text(encoding="utf-8", errors="replace")[:150]
                return {"label": f"已验证 {fpath.name}",
                        "content": f"  {size}B: {text}"}
            return None
        except Exception:
            return None

    async def _preempt_disk_check(self, intent, message, result):
        """System info -> auto disk check."""
        try:
            import shutil
            disk = shutil.disk_usage(str(Path.home()))
            gb_total = disk.total / (1024**3)
            gb_used = disk.used / (1024**3)
            gb_free = disk.free / (1024**3)
            pct = (disk.used / disk.total) * 100
            status = "TIGHT" if pct > 80 else ("OK" if pct < 60 else "HALF")
            content = f"  {status} | {gb_used:.1f}G/{gb_total:.1f}G ({pct:.0f}%) | free {gb_free:.1f}G"
            return {"label": "Disk check", "content": content}
        except Exception:
            return None

    async def _preempt_deep_dive(self, intent, message, result):
        """Search -> suggest deep dive."""
        q = message
        for kw in ["search", "sou", "cha"]:
            if kw in q.lower():
                q = q.split(kw, 1)[-1].strip()
                break
        return {"label": "Deep dive",
                "content": f"fetch {q[:30]} for details"}

    async def _preempt_check_log(self, intent, message, result):
        """Error -> suggest log check."""
        return {"label": "Debug",
                "content": "check-log to see recent errors"}

    async def _preempt_deep_question(self, intent, message, result):
        """Knowledge -> suggest deeper question."""
        topic = message[:30]
        return {"label": "Deeper",
                "content": f"ask: {topic} details"}

    async def _preempt_resume_context(self, intent, message, result):
        """Greeting -> resume last topic."""
        if len(self.session["history"]) >= 2:
            prev_intent, prev_msg, _ = self.session["history"][-2]
            if prev_intent not in ("greeting", None):
                return {"label": "Resume",
                        "content": f"Last: {prev_msg[:50]}..."}
        return None

    async def _preempt_syntax_check(self, intent, message, result):
        """Code gen -> syntax check recent .py file."""
        try:
            import py_compile
            for root_dir in [Path.home() / "Desktop", Path.cwd()]:
                if root_dir.exists():
                    py_files = sorted(
                        root_dir.glob("*.py"),
                        key=lambda p: p.stat().st_mtime, reverse=True
                    )
                    if py_files:
                        fpath = py_files[0]
                        try:
                            py_compile.compile(str(fpath), doraise=True)
                            return {"label": "Syntax OK",
                                    "content": f"  {fpath.name} valid"}
                        except py_compile.PyCompileError as e:
                            return {"label": "Syntax ERR",
                                    "content": f"  {fpath.name}: {str(e)[:80]}"}
            return None
        except Exception:
            return None

    async def _preempt_read_recent(self, intent, message, result):
        return None  # placeholder

    async def _preempt_verify_send(self, intent, message, result):
        return None  # placeholder
