"""能力自补闭环 —— 「缺 → 找 → 甄别 → 取形式重编 → 沙箱验 → 纳库 → 不好用退回」 (2026-09-21 立)

要解决的
═══════════════════════════════════════════════════════════════════
链条任务里某个节点干不了、库里也没有 —— 今天的现实是:
  · 只**自动记账** (gap_ledger) + 回复里给一句"可以 /depot search xxx"
  · 找/甄别/重编/纳库 四步**全靠人敲命令**
  · "用着好 → 自动结晶成自有技能" 这一环**完全没有**
用户要的是闭环: 下次再遇到同类活, **库里有、记忆里有**, 直接能用。

六环 + 一反环
───────────────────────────────────────────────────────────────
  ①缺    gap_ledger 里 blocked_local / no_tool, 出现 ≥N 次 → 这是真需求
  ②找    depot.search_github (只读网络; **默认关闭**, 要显式 allow_network=True)
  ③甄别  depot 的三档 verdict: safe / caution / reject ← **闸门**
  ④重编  **取形式**: 用**本地已有工具**重编成自有技能候选。
         绝不把第三方代码搬进来 —— 这不是洁癖, 是"别人拿到能跑 + 不带走你的凭据"的前提
  ⑤验证  沙箱: DAG 可解析 + 工具全在 + 占位符齐 + (可注入 runner 真跑一次)
  ⑥纳库  必须同时满足: 甄别 safe + 验证通过 + 用量门槛(用够 N 次且成功率高) → 移入 tmm_skills/
  反环   用一次败一次 (skill_usage.rotten) → 退回候选池并记原因

★ 默认 dry-run: 不联网、不写任何东西, 只报"这一环会做什么"。要真跑必须显式 apply=True。
★ 探针安全: 台账/用量都走各自的环境变量隔离 (TMM_GAP_DB / TMM_SKILL_USAGE_FILE / TMM_ARTIFACTS_FILE)。

自检: python core/autofill.py
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("core.autofill")

#: 六环 (顺序即设计; 纳库前每一环都要过)
STAGES = ("缺", "找", "甄别", "重编", "验证", "纳库")

#: 纳库门槛 (缺一不可)
MIN_GAP_COUNT = 2          # 同一缺口出现几次才算真需求
MIN_USES = 3               # 至少被用过几次
MIN_RATE = 0.8             # 成功率下限
FORBIDDEN_VERDICTS = ("caution", "reject")   # 甄别不是 safe → 一律不放行


def targets(min_count: int = MIN_GAP_COUNT, limit: int = 20) -> list:
    """① 缺: 从缺口台账取"真需求" (本地救不了的 / 没这能力的, 且出现够多次)。"""
    out = []
    try:
        from core.gap_ledger import get_gap_ledger
        gl = get_gap_ledger()
        for g in (gl.list_gaps(limit=limit) or []):
            if str(g.get("status")) in ("done", "dropped"):
                continue
            if int(g.get("count") or 0) < min_count:
                continue
            if str(g.get("category") or "") not in ("blocked_local", "no_tool", "skill_failed"):
                continue
            out.append(g)
    except Exception as e:
        logger.warning("targets: %s", e)
    return out


def scout(query: str, limit: int = 5, allow_network: bool = False) -> list:
    """② 找: 外面有没有现成的。**默认不联网** —— 只回"会去搜什么"。"""
    q = (query or "").strip()
    if not q:
        return []
    if not allow_network:
        return [{"skipped": True, "reason": "未允许联网 (allow_network=False)", "query": q}]
    try:
        from core import depot
        return depot.search_github(q, limit=limit) or []
    except Exception as e:
        logger.warning("scout: %s", e)
        return []


def screen(cand: dict, allow_network: bool = False) -> dict:
    """③ 甄别: 三档 verdict (safe/caution/reject)。这是**闸门**, 不是建议。"""
    if not allow_network or not cand or cand.get("skipped"):
        return {"verdict": "unknown", "reason": "未允许联网, 未做甄别",
                "passed": False, "name": (cand or {}).get("name", "")}
    name = str(cand.get("name") or cand.get("repo") or "").strip()
    try:
        from core import depot
        ev = {"name": name, "source": cand}
        v = depot.verdict(ev)                      # 复用既有安全评估 (许可证/静态扫描/阻断项)
        verdict = str(v.get("verdict") or v.get("level") or "unknown").lower()
        return {"verdict": verdict, "passed": verdict == "safe",
                "blockers": v.get("blockers") or [], "name": name, "raw": v}
    except Exception as e:
        return {"verdict": "unknown", "passed": False, "name": name,
                "reason": f"{type(e).__name__}: {e}"}


def keep_safe(screened: list) -> list:
    """闸门: 只留 verdict=safe 的。caution/reject 一律不放行 (不静默, 进 blocked 列表由调用方展示)。"""
    return [s for s in (screened or []) if s.get("passed") and str(s.get("verdict")) == "safe"]


def forge(target: dict, proposal, local_tools=None) -> dict:
    """④ 重编 (取形式): 把"要办的事"编成**只用本地积木**的自有技能候选。

    proposal: 已有 Proposal 对象 (由调用方/模型产出) —— 本函数负责**把住取形式的底线**:
      技能里的每一步只能引用本地已有工具; 出现任何"引入第三方代码/新依赖"的步骤 → 拒绝。
    """
    if proposal is None:
        return {"ok": False, "error": "没有候选方案"}
    tools_in_steps = set()
    for st in (getattr(proposal, "steps", None) or []):
        t = ""
        if isinstance(st, dict):
            t = str(st.get("tool") or st.get("use") or "")
        elif isinstance(st, (list, tuple)) and st:
            t = str(st[0])
        if t:
            tools_in_steps.add(t)
    tools_in_steps |= set(getattr(proposal, "requires_tools", None) or [])
    if all(_looks_like_code_intro(t) for t in tools_in_steps) and tools_in_steps:
        return {"ok": False, "error": "候选里只有'引入第三方代码'的步骤 —— 取形式要求用本地积木重编"}
    if local_tools is not None:
        unknown = sorted(t for t in tools_in_steps if t and t not in set(local_tools))
        if unknown:
            return {"ok": False, "error": f"步骤引用了本地不存在的工具: {unknown} (违反'取形式')",
                    "unknown_tools": unknown}
    return {"ok": True, "proposal": proposal, "tools_in_steps": sorted(tools_in_steps)}


def _looks_like_code_intro(tool: str) -> bool:
    t = (tool or "").lower()
    return any(k in t for k in ("pip", "install", "git_clone", "download", "wheel", "package"))


def prove(proposal, factory, runner=None) -> dict:
    """⑤ 验证: 沙箱里证明"它真能用"。(用工厂的 validate + 工具可调用性; 可选真跑。)"""
    reasons = []
    if proposal is None:
        return {"ok": False, "reasons": ["没有候选"]}
    if factory is None:
        return {"ok": False, "reasons": ["没有工厂, 无法复检"]}
    try:
        factory.validate(proposal)
    except Exception as e:
        return {"ok": False, "reasons": [f"复检抛错: {type(e).__name__}: {e}"]}
    if getattr(proposal, "errors", None):
        reasons += [f"工厂复检未过: {e}" for e in proposal.errors[:4]]
    tools = set(getattr(proposal, "requires_tools", None) or [])
    for st in (getattr(proposal, "steps", None) or []):
        if isinstance(st, dict) and st.get("tool"):
            tools.add(str(st["tool"]))
    if not tools:
        reasons.append("没有任何工具步骤 —— 这种技能跑起来什么也不会发生")
    else:
        for t in sorted(tools):
            try:
                if factory._tool_callable(t) is None:      # noqa: SLF001 (工厂既有内部判据)
                    reasons.append(f"工具不可调用: {t}")
            except Exception as e:
                reasons.append(f"工具可调用性检查失败 {t}: {type(e).__name__}")
    evidence = {"tools": sorted(tools), "steps": len(getattr(proposal, "steps", None) or [])}
    if not reasons and runner is not None:
        try:
            ev = runner(proposal)
            evidence["run"] = ev
            if not (isinstance(ev, dict) and ev.get("ok")):
                reasons.append(f"沙箱试跑未通过: {str(ev)[:120]}")
        except Exception as e:
            reasons.append(f"沙箱试跑抛错: {type(e).__name__}: {str(e)[:100]}")
    return {"ok": not reasons, "reasons": reasons, "evidence": evidence}


def usage_ready(name: str, min_uses: int = MIN_USES, min_rate: float = MIN_RATE) -> dict:
    """⑥ 纳库的用量门槛: 用够次数 + 成功率达标 (由 skill_usage 提供, 没有记录就不放行)。"""
    try:
        from core import skill_usage as U
        d = U.stats(name) or {}
    except Exception as e:
        return {"ok": False, "reason": f"用量账读不到: {type(e).__name__}"}
    uses, rate = int(d.get("uses") or 0), float(d.get("rate") or 0.0)
    ok = uses >= min_uses and rate >= min_rate
    return {"ok": ok, "uses": uses, "rate": rate,
            "reason": "" if ok else f"用量不足 (用 {uses} 次/成功率 {rate:.0%}, 需 ≥{min_uses} 次且 ≥{min_rate:.0%})"}


def _probe_blocked(action: str) -> dict:
    """硬探针闸: 验证流量绝不改技能库 (TMM_PROBE=1)。"""
    if os.environ.get("TMM_PROBE") in ("1", "true", "yes"):
        return {"ok": False, "error": f"probe 流量不执行 {action}"}
    return {}


def enshrine(name: str, factory, screening: dict, proof: dict, min_uses: int = MIN_USES,
             min_rate: float = MIN_RATE) -> dict:
    """⑥ 纳库: **四道闸全过**才生效 (甄别 safe · 验证通过 · 取形式 · 用量达标)。

    任何一道不过 → 拒绝并说清哪道 (不静默)。生效 = 从候选池移入正式技能目录。
    """
    g0 = _probe_blocked("纳库")
    if g0:
        return g0
    gates = {}
    gates["甄别= safe"] = bool(screening and screening.get("passed")
                            and str(screening.get("verdict")) == "safe")
    gates["验证通过"] = bool(proof and proof.get("ok"))
    gates["取形式(本地积木)"] = bool(proof and proof.get("evidence", {}).get("tools"))
    u = usage_ready(name, min_uses, min_rate)
    gates["用量达标"] = bool(u.get("ok"))
    failed = [k for k, v in gates.items() if not v]
    if failed:
        return {"ok": False, "gates": gates, "blocked_by": failed,
                "error": "纳库被拦: " + " / ".join(failed)
                         + ((" —— " + u["reason"]) if not u.get("ok") else "")}
    if factory is None:
        return {"ok": False, "gates": gates, "error": "没有工厂, 无法生效"}
    try:
        r = factory.promote(name)
    except Exception as e:
        return {"ok": False, "gates": gates, "error": f"生效抛错: {type(e).__name__}: {e}"}
    if not isinstance(r, dict) or not r.get("ok"):
        return {"ok": False, "gates": gates, "error": f"生效失败: {str(r)[:160]}", "raw": r}
    return {"ok": True, "gates": gates, "name": name, "raw": r}


def demote(name: str, reason: str, factory=None) -> dict:
    """反环: 用一次败一次 → 退回候选池 (留原因, 不静默删)。"""
    g0 = _probe_blocked("退回")
    if g0:
        return g0
    nm = (name or "").strip()
    if not nm or factory is None:
        return {"ok": False, "error": "缺名字或工厂"}
    src = factory.skills_dir / nm
    dst = factory.candidate_dir / nm
    if not src.is_dir():
        return {"ok": False, "error": f"正式技能不存在: {nm}"}
    if dst.exists():
        return {"ok": False, "error": f"候选池里已有同名: {nm}"}
    try:
        import shutil
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        (dst / "DEMOTED.txt").write_text(
            f"退回原因: {reason}\n(退回后可改好再 /skill approve {nm} 重新生效)\n", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"退回失败: {type(e).__name__}: {e}"}
    return {"ok": True, "name": nm, "reason": reason}


def run_cycle(apply: bool = False, allow_network: bool = False, min_count: int = MIN_GAP_COUNT,
              limit: int = 5) -> dict:
    """跑一轮闭环。**默认 dry-run** (不联网/不落盘), 只报"每一环会做什么"。

    ★ 硬探针闸: TMM_PROBE=1 时强制降为 dry-run (验证流量绝不联网/绝不动技能库)。
    """
    if os.environ.get("TMM_PROBE") in ("1", "true", "yes"):
        apply, allow_network = False, False
    rep = {"apply": apply, "allow_network": allow_network, "stages": {k: [] for k in STAGES},
           "blocked": [], "demote": [], "note": ""}
    tgt = targets(min_count=min_count, limit=limit)
    rep["stages"]["缺"] = [{"id": g.get("id"), "category": g.get("category"),
                            "count": g.get("count"), "sample": str(g.get("sample") or "")[:60]}
                           for g in tgt]
    if not tgt:
        rep["note"] = f"没有够格的缺口 (需 {min_count} 次以上且属 本地救不了/没这能力)"
        return rep
    for g in tgt:
        q = str(g.get("sample") or g.get("signature") or "")
        cands = scout(q, limit=3, allow_network=allow_network)
        rep["stages"]["找"] += cands
        screened = [screen(c, allow_network=allow_network) for c in cands if not c.get("skipped")]
        rep["stages"]["甄别"] += screened
        ok = keep_safe(screened)
        rep["blocked"] += [s for s in screened if not s.get("passed")]
        if not ok:
            continue
        if not apply:
            rep["stages"]["重编"].append({"dry_run": True, "would_forge": [s["name"] for s in ok]})
            continue
        rep["stages"]["重编"].append({"pending": "需要候选方案 (Proposal) 才能重编"})
    if not apply:
        rep["note"] = "dry-run: 未联网、未落盘。加 apply=True + allow_network=True 才真跑。"
        return rep
    # 反环: 烂的退回
    if apply:
        try:
            from core import skill_usage as U
            for d in U.rotten():
                r = demote(d["name"], f"用量账: {d['fail']} 败 / 成功率 {d['rate']:.0%}")
                rep["demote"].append(r)
        except Exception as e:
            rep["demote"].append({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return rep


def render(rep: dict) -> str:
    lines = [f"能力自补闭环 ({'真跑' if rep.get('apply') else 'dry-run'}"
             f"{', 允许联网' if rep.get('allow_network') else ', 不联网'}):"]
    for k in STAGES:
        v = rep["stages"].get(k) or []
        lines.append(f"  {k}: {len(v)} 项")
        for it in v[:3]:
            t = it.get("name") or it.get("sample") or it.get("would_forge") or it.get("pending") or str(it)[:40]
            lines.append(f"      · {str(t)[:70]}")
    if rep.get("blocked"):
        lines.append(f"  被甄别拦下: {len(rep['blocked'])} 项 (caution/reject 不放行)")
    if rep.get("demote"):
        lines.append(f"  退回候选池: {len(rep['demote'])} 项")
    if rep.get("note"):
        lines.append("  " + rep["note"])
    return "\n".join(lines)


if __name__ == "__main__":   # 自检 (全程离线: 不联网 + 全部落临时目录)
    import os
    import shutil
    import sys
    import tempfile
    # 直接 `python core/autofill.py` 时 sys.path[0] 是 core/ → 补上项目根
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    P, F = [], []

    def chk(n, c, d=""):
        (P if c else F).append(n)
        print(("  PASS " if c else "  FAIL ") + n + (f"   <- {d}" if d and not c else ""))

    sbx = Path(tempfile.mkdtemp(prefix="_vfy_autofill_"))
    # 自检/门禁要测"用量达标纳库"这条路径 → 显式开真人信号 (fail-closed 的正门)
    os.environ["TMM_LIVE"] = "1"
    os.environ["TMM_SKILL_USAGE_FILE"] = str(sbx / "usage.jsonl")
    os.environ["TMM_GAP_DB"] = str(sbx / "gap.db")
    try:
        from core.gap_ledger import GapLedger, reset_gap_ledger
        import core.gap_ledger as _gl_mod
        reset_gap_ledger()
        _gl_mod._ledger = GapLedger(db_path=sbx / "gap.db")

        # ① 缺 (要用能让 classify 认成 no_tool 的措辞: 任务型 + 模型承认干不了 + 兜底路由)
        for _i in range(3):
            _gl_mod._ledger.record("帮我把散落的收据做成月报",
                                  "我没法做这个，没有相关的工具。", "model.fallback")
        t = targets(min_count=2)
        chk("★ ①缺: 够次数的缺口被取出", len(t) >= 1, t)
        chk("★ ①缺: 次数不够的不取", targets(min_count=99) == [])

        # ② 找 (默认不联网)
        s = scout("收据月报", allow_network=False)
        chk("★ ②找: 默认不联网, 只回'会搜什么'", s and s[0].get("skipped") is True, s)

        # ③ 甄别 (闸门)
        sc_refuse = screen({"name": "x"}, allow_network=False)
        chk("★ ③甄别: 没联网 → unknown 且不放行", sc_refuse["passed"] is False and sc_refuse["verdict"] == "unknown")
        fake = [{"name": "a", "verdict": "safe", "passed": True},
                {"name": "b", "verdict": "caution", "passed": False},
                {"name": "c", "verdict": "reject", "passed": False}]
        chk("★ ③闸门: 只放行 safe", [x["name"] for x in keep_safe(fake)] == ["a"])

        # ④ 重编 (取形式: 只用本地积木)
        class _Prop:
            def __init__(self, steps, tools, name="receipt-monthly-report"):
                self.name = name
                self.description = "把收据汇总成月报"
                self.triggers = ["收据月报"]
                self.params = {}
                self.steps = steps
                self.requires_tools = tools
                self.errors = []
                self.warnings = []
                self.raw = ""

        good = _Prop([{"tool": "file_ops", "action": "list"}], ["file_ops"])
        bad_code = _Prop([{"tool": "pip_install"}], ["pip_install"])
        bad_unknown = _Prop([{"tool": "magic_tool"}], ["magic_tool"])
        chk("★ ④取形式: 只用本地积木 → 通过", forge({"id": 1}, good, local_tools={"file_ops"})["ok"])
        chk("★ ④取形式: '引入第三方代码'被拒", forge({"id": 1}, bad_code, local_tools={"file_ops"})["ok"] is False)
        chk("★ ④取形式: 引用不存在的工具被拒", forge({"id": 1}, bad_unknown, local_tools={"file_ops"})["ok"] is False)
        chk("★ ④取形式: 没有方案被拒", forge({"id": 1}, None)["ok"] is False)

        # ⑤ 验证 + ⑥ 纳库 (用沙箱里的真工厂)
        from core.skill_factory import Proposal, SkillFactory
        sdir, cdir = sbx / "skills", sbx / "cands"
        sdir.mkdir(parents=True, exist_ok=True)
        f = SkillFactory(generate=None, engine=None, skills_dir=sdir, candidate_dir=cdir)
        prop = Proposal(name="receipt-monthly-report", description="把收据汇总成月报",
                        triggers=["收据月报"], steps=[{"tool": "file_ops", "action": "list",
                                                       "path": "{dir}"}],
                        requires_tools=["file_ops"])
        pr = prove(prop, f)
        chk("★ ⑤验证: 工厂复检 + 工具可调用 → 通过", pr["ok"], pr.get("reasons"))
        chk("★ ⑤验证: 证据里记下用了哪些工具", "file_ops" in pr["evidence"]["tools"], pr.get("evidence"))
        bad_prop = Proposal(name="no-tool-skill", description="什么都不干", triggers=["x"], steps=[],
                            requires_tools=[])
        chk("★ ⑤验证: 没工具步骤的技能不通过 (跑起来什么都不会发生)",
            prove(bad_prop, f)["ok"] is False)

        f.save(prop)
        chk("★ 候选已落候选池 (不进正式库)", (cdir / prop.name).is_dir() and not (sdir / prop.name).exists())

        from core import skill_usage as U
        # 用量不够 → 必拦
        r1 = enshrine(prop.name, f, {"verdict": "safe", "passed": True}, pr)
        chk("★★ 纳库被拦 (用量不足)", r1["ok"] is False and "用量达标" in r1["blocked_by"], r1)
        # 用量够了, 但甄别不是 safe → 必拦
        for _ in range(4):
            U.record(prop.name, True)
        r2 = enshrine(prop.name, f, {"verdict": "caution", "passed": False}, pr)
        chk("★★ 纳库被拦 (甄别 caution)", r2["ok"] is False and "甄别= safe" in r2["blocked_by"], r2)
        # 用量够 + 甄别 safe + 验证过 + 取形式 → 生效
        r3 = enshrine(prop.name, f, {"verdict": "safe", "passed": True}, pr)
        chk("★★ 四道闸全过 → 生效 (进正式技能库)", r3["ok"] is True, r3)
        chk("★ 生效后候选池清空、正式库有它",
            (sdir / prop.name).is_dir() and not (cdir / prop.name).exists())

        # 反环: 用一次败一次 → 退回
        #   (先清掉前面为了过"用量门槛"记的 4 次成功 —— 否则成功率 57% 不算"用一次败一次",
        #    那是断言口径该说清的事, 不是代码 bug)
        U.reset(prop.name)
        for _ in range(3):
            U.record(prop.name, False)
        chk("★ 反环判据: 烂的能被识别", any(d["name"] == prop.name for d in U.rotten()), U.rotten())
        rd = demote(prop.name, "用量账: 连败", factory=f)
        chk("★★ 反环: 退回候选池并留原因",
            rd["ok"] and (cdir / prop.name).is_dir() and (cdir / prop.name / "DEMOTED.txt").is_file(), rd)

        # run_cycle 默认 dry-run: 不落盘、不联网
        before = sorted(p.name for p in sbx.rglob("*"))
        rep = run_cycle()
        after = sorted(p.name for p in sbx.rglob("*"))
        chk("★ dry-run: 不联网", "allow_network" in rep and rep["allow_network"] is False)
        chk("★ dry-run: 不落盘 (沙箱内容未变)", before == after)
        chk("★ dry-run: 会报出'缺'这一环", isinstance(rep["stages"]["缺"], list))
        chk("★ 渲染有六环名", all(k in render(rep) for k in STAGES))
        # ★ 硬探针闸: TMM_PROBE=1 时纳库/退回/dry-run 都被拦
        os.environ["TMM_PROBE"] = "1"
        try:
            chk("★★ 探针闸: 纳库被拦", enshrine("x", f, {"verdict": "safe", "passed": True}, {"ok": True})["ok"] is False)
            chk("★★ 探针闸: 退回被拦", demote("x", "r", factory=f)["ok"] is False)
            _r = run_cycle(apply=True, allow_network=True)
            chk("★★ 探针闸: 整轮被强制 dry-run (联网/落盘都被关)", _r["apply"] is False and _r["allow_network"] is False)
        finally:
            os.environ.pop("TMM_PROBE", None)
    finally:
        os.environ.pop("TMM_SKILL_USAGE_FILE", None)
        os.environ.pop("TMM_LIVE", None)
        os.environ.pop("TMM_GAP_DB", None)
        shutil.rmtree(sbx, ignore_errors=True)
    print()
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    sys.exit(1 if F else 0)
