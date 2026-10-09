# -*- coding: utf-8 -*-
"""观察者工具 —— 让那四条后台轴**看得见**, 而不是"悄悄在背后判断我"。

为什么要有这个文件
═══════════════════════════════════════════════════════════════════
core/observers.py 是本体 (抄 Claude Code 的 observer/reconciler 分工: 观察者只提案,
协调器决定什么进主线)。但没有工具暴露它的话, 用户既看不到它在提什么, 也看不到
**它把什么丢了** —— 而"丢了什么、为什么丢"恰恰是这套东西可信度的全部来源
(不透明的判断 = 换个人说了算)。

四个 action:
  run      跑一轮, 看被采纳的提醒 + 被丢掉的(带原因)
  axes     四条轴各自问的是什么
  explain  某条为什么没进 (或者为什么进)
  forget   清"已呈过"记忆 (想让它再提一遍)
  stats    状态
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL = {
    "name": "observers",
    "description": ("后台观察者: 四条轴 (在途单/证据/记忆/技能) 各自提案, 协调器只放行有证据且相关的。"
                    "action: run=跑一轮看采纳与被丢(带原因) · axes=四条轴自述 · "
                    "explain=为什么没进 · forget=清已呈记忆 · stats=状态。"
                    "★ 提案不是结论: 它只提醒(你还有单没关/这句话缺证据), 不替你判断。"),
    "keywords": ["观察者", "后台提醒", "为什么没进主线"],
}


def run(action: str = "run", message: str = "", axis: str = "", limit: int = 3):
    try:
        from core.observers import get_observers
    except Exception as e:
        return {"success": False, "output": f"观察者模块不可用: {e}", "error": str(e)}
    obs = get_observers()
    a = (action or "run").strip().lower()

    if a == "axes":
        rows = obs.axes()
        lines = ["四条观察轴 (只提案, 不替你判断):"]
        for r in rows:
            lines.append(f"  {r['axis']}: {r['问的是什么']}")
        return {"success": True, "output": "\n".join(lines), "data": rows}

    if a == "stats":
        s = obs.stats()
        return {"success": True,
                "output": f"启用 {len(s['axes'])} 条轴 {s['axes']} · 已呈过 {s['seen']} 条",
                "data": s}

    if a == "forget":
        n = obs.reconciler.forget(axis or None)
        return {"success": True, "output": f"已清 {'全部' if not axis else axis + ' 轴'} 的已呈记忆 (还剩 {n} 条)。"}

    if a == "explain":
        r = obs.observe(message=message, commit=False)
        if not axis:
            lines = ["本轮裁定:"]
            for p in r["accepted"]:
                lines.append(f"  进: [{p['axis']}] {p['text'][:70]}")
            for d in r["dropped"]:
                lines.append(f"  丢: [{d['axis']}] {d['text'][:50]} ← {d['why']}")
            if not r["accepted"] and not r["dropped"]:
                lines.append("  (没有任何提案 —— 四条轴都没话说, 这很正常)")
            return {"success": True, "output": "\n".join(lines), "data": r}
        keep = [d for d in r["dropped"] if d["axis"] == axis]
        if keep:
            return {"success": True,
                    "output": f"{axis} 轴本轮全被丢:\n" + "\n".join(f"  {d['why']}" for d in keep),
                    "data": keep}
        hit = [p for p in r["accepted"] if p["axis"] == axis]
        if hit:
            return {"success": True, "output": f"{axis} 轴承采纳 {len(hit)} 条: {hit[0]['text'][:80]}", "data": hit}
        return {"success": True, "output": f"{axis} 轴本轮没有提案 (无话可说, 不是被丢)。", "data": []}

    # run
    r = obs.observe(message=message, commit=False)
    if not r["accepted"] and not r["dropped"]:
        return {"success": True, "output": "四条轴都没提案 —— 没有在途单、没有缺证据的断言、没有相关记忆或技能命中。", "data": r}
    lines = []
    if r["accepted"]:
        lines.append(f"采纳 {len(r['accepted'])} 条 (会进主线的提醒):")
        for p in r["accepted"]:
            lines.append(f"  [{p['axis']}] {p['text'][:90]}")
            lines.append(f"      证据: {p['evidence']}")
    else:
        lines.append("采纳 0 条 (没有够格的提醒)。")
    if r["dropped"]:
        lines.append(f"被丢 {len(r['dropped'])} 条:")
        for d in r["dropped"][:6]:
            lines.append(f"  [{d['axis']}] {d['text'][:50]} ← {d['why']}")
    if limit != 3:
        obs.reconciler.max_accepted = 3
    return {"success": True, "output": "\n".join(lines), "data": r}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
