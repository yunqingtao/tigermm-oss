# -*- coding: utf-8 -*-
r"""在途任务工具 —— 回答「做完了吗 / 继续跑 / 到哪了」。

为什么单独一个工具 (别塞进 task_ledger)
═══════════════════════════════════════════════════════════════════
`tools/task_ledger.py` 答的是**汇总** (今天几轮/成败/哪条路由在失败), 它**故意不存原话**,
所以「做完了吗」这类问句接过去只能编 —— 那条边界已经钉成反向用例。
本工具读的是**另一本账** `core/task_flow.py` (在途台账): 它记的是**单件任务的生命周期**
(开单 → 每步 → 关单), 所以能老实回答"那件事现在什么状态"。

★ 诚实边界 (门禁里有反向用例)
   - 没在跑 ⇒ **明说没有**, 不许把"最近完成的那件"说成"在跑的";
   - 在跑 ⇒ 给: 任务原话 / 起了多久 / 走到第几步 / 最后动静是几秒前;
   - 最近的天数太久 ⇒ 说明"那是很久以前那件了", 不要让它看起来像刚跑完。
"""
from __future__ import annotations

import time

TOOL = {
    "name": "task_flow",
    "description": "在途任务台账: 现在在跑哪件活 / 做到哪一步了 / 上一件做完了没。"
                   "action: current=现在在跑的(默认) · last=最近完成的几件。"
                   "★ 答的是**单件任务的状态**, 不是统计 (统计用 task_ledger)。",
    # ★ keywords 只放"默认动作(current)就答对"的说法 (与 tools/memory.py 同一条教训):
    #   plugin.keyword 是 P_PLUGIN=500, 会把命中的词从原话里删掉且**不传 action**,
    #   所以需要特定动作的说法必须交给带 action 的链 (IR chain)。
    # ★ 2026-09-25 补 "你刚才干嘛了": 兜底复盘里第 1 条就是它, 而它问的正是"上一件" ——
    #   默认动作 current 在没在跑时就答"上一件", 恰好对上 (所以配进 keywords)。
    "keywords": ["做完了吗", "做完没", "完成了吗", "完了吗", "到哪了", "进度怎么样",
                 "你刚才干嘛了", "刚才干嘛了", "你刚才干什么了", "刚才干什么了"],
    "params": [
        {"name": "action", "type": "string", "required": False,
         "enum": ["current", "last"],
         "description": "current=现在在跑什么(默认) · last=最近完成的几件"},
        {"name": "limit", "type": "integer", "required": False,
         "description": "last 最多列几件 (默认 5)"},
    ],
    "trigger": ["做完了吗", "做完没", "完成了吗", "完了吗", "到哪了", "在跑什么", "任务状态"],
    "permission": ["read"],
    "category": "ledger",
}


def _ago(ts: float) -> str:
    d = max(0.0, time.time() - float(ts or 0))
    if d < 60:
        return "%d 秒" % int(d)
    if d < 3600:
        return "%d 分钟" % int(d // 60)
    if d < 86400:
        return "%.1f 小时" % (d / 3600)
    return "%.1f 天" % (d / 86400)


def _line(t: dict) -> str:
    n = "%d/%d 步" % (t["done"], t["steps"]) if t["steps"] else ("%d 步" % t["done"])
    tools = (" 用到 " + ", ".join(t["tools"][:4])) if t["tools"] else ""
    return "「%s」 %s%s" % ((t["task"] or "(没说内容)")[:60], n, tools)


def _do_current() -> dict:
    from core import task_flow
    run = task_flow.inflight()
    if run:
        ln = ["现在有 %d 件在跑:" % len(run)]
        for t in run[-5:]:
            ln.append("  " + _line(t))
            ln.append("    起了 %s · 最后动静 %s前"
                      % (_ago(t["started_at"]), _ago(t["last_at"])))
        return {"success": True, "output": "\n".join(ln), "inflight": len(run),
                "tasks": [{"task": t["task"], "done": t["done"], "steps": t["steps"]}
                          for t in run[-5:]]}
    # ★ 没在跑就**明说没有** —— 但顺手把上一件交代清楚 (用户真正想知道的是那件)
    done = task_flow.recent(1)
    if not done:
        return {"success": True, "output": "现在没有在跑的任务 (台账里也还没有完成过任何一件)。"
                                          "你说个活我就能接着办。", "inflight": 0, "tasks": []}
    t = done[0]
    verdict = "成功" if t["ok"] else "没成"
    ln = ["现在没有在跑的任务 —— 上一件已经结束了。",
          "  上一件: %s" % _line(t),
          "  结果: %s%s · %s前结束"
          % (verdict, (" (" + t["note"] + ")") if t["note"] else "", _ago(t["ended_at"]))]
    if time.time() - t["ended_at"] > 86400:
        ln.append("  ★ 注意: 这是 %.0f 天前那件了, 不是刚跑完的。"
                  % ((time.time() - t["ended_at"]) / 86400))
    return {"success": True, "output": "\n".join(ln), "inflight": 0,
            "tasks": [{"task": t["task"], "ok": t["ok"], "ended_at": t["ended_at"]}]}


def _dur(sec: float) -> str:
    """时长给人看的说法。"""
    s = max(0.0, float(sec or 0))
    if s < 60:
        return "%.0f 秒" % s
    if s < 3600:
        return "%.0f 分钟" % (s / 60)
    if s < 86400:
        return "%.1f 小时" % (s / 3600)
    return "%.1f 天" % (s / 86400)


def _do_last(limit: int) -> dict:
    from core import task_flow
    done = task_flow.recent(limit)
    run = task_flow.inflight()
    if not done and not run:
        return {"success": True, "output": "台账里还没有任务记录。它记的是走规则链、"
                                          "技能或任务规划的活。", "tasks": []}
    ln = []
    if run:
        ln.append("现在还有 %d 件在跑:" % len(run))
        for t in run[-3:]:
            ln.append("  " + _line(t))
    ln.append("最近完成 %d 件 (新的在前):" % len(done))
    for t in done:
        ln.append("  %s %s「%s」 耗时 %s%s"
                  % (time.strftime("%m-%d %H:%M", time.localtime(t["ended_at"])),
                     "✓" if t["ok"] else "✗", (t["task"] or "")[:50],
                     _dur(t["ended_at"] - t["started_at"]),
                     (" · " + t["note"]) if t["note"] else ""))
    return {"success": True, "output": "\n".join(ln),
            "tasks": [{"task": t["task"], "ok": t["ok"], "ended_at": t["ended_at"]} for t in done]}


async def run(action: str = "current", limit: int = 5, **kwargs) -> dict:
    """读在途任务台账。见 TOOL['description']。"""
    action = (action or "current").strip().lower()
    try:
        limit = max(1, min(int(limit or 5), 50))
    except (TypeError, ValueError):
        limit = 5
    try:
        if action in ("current", "now", "在跑", "进度"):
            return _do_current()
        if action in ("last", "recent", "最近"):
            return _do_last(limit)
        return {"success": False,
                "error": "未知 action: %s (可用: current / last)" % action}
    except Exception as e:                               # noqa: BLE001
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e)}
