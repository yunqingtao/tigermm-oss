# -*- coding: utf-8 -*-
"""出口闸门运维工具 —— 让用户能**看得见、批得了**那道闸。

为什么要有这个文件 (真缺口)
═══════════════════════════════════════════════════════════════════
core/egress_guard.py 是闸门本体, 但它天然会拦下"带内容外发"的动作, 而拦下之后
必须有人能看到、能批准 —— 否则我用大模型时只会遇到"这个动作没执行", 却不知道
去哪儿批, 闸门就成了纯粹的功能缺失。

本工具暴露四件事 (全部只读/无需联网):
  · stats   闸门当前状态: 模式 / 是否带污点 / 待批几笔 / 有效凭证几张
  · list    待批队列 (谁想发什么出去, 为什么被拦)
  · approve/deny  ← 批准=给一张授权凭证 (carrier 当天有效, destructive 一次性)
  · audit   最近的出口决策流水 (含 guard_error 那种"闸门自身故障"的记录)
  · secrets 凭据代管在册的**名字**清单 (永不返回值) + 用钥流水

★ keywords 只放有辨识度的词 (教训: P_PLUGIN=500 会把别的句子抢走)。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL = {
    "name": "egress",
    "description": ("出口闸门: 查看/批准本机数据外发动作。"
                    "action: stats=闸门状态 · list=待批队列 · approve=批准(给凭证) · "
                    "deny=拒绝 · audit=决策流水 · secrets=在册凭据名(不含值) · "
                    "mode=切换模式(ask/auto_review/untrusted/yolo)。"
                    "★ 带内容的动作被拦下时用它, 别以为是功能坏了。"),
    "keywords": ["出口闸门", "待批队列", "凭据代管", "egress"],
}


def _llm_out(output, data=None):
    d = {"success": True, "output": output}
    if data is not None:
        d["data"] = data
    return d


def run(action: str = "stats", pid: str = "", note: str = "", mode: str = "", n: int = 20):
    try:
        from core.egress_guard import get_guard
        from core.secret_broker import get_broker
    except Exception as e:
        return {"success": False, "output": f"闸门模块不可用: {e}", "error": str(e)}

    g = get_guard()
    a = (action or "stats").strip().lower()

    if a in ("stats", ""):
        s = g.stats()
        lines = [
            f"闸门模式: {s['mode']}",
            f"本会话污点: {'有' if s['tainted'] else '无'}"
            + (f" ({'; '.join(s['taint_reasons'])})" if s["taint_reasons"] else ""),
            f"待批: {s['pending']} 笔 · 有效凭证: {s['grants']} 张",
        ]
        if s["pending"]:
            lines.append("有动作在排队等你批 —— 说\"看下待批队列\"就能看到。")
        return _llm_out(" | ".join(lines), s)

    if a in ("list", "pending"):
        items = g.list_pending()
        if not items:
            return _llm_out("待批队列是空的 (没有动作在等你批)。", [])
        lines = [f"待批 {len(items)} 笔:"]
        for it in items:
            lines.append(f"  #{it['id']} [{it['klass']}] {it['summary']} —— {it['reason']}")
        lines.append("批准: /tool egress approve pid=<编号>  拒绝: ... deny pid=<编号>")
        return _llm_out("\n".join(lines), items)

    if a == "approve":
        if not pid:
            return {"success": False, "output": "要批哪一笔? 给个编号 (先 action=list 看队列)", "error": "缺 pid"}
        grant = g.approve(pid, note)
        if not grant:
            return {"success": False, "output": f"队列里没有 #{pid} 这笔 (可能已被批/已过期)", "error": "not found"}
        kind = "一次性" if grant.get("one_shot") else "当天有效"
        return _llm_out(f"已批准 #{pid} → {grant['tool']} 拿到一张{kind}凭证。再让它做那个动作就会放行。", grant)

    if a == "deny":
        if not pid:
            return {"success": False, "output": "要拒哪一笔? 给个编号", "error": "缺 pid"}
        ent = g.deny(pid, note)
        if not ent:
            return {"success": False, "output": f"队列里没有 #{pid} 这笔", "error": "not found"}
        return _llm_out(f"已拒绝 #{pid} ({ent['tool']}) —— 不执行, 也不留授权。", ent)

    if a in ("audit", "log"):
        tail = g.audit_tail(int(n or 20))
        if not tail:
            return _llm_out("还没有出口决策记录。", [])
        lines = [f"最近 {len(tail)} 笔出口决策:"]
        for e in tail:
            lines.append(f"  {e['ts']} [{e['decision']}] {e['tool']}({e['klass']}) {e['reason'][:70]}")
        errs = [e for e in tail if e["kind"] == "guard_error"]
        if errs:
            lines.append(f"★ 其中 {len(errs)} 笔是闸门自身故障放行 (要修, 不是放行合理)")
        return _llm_out("\n".join(lines), tail)

    if a in ("secrets", "vault"):
        b = get_broker()
        names = b.names()
        tail = b.audit_tail(int(n or 10))
        lines = [f"凭据代管在册 {len(names)} 项 (只列名字, 永不显示值):",
                 "  " + (", ".join(names) if names else "(空 —— keys.json 里没有可代管的项)")]
        if tail:
            lines.append("最近用钥流水:")
            for e in tail:
                lines.append(f"  {e['ts']} {e['direction']} {e['tool']} {e['names']}")
        lines.append("用法: 参数里写 {{secret:名字}} 即可, 真值由闸门在执行前一刻换上, "
                     "工具返回里出现的真值会被换回占位符。")
        return _llm_out("\n".join(lines), {"names": names, "audit": tail})

    if a == "mode":
        m = (mode or "").strip().lower()
        if m not in ("ask", "auto_review", "untrusted", "yolo"):
            return {"success": False, "output": "模式只有四个: ask / auto_review / untrusted / yolo",
                    "error": "bad mode"}
        g.policy["mode"] = m
        import json as _j
        g.policy_path.parent.mkdir(parents=True, exist_ok=True)
        g.policy_path.write_text(_j.dumps(g.policy, ensure_ascii=False, indent=2), encoding="utf-8")
        return _llm_out(f"闸门模式已切到 {m} (写进 {g.policy_path.name}, 重启后仍是这档)。", g.policy)

    if a in ("untaint", "clear"):
        g.clear_taint()
        return _llm_out("已清掉本会话污点 (只在你确认那些数据已经处理完/不外发时用)。", {})

    return {"success": False, "output": f"不认识的 action: {action}", "error": "bad action"}


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(run(), ensure_ascii=False, indent=2))
