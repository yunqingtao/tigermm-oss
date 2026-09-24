"""
Learner — cross-session correction & preference learning.  **[SUPERSEDED 2026-09-19]**

★ 本模块已被 core/learn_loop.py 取代 (统一存储 + 置信 + 测量 + 衰减 + 人工门)。
  审计发现: get_learner() 建了对象但 analyze_message / get_context **从未被调用**,
  learnings.db 里那几行是历史手工数据 —— 组件是死的。为了不再出现"三套学习并存",
  新代码一律用 learn_loop; 本模块保留仅为旧端兼容 (analyze_message 已委托给它)。
==========================================================
Persists user corrections to SQLite, survives restart,
injects learned preferences into prompts.

Learns:
  - "不对，用中文" → preference: chinese_output
  - "默认发给涛哥" → preference: default_recipient=涛哥
  - "温度用摄氏度" → preference: celsius
"""
import json, time, logging, sqlite3, re
from pathlib import Path

logger = logging.getLogger("core.learner")

SCHEMA = """
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,       -- preference, correction, behavior
    key TEXT NOT NULL,            -- e.g. 'language', 'default_recipient'
    value TEXT NOT NULL,          -- e.g. 'chinese', '涛哥'
    weight INTEGER DEFAULT 1,    -- times reinforced
    source TEXT,                  -- what triggered this
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_learnings_key ON learnings(category, key);
"""

# Patterns to detect user corrections
CORRECTION_PATTERNS = [
    (r'(?:不对|错|不是|不要|别)\s*[，,]?\s*(.+)', "correction"),
    (r'(?:应该说|应该说|用|改成|换成)\s*(.+)', "correction"),
    (r'(?:以后|默认|总是|永远)\s*(.+)', "preference"),
    (r'(?:用中文|中文回复|中文输出)', "preference:language=chinese"),
    (r'(?:温度用|天气用)\s*(.+)', "preference:weather_unit"),
    (r'(?:发给|发送给)\s*(.+?)(?:[。！，,\s]|$)', "contact"),
]


class Learner:
    """Cross-session learning engine."""

    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "learnings.db"
        self._cache = {}  # key → value for fast lookup
        self._init_db()
        self._load_cache()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def _load_cache(self):
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT category, key, value, weight FROM learnings WHERE weight >= 1"
        ).fetchall()
        conn.close()
        self._cache.clear()
        for cat, key, val, w in rows:
            ck = f"{cat}:{key}"
            self._cache[ck] = {"value": val, "weight": w}

    def learn(self, category: str, key: str, value: str, source: str = ""):
        """Record a learning. Reinforces existing or creates new."""
        now = time.time()
        conn = sqlite3.connect(str(self.db_path))
        
        existing = conn.execute(
            "SELECT id, weight FROM learnings WHERE category = ? AND key = ?",
            (category, key)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE learnings SET weight = weight + 1, value = ?, source = ?, updated_at = ? WHERE id = ?",
                (value, source, now, existing[0])
            )
            logger.debug("Learner: reinforced %s:%s (weight=%d)", category, key, existing[1] + 1)
        else:
            conn.execute(
                "INSERT INTO learnings (category, key, value, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (category, key, value, source, now, now)
            )
            logger.info("Learner: learned %s:%s = %s", category, key, value)

        conn.commit()
        conn.close()
        self._load_cache()

    def get(self, key: str, default=None):
        """Get learned value by key."""
        for ck, data in self._cache.items():
            if ck.endswith(f":{key}"):
                return data["value"]
        return default

    def get_context(self) -> str:
        """Get learned preferences as prompt context."""
        if not self._cache:
            return ""
        
        lines = ["[已学习的偏好]"]
        for ck, data in self._cache.items():
            cat, key = ck.split(":", 1)
            if data["weight"] >= 2:  # only inject if reinforced
                lines.append(f"  - {key}: {data['value']} (权重{data['weight']})")
        
        return "\n".join(lines) if len(lines) > 1 else ""

    def analyze_message(self, message: str):
        """[SUPERSEDED] 委托给统一学习闭环 learn_loop —— 保证行为不再分叉。

        旧实现往 learnings.db 写, 而读取路径 (get_context) 从没被调用, 于是"学了白学"。
        现在转交 learn_loop.observe(), 与主流程同一条路。
        """
        try:
            from core.learn_loop import get_learn_loop
            got = get_learn_loop().observe(message)
            return bool(got)
        except Exception:
            return False


# Singleton
_learner = None

def get_learner(data_dir: Path) -> Learner:
    global _learner
    if _learner is None:
        _learner = Learner(data_dir)
    return _learner
