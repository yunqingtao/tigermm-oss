"""LearnLoop — 真正的学习闭环 (记录 → 置信 → 生效 → 测量 → 衰减)。

为什么要有这个模块 (2026-09-19 审计实证):
    项目里原有**三套**学习机制, 两套是死的、一套只有一半:
      ① core/self_evolve.py  main.py 传 original_msg="" → _extract_intent 返回空
                             → 永远 return None (**整条路是死的**);
                             且 apply_rules_to_autoguide 找的 "# end of rules" 标记
                             在 auto_guide.py 里不存在 → 计数器照样 +1 → **谎报 added**;
                             设计是**往源码里写规则** (自我修改生产源码, 无门禁)。
      ② core/learner.py      get_learner(DATA_DIR) 建了对象, 但 analyze_message /
                             get_context **从未被调用** → 组件是死的。
      ③ core/perception.py   活的 (注入提示词), 但只"提醒别再犯", 不成规则、不计效果。
    AutoGuide 的 471 条罐头答案里, _run_autoguide 只放行 {identity, help, greeting},
    143 tech + 219 knowledge + 1975 术语全部"匹配后放弃" → 365 条是**死重**。

本模块的设计 (与"技能工厂"同源的三条原则):
    ① 规则存**数据**, 绝不改源码 —— 运行时加载。
    ② 闭环必须有**测量**: 命中计数 → 稳固; 被反证 → 降级/撤销; 久未命中 → 休眠。
    ③ 重大/危险的学习走**人工确认门** (candidates/activate/reject)。

规则三类:
    preference  偏好   key=名字(如 default_recipient) value=值      → 注入提示词, 立即生效
    answer      答案   key=触发问题        value=答案               → 命中直接回答
    (预留 route  路由   key=触发问题        value=意图/工具名)

置信规则 (可验证的明确语义):
    · 新规则: status=active, weight=1 (低置信)
    · 命中 3 次且从未被反证 → weight=2 (稳固)                  ← 自我强化
    · 被反证 1 次: weight>=2 → weight-=1 (降级); weight==1 → 撤销 (status=rejected)
    · 同一 key 出现**不同** value → 旧规则 superseded (status=rejected)
    · 久未命中 (RULE_TTL_DAYS) → status=stale (休眠, 不删除, 可查可复活)
    · 规则总数超 MAX_LEARNED_RULES → 淘汰得分最低的 (得分 = weight*10 + hits - misses)
"""
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("core.learn_loop")

try:
    from config.settings import DATA_DIR, MAX_LEARNED_RULES, RULE_TTL_DAYS
except Exception:                                        # 独立跑测试时的兜底
    DATA_DIR = Path(__file__).parent.parent / "data"
    MAX_LEARNED_RULES = 1000
    RULE_TTL_DAYS = 30

DB_NAME = "learned_rules.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- preference / answer
    key TEXT NOT NULL,             -- preference: 名字 ; answer: 触发问题
    value TEXT NOT NULL,           -- 学到的内容
    weight INTEGER DEFAULT 1,      -- 置信 (>=2 = 稳固)
    hits INTEGER DEFAULT 0,        -- 生效次数 (测量)
    misses INTEGER DEFAULT 0,      -- 被反证次数
    status TEXT DEFAULT 'active',  -- active / rejected / stale
    source TEXT,                   -- 触发它的原话
    note TEXT,                     -- 替换/撤销原因
    created_at REAL,
    updated_at REAL,
    last_hit_at REAL
);
CREATE INDEX IF NOT EXISTS idx_rules_kind_key ON rules(kind, key);
CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status);

CREATE TABLE IF NOT EXISTS open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL UNIQUE,
    route TEXT,                    -- 当时由谁回答
    count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'open',    -- open / answered / dropped
    created_at REAL,
    updated_at REAL
);
"""

# 反证信号 (用户说"不对/错了/应该是…")
_NEGATIVE = re.compile(
    r"(?:不对|错了|不是|不要|别这样|说错|答错|不对啊|不行)[，,、\s]*(.*)$")
_REPLACE = re.compile(r"(?:应该(?:是|说|用)|改成|换成|要用|改为)[，,、\s]*(.+)$")
# 偏好信号 (用户用"以后/默认/总是/永远"教系统)
_PREFERENCE = re.compile(
    r"(?:以后|默认|总是|永远|记住)[，,、\s]*(.+)$")
# "用中文/中文回复" 这类定式偏好
_PREF_RULES = [
    (re.compile(r"用中文|中文回复|中文输出"), "language", "chinese"),
    (re.compile(r"(?:温度|天气)用\s*(摄氏度|华氏度)"), "weather_unit", None),
]
# 教学句式开头 (路由据此判定"用户在教系统", 而不是在下命令)
_TEACH_START = re.compile(r"^\s*(?:请|帮我)?(?:以后|默认|总是|永远|记住)")
# 实体记忆形态提示 (这些词出现 = 在记联系人, 不是记偏好)
_ENTITY_HINT = re.compile(r"邮箱|邮件地址|电话|手机|地址|生日|微信|email|e-mail", re.I)

# 偏好 key 抽取: 值里出现这些词就归到对应 key (否则 key=general)
_PREF_KEYS = [
    (re.compile(r"发给|发送给|收件人|邮件给"), "default_recipient"),
    (re.compile(r"中文|英文"), "language"),
    (re.compile(r"摄氏度|华氏度|温度|天气"), "weather_unit"),
    (re.compile(r"简洁|简短|别啰嗦|详细|展开"), "verbosity"),
    (re.compile(r"桌面|下载|文档"), "default_dir"),
]


class LearnLoop:
    """单一学习存储 + 闭环。SQLite, 跨会话存活。"""

    def __init__(self, data_dir: Path = None, db_path: Path = None,
                 max_rules: int = None, ttl_days: int = None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.db_path = Path(db_path or (self.data_dir / DB_NAME))
        self.max_rules = int(max_rules if max_rules is not None else MAX_LEARNED_RULES)
        self.ttl_days = float(ttl_days if ttl_days is not None else RULE_TTL_DAYS)
        self._init_db()

    # ── 存储 ──
    def _conn(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = self._conn()
        try:
            c.executescript(SCHEMA)
            c.commit()
        finally:
            c.close()

    # ── 记录 (闭环第 1 环) ──
    def record(self, kind: str, key: str, value: str, source: str = "") -> dict:
        """记一条规则。同 key 同 value → 强化(weight+1); 同 key 不同 value → 取代旧的。

        返回 {"action": "created"/"reinforced"/"superseded", "id": N, "rule": {...}}
        """
        kind = (kind or "").strip()
        key = (key or "").strip()[:200]
        value = (value or "").strip()[:500]
        if not kind or not key or not value:
            return {"action": "invalid", "id": None, "rule": None}
        now = time.time()
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM rules WHERE kind=? AND key=? AND status IN ('active','stale')",
                (kind, key)).fetchall()
            same = next((r for r in rows if (r["value"] or "").strip() == value), None)
            if same:                                     # 强化
                c.execute("UPDATE rules SET weight=weight+1, updated_at=? WHERE id=?",
                          (now, same["id"]))
                c.commit()
                c.execute("UPDATE rules SET status='active' WHERE id=? AND status='stale'", (same["id"],))
                c.commit()
                return {"action": "reinforced", "id": same["id"], "rule": self.get(same["id"])}
            for r in rows:                               # 取代 (不同 value)
                c.execute("UPDATE rules SET status='rejected', note=?, updated_at=? WHERE id=?",
                          (f"被新的纠正取代: {value[:60]}", now, r["id"]))
            c.execute(
                "INSERT INTO rules (kind,key,value,weight,hits,misses,status,source,note,"
                "created_at,updated_at,last_hit_at) VALUES (?,?,?,1,0,0,'active',?,'',?,?,NULL)",
                (kind, key, value, source[:300], now, now))
            rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()
            act = "superseded" if rows else "created"
            logger.info("LearnLoop: %s %s:%s = %s", act, kind, key[:40], value[:40])
            return {"action": act, "id": rid, "rule": self.get(rid)}
        finally:
            c.close()

    def record_preference(self, key: str, value: str, source: str = "") -> dict:
        return self.record("preference", key, value, source)

    def record_answer(self, trigger: str, answer: str, source: str = "") -> dict:
        return self.record("answer", trigger, answer, source)

    # ── 观察 (闭环入口: 每轮对话后调用) ──
    def observe(self, message: str, response: str = None, route: str = None,
                prev_user: str = None, prev_assistant: str = None) -> dict:
        """从一轮对话里学东西。返回本轮的收获 (空 dict = 没学到)。

        学什么:
          1. 纠正   "不对, 应该是摄氏度"  → 若知道上一轮问了什么, 学成 answer 规则
          2. 偏好   "以后默认发给涛哥"    → preference 规则 (立即生效)
          3. 未答上 由 model.fallback 兜底且回复里带"不知道/没找到" → 记入待学池
        """
        msg = (message or "").strip()
        if not msg or msg.startswith("/"):               # 斜杠命令不学习
            return {}
        got = {}

        # 1) 偏好 (先判 — "以后…" 比 "不对" 更强); 用共享的纯解析, 判定口径不分叉
        pref = self.parse_preference(msg, require_start=False)
        if pref:
            got["preference"] = self.record_preference(pref[0], pref[1], msg)
            return got

        # 2) 纠正 (需要知道被纠正的是什么 → 用上一轮)
        neg = _NEGATIVE.search(msg)
        if neg:
            rep = _REPLACE.search(msg)
            if rep:                                      # 明确给出替代值
                # ★ 顺序要紧 (踩过): **先**惩罚旧规则, **再**记新规则 ——
                #   反过来会把刚学会的这条自己否掉 (相似度 1.0 命中新规则 → 撤销)。
                if prev_user:
                    self._penalize_rule_for(prev_user)
                target = (prev_user or "").strip()
                if target and not target.startswith("/") and len(target) >= 2:
                    got["answer"] = self.record_answer(target, rep.group(1).strip(), msg)
            else:
                got["correction"] = {"ack": True, "text": msg[:80]}
                if prev_user:
                    self._penalize_rule_for(prev_user)

        # 3) 未答上的问题 → 待学池
        if route and route in ("model.fallback", "error") and response:
            low = str(response)
            if any(k in low for k in ("不知道", "不清楚", "没找到", "无法回答", "暂时没法")):
                qid = self.add_open_question(msg, route)
                if qid:
                    got["open_question"] = qid
        return got

    # ── 生效 (闭环第 3 环) ──
    def hints(self) -> str:
        """活跃偏好 → 供提示词注入的文本 (无偏好返回 "")。"""
        rows = self._active("preference")
        if not rows:
            return ""
        lines = ["[已学会的偏好 — 请遵守]"]
        for r in rows:
            star = " (已确认)" if r["weight"] >= 2 else ""
            lines.append(f"  - {r['key']}: {r['value']}{star}")
        return "\n".join(lines)

    def apply(self, message: str, threshold: float = 0.5) -> dict:
        """拿学到的答案规则直接回答。命中则计一次 hits。返回 rule dict 或 None。"""
        from core.text_match import similarity   # CJK 双字组口径 (见 text_match 说明)
        msg = (message or "").strip()
        if not msg:
            return None
        best, best_s = None, 0.0
        for r in self._active("answer"):
            s = similarity(msg, r["key"])
            if s > best_s:
                best, best_s = r, s
        if best and best_s >= threshold:
            self.note_hit(best["id"])
            return dict(best)
        return None

    def parse_preference(self, message: str, require_start: bool = False):
        """**纯**函数: 从消息里抽出 (key, value) 偏好; 不是偏好句 → None。

        为什么要抽出来: ① 路由的 match() 必须无副作用; ② 确认回复要显示"学到了什么",
        也得**不落库**地先算出来。observe() 与路由共用这一份实现, 避免两套判定漂移。

        require_start=True (给路由用): 必须是**教学句式开头** (以后/默认/总是/永远/记住
        [+可选"请/帮我"]), 才认 —— 保守, 免得把普通命令当教学而抢走执行。
        require_start=False (给 observe 用): 句中任意位置出现教学词都认 (宽松, 多学不亏)。
        """
        msg = (message or "").strip()
        if not msg or len(msg) > 60 or msg.startswith("/") or msg.startswith("@"):
            return None
        if require_start and not _TEACH_START.match(msg):
            return None
        if "记住" in msg and _ENTITY_HINT.search(msg):
            # "记住：张三 邮箱 z@t.com" 是**实体记忆**形态 → 交给实体链路, 不当偏好
            return None
        for pat, key, fixed in _PREF_RULES:
            if pat.search(msg):
                return (key, fixed or msg)
        m = _PREFERENCE.search(msg)
        if not m:
            return None
        val = m.group(1).strip()
        if len(val) < 2 or (require_start and not _TEACH_START.match(msg)):
            return None
        key = "general"
        for kpat, kname in _PREF_KEYS:
            if kpat.search(val):
                key = kname
                break
        return (key, val)

    def matches(self, message: str, threshold: float = 0.5):
        """**纯**查询: 有没有学到的答案规则命中 (不计数)。

        为什么单独一个方法: 「匹配」可能发生在 routes 的 match() 里, 而 match 必须是
        纯的 (不能有副作用) —— 计数放到 run() 走 apply()。否则命中数会翻倍。
        """
        from core.text_match import similarity
        msg = (message or "").strip()
        if not msg:
            return None
        best, best_s = None, 0.0
        for r in self._active("answer"):
            s = similarity(msg, r["key"])
            if s > best_s:
                best, best_s = r, s
        return dict(best) if (best and best_s >= threshold) else None

    def note_hit(self, rule_id: int) -> dict:
        """命中一次 (测量)。命中 3 次且无反证 → 升为稳固 (weight=2)。"""
        now = time.time()
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            if not r:
                return {}
            hits = (r["hits"] or 0) + 1
            weight = r["weight"] or 1
            promoted = False
            if hits >= 3 and weight < 2:
                weight, promoted = 2, True
            c.execute("UPDATE rules SET hits=?, weight=?, last_hit_at=?, updated_at=? WHERE id=?",
                      (hits, weight, now, now, rule_id))
            c.commit()
            return {"id": rule_id, "hits": hits, "weight": weight, "promoted": promoted}
        finally:
            c.close()

    # ── 反证 (闭环第 4 环: 被纠错 → 降级/撤销) ──
    def note_correction(self, rule_id: int, reason: str = "") -> dict:
        now = time.time()
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            if not r:
                return {}
            misses = (r["misses"] or 0) + 1
            weight = r["weight"] or 1
            if weight >= 2:                              # 稳固的先降级
                c.execute("UPDATE rules SET misses=?, weight=weight-1, updated_at=? WHERE id=?",
                          (misses, now, rule_id))
                c.commit()
                return {"id": rule_id, "action": "demoted", "weight": weight - 1}
            c.execute("UPDATE rules SET misses=?, status='rejected', note=?, updated_at=? WHERE id=?",
                      (misses, (reason or "被用户反证")[:120], now, rule_id))
            c.commit()
            logger.info("LearnLoop: 规则 %s 被反证 → 撤销", rule_id)
            return {"id": rule_id, "action": "rejected"}
        finally:
            c.close()

    def _penalize_rule_for(self, prev_user: str) -> dict:
        """上一轮的问题命中了某条**答案规则**但被纠正 → 惩罚那条规则。"""
        from core.text_match import similarity
        best, best_s = None, 0.0
        for r in self._active("answer"):
            s = similarity(prev_user, r["key"])
            if s > best_s:
                best, best_s = r, s
        if best and best_s >= 0.5:
            return self.note_correction(best["id"], "用户纠正了这条答案")
        return {}

    # ── 衰减 (闭环第 5 环: 测量 → 淘汰) ──
    def decay(self) -> dict:
        """久未命中 → 休眠; 超上限 → 淘汰低分。不删除数据 (stale 可查可复活)。"""
        now = time.time()
        ttl = self.ttl_days * 86400
        c = self._conn()
        try:
            n_stale = 0
            for r in c.execute("SELECT * FROM rules WHERE status='active' AND weight<2").fetchall():
                last = r["last_hit_at"] or r["created_at"] or 0
                if now - last > ttl:
                    c.execute("UPDATE rules SET status='stale', note=?, updated_at=? WHERE id=?",
                              (f"{self.ttl_days:g} 天未命中 → 休眠", now, r["id"]))
                    n_stale += 1
            n_evict = 0
            total = c.execute("SELECT COUNT(*) FROM rules WHERE status='active'").fetchone()[0]
            if total > self.max_rules:
                over = total - self.max_rules
                rows = c.execute(
                    "SELECT id FROM rules WHERE status='active' "
                    "ORDER BY (weight*10 + hits - misses) ASC, hits ASC, created_at ASC, id ASC LIMIT ?",
                    (over,)).fetchall()
                for r in rows:
                    c.execute("UPDATE rules SET status='stale', note=?, updated_at=? WHERE id=?",
                              ("超出规则上限 → 淘汰", now, r["id"]))
                    n_evict += 1
            c.commit()
            return {"stale": n_stale, "evicted": n_evict}
        finally:
            c.close()

    # ── 待学池 ──
    def add_open_question(self, question: str, route: str = "") -> int:
        q = (question or "").strip()[:300]
        if not q:
            return 0
        now = time.time()
        c = self._conn()
        try:
            row = c.execute("SELECT id, count FROM open_questions WHERE question=?", (q,)).fetchone()
            if row:
                c.execute("UPDATE open_questions SET count=count+1, updated_at=? WHERE id=?",
                          (now, row["id"]))
                c.commit()
                return row["id"]
            c.execute("INSERT INTO open_questions (question,route,count,status,created_at,updated_at)"
                      " VALUES (?,?,1,'open',?,?)", (q, route[:40], now, now))
            qid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit()
            return qid
        finally:
            c.close()

    def open_questions(self, limit: int = 20) -> list:
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM open_questions WHERE status='open' "
                "ORDER BY count DESC, updated_at DESC LIMIT ?", (limit,))]
        finally:
            c.close()

    def answer_open_question(self, question: str, answer: str) -> dict:
        """把"待学问题"补成一条答案规则, 并标记该问题已答。"""
        r = self.record_answer(question, answer, source="open_question")
        c = self._conn()
        try:
            c.execute("UPDATE open_questions SET status='answered', updated_at=? WHERE question=?",
                      (time.time(), (question or "").strip()[:300]))
            c.commit()
        finally:
            c.close()
        return r

    # ── 查询 / 人工门 ──
    def _active(self, kind: str) -> list:
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM rules WHERE kind=? AND status='active' "
                "ORDER BY weight DESC, hits DESC, updated_at DESC", (kind,))]
        finally:
            c.close()

    def get(self, rule_id: int) -> dict:
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            return dict(r) if r else {}
        finally:
            c.close()

    def list_rules(self, status: str = None, kind: str = None, limit: int = 100) -> list:
        q, args = "SELECT * FROM rules WHERE 1=1", []
        if status:
            q += " AND status=?"; args.append(status)
        if kind:
            q += " AND kind=?"; args.append(kind)
        q += " ORDER BY weight DESC, hits DESC, updated_at DESC LIMIT ?"; args.append(limit)
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(q, args)]
        finally:
            c.close()

    def activate(self, rule_id: int) -> bool:
        return self._set_status(rule_id, "active", "人工确认")

    def reject(self, rule_id: int, reason: str = "人工否决") -> bool:
        return self._set_status(rule_id, "rejected", reason)

    def forget(self, rule_id: int) -> bool:
        c = self._conn()
        try:
            n = c.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount
            c.commit()
            return bool(n)
        finally:
            c.close()

    def _set_status(self, rule_id: int, status: str, note: str = "") -> bool:
        c = self._conn()
        try:
            n = c.execute("UPDATE rules SET status=?, note=?, updated_at=? WHERE id=?",
                          (status, note[:120], time.time(), rule_id)).rowcount
            c.commit()
            return bool(n)
        finally:
            c.close()

    def stats(self) -> dict:
        c = self._conn()
        try:
            out = {"by_status": {}, "by_kind": {}, "hits": 0, "misses": 0, "open_questions": 0}
            for r in c.execute("SELECT status, COUNT(*) n FROM rules GROUP BY status"):
                out["by_status"][r["status"]] = r["n"]
            for r in c.execute("SELECT kind, COUNT(*) n FROM rules WHERE status='active' GROUP BY kind"):
                out["by_kind"][r["kind"]] = r["n"]
            agg = c.execute("SELECT COALESCE(SUM(hits),0) h, COALESCE(SUM(misses),0) m FROM rules").fetchone()
            out["hits"], out["misses"] = agg["h"], agg["m"]
            out["open_questions"] = c.execute(
                "SELECT COUNT(*) FROM open_questions WHERE status='open'").fetchone()[0]
            return out
        finally:
            c.close()


# ── 单例 ──
_loop = None


def get_learn_loop(data_dir: Path = None) -> LearnLoop:
    global _loop
    if _loop is None:
        _loop = LearnLoop(data_dir)
    return _loop


def reset_learn_loop():
    """给测试/验证器用 (换库)。"""
    global _loop
    _loop = None
