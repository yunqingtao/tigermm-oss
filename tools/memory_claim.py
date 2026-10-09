# -*- coding: utf-8 -*-
"""记忆证据层工具 —— 让"记下来的话"能查出处、能被覆盖、会自己过期。

为什么要有这个文件
═══════════════════════════════════════════════════════════════════
core/memory_claims.py 是本体 (抄 Meta Muse 的记忆三件: 引文/覆盖/置信度), 但没有工具
包它的话, 用户问"你凭什么这么说""我以前怎么交代的"时路由层没东西可接, 只能掉大模型
闲聊 → 编一个像样的答案。本工具把那三条路接出来:

  add       记一条 (必须带原话或来源 —— 无凭据不入库)
  search    查 (默认只出还在生效的)
  explain   一条的完整交代: 正文/引文/来源/覆盖链/引文是否回原库核对过
  supersede 用新说法覆盖旧说法 (旧的留档不删, 能回答"我什么时候改的口")
  stats     库存与治理状态
  harvest   从今天的真实对话里捞出"你教的规矩"(原话照抄当引文)

★ keywords 只放有辨识度的 (教训: P_PLUGIN=500 会抢走别的句子)。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL = {
    "name": "memory_claim",
    "description": ("记忆证据层: 带出处的长期记忆。action: add=记一条(需原话quote或来源) · "
                    "search=查(关键词) · explain=一条的交代(引文/来源/覆盖链/核对) · "
                    "supersede=新说法覆盖旧说法(旧的留档) · stats=库存 · harvest=从今天对话捞你教的规矩。"
                    "★ 说不出出处的记忆本条一律拒收 —— 这是设计, 不是故障。"),
    "keywords": ["记忆证据", "凭什么这么说", "出处", "覆盖旧说法", "捞规矩"],
}


def run(action: str = "search", text: str = "", quote: str = "", source: str = "",
        cid: int = 0, old_id: int = 0, q: str = "", limit: int = 10,
        source_kind: str = "user", message_id: int = 0, confidence: float = 0.7):
    try:
        from core.memory_claims import get_claims, harvest_teachings
    except Exception as e:
        return {"success": False, "output": f"记忆证据层不可用: {e}", "error": str(e)}

    m = get_claims()
    a = (action or "search").strip().lower()

    if a == "add":
        r = m.add(text, quote=quote, source_kind=source_kind, source_ref=source,
                  message_id=(message_id or None), confidence=confidence)
        if not r.get("success"):
            return {"success": False, "output": "没记 —— " + str(r.get("error")), "error": r.get("error")}
        return {"success": True, "output": r["output"], "data": r}

    if a == "search":
        rows = m.search(q or text, limit=int(limit or 10))
        if not rows:
            return {"success": True, "output": f"没查到 (关键词: {q or text or '(全部)'})", "data": []}
        lines = [f"查到 {len(rows)} 条:"]
        for r in rows:
            mark = "✓" if r.get("verified") else "?"
            lines.append(f"  #{r['id']} [{mark}] {r['text'][:60]}  (置信 {r['confidence']:.2f}, 来源 {r['source_kind']})")
        lines.append("看某一条的交代: explain cid=<编号>")
        return {"success": True, "output": "\n".join(lines), "data": rows}

    if a == "explain":
        if not cid:
            return {"success": False, "output": "要交代哪一条? 给编号 cid=<编号>", "error": "缺 cid"}
        r = m.explain(int(cid))
        if not r.get("success"):
            return {"success": False, "output": r.get("error"), "error": r.get("error")}
        ev = r["evidence"]
        lines = [r["output"], f"  原话/来源: {ev['quote'] or '(来源: %s)' % ev['source_ref']}",
                 f"  出处: {ev['source_kind']}"
                 + (f" · 消息 #{ev['message_id']}" if ev.get("message_id") else "")]
        if r["lineage_older"]:
            lines.append("  它覆盖了: " + " / ".join(f"#{x['id']} {x['text'][:30]}" for x in r["lineage_older"]))
        if r["lineage_newer"]:
            lines.append("  被谁覆盖: " + " / ".join(f"#{x['id']} {x['text'][:30]}" for x in r["lineage_newer"]))
        return {"success": True, "output": "\n".join(lines), "data": r}

    if a == "supersede":
        if not old_id or not text:
            return {"success": False, "output": "覆盖要两样: 旧的编号 old_id 和新的说法 text", "error": "缺参"}
        r = m.supersede(int(old_id), text, quote=quote, source_ref=source,
                        message_id=(message_id or None))
        return {"success": bool(r.get("success")), "output": r.get("output") or r.get("error"), "data": r}

    if a == "stats":
        s = m.stats()
        act = s["by_status"].get("active", 0)
        lines = [f"在册 {sum(s['by_status'].values())} 条 (生效 {act} · "
                 f"被覆盖 {s['by_status'].get('superseded', 0)} · 过期 {s['by_status'].get('expired', 0)})",
                 f"其中引文未核对 {s['unverified']} 条 (那些只能算'你说过', 未回原库验过)"]
        return {"success": True, "output": " | ".join(lines), "data": s}

    if a == "harvest":
        import sqlite3
        try:
            db = m.session_db
            if not db.exists():
                return {"success": False, "output": f"会话库不在: {db}", "error": "no session db"}
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=8)
            rows = c.execute("SELECT id, role, content FROM messages ORDER BY id DESC LIMIT 400").fetchall()
            c.close()
        except Exception as e:
            return {"success": False, "output": f"会话库读不了: {e}", "error": str(e)}
        cand = harvest_teachings(rows)
        if not cand:
            return {"success": True, "output": "最近 400 条里没捞到'你教的规矩'类句子。", "data": []}
        n = 0
        for cd in cand[:20]:
            r = m.add(cd["text"], quote=cd["quote"], source_kind="user",
                      source_ref="harvest", message_id=cd["message_id"], confidence=0.7)
            n += 1 if r.get("success") else 0
        return {"success": True,
                "output": f"捞到 {len(cand)} 句, 已入库 {n} 句 (原话当引文, 未改写)。",
                "data": cand}

    if a == "decay":
        d, e = m.decay()
        return {"success": True, "output": f"衰减 {d} 条, 过期 {e} 条 (只碰未过期的)。", "data": {"decayed": d, "expired": e}}

    return {"success": False, "output": f"不认识的 action: {action}", "error": "bad action"}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
