"""技能/能力 使用价值账 —— 「用着好 → 留 / 用着不行 → 退」的判据 (2026-09-21 立)

为什么需要它
═══════════════════════════════════════════════════════════════════
缺环 A: 项目里只有"规则命中计数"(learn_loop.note_hit), **技能维度是空白** ——
        没有任何地方回答"这个技能被用过几次、成了几次"。
        没有这个数字, "自动纳库/自动降级"就没有判据, 只能靠人感觉。

它记什么
───────────────────────────────────────────────────────────────
  每次技能/能力被使用 → 一条 {name, kind, ok, route, at, note}
  派生: uses / ok / fail / rate / last_used / first_used

判据 (给 autofill 用)
───────────────────────────────────────────────────────────────
  rank(min_uses, min_rate)  → 够格"自动纳入正式库"的 (用得多 + 成得多)
  rotten(min_fail, max_rate) → 该退回候选池的 (用一次败一次)

★ 探针安全: 落盘路径受 TMM_SKILL_USAGE_FILE 控制 (已被 _probe_env 隔离)。

自检: python core/skill_usage.py
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("core.skill_usage")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "data" / "skill_usage.jsonl"


def store_path() -> Path:
    p = os.environ.get("TMM_SKILL_USAGE_FILE")
    return Path(p) if p else DEFAULT_FILE


def record(name: str, ok: bool, route: str = "", note: str = "", kind: str = "skill") -> dict:
    """记一次使用。失败不影响调用方 (调用方负责 try)。

    ★★ 失败即闭 (fail-closed): 只有**真人会话**才记账。
      判据是**正面信号** TMM_LIVE=1, 由真实入口设置 (main.py / start_web_ui.py / tmm_app.py);
      没有它 = 测试/门禁/探针/脚本 → 一律不记。

      为什么倒过来写 (用两次污染换的):
        原来只拦 TMM_PROBE, 结果**门禁直接调 process() 但不传 probe** 时照样写进了用户真账
        (实测: verify_skill_dag_layer 的 test-copy-file 等 5 条)。
        "黑名单式防守"永远漏; "默认不写 + 明确信号才写"才是不会漏的那一侧。
    """
    if os.environ.get("TMM_PROBE") in ("1", "true", "yes"):
        return {"ok": False, "error": "probe 流量不记账"}
    if os.environ.get("TMM_LIVE") not in ("1", "true", "yes"):
        return {"ok": False, "error": "非真人会话不记账 (缺 TMM_LIVE)"}
    nm = (name or "").strip()
    if not nm:
        return {"ok": False, "error": "空名字"}
    rec = {"name": nm, "kind": kind or "skill", "ok": bool(ok),
           "route": str(route or "")[:60], "note": str(note or "")[:200], "at": int(time.time())}
    f = store_path()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "record": rec}


def _read_all() -> list:
    f = store_path()
    if not f.is_file():
        return []
    out = []
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def stats(name: str = None) -> dict:
    """→ {name: {uses, ok, fail, rate, last_used, first_used}}; name 给定则只回那一条。"""
    agg: dict = {}
    for r in _read_all():
        nm = str(r.get("name") or "")
        if not nm:
            continue
        if name and nm != name:
            continue
        d = agg.setdefault(nm, {"name": nm, "kind": r.get("kind") or "skill",
                                "uses": 0, "ok": 0, "fail": 0,
                                "last_used": 0, "first_used": 0})
        d["uses"] += 1
        if r.get("ok"):
            d["ok"] += 1
        else:
            d["fail"] += 1
        at = int(r.get("at") or 0)
        d["last_used"] = max(d["last_used"], at)
        d["first_used"] = min(d["first_used"] or at, at) or at
    for d in agg.values():
        d["rate"] = round(d["ok"] / d["uses"], 3) if d["uses"] else 0.0
    return agg.get(name, {}) if name else agg


def rank(min_uses: int = 3, min_rate: float = 0.8) -> list:
    """够格"自动纳入正式库"的 (用得多 + 成得多), 按 uses 降序。"""
    out = [d for d in stats().values() if d["uses"] >= min_uses and d["rate"] >= min_rate]
    return sorted(out, key=lambda d: (-d["uses"], d["name"]))


def rotten(min_fail: int = 2, max_rate: float = 0.34) -> list:
    """该退回候选池的 (用一次败一次)。"""
    out = [d for d in stats().values() if d["fail"] >= min_fail and d["rate"] <= max_rate]
    return sorted(out, key=lambda d: (-d["fail"], d["name"]))


def reset(name: str = None) -> int:
    """清账 (name 给定则只清那一条)。返回删掉的条数。仅测试/维护用。"""
    f = store_path()
    if not f.is_file():
        return 0
    rows = _read_all()
    keep = [r for r in rows if name and str(r.get("name")) != name]
    dropped = len(rows) - len(keep)
    try:
        f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    except Exception:
        return 0
    return dropped


def render() -> str:
    """人看的账本。"""
    st = stats()
    if not st:
        return "使用账本为空 (还没有技能被用过)。"
    lines = [f"技能/能力 使用账 ({len(st)} 项):", "  名称                    次数  成  败   成功率  最近"]
    for d in sorted(st.values(), key=lambda x: (-x["uses"], x["name"]))[:20]:
        when = time.strftime("%m-%d %H:%M", time.localtime(d["last_used"])) if d["last_used"] else "-"
        lines.append(f"  {d['name'][:22]:22} {d['uses']:5} {d['ok']:4} {d['fail']:4} "
                     f"{d['rate'] * 100:6.1f}%  {when}")
    r = rank()
    if r:
        lines.append(f"  → 够格纳库 ({len(r)}): " + ", ".join(x["name"] for x in r[:6]))
    bad = rotten()
    if bad:
        lines.append(f"  → 该退回 ({len(bad)}): " + ", ".join(x["name"] for x in bad[:6]))
    return "\n".join(lines)


if __name__ == "__main__":   # 自检
    import sys
    import tempfile
    P, F = [], []

    def chk(n, c, d=""):
        (P if c else F).append(n)
        print(("  PASS " if c else "  FAIL ") + n + (f"   <- {d}" if d and not c else ""))

    sbx = Path(tempfile.mkdtemp(prefix="_vfy_usage_")) / "u.jsonl"
    os.environ["TMM_SKILL_USAGE_FILE"] = str(sbx)
    os.environ["TMM_LIVE"] = "1"          # 自检要测"真人会话"路径, 显式开正面信号
    try:
        chk("空账 → 无统计", stats() == {})
        record("s1", True)
        record("s1", True)
        record("s1", False, note="参数缺")
        record("s2", True)
        st = stats("s1")
        chk("★ 计数正确 (3 次 / 成 2 / 败 1)", st.get("uses") == 3 and st.get("ok") == 2 and st.get("fail") == 1, st)
        chk("★ 成功率正确 (0.667)", abs(st.get("rate", 0) - 0.667) < 0.01, st.get("rate"))
        chk("★ 空名字拒绝", record("", True).get("ok") is False)
        chk("★ rank 门槛生效 (s2 只 1 次 → 不够格)",
            [d["name"] for d in rank(min_uses=3)] == [], rank(min_uses=3))
        chk("★ rank 达标能选出 (s1 3次 66.7% ≥ 0.6)", [d["name"] for d in rank(min_uses=3, min_rate=0.6)] == ["s1"])
        chk("★ rotten 能识别 (s1 败1 不够 min_fail=2)", rotten(min_fail=2) == [])
        record("s3", False)
        record("s3", False)
        chk("★ rotten 抓到用一次败一次的", [d["name"] for d in rotten()] == ["s3"], rotten())
        chk("★ rank 不误收烂的", all(d["name"] != "s3" for d in rank(min_uses=2, min_rate=0.5)))
        chk("★ 渲染含表头与结论", "使用账" in render() and ("够格纳库" in render() or "该退回" in render()))
        chk("★ 落盘是 JSONL 且只读不写坏", sbx.is_file() and len(sbx.read_text(encoding="utf-8").strip().splitlines()) == 6)
        chk("★ reset 只清指定项", reset("s3") == 2 and "s3" not in stats())
        # ★★ fail-closed: 去掉真人信号 → 一律不记
        os.environ.pop("TMM_LIVE", None)
        n0 = len(_read_all())
        chk("★★ 失败即闭: 无 TMM_LIVE 时不记账 (测试/门禁写不进来)",
            record("no-live-should-not-write", True).get("ok") is False and len(_read_all()) == n0)
        os.environ["TMM_LIVE"] = "1"
        # ★ 探针闸: TMM_PROBE=1 时绝不记账
        os.environ["TMM_PROBE"] = "1"
        try:
            n_before = len(_read_all())
            r = record("probe-should-not-write", True)
            chk("★★ 探针闸: TMM_PROBE=1 时拒绝记账 (验证流量不改用户状态)",
                r.get("ok") is False and len(_read_all()) == n_before, r)
        finally:
            os.environ.pop("TMM_PROBE", None)
        chk("★ 探针关掉后能正常记账", record("after-probe", True).get("ok") is True)
    finally:
        os.environ.pop("TMM_SKILL_USAGE_FILE", None)
        os.environ.pop("TMM_LIVE", None)
        import shutil
        shutil.rmtree(sbx.parent, ignore_errors=True)
    print()
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    sys.exit(1 if F else 0)
