"""
模型用量统计工具 — "钱花在哪了"。

数据来源: core/model_client.record_usage() 每次调用往
  data/model_usage.jsonl 追加一行 JSON (调用后记, 失败也不影响对话)。

为什么要有 (2026-09-20 对照 OpenClaw 的 model-usage 补):
    每次调用其实都拿到了 token 数, 但从没人存下来 → 用户问
    "用了多少 / 哪个模型花得多" 只能靠猜。

★ 只读: 仅读 JSONL 并聚合, 不改文件 (不轮转、不清理 —— 清理是另一个决定)。
★ 坏行跳过并计数: 日志被截断/手改过也不该让统计整个失败。
★ 不编价格: 只报 token 与调用次数; 换算成钱要按各自的实际计费标准,
  工具里**不许假装知道单价** (编一个价目表比不报更糟)。
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PLUGIN = {
    "name": "usage_stats",
    "description": "模型用量统计 (次数/耗时/token), 按模型与日期汇总",
    "version": "1.0",
    "requires": [],
    "trigger": ["用量", "花销", "调用次数", "模型统计", "token 用量"],
    "permission": ["read"],
    "category": "data",
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_path() -> str:
    return os.environ.get("TMM_USAGE_LOG") or os.path.join(PROJECT_ROOT, "data", "model_usage.jsonl")


def load_rows(limit: int = 20000, days: int = 0):
    """读 JSONL → (行列表, 坏行数)。文件不存在 → ([], 0)。"""
    p = log_path()
    if not os.path.isfile(p):
        return [], 0, False
    cutoff = (time.time() - days * 86400) if days else 0
    rows, bad = [], 0
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                if not isinstance(r, dict):
                    bad += 1
                    continue
                if cutoff and float(r.get("ts") or 0) < cutoff:
                    continue
                rows.append(r)
    except Exception as e:
        logger.warning("usage log read failed: %s", e)
        return [], bad, True
    if len(rows) > limit:
        rows = rows[-limit:]
    return rows, bad, True


def _agg(rows):
    """按模型 / 按天 / 按成败聚合成统计结构。"""
    per_model, per_day = {}, {}
    ok_n, err_n = 0, 0
    tok_total = tok_prompt = tok_completion = 0
    lat_sum = 0.0
    last_ts = 0.0
    for r in rows:
        m = str(r.get("model") or "?")
        d = datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%Y-%m-%d") \
            if r.get("ts") else "?"
        ok = bool(r.get("ok"))
        pt = int(r.get("prompt_tokens") or 0)
        ct = int(r.get("completion_tokens") or 0)
        tt = int(r.get("total_tokens") or 0) or (pt + ct)
        el = float(r.get("elapsed") or 0)
        last_ts = max(last_ts, float(r.get("ts") or 0))

        a = per_model.setdefault(m, {"calls": 0, "ok": 0, "err": 0, "prompt": 0,
                                     "completion": 0, "total": 0, "lat": 0.0, "last": 0.0})
        a["calls"] += 1
        a["ok" if ok else "err"] += 1
        a["prompt"] += pt
        a["completion"] += ct
        a["total"] += tt
        a["lat"] += el
        a["last"] = max(a["last"], float(r.get("ts") or 0))

        b = per_day.setdefault(d, {"calls": 0, "err": 0, "total": 0})
        b["calls"] += 1
        b["err"] += 0 if ok else 1
        b["total"] += tt

        ok_n += 1 if ok else 0
        err_n += 0 if ok else 1
        tok_prompt += pt
        tok_completion += ct
        tok_total += tt
        lat_sum += el
    return {
        "calls": len(rows), "ok": ok_n, "err": err_n,
        "prompt_tokens": tok_prompt, "completion_tokens": tok_completion,
        "total_tokens": tok_total,
        "avg_latency": round(lat_sum / len(rows), 2) if rows else 0.0,
        "last": last_ts,
        "models": [dict(name=k, avg_latency=round(v["lat"] / v["calls"], 2) if v["calls"] else 0, **v)
                   for k, v in sorted(per_model.items(), key=lambda kv: -kv[1]["calls"])],
        "days": [dict(day=k, **v) for k, v in sorted(per_day.items())],
    }


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _render(st, bad: int, days: int):
    if not st["calls"]:
        return ("没有用量记录。\n"
                "说明: 用量从 2026-09-20 起开始记录, 之后每次模型调用都会记一行;\n"
                "探针/验证流量不记账, 所以数字只反映真实使用。")
    head = (f"模型用量: 共 {st['calls']} 次调用 · 成功 {st['ok']} · 失败 {st['err']} · "
            f"平均 {st['avg_latency']}s · 最近 {_fmt_ts(st['last'])}")
    if days:
        head += f" · 统计窗口: 最近 {days} 天"
    out = [head, f"token 合计: 输入 {st['prompt_tokens']} · 输出 {st['completion_tokens']} · "
                 f"总计 {st['total_tokens']}"]
    out.append("")
    out.append("按模型:")
    for m in st["models"]:
        out.append(f"  {m['name']:<10} {m['calls']:>4} 次 (失败 {m['err']}) · "
                   f"平均 {m['avg_latency']}s · token {m['total']} "
                   f"(入 {m['prompt']}/出 {m['completion']})")
    if len(st["days"]) > 1:
        out.append("")
        out.append("按日期:")
        for d in st["days"][-14:]:
            out.append(f"  {d['day']}  {d['calls']:>4} 次" + (f" (失败 {d['err']})" if d["err"] else "")
                       + f" · token {d['total']}")
    if bad:
        out.append(f"\n注意: 日志里有 {bad} 行无法解析 (已跳过)。")
    out.append("\n说明: 只统计 token 与次数, 不含金额 —— 各家单价不同, 按你的实际计费标准换算。")
    return "\n".join(out)


async def run(action: str = "", days: int = 0, limit: int = 20000, **kwargs) -> dict:
    """查模型用量。

    action:
      summary (默认) — 总览 + 按模型 + 按日期
      today          — 只看今天
    """
    action = (action or "summary").strip().lower()
    if action in ("today", "day") and not days:
        days = 1
    if action not in ("summary", "stats", "today", "day"):
        return {"success": False, "error": f"未知 action: {action} (可用: summary / today)"}
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    try:
        limit = max(1, min(int(limit or 20000), 200000))
    except (TypeError, ValueError):
        limit = 20000

    rows, bad, existed = load_rows(limit=limit, days=days)
    st = _agg(rows)
    st["log_exists"] = existed
    st["log_path"] = log_path()
    return {"success": True, "output": _render(st, bad, days), **st}
