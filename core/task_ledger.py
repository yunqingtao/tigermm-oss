"""日常干活账 —— 「这台机器上到底攒下了什么」的底账 (2026-09-23 立)

为什么需要它 (与既有账本的分工, 不重叠)
═══════════════════════════════════════════════════════════════════
  learn_loop.learnings   **学到的** —— 用户教的偏好/纠正   (只在你"教它"时才记)
  learn_loop.rules       同上, 可执行的规则               (同上: 触发面极窄)
  gap_ledger.gaps        **干不成的** —— 能力缺口         (只在失败时记)
  skill_usage.jsonl      **技能维度**的使用价值           (只在技能被调用时记)
  ─────────────────────────────────────────────────────────────
  task_ledger (本模块)   **干成的日常活** ← 缺的就是这一路

缺环 (2026-09-23 查盘实测):
  真人用了 50 天, learnings=2 / rules=0 / gaps=2 (还都是测试留下的)。
  根因不是管道坏 (技能账 115 条正常在记), 而是**触发面太窄** ——
  前四本账都只在"出事"或"被教"时才写, 用户**正常用它把活干成了, 什么都不记**。
  ⇒ 用得越多账越空, 看不出"能干什么、干得怎么样"。

它记什么 (每次真实对话轮 → 一条)
───────────────────────────────────────────────────────────────
  category  能力类别 (写文件/查询/搜索/规划/技能:xx/问答/…)  ← **规则派生, 不是原话**
  route     谁干的
  tools     用了哪些工具 (能力维度)
  ok        成没成
  day       YYYY-MM-DD (同类按天聚合)
  elapsed   耗时

★ 隐私口径 (重要): **只存派生类别与工具名, 不存用户原话/路径/内容**。
  理由: 这些账要能"随包带走、新机不重学"(用户要求)。存原话就没法带走。
  需要看"具体哪句话"时去 data/chat_sessions.db (那本账本就不出门)。

★ 探针安全: 路径受 TMM_TASK_DB 控制 (被 _probe_env 隔离);
  写入受 TMM_LIVE=1 正面信号门禁 (与 skill_usage 同一条纪律: 默认不写)。

自检: python core/task_ledger.py
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

logger = logging.getLogger("core.task_ledger")

ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "task_ledger.db"
_REAL_DB = ROOT / "data" / DB_NAME

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,      -- 能力类别 (派生, 非原话)
    route       TEXT,               -- 谁干的
    ok          INTEGER NOT NULL,   -- 1 成 / 0 败
    tools       TEXT,               -- 用了哪些工具 (逗号分隔)
    day         TEXT NOT NULL,      -- YYYY-MM-DD
    count       INTEGER DEFAULT 1,  -- 同类+同路+同结果+同天 → 聚计
    elapsed_ms  INTEGER,
    created_at  REAL,
    updated_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_key
    ON tasks(category, route, ok, day);
CREATE INDEX IF NOT EXISTS idx_tasks_day ON tasks(day);
CREATE INDEX IF NOT EXISTS idx_tasks_cat ON tasks(category);
"""


def resolve_db(db_path=None) -> Path:
    """决定用哪个账本文件 (验证/门禁隔离, 与 gap_ledger.resolve_gap_db 同源规则)。"""
    if db_path is None:
        db_path = _REAL_DB
    env = os.environ.get("TMM_TASK_DB")
    if env:
        try:
            if Path(db_path).resolve() == _REAL_DB.resolve():
                return Path(env)
        except Exception:
            pass
    return Path(db_path)


# ═══════════════════════════════════════════════════════════════════
# 纯函数: 分类 + 成败判定 (无副作用 → 可重复调用, 便于门禁)
# ═══════════════════════════════════════════════════════════════════

#: 回复里的**失败信号** (工具真跑了但失败) —— 取自 gap_ledger 同族口径
_FAIL_SIG = re.compile(
    r"✗|没执行成功|执行失败|失败[:：]|Error[:：]|Traceback|Access denied|"
    r"未找到|could not|not found|timeout|拒绝|无法访问|不支持"
)
#: 明确的"在追问用户要输入" —— 这是健康交互, 不算失败
_ASKING = re.compile(r"请提供|请带上|缺必填参数|需要你|请给|请补充|请问|告诉我")

#: 类别规则 (按序匹配 route, 命中即止; 都用 route/工具名判断, 不看用户原话)
_ROUTE_CATS = (
    ("planner.multi_step", "规划任务"),
    ("understand.request", "自然话理解"),
    ("save.referent_gen", "自己生成+落盘"),
    ("skill.trigger_first", "技能"),
    ("skill.dag_catchall", "技能"),
    ("skill_dag", "技能"),
    ("search.web", "搜索"),
    ("open.site", "打开站点"),
    ("plugin.keyword", "查询"),
    ("plugin.geocode", "查询"),
    ("analysis.office_first", "表格/文档分析"),
    ("analysis.preread_reuse", "文档追问"),
    ("analysis.goto_model", "文档分析"),
    ("ir.chain", "规则链"),
    ("email.regex_send", "邮件"),
    ("url.open", "打开链接"),
    ("fts.search", "历史检索"),
    ("brain.route", "本地决策"),
    ("route.smart", "复杂问答"),
    ("learned.answer", "学过的答案"),
    ("lookup.prefill", "缺信息自查"),
    ("model.fallback", "问答"),
    ("l4.knowledge", "本地知识"),
    ("cmd.", "命令"),
)
#: 工具名 → 类别 (route 判不出来时的兜底; 也用于"能力维度")
_TOOL_HINT = (
    ("file_ops", "文件"),
    ("tiger_office", "办公文档"),
    ("web_search", "搜索"),
    ("check_mail", "邮件"),
    ("send_email", "邮件"),
    ("openmeteo", "天气"),
    ("system_info", "系统信息"),
    ("windows_desktop", "截图"),
    ("chart", "图表"),
    ("image_gen", "生成图"),
    ("vision", "看图"),
    ("kb_", "知识库"),
    ("plantuml", "图表"),
)


def categorize(message: str, route: str = "", tools=None) -> str:
    """把一轮对话归到**能力类别** (派生标签, 不含用户原话)。

    ★ 只用 route 与工具名判断 —— 这样账本能随包带走 (不夹私话)。
    """
    r = str(route or "")
    for key, cat in _ROUTE_CATS:
        if key in r:
            if cat == "技能":
                # 技能名比"技能"两个字有用得多 → 取 route 里的具体技能
                return f"技能"
            return cat
    for t in (tools or []):
        tl = str(t).lower()
        for key, cat in _TOOL_HINT:
            if key in tl:
                return cat
    if not r:
        return "未分类"
    return "通用"


def judge_ok(response: str, route: str = "") -> bool:
    """这一轮**干成了吗**。纯函数。

    判据: ① 回复里有失败信号 → 没成
          ② (例外) 系统在追问用户要输入 → 那是健康交互, 算"完成了一轮"(ok)
          ③ 路由是 error → 没成
          ④ 其余 → 成
    """
    resp = str(response or "")
    r = str(route or "")
    if _ASKING.search(resp):
        return True
    if _FAIL_SIG.search(resp):
        return False
    if r in ("error",):
        return False
    return bool(resp.strip())


def classify_turn(message: str, response: str, route: str = "", tools=None,
                  elapsed: float = 0.0) -> dict | None:
    """判定这一轮要不要记账、记成什么。返回 dict 或 None (= 不记)。纯函数。

    不记的情况: 空消息 / 斜杠命令 (那是命令用法, 不是"干了一件活")。
    """
    msg = str(message or "").strip()
    if not msg:
        return None
    if msg.startswith(("/", "@", "!", "！")):
        return None            # ★ 命令与点名不是"日常活" (实测: /ledger 被记成"命令" 1 轮)
    return {
        "category": categorize(msg, route, tools),
        "route": str(route or "")[:60],
        "ok": 1 if judge_ok(response, route) else 0,
        "tools": ",".join(sorted({str(t) for t in (tools or []) if t}))[:200],
        "day": date.today().isoformat(),
        "elapsed_ms": int(float(elapsed or 0) * 1000),
    }


# ═══════════════════════════════════════════════════════════════════
# 账本 (存储 + 聚合)
# ═══════════════════════════════════════════════════════════════════

class TaskLedger:
    """日常干活账。SQLite, 跨会话存活, 同类按天聚合 (表不会无限膨胀)。"""

    def __init__(self, data_dir: Path = None, db_path: Path = None):
        base = Path(data_dir) if data_dir else (ROOT / "data")
        self.db_path = resolve_db(db_path if db_path is not None else (base / DB_NAME))
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ── 写 ──
    def record(self, message: str, response: str, route: str = "", tools=None,
               elapsed: float = 0.0) -> dict:
        """记一轮。★ 失败即闭: 只有真人会话才记 (与 skill_usage 同一条纪律)。"""
        if os.environ.get("TMM_PROBE") in ("1", "true", "yes"):
            return {"ok": False, "error": "probe 流量不记账"}
        if os.environ.get("TMM_LIVE") not in ("1", "true", "yes"):
            return {"ok": False, "error": "非真人会话不记账 (缺 TMM_LIVE)"}
        rec = classify_turn(message, response, route, tools, elapsed)
        if not rec:
            return {"ok": False, "error": "无需记账"}
        now = time.time()
        try:
            with self._conn() as c:
                cur = c.execute(
                    "SELECT id, count FROM tasks WHERE category=? AND route=? AND ok=? AND day=?",
                    (rec["category"], rec["route"], rec["ok"], rec["day"]))
                row = cur.fetchone()
                if row:
                    c.execute("UPDATE tasks SET count=count+1, updated_at=?, elapsed_ms=? WHERE id=?",
                              (now, rec["elapsed_ms"], row["id"]))
                else:
                    c.execute(
                        "INSERT INTO tasks (category, route, ok, tools, day, count,"
                        " elapsed_ms, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,1,?,?,?)",
                        (rec["category"], rec["route"], rec["ok"], rec["tools"],
                         rec["day"], rec["elapsed_ms"], now, now))
        except Exception as e:
            logger.debug("task_ledger 写入失败: %s", e)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "record": rec}

    # ── 读 ──
    def stats(self) -> dict:
        """总账: 轮次/成败/类别/路由/天。"""
        with self._conn() as c:
            tot = c.execute("SELECT COALESCE(SUM(count),0) n,"
                            " COALESCE(SUM(CASE WHEN ok=1 THEN count END),0) ok,"
                            " COUNT(*) kinds, MIN(day) d0, MAX(day) d1 FROM tasks").fetchone()
            by_cat = c.execute(
                "SELECT category, SUM(count) n, SUM(CASE WHEN ok=1 THEN count END) ok"
                " FROM tasks GROUP BY category ORDER BY n DESC").fetchall()
            by_day = c.execute(
                "SELECT day, SUM(count) n, SUM(CASE WHEN ok=1 THEN count END) ok"
                " FROM tasks GROUP BY day ORDER BY day DESC LIMIT 14").fetchall()
            by_route = c.execute(
                "SELECT route, SUM(count) n FROM tasks GROUP BY route"
                " ORDER BY n DESC LIMIT 12").fetchall()
        n = int(tot["n"] or 0)
        return {
            "turns": n,
            "ok": int(tot["ok"] or 0),
            "fail": n - int(tot["ok"] or 0),
            "rate": round((int(tot["ok"] or 0) / n), 3) if n else 0.0,
            "kinds": int(tot["kinds"] or 0),
            "first_day": tot["d0"], "last_day": tot["d1"],
            "by_category": [dict(r) for r in by_cat],
            "by_day": [dict(r) for r in by_day],
            "by_route": [dict(r) for r in by_route],
        }

    def tools_seen(self) -> list:
        """出现过哪些工具 (能力维度: 这台机器会干哪些活)。"""
        seen = set()
        with self._conn() as c:
            for r in c.execute("SELECT tools FROM tasks WHERE tools != ''"):
                for t in str(r["tools"]).split(","):
                    if t.strip():
                        seen.add(t.strip())
        return sorted(seen)

    def render(self) -> str:
        """给人看的一页 ( /ledger ) —— 纯文本, 干净, 不带调试痕迹。"""
        st = self.stats()
        if not st["turns"]:
            return ("干活账: 还是空的。\n"
                    "  (只有在**真实对话**里干成活才记账 —— 测试/门禁不写。\
用一阵子再看就有数了。)")
        ln = ["干活账 (日常干成的活)"]
        ln.append(f"  共 {st['turns']} 轮 · 成 {st['ok']} / 败 {st['fail']}"
                  f" · 成功率 {st['rate'] * 100:.0f}%")
        ln.append(f"  第一轮 {st['first_day']} · 最近 {st['last_day']} · 覆盖 {st['kinds']} 类活")
        if st["by_category"]:
            ln.append("  按类别:")
            for r in st["by_category"][:10]:
                n, ok = int(r["n"]), int(r["ok"] or 0)
                ln.append(f"    {r['category']:<14} {n:>5} 轮 · 成 {ok}"
                          f" ({(ok / n * 100):.0f}%)".replace("  (0%)", "") if n else "")
        tl = self.tools_seen()
        if tl:
            ln.append(f"  用过的能力 ({len(tl)}): " + ", ".join(tl[:14])
                      + ("…" if len(tl) > 14 else ""))
        if st["by_day"]:
            ln.append("  最近几天:")
            for r in st["by_day"][:7]:
                ln.append(f"    {r['day']}  {int(r['n']):>4} 轮")
        return "\n".join(ln)


# ═══════════════════════════════════════════════════════════════════
# 单例 + 自检
# ═══════════════════════════════════════════════════════════════════

_LEDGER: TaskLedger | None = None


def get_task_ledger(data_dir: Path = None) -> TaskLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = TaskLedger(data_dir)
    return _LEDGER


def reset_task_ledger():
    global _LEDGER
    _LEDGER = None


def _self_test() -> int:
    """自检: ① 分类/成败是纯函数且判得对 ② 门禁真挡住非真人会话 ③ 聚合正确。"""
    import tempfile
    P, F = [], []

    def chk(n, c, d=""):
        (P if c else F).append(n)
        print(("  PASS " if c else "  FAIL ") + n + (("   <- " + str(d)) if d and not c else ""))

    print("=" * 74)
    print("task_ledger 自检")
    print("=" * 74)

    # ① 纯函数
    chk("① 分类: 规划链 → 规划任务", categorize("x", "planner.multi_step") == "规划任务")
    chk("① 分类: 技能路由 → 技能", categorize("x", "skill.trigger_first") == "技能")
    chk("① 分类: fallback → 问答", categorize("x", "model.fallback") == "问答")
    chk("① 分类: route 空但有 file_ops → 文件", categorize("x", "", ["file_ops"]) == "文件")
    chk("① 成败: 正常回复 → 成", judge_ok("已经写好放到桌面了", "planner.multi_step") is True)
    chk("① 成败: ✗ 失败信号 → 败", judge_ok("✗ 写文件 没执行成功", "l4.knowledge") is False)
    chk("① 成败: 追问用户 → 成 (健康交互)",
        judge_ok("请提供收件人、主题和内容", "model.fallback") is True)
    chk("① 分类不含用户原话 (隐私)",
        "青山绿水" not in str(classify_turn("写一首关于青山绿水的诗", "好了", "planner.multi_step")))
    chk("① 斜杠命令不记账 (/ledger)", classify_turn("/ledger", "干活账…", "cmd.ledger") is None)
    chk("① @点名 不记账", classify_turn("@deepseek 你好", "你好", "model.explicit") is None)
    chk("① 空消息不记账", classify_turn("   ", "x", "model.fallback") is None)

    # ② 门禁
    _iso = Path(tempfile.mkdtemp(prefix="tmm_task_ledger_"))
    os.environ["TMM_TASK_DB"] = str(_iso / "t.db")
    os.environ.pop("TMM_LIVE", None)
    os.environ.pop("TMM_PROBE", None)
    lg = TaskLedger(db_path=_iso / "t.db")
    r1 = lg.record("写一首诗放桌面", "好了", "planner.multi_step")
    chk("② 无 TMM_LIVE → 不记账 (失败即闭)", r1.get("ok") is False, r1)
    os.environ["TMM_PROBE"] = "1"
    os.environ["TMM_LIVE"] = "1"
    r2 = lg.record("写一首诗放桌面", "好了", "planner.multi_step")
    chk("② 有 TMM_PROBE → 仍不记 (probe 优先)", r2.get("ok") is False, r2)

    # ③ 聚合
    os.environ.pop("TMM_PROBE", None)
    lg.record("写一首诗放桌面", "好了", "planner.multi_step")
    lg.record("再写一首", "好了", "planner.multi_step")          # 同类同天 → 聚计
    lg.record("查天气", "北京 晴", "plugin.keyword")             # 另一类
    lg.record("写文件", "✗ 没执行成功", "l4.knowledge")          # 失败
    st = lg.stats()
    chk("③ 总轮次 = 4", st["turns"] == 4, st["turns"])
    chk("③ 成 3 / 败 1", st["ok"] == 3 and st["fail"] == 1, (st["ok"], st["fail"]))
    chk("③ 同类聚计 (规划任务 2 轮)", any(r["category"] == "规划任务" and r["n"] == 2
                                        for r in st["by_category"]), st["by_category"])
    chk("③ 渲染不含空行/乱码", "干活账" in lg.render() and "\n\n" not in lg.render())
    os.environ.pop("TMM_TASK_DB", None)
    os.environ.pop("TMM_LIVE", None)
    import shutil
    shutil.rmtree(_iso, ignore_errors=True)

    print("\n结果: %d PASS / %d FAIL" % (len(P), len(F)))
    for x in F:
        print("  -", x)
    return 1 if F else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
