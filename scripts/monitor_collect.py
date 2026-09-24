# -*- coding: utf-8 -*-
r"""monitor_collect — 日常对话「全量监控」采集 + 自动症状识别 (只读)。

用户的用法 (2026-09-24 定):
    用户**只管提问**, 不标对错、不判是否执行; 全部由采集器 + 每日复盘来发现。
所以这一层必须**完全自动**: 不依赖任何人工标记。

数据来源 (全部既有的, 不新增采集负担):
    data/chat_sessions.db   messages  每轮原话/回复/路由/模型/耗时/时间
    data/skill_usage.jsonl            技能执行 ok/route/note
    data/artifacts.jsonl              产物落盘 (path/at/source)
    data/task_ledger.db    tasks      按日聚合的类别/路由/成功/耗时

产出:
    docs/monitor/<YYYY-MM-DD_HHMM>.md   给人看的日报 (复盘用)
    data/monitor/<YYYY-MM-DD_HHMM>.json 结构化 (机器用)
    data/monitor_cursor.json            游标 (下次只看新增)

用法:
    python -B scripts/monitor_collect.py            # 只处理游标之后的新增
    python -B scripts/monitor_collect.py --all      # 全量 (首次/重算)
    python -B scripts/monitor_collect.py --since 24h
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT_MD = ROOT / "docs" / "monitor"
OUT_JSON = DATA / "monitor"
CURSOR = DATA / "monitor_cursor.json"

# ── 症状判据 (自动, 不靠人工标记) ────────────────────────────────────────
RE_FAIL_TEXT = re.compile(
    r"(^\s*[✗❌]|执行未全部成功|没执行成功|File not found|Traceback|"
    r"EOF when reading|PermissionError|Access denied|"
    r"^\s*Step\s+\d+\s*[（(]|失败[:：]|执行失败|无法执行)"
)
RE_ASK_BACK = re.compile(r"(请补充|请先告诉|需要我帮你|要我帮|你想让|哪个文件|什么意思|不清楚|没听清)")
# 用户消息里带"要做一件事"的迹象 (用于"说了要做但没产物"的判定)
RE_TASKISH = re.compile(
    r"(写|做|画|生成|保存|存到|存到|导出|整理|分析|列出|列出|搜索|发|建|创建|改|修)"
)
RE_PATH = re.compile(r"[A-Za-z]:[\\/]")
SLOW_SEC = 20.0
HOLLOW_LEN = 12

# ── 失败分层 (用户 2026-09-24 定调) ──────────────────────────────────────
# 用户的话: "我只负责每天给他各种类型的测试, 看链路通不通; 具执行对不不对,
#            那是**工具和 skill 的问题**, 再就是**分配器**的问题, 或者**链路环境**,
#            或者**其他配置**。咱主要就是纠偏。"
# ⇒ 日报不能按"用户觉得对不对"组织, 要按**失败发生在哪一层**组织, 修才对得上方子。
LAYER_OF_SYMPTOM = {
    "失败/报错": "① 链路断裂",        # 抛异常/甩栈/工具直接失败
    "回复空洞": "① 链路断裂",
    "只念未做": "② 路由错配",         # 该执行却去读/该技能却给模型
    "反问/要补充": "② 路由错配",
    "说了要做但没产物": "③ 工具/技能",  # 路走对了但产出不对/没有
    "未沉淀路由": "④ 未沉淀",          # 没有专属路由, 落到大模型现场指挥
    "慢": "⑤ 性能",
}
#: 「模型直答/兜底」= 没有专属处理器, 由大模型临场回答 (纠偏的主要目标)
#   ★ 2026-09-24 修正 (自测发现指标假绿): 一开始只算 model.fallback/understand.request,
#   结果显示"真路由 100%" —— 因为真正的兜底大头是 `general` (105 轮!),
#   它是**模型直接回答、没走任何工具**, 从纠偏角度看正是"该沉淀没沉淀"。
#   必须把它算进兜底, 否则每一天都是"100% 健康"的假象。
FALLBACK_INTENTS = {"model.fallback", "understand.request", "general", "ask", "chat", "identity"}


def _connect_ro(db: Path, attempts: int = 4):
    """优先正常只读; 失败退回 immutable (引擎在跑时更稳)。

    ★★ 2026-09-24 加**重试** (真踩到): 引擎持续写 WAL 时, 新进程偶尔会打不开
      —— 实测同一分钟内 `mode=ro` 报 `database disk image is malformed` /
      `file is not a database`, 而 `immutable=1` 却能读, 过几秒又全好。
      这是**瞬时争用**, 不是数据损坏。采集器是**无人值守定时任务**,
      绝不能因此崩掉 (否则当晚复盘直接失败), 所以: 退避重试 + 明确报错。

    返回 (connection, 用的模式名)。读不了 → 抛 RuntimeError 并带上诊断信息。
    """
    import time as _time
    last = None
    for _i in range(max(1, attempts)):
        for label, uri in (("mode=ro", f"file:{db.as_posix()}?mode=ro"),
                           ("immutable", f"file:{db.as_posix()}?immutable=1"),
                           ("plain", str(db))):
            try:
                c = sqlite3.connect(uri, uri=uri.startswith("file:"), timeout=8)
                c.execute("select 1 from messages limit 1")
                if label != "mode=ro":
                    print(f"[monitor] 提示: 用 {label} 模式读的库 (正常只读暂时不可用, 多为瞬时争用)")
                return c, label
            except Exception as _e:
                last = f"{label}: {type(_e).__name__}: {str(_e)[:70]}"
        if _i < attempts - 1:
            _time.sleep(1.5 * (_i + 1))          # 退避
    # 全失败 → 带诊断抛出 (让定时任务在输出里看到可读的原因)
    _sz = {}
    for _suf in ("", "-wal", "-shm"):
        _f = Path(str(db) + _suf)
        _sz[_f.name] = _f.stat().st_size if _f.exists() else None
    raise RuntimeError(
        f"读不了 {db} (试了 {attempts} 轮)\n"
        f"  最后一次错误: {last}\n"
        f"  文件: {_sz}\n"
        f"  提示: 若某进程被强杀过, 可能留下陈旧 -shm; 先优雅停引擎再 checkpoint (见技能条目 六十二)")


def load_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def detect(rows: list, skills: list, arts: list) -> dict:
    """对一批消息做症状识别。rows 按 id 升序。"""
    findings = []
    # 按 session 配对 user→assistant
    by_sess: dict = {}
    for r in rows:
        by_sess.setdefault(r["session_id"], []).append(r)

    for sid, msgs in by_sess.items():
        for i, m in enumerate(msgs):
            if m["role"] != "assistant":
                continue
            prev = None
            for j in range(i - 1, -1, -1):
                if msgs[j]["role"] == "user":
                    prev = msgs[j]
                    break
            txt = (m["content"] or "")
            umsg = (prev["content"] or "") if prev else ""
            ts = m.get("timestamp") or 0
            hit = []

            if RE_FAIL_TEXT.search(txt):
                hit.append(("失败/报错", "高"))
            if (m.get("intent") in ("model.fallback", "understand.request", "plan")
                    and not txt.strip().startswith(("✓", "✅"))):
                hit.append((f"未沉淀路由 ({m.get('intent')})", "中"))
            if (m.get("elapsed") or 0) > SLOW_SEC:
                hit.append((f"慢 {m.get('elapsed'):.1f}s", "低"))
            if RE_ASK_BACK.search(txt) and RE_TASKISH.search(umsg):
                hit.append(("反问/要补充 (可能是没接住)", "中"))
            if len(txt.strip()) < HOLLOW_LEN and RE_TASKISH.search(umsg):
                hit.append(("回复空洞", "高"))
            # 疑似"只念未做": 回复≈用户原话 (粘路径/清单被原样念回)
            if umsg and txt.strip() and umsg.strip()[:40] in txt and len(txt) < len(umsg) + 40:
                hit.append(("疑似只念未做", "高"))
            # 用户说了要做且带路径, 但 ±180s 内没有任何产物
            # ★ 2026-09-24 收窄 (首跑假阳): 光看产物台账会把"真写了但没进台账"的判成没做
            #   (实测 "把结果写入 F:\data.txt" 明明写了却报"没产物")。所以先查**磁盘**:
            #   用户消息里提到的路径现在存在 → 判定真做了, 不报。
            if RE_TASKISH.search(umsg) and RE_PATH.search(umsg):
                near = [a for a in arts if abs((a.get("at") or 0) - ts) < 180]
                if not near:
                    _on_disk = False
                    for _m in RE_PATH.findall(umsg) or []:
                        pass
                    for _cand in re.findall(r"[A-Za-z]:[\\/][^\s，。；;、！？\"']+", umsg):
                        try:
                            if Path(_cand.rstrip(".。，")).exists():
                                _on_disk = True
                                break
                        except Exception:
                            continue
                    if not _on_disk:
                        hit.append(("说了要做但没产物", "高"))

            if hit:
                _layer = "① 链路断裂"
                for _n, _lv in hit:
                    _l = LAYER_OF_SYMPTOM.get(_n.split(" (")[0])
                    if _l:
                        _layer = _l
                        break
                findings.append({
                    "layer": _layer,
                    "session": sid, "at": ts,
                    "time": time.strftime("%m-%d %H:%M:%S", time.localtime(ts or 0)),
                    "user": umsg[:300], "reply": txt[:400],
                    "intent": m.get("intent"), "model": m.get("model"),
                    "elapsed": m.get("elapsed"), "symptoms": hit,
                })

    # 用户连续重发同一条 = 上一条没满足 (很强的信号)
    repeats = []
    for sid, msgs in by_sess.items():
        us = [x for x in msgs if x["role"] == "user"]
        for a, b in zip(us, us[1:]):
            if (a["content"] or "").strip() and (a["content"] or "").strip() == (b["content"] or "").strip():
                repeats.append({"session": sid, "text": (a["content"] or "")[:150],
                                "time": time.strftime("%m-%d %H:%M", time.localtime(b.get("timestamp") or 0))})

    # 技能失败
    sk_fail = [s for s in skills if not s.get("ok")]

    # ── 链路健康度 + 路由覆盖 (纠偏要看: 哪些路由从没走通过、哪条路一直挂) ──
    asst = [r for r in rows if r["role"] == "assistant"]
    route_stats: dict = {}
    for m in asst:
        rt = m.get("intent") or "?"
        d = route_stats.setdefault(rt, {"n": 0, "fail": 0})
        d["n"] += 1
        if RE_FAIL_TEXT.search(m.get("content") or ""):
            d["fail"] += 1
    _n_asst = len(asst) or 1
    _fb = sum(1 for m in asst if (m.get("intent") or "") in FALLBACK_INTENTS)
    _broken = sum(1 for m in asst if RE_FAIL_TEXT.search(m.get("content") or ""))
    health = {
        "turns": len(asst),
        "real_route_pct": round(100.0 * (_n_asst - _fb) / _n_asst, 1),
        "fallback_pct": round(100.0 * _fb / _n_asst, 1),
        "fallback_n": _fb,
        "broken_n": _broken,
        "broken_pct": round(100.0 * _broken / _n_asst, 1),
        "routes": sorted(route_stats.items(), key=lambda kv: -kv[1]["n"]),
    }
    # 按层归并
    by_layer: dict = {}
    for f in findings:
        by_layer.setdefault(f.get("layer") or "?", []).append(f)

    return {"findings": findings, "repeats": repeats, "skill_fail": sk_fail,
            "health": health, "by_layer": by_layer}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量 (忽略游标)")
    ap.add_argument("--since", default="", help="如 24h / 3d")
    args = ap.parse_args()

    db = DATA / "chat_sessions.db"
    if not db.exists():
        print(f"[monitor] 找不到 {db}")
        return 1

    last_id = 0
    if CURSOR.exists() and not args.all:
        try:
            last_id = int(json.loads(CURSOR.read_text(encoding="utf-8")).get("last_msg_id", 0))
        except Exception:
            last_id = 0
    since_ts = 0.0
    if args.since:
        m = re.fullmatch(r"(\d+)([hd])", args.since.strip())
        if m:
            n, unit = int(m.group(1)), m.group(2)
            since_ts = time.time() - n * (3600 if unit == "h" else 86400)
            last_id = 0

    c, _read_mode = _connect_ro(db)
    c.row_factory = sqlite3.Row
    q = "select id, session_id, role, content, intent, model, elapsed, timestamp from messages where id > ?"
    p = [last_id]
    if since_ts:
        q += " and timestamp >= ?"
        p.append(since_ts)
    rows = [dict(r) for r in c.execute(q + " order by id", p)]
    c.close()

    skills = load_jsonl(DATA / "skill_usage.jsonl")
    arts = load_jsonl(DATA / "artifacts.jsonl")
    if rows:
        lo = min(r.get("timestamp") or 0 for r in rows) - 300
        # ★ 用**时间窗**过滤, 且**不用 `or skills` 兜底** —— 否则"没有新消息"时
        #   会把整份历史技能失败重新报一遍 (实测第二次跑仍报"技能失败 8")。
        skills = [s for s in skills if (s.get("at") or 0) >= lo]
        arts = [a for a in arts if (a.get("at") or 0) >= lo]
    else:
        skills, arts = [], []

    res = detect(rows, skills, arts)

    # ★ 没有新消息就**不写文件** —— 否则空日报会覆盖掉当天有内容的那份
    #   (实测: 门禁里跑完 --all 再跑一次无参, 空报告把好报告盖了 → 门禁 F4 红)。
    if not rows:
        print("[monitor] 0 条新消息 —— 不写日报 (避免空报告覆盖当天有内容的那份)")
        return 0

    stamp = time.strftime("%Y-%m-%d_%H%M")
    day = time.strftime("%Y-%m-%d")
    OUT_MD.mkdir(parents=True, exist_ok=True)
    OUT_JSON.mkdir(parents=True, exist_ok=True)

    hi = [f for f in res["findings"] if any(lv == "高" for _, lv in f["symptoms"])]
    mid = [f for f in res["findings"] if f not in hi and any(lv == "中" for _, lv in f["symptoms"])]
    lo = [f for f in res["findings"] if f not in hi and f not in mid]

    h = res.get("health") or {}
    L = [f"# 对话监控日报 {stamp}", "",
         "## 链路健康度 (纠偏主指标)", "",
         f"- 助手轮次 **{h.get('turns', 0)}** · 走专属路由(工具/技能) **{h.get('real_route_pct', 0)}%**"
         f" · **模型直答/兜底 {h.get('fallback_pct', 0)}%** ({h.get('fallback_n', 0)} 轮)"
         f" ← 这一项越低越好 (越高说明越该沉淀没沉淀)",
         f"- 报错轮次 **{h.get('broken_n', 0)}** ({h.get('broken_pct', 0)}%)",
         f"- 新消息 {len(rows)} 条 (id > {last_id}) · 候选 **{len(res['findings'])}** 条"
         f" (高 {len(hi)} / 中 {len(mid)} / 低 {len(lo)})"
         f" · 重发 {len(res['repeats'])} · 技能失败 {len(res['skill_fail'])}",
         "  ⚠️ 候选清单**不是结论** —— 含假阳 (诚实回答'做不到'/文件其实写了); 每日复盘逐条判。",
         "",
         "## 失败分层 (修哪一层 —— 分配器 / 工具技能 / 链路环境 / 配置)", ""]
    for _lay in sorted((res.get("by_layer") or {}).keys()):
        L.append(f"- **{_lay}** — {len(res['by_layer'][_lay])} 条")
    L.append("")
    _rs = h.get("routes") or []
    if _rs:
        L += ["## 路由覆盖 (哪些路走了 / 哪条一直挂)", "",
              "| 路由 | 轮次 | 失败 | 失败率 |", "|---|---|---|---|"]
        for _rt, _d in _rs[:18]:
            _fr = (100.0 * _d["fail"] / _d["n"]) if _d["n"] else 0
            _flag = " ⚠️" if _d["fail"] and _fr >= 50 else ""
            L.append(f"| `{_rt}` | {_d['n']} | {_d['fail']} | {_fr:.0f}%{_flag} |")
        L.append("")
    if res["repeats"]:
        L += ["## 用户重发 (上一条没满足)", ""]
        for r in res["repeats"][:20]:
            L.append(f"- `{r['time']}` {r['text']}")
        L.append("")
    _order = {"高": 0, "中": 1, "低": 2}
    for _lay in sorted((res.get("by_layer") or {}).keys()):
        group = sorted(res["by_layer"][_lay],
                       key=lambda f: min(_order.get(lv, 3) for _n, lv in f["symptoms"]))
        if not group:
            continue
        L += [f"## {_lay} ({len(group)})", ""]
        for f in group[:30]:
            sym = " / ".join(f"{n}({lv})" for n, lv in f["symptoms"])
            L += [f"### `{f['time']}` {sym}",
                  f"- 路由 `{f['intent']}` · 模型 `{f['model']}` · {f['elapsed']}s",
                  f"- 用户: {f['user'][:180]}",
                  f"- 回复: {f['reply'][:220]}", ""]
    if res["skill_fail"]:
        L += ["## 技能失败", ""]
        for s in res["skill_fail"][:20]:
            L.append(f"- `{time.strftime('%H:%M', time.localtime(s.get('at') or 0))}` "
                     f"{s.get('name')} · {(s.get('note') or '')[:110]}")
        L.append("")
    (OUT_MD / f"{stamp}.md").write_text("\n".join(L), encoding="utf-8")

    payload = {"stamp": stamp, "day": day, "msg_count": len(rows), "last_id": last_id,
               "symptom_counts": {"high": len(hi), "mid": len(mid), "low": len(lo)},
               **res}
    (OUT_JSON / f"{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if rows:
        CURSOR.write_text(json.dumps(
            {"last_msg_id": max(int(r["id"]) for r in rows),
             "last_run": time.time(), "stamp": stamp}, ensure_ascii=False), encoding="utf-8")

    _h = res.get("health") or {}
    print(f"[monitor] {len(rows)} 条新消息 | 可疑 {len(res['findings'])} "
          f"(高 {len(hi)}/中 {len(mid)}/低 {len(lo)}) | 重发 {len(res['repeats'])} | "
          f"技能失败 {len(res['skill_fail'])}")
    if _h.get("turns"):
        _lay = " ".join(f"{k.split()[0]}={len(v)}"
                        for k, v in sorted((res.get("by_layer") or {}).items()))
        print(f"[monitor] 链路: 专属路由 {_h['real_route_pct']}% · 模型直答/兜底 "
              f"{_h['fallback_pct']}%({_h['fallback_n']}轮) · 报错 {_h['broken_n']}轮({_h['broken_pct']}%)")
        print(f"[monitor] 分层: {_lay}")
    print(f"[monitor] 日报: {OUT_MD / (stamp + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
