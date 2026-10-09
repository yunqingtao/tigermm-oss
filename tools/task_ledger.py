# -*- coding: utf-8 -*-
r"""干活账工具 —— 把 `core/task_ledger.py` 已记的账**读出来**给用户。

为什么必须有这个文件 (真缺口, 2026-09-25 只读勘查抓到)
═══════════════════════════════════════════════════════════════════
`core/task_ledger.py` 从 2026-09-23 起就在**每轮真实对话记账** (本机已 1211 轮),
但它是**只写不读**: 对外只有 `stats()` / `tools_seen()` / `render()`, 且**没有任何
工具包它** (grep `task_ledger` 在 tools/ 下零命中)。后果是用户问
「今天干了几轮」「为什么老失败」时, 路由层**没有东西可接** ⇒ 掉大模型闲聊/兜底。

★ 只说账本**真能回答**的 (诚实边界, 不许吹):
  能答: 干了多少轮 / 成多少败多少 / 哪类活多 / 哪条路由在失败 / 用到了哪些能力。
  **答不了**: "做完了吗""继续跑" —— 那问的是**某一件事**的在途状态, 账本只有
  聚合 (类别/路由/成败/天), **不存原话也不存单件任务状态** (隐私口径是设计出来的)。
  所以这类问句**不许**被路由到本工具 —— 接过去只能编一个像样的答案, 那是撒谎。
  这一点钉在 verify_task_ledger 的反向用例里。
"""
from __future__ import annotations

import time

TOOL = {
    "name": "task_ledger",
    "description": "干活账 (本机每天真实干成的活): 轮次/成败/类别/路由/用过的能力。"
                   "action: today=今天的账 · failed=失败集中在哪 (最近 N 天) · "
                   "routes=路由分布 · tools=用过的能力清单。"
                   "★ 只答**汇总**: 单件任务做完没做完/在途状态它不知道, 别用它答那个。",
    # ★ keywords 想清楚再放 (与 tools/memory.py 同一条教训):
    #   `plugin.keyword` 是 P_PLUGIN=500, 而 learn.teach 是 P_LEARN_TEACH=370 ⇒
    #   这里放"记住/默认/以后"会把**教规矩**的句子抢走。本工具一个都不放。
    "keywords": ["干活账", "账本", "干了几轮", "干了多少活", "失败统计", "成功率"],
    "params": [
        {"name": "action", "type": "string", "required": False,
         "enum": ["today", "failed", "routes", "tools"],
         "description": "today=今天的账(默认) · failed=失败集中在哪 · routes=路由分布 · tools=能力清单"},
        {"name": "days", "type": "integer", "required": False,
         "description": "failed/routes 回看几天 (默认 7, 上限 90)"},
        {"name": "limit", "type": "integer", "required": False,
         "description": "最多列几条 (默认 10)"},
    ],
    "trigger": ["干活账", "账本", "干了几轮", "干了多少活", "失败统计", "成功率"],
    "permission": ["read"],
    "category": "ledger",
}


def _ledger():
    """拿账本单例。库路径受 `TMM_TASK_DB` 覆盖 (门禁/探针必须指到临时库)。"""
    from core.task_ledger import get_task_ledger
    return get_task_ledger()


def _rows_for(c, days, limit):
    return c.execute(
        "SELECT category, route, ok, SUM(count) n FROM tasks "
        "WHERE day >= date('now', ?) GROUP BY category, route, ok "
        "ORDER BY n DESC LIMIT ?", ("-%d day" % days, int(limit))).fetchall()


def _do_today(limit: int) -> dict:
    led = _ledger()
    st = led.stats()
    if not st.get("turns"):
        return {"success": True, "output": "干活账还是空的 —— 只在**真实对话**里干成活才记 "
                                          "(测试/门禁不写)。用一阵子再看就有数了。",
                "turns": 0}
    day = time.strftime("%Y-%m-%d")
    with led._closed() as c:
        r = c.execute("SELECT COALESCE(SUM(count),0) n, "
                      "COALESCE(SUM(CASE WHEN ok=1 THEN count END),0) ok "
                      "FROM tasks WHERE day=?", (day,)).fetchone()
        cats = c.execute("SELECT category, SUM(count) n FROM tasks WHERE day=? "
                         "GROUP BY category ORDER BY n DESC LIMIT ?", (day, int(limit))).fetchall()
    n, ok = int(r["n"] or 0), int(r["ok"] or 0)
    ln = ["今天 (%s) 干了 %d 轮: 成 %d / 败 %d" % (day, n, ok, n - ok)]
    if cats:
        ln.append("  主要活: " + " · ".join("%s %d" % (x["category"], x["n"]) for x in cats))
    ln.append("  累计: %d 轮 · 成功率 %.0f%% · 覆盖 %d 类活"
              % (st["turns"], st["rate"] * 100, st["kinds"]))
    return {"success": True, "output": "\n".join(ln), "turns": n, "ok": ok,
            "total": st["turns"], "rate": st["rate"]}


def _do_failed(days: int, limit: int) -> dict:
    led = _ledger()
    with led._closed() as c:
        rows = _rows_for(c, days, 200)
        tot = c.execute("SELECT COALESCE(SUM(count),0) n, "
                        "COALESCE(SUM(CASE WHEN ok=1 THEN count END),0) ok "
                        "FROM tasks WHERE day >= date('now', ?)", ("-%d day" % days,)).fetchone()
    n, ok = int(tot["n"] or 0), int(tot["ok"] or 0)
    bad = [r for r in rows if not int(r["ok"] or 0)]
    if not n:
        return {"success": True, "output": "最近 %d 天账上是空的 (没记到活)。" % days, "turns": 0}
    head = "最近 %d 天: %d 轮 · 败 %d (%.0f%%)" % (days, n, n - ok, (n - ok) / n * 100 if n else 0)
    if not bad:
        return {"success": True, "output": head + "\n  没有失败的活 —— 这 %d 天全成。" % days,
                "turns": n, "failed": 0}
    ln = [head, "  失败集中在:"]
    for r in bad[:int(limit)]:
        ln.append("    %-14s %-22s %d 轮" % (r["category"], r["route"], int(r["n"])))
    ln.append("  (账本只记**类别/路由**这一层, 不存你的原话 —— 要具体某轮请说个大概时间/关键词)")
    return {"success": True, "output": "\n".join(ln), "turns": n, "failed": n - ok,
            "by_route": [{"category": r["category"], "route": r["route"], "n": int(r["n"])}
                         for r in bad[:int(limit)]]}


def _do_routes(days: int, limit: int) -> dict:
    led = _ledger()
    with led._closed() as c:
        rows = c.execute("SELECT route, SUM(count) n FROM tasks WHERE day >= date('now', ?) "
                         "GROUP BY route ORDER BY n DESC LIMIT ?", ("-%d day" % days, int(limit))).fetchall()
    if not rows:
        return {"success": True, "output": "最近 %d 天账上是空的。" % days, "routes": []}
    ln = ["最近 %d 天的路由分布 (前 %d 条):" % (days, len(rows))]
    for r in rows:
        ln.append("    %-26s %d 轮" % (r["route"], int(r["n"])))
    return {"success": True, "output": "\n".join(ln),
            "routes": [{"route": r["route"], "n": int(r["n"])} for r in rows]}


def _do_tools() -> dict:
    led = _ledger()
    tl = led.tools_seen()
    if not tl:
        return {"success": True, "output": "账上还没记到用过的能力。", "tools": []}
    return {"success": True, "output": "用过的能力 (%d): %s" % (len(tl), ", ".join(tl)),
            "tools": tl}


async def run(action: str = "today", days: int = 7, limit: int = 10, **kwargs) -> dict:
    """读干活账。见 TOOL['description']。"""
    action = (action or "today").strip().lower()
    try:
        days = max(1, min(int(days or 7), 90))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        limit = 10

    try:
        if action in ("today", "摘要", "summary"):
            return _do_today(limit)
        if action in ("failed", "fail", "失败"):
            return _do_failed(days, limit)
        if action in ("routes", "route", "路由"):
            return _do_routes(days, limit)
        if action in ("tools", "能力"):
            return _do_tools()
        return {"success": False,
                "error": "未知 action: %s (可用: today / failed / routes / tools)" % action}
    except Exception as e:
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e)}
