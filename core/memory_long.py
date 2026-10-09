"""
Long-Term Memory Engine — auto-extract preferences, facts, rules from sessions.
Three tiers: user profile, project memory, raw facts DB.
Uses Ollama for free summarization. Triggers on session close.
"""
import json, time, re, sqlite3
from pathlib import Path
from collections import Counter
import logging

logger = logging.getLogger("core.memory_long")

# ═══ SQLite schema ═══
FACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,           -- 'preference', 'rule', 'fact', 'pattern', 'persona'
    content TEXT,            -- the extracted fact
    source_session TEXT,     -- which session it came from
    confidence REAL,         -- 0.0-1.0 extraction confidence
    ref_count INTEGER DEFAULT 1,  -- how many times confirmed/referenced
    first_seen REAL,         -- timestamp
    last_seen REAL,          -- timestamp
    embedding BLOB,          -- optional vector for semantic search
    active INTEGER DEFAULT 1 -- 0 = pruned/merged
);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_active ON facts(active);
CREATE INDEX IF NOT EXISTS idx_facts_ref ON facts(ref_count DESC);
"""


class MemoryLongTerm:
    """Long-term memory: auto-extracts, stores, recalls facts across sessions."""

    def __init__(self, data_dir: Path, ollama_model: str = "qwen2.5:7b"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.ollama_model = ollama_model
        self._ollama_ok = None

        # SQLite for raw facts
        self.db_path = self.data_dir / "long_term.db"
        self._init_db()

        # Markdown files for human-readable memory
        self.user_profile_path = self.data_dir / "user_profile.md"
        self.project_memory_path = self.data_dir / "project_memory.md"

        # Extraction config
        self.max_facts = 200       # prune when exceeding
        self.min_confidence = 0.6  # only store facts above this

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(FACTS_SCHEMA)

    # ═══ Ollama helper ═══
    async def _ollama_summarize(self, prompt: str, max_tokens: int = 300) -> str:
        """Call Ollama for free summarization."""
        import urllib.request
        try:
            data = json.dumps({
                "model": self.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.1}
            }).encode("utf-8")
            req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=data,
                                        headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=60)
            return json.loads(resp.read()).get("response", "")
        except Exception as e:
            logger.warning("Ollama summarize failed: %s", e)
            return ""

    # ═══ Core: extract facts from conversation ═══
    async def extract_from_session(self, turns: list[dict], session_id: str = "") -> list[dict]:
        """Extract long-term facts from a completed session.
        Returns list of {'category':..., 'content':..., 'confidence':...}
        """
        if not turns or len(turns) < 3:
            return []

        # Build conversation summary for Ollama
        conv_text = ""
        for t in turns[-20:]:  # last 20 turns max
            role = "用户" if t.get("role") == "user" else "虎哥"
            content = str(t.get("content", ""))[:300]
            conv_text += f"{role}: {content}\n"

        prompt = f"""从以下对话中提取值得长期记住的信息。返回JSON数组，每条包含category和content。

Category必须是以下之一:
- preference: 用户的偏好、习惯、要求
- rule: 项目规则、约定、约束
- fact: 客观事实、知识点
- pattern: 重复出现的操作模式
- persona: 用户身份、角色信息

要求:
1. 只提取真正重要、未来可能用到的信息
2. 忽略临时的、一次性的信息
3. content要简洁，不超过80字
4. confidence用0-1表示可信度
5. 如果没有值得记住的信息，返回空数组[]

对话:
{conv_text}

输出纯JSON数组(不要markdown代码块):"""

        result = await self._ollama_summarize(prompt, max_tokens=500)
        if not result:
            return []

        # Parse JSON
        try:
            # Strip markdown code blocks if present
            result = result.strip()
            if result.startswith("```"):
                result = re.sub(r'^```\w*\n?', '', result)
                result = re.sub(r'\n?```$', '', result)
            facts = json.loads(result)
            if not isinstance(facts, list):
                return []
        except json.JSONDecodeError:
            # Try to extract array with regex
            m = re.search(r'\[.*\]', result, re.DOTALL)
            if m:
                try:
                    facts = json.loads(m.group())
                except Exception:
                    return []
            else:
                return []

        # Filter and clean
        valid = []
        for f in facts:
            if not isinstance(f, dict):
                continue
            cat = f.get("category", "fact")
            content = str(f.get("content", "")).strip()
            conf = float(f.get("confidence", 0.7))
            if cat in ("preference", "rule", "fact", "pattern", "persona") and len(content) > 3 and conf >= self.min_confidence:
                valid.append({"category": cat, "content": content, "confidence": conf})
        return valid

    # ═══ Store facts ═══
    def store_facts(self, facts: list[dict], session_id: str = ""):
        """Store extracted facts into DB and update markdown files."""
        now = time.time()
        stored = 0
        with sqlite3.connect(str(self.db_path)) as conn:
            for f in facts:
                # Check for duplicates (similar content)
                existing = conn.execute(
                    "SELECT id, ref_count FROM facts WHERE content = ? AND active = 1",
                    (f["content"],)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE facts SET ref_count = ref_count + 1, last_seen = ?, confidence = MAX(confidence, ?) WHERE id = ?",
                        (now, f.get("confidence", 0.7), existing[0])
                    )
                else:
                    conn.execute(
                        "INSERT INTO facts (category, content, source_session, confidence, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                        (f["category"], f["content"], session_id, f.get("confidence", 0.7), now, now)
                    )
                stored += 1

        if stored:
            self._rebuild_markdown()
            self._prune_if_needed()
            self._maybe_decay()
        return stored

    def _maybe_decay(self, interval: float = 6 * 3600) -> dict:
        """限频触发记忆代谢: 距上次代谢超过 interval 才跑, 返回 {} 表示跳过。"""
        last = getattr(self, "_last_decay_ts", 0.0)
        now = time.time()
        if now - last < interval:
            return {}
        self._last_decay_ts = now
        return self.decay_and_compact()

    # ═══ Recall: build context for new session ═══
    def build_context_for_model(self, message: str, max_facts: int = 8) -> str:
        """Search relevant memories for current message. Returns text to inject into system prompt."""
        keywords = self._extract_keywords(message)
        if not keywords:
            return ""

        with sqlite3.connect(str(self.db_path)) as conn:
            # Simple keyword match across categories
            facts = []
            for kw in keywords[:5]:
                rows = conn.execute(
                    "SELECT category, content, ref_count FROM facts WHERE active = 1 AND content LIKE ? ORDER BY ref_count DESC LIMIT 3",
                    (f"%{kw}%",)
                ).fetchall()
                for row in rows:
                    facts.append((row[0], row[1], row[2]))

            # Deduplicate and sort by ref_count
            seen = set()
            unique = []
            for cat, content, ref in sorted(facts, key=lambda x: -x[2]):
                if content not in seen:
                    seen.add(content)
                    unique.append((cat, content))

        if not unique:
            return ""

        # 回忆即巩固: 命中的事实刷新权重(限频, 避免每轮都写库)
        if time.time() - getattr(self, "_last_recall_ts", 0.0) > 300:
            self._last_recall_ts = time.time()
            for _cat, _content in unique[:max_facts]:
                try:
                    self.record_use(_content)
                except Exception:
                    pass

        lines = ["# Long-term Memory"]
        for cat, content in unique[:max_facts]:
            cat_cn = {"preference": "偏好", "rule": "规则", "fact": "事实", "pattern": "模式", "persona": "身份"}.get(cat, cat)
            lines.append(f"  [{cat_cn}] {content}")
        return "\n".join(lines)

    # ═══ Markdown export ═══
    def _rebuild_markdown(self):
        """Regenerate user_profile.md and project_memory.md from DB."""
        with sqlite3.connect(str(self.db_path)) as conn:
            # User profile: preferences + persona
            prefs = conn.execute(
                "SELECT content FROM facts WHERE category IN ('preference','persona') AND active=1 ORDER BY ref_count DESC LIMIT 20"
            ).fetchall()

            # Project memory: rules + patterns + facts
            rules = conn.execute(
                "SELECT content FROM facts WHERE category IN ('rule','pattern','fact') AND active=1 ORDER BY ref_count DESC LIMIT 30"
            ).fetchall()

        if prefs:
            lines = ["# 用户档案\n"]
            for (c,) in prefs:
                lines.append(f"- {c}")
            self.user_profile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if rules:
            lines = ["# 项目记忆\n", "> 自动提取，用于后续会话上下文\n"]
            for (c,) in rules:
                lines.append(f"- {c}")
            self.project_memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ═══ Prune ═══
    def _prune_if_needed(self):
        """Remove stale/low-frequency facts when exceeding max_facts."""
        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM facts WHERE active=1").fetchone()[0]
            if count <= self.max_facts:
                return

            # Mark low-ref facts as inactive
            excess = count - self.max_facts + 20
            conn.execute(
                f"UPDATE facts SET active=0 WHERE id IN (SELECT id FROM facts WHERE active=1 ORDER BY ref_count ASC, last_seen ASC LIMIT {excess})"
            )
            conn.commit()
            logger.info("Pruned %d low-frequency facts (kept %d)", excess, self.max_facts - 20)

    # ═══ Memory metabolism: 遗忘曲线衰减 + 巩固 ═══
    # Ebbinghaus 模型: score = ref_count * exp(-lambda * age_days), 半衰期 30 天
    # 低频+久远 → 归档(active=0); 高频+被引用 → 巩固(ref_count 增长, last_seen 刷新)

    DECAY_HALF_LIFE_DAYS = 30.0      # 半衰期: 30 天权重减半
    DECAY_ARCHIVE_DAYS = 60.0        # 超过 60 天未见的低频事实 → 归档
    DECAY_MIN_REF = 2                # ref_count < 2 且超期 → 归档候选

    def _decay_score(self, ref_count: int, age_days: float) -> float:
        """遗忘曲线得分: ref_count * 2^(-age/half_life)。"""
        import math
        return ref_count * (2.0 ** (-age_days / self.DECAY_HALF_LIFE_DAYS))

    def decay_and_compact(self, force: bool = False) -> dict:
        """记忆代谢: 遗忘曲线降级 + 高价值巩固。返回代谢报告。

        规则:
        1. ref_count >= 5 且长期活跃 → 巩固(权重+1), 视为核心记忆
        2. age > 60 天且 ref_count < 2 且 score < 0.3 → 归档(active=0)
        3. 归档超过 90 天 → 物理删除(彻底遗忘)
        """
        import math
        now = time.time()
        report = {"archived": 0, "deleted": 0, "reinforced": 0, "total_active": 0}
        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, ref_count, first_seen, last_seen, active FROM facts"
            ).fetchall()
            for fid, ref, first_seen, last_seen, active in rows:
                if not active:
                    # 已归档且超 90 天 → 物理删除
                    if now - (last_seen or first_seen or now) > 86400 * 90:
                        conn.execute("DELETE FROM facts WHERE id = ?", (fid,))
                        report["deleted"] += 1
                    continue
                age_days = (now - (last_seen or now)) / 86400.0
                score = self._decay_score(ref or 1, age_days)
                # 巩固: 高频核心记忆
                if ref >= 5 and age_days < self.DECAY_ARCHIVE_DAYS:
                    conn.execute(
                        "UPDATE facts SET ref_count = ref_count + 1 WHERE id = ?", (fid,))
                    report["reinforced"] += 1
                # 归档: 久远 + 低频 + 得分低
                elif (age_days > self.DECAY_ARCHIVE_DAYS
                      and (ref or 1) < self.DECAY_MIN_REF
                      and score < 0.3):
                    conn.execute(
                        "UPDATE facts SET active = 0, last_seen = ? WHERE id = ?",
                        (now, fid))
                    report["archived"] += 1
            conn.commit()
            report["total_active"] = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE active=1").fetchone()[0]
        if report["archived"] or report["deleted"] or report["reinforced"]:
            self._rebuild_markdown()
        logger.info("memory metabolism: %s", report)
        return report

    def record_use(self, fact_content: str) -> bool:
        """事实被召回/引用时巩固: ref_count+1, last_seen 刷新。返回是否命中。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute(
                "UPDATE facts SET ref_count = ref_count + 1, last_seen = ? "
                "WHERE active = 1 AND content = ?",
                (time.time(), fact_content))
            conn.commit()
            return cur.rowcount > 0

    # ═══ Helpers ═══
    def _extract_keywords(self, text: str) -> list[str]:
        """Extract meaningful keywords from user message."""
        words = []
        # CJK: split on common delimiters, take 2-6 char meaningful segments
        cjk = re.findall(r'[\u4e00-\u9fff]+', text)
        for seg in cjk:
            # Try jieba if available (suppress noise)
            try:
                import jieba
                jieba.setLogLevel(60)  # silence debug output
                words.extend([w for w in jieba.cut(seg) if len(w) >= 2])
            except ImportError:
                # Fallback: sliding window 2-4 chars, but only if segment is short
                if len(seg) <= 6:
                    words.append(seg)
                else:
                    for i in range(0, len(seg)-1, 2):
                        chunk = seg[i:i+3]
                        if len(chunk) >= 2:
                            words.append(chunk)
        # English: 3+ char words
        eng = re.findall(r'[a-zA-Z]{3,}', text)
        words.extend(eng)
        # Deduplicate, remove stops
        stops = {'这个', '那个', '什么', '怎么', '一个', '一些', '可以', '需要', '应该',
                 'the', 'and', 'for', 'this', 'that', 'are', 'not', 'but'}
        return list(dict.fromkeys(w for w in words if w.lower() not in stops))

    # ═══ Status / Debug ═══
    def status(self) -> dict:
        with sqlite3.connect(str(self.db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM facts WHERE active=1").fetchone()[0]
            by_cat = conn.execute(
                "SELECT category, COUNT(*) FROM facts WHERE active=1 GROUP BY category"
            ).fetchall()
        return {
            "total_facts": total,
            "by_category": dict(by_cat),
            "user_profile": str(self.user_profile_path) if self.user_profile_path.exists() else None,
            "project_memory": str(self.project_memory_path) if self.project_memory_path.exists() else None,
        }


# ═══ Singleton ═══
_long_mem: MemoryLongTerm | None = None


def get_long_memory(data_dir: Path = None, ollama_model: str = "qwen2.5:7b") -> MemoryLongTerm:
    global _long_mem
    if _long_mem is None and data_dir is not None:
        _long_mem = MemoryLongTerm(data_dir, ollama_model)
    return _long_mem
