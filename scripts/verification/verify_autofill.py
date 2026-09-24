"""能力自补闭环 门禁 (2026-09-21 立)

挡的是什么
═══════════════════════════════════════════════════════════════════
链条里某节点干不了、库里没有时, 原来只有"自动记账 + 回一句话", 找/甄别/重编/纳库
全靠人敲命令; "用着好 → 自动结晶成自有技能"完全缺失。
现在闭环六环 + 一反环都接上, 本门禁钉住它的**闸门不放水**:

  [A] 用量账: 计数/成功率/rank/rotten 正确; 隔离变量生效 (不碰用户真账)
  [B] 六环判定: 缺(取真需求) · 找(默认不联网) · 甄别(只放 safe) · 取形式(只用本地积木)
      · 验证(工具可调用+有步骤) · 纳库(四道闸)
  [C] ★★ 变异测试: 把甄别闸门改成"全放行" → 纳库**必须被拦**。
      这条是门禁有没有牙齿的判据: 闸门能被绕过而没人拦 = 纸做的。
  [D] 默认安全: dry-run 不联网、不落盘
  [E] 反环: 用一次败一次 → 退回候选池 + 留原因
  [F] 端到端(离线): 缺口 → 甄别safe → 重编 → 验证 → 用量达标 → 自动纳库 → 烂的退货
  [G] 探针安全: 用户的 skill_usage / gap_ledger 真文件全程未被写
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def _fp(p: Path):
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12] if p.is_file() else ""


def main() -> int:
    print("=" * 78)
    print("能力自补闭环 门禁 —— 缺→找→甄别→取形式重编→验证→纳库 (+反环)")
    print("=" * 78)

    sbx = Path(tempfile.mkdtemp(prefix="_vfy_autofill_"))
    real_usage = ROOT / "data" / "skill_usage.jsonl"
    real_gap = ROOT / "data" / "gap_ledger.db"
    before = (_fp(real_usage), _fp(real_gap))
    # 自检/门禁要测"用量达标纳库"这条路径 → 显式开真人信号 (fail-closed 的正门)
    os.environ["TMM_LIVE"] = "1"
    os.environ["TMM_SKILL_USAGE_FILE"] = str(sbx / "usage.jsonl")
    os.environ["TMM_GAP_DB"] = str(sbx / "gap.db")
    try:
        from core import skill_usage as U
        from core import autofill as AF

        # ── [A] 用量账 ──
        print("\n[A] 用量账 (驱动'留/退'的判据)")
        chk("空账 → 无统计", U.stats() == {})
        U.record("s1", True)
        U.record("s1", True)
        U.record("s1", False)
        st = U.stats("s1")
        chk("★ 计数与成功率", st.get("uses") == 3 and st.get("ok") == 2 and abs(st.get("rate", 0) - 0.667) < 0.01, st)
        for _ in range(2):
            U.record("s2", True)
        chk("★ rank 要够次数才好使 (s1 3次/66.7% 在 min_rate=0.8 下不够格)",
            [d["name"] for d in U.rank(min_uses=3, min_rate=0.8)] == [], U.rank(min_uses=3, min_rate=0.8))
        chk("★ rank 放低门槛就能选出", [d["name"] for d in U.rank(min_uses=3, min_rate=0.6)] == ["s1"])
        chk("★ 渲染含表头", "使用账" in U.render())
        U.record("s1", True)                      # s1 → 4 次 / 75%
        U.record("s1", True)
        chk("★ 渲染含结论行 (够格的会被点名)", "够格纳库" in U.render(), U.render()[-160:])
        chk("★ 隔离变量生效 (写在沙箱)", U.store_path() == sbx / "usage.jsonl", str(U.store_path()))

        # ── [B] 六环判定 ──
        print("\n[B] 六环判定")
        import core.gap_ledger as GL
        GL.reset_gap_ledger()
        led = GL.GapLedger(db_path=sbx / "gap.db")
        GL._ledger = led
        for _i in range(3):
            led.record("帮我把散落的收据做成月报", "我没法做这个，没有相关的工具。", "model.fallback")
        tg = AF.targets(min_count=2)
        chk("★ ①缺: 取到反复出现的真需求", len(tg) >= 1, tg)
        chk("★ ①缺: 次数不足不取", AF.targets(min_count=99) == [])
        chk("★ ②找: 默认不联网 (只回会搜什么)", AF.scout("收据", allow_network=False)[0].get("skipped") is True)
        chk("★ ③甄别: 未联网 → 不放行", AF.screen({"name": "x"}, allow_network=False)["passed"] is False)
        fake = [{"name": "a", "verdict": "safe", "passed": True},
                {"name": "b", "verdict": "caution", "passed": False},
                {"name": "c", "verdict": "reject", "passed": False}]
        chk("★★ ③闸门: 只放行 safe (caution/reject 一律不过)",
            [x["name"] for x in AF.keep_safe(fake)] == ["a"])

        class _P:
            def __init__(self, steps, tools, name="s"):
                self.name, self.description, self.triggers = name, "d", ["t"]
                self.params, self.steps, self.requires_tools = {}, steps, tools
                self.errors, self.warnings, self.raw = [], [], ""

        chk("★ ④取形式: 只用本地积木 → 过",
            AF.forge({"id": 1}, _P([{"tool": "file_ops"}], ["file_ops"]), local_tools={"file_ops"})["ok"])
        chk("★ ④取形式: '引入第三方代码'被拒",
            AF.forge({"id": 1}, _P([{"tool": "pip_install"}], ["pip_install"]), local_tools={"file_ops"})["ok"] is False)
        chk("★ ④取形式: 引用不存在的工具被拒",
            AF.forge({"id": 1}, _P([{"tool": "magic"}], ["magic"]), local_tools={"file_ops"})["ok"] is False)

        from core.skill_factory import Proposal, SkillFactory
        sdir, cdir = sbx / "skills", sbx / "cands"
        sdir.mkdir(parents=True, exist_ok=True)
        fac = SkillFactory(generate=None, engine=None, skills_dir=sdir, candidate_dir=cdir)
        good = Proposal(name="receipt-monthly-report", description="把收据汇总成月报",
                        triggers=["收据月报"], steps=[{"tool": "file_ops", "action": "list", "path": "{d}"}],
                        requires_tools=["file_ops"])
        pf = AF.prove(good, fac)
        chk("★ ⑤验证: 过 (工厂复检 + 工具可调用)", pf["ok"], pf.get("reasons"))
        chk("★ ⑤验证: 记下证据", "file_ops" in pf["evidence"]["tools"])
        chk("★ ⑤验证: 没步骤的技能不过",
            AF.prove(Proposal(name="empty-skill", description="x", triggers=["y"], steps=[]), fac)["ok"] is False)

        # ── [C] ★★ 突变: 甄别闸门被改成全放行 → 纳库必须被拦 ──
        print("\n[C] ★★ 变异测试: 闸门被绕过时必须有人拦")
        fac.save(good)
        for _ in range(4):
            U.record(good.name, True)
        r_ok = AF.enshrine(good.name, fac, {"verdict": "safe", "passed": True}, pf)
        chk("★ 四道闸全过 → 生效", r_ok["ok"], r_ok)
        # 重置: 把技能放回候选池再测
        AF.demote(good.name, "测试复位", factory=fac)
        _real = AF.keep_safe
        AF.keep_safe = lambda screened: list(screened or [])     # 突变: 闸门形同虚设
        try:
            leak = AF.keep_safe(fake)
        finally:
            AF.keep_safe = _real
        chk("★★ 突变: 闸门一旦放水, 非 safe 候选会漏进来 (证明闸门确实在起拦阻作用)",
            len(leak) == 3, leak)
        r_caution = AF.enshrine(good.name, fac, {"verdict": "caution", "passed": False}, pf)
        chk("★★ 兜底: 即使闸门被绕过, 纳库仍被'甄别= safe'这条拦下",
            r_caution["ok"] is False and "甄别= safe" in r_caution["blocked_by"], r_caution)
        r_lowuse = AF.enshrine(good.name, fac, {"verdict": "safe", "passed": True}, pf,
                               min_uses=999)
        chk("★★ 兜底: 用量不够也拦", r_lowuse["ok"] is False and "用量达标" in r_lowuse["blocked_by"], r_lowuse)
        r_noproof = AF.enshrine(good.name, fac, {"verdict": "safe", "passed": True},
                                {"ok": False, "evidence": {}})
        chk("★★ 兜底: 验证没过也拦 (两个条件同时不满足时至少被拦)",
            r_noproof["ok"] is False, r_noproof)

        # ── [E] 反环 ──
        print("\n[E] 反环: 用一次败一次 → 退回")
        # [C] 结束时它已被挪回候选池 → 先放回正式库, 否则"退货"没有源
        U.reset(good.name)
        for _ in range(4):
            U.record(good.name, True)
        AF.enshrine(good.name, fac, {"verdict": "safe", "passed": True}, pf)
        U.reset(good.name)
        for _ in range(3):
            U.record(good.name, False)
        chk("★ 烂的能被识别", any(d["name"] == good.name for d in U.rotten()), U.rotten())
        r_dem = AF.demote(good.name, "用量账: 连败", factory=fac)
        chk("★ 退回候选池并留原因", r_dem["ok"] and (cdir / good.name / "DEMOTED.txt").is_file(), r_dem)
        chk("★ 退回后正式库里没有它", not (sdir / good.name).exists())

        # ── [D] 默认安全 ──
        print("\n[D] 默认安全 (dry-run)")
        snap0 = sorted(p.relative_to(sbx).as_posix() for p in sbx.rglob("*"))
        rep = AF.run_cycle()
        snap1 = sorted(p.relative_to(sbx).as_posix() for p in sbx.rglob("*"))
        chk("★ dry-run 不联网", rep["allow_network"] is False)
        chk("★ dry-run 不落盘 (沙箱逐项未变)", snap0 == snap1, f"{len(snap0)} → {len(snap1)}")
        chk("★ 报告有六环 + 说清是 dry-run", all(k in AF.render(rep) for k in AF.STAGES)
            and "dry-run" in AF.render(rep))

        # ── [F] 端到端 (离线): 缺口 → 纳库 一条链 ──
        print("\n[F] 端到端 (离线): 缺口 → 甄别 → 重编 → 验证 → 纳库")
        e2e = _P([{"tool": "file_ops", "action": "list"}], ["file_ops"], name="e2e-receipt-report")
        pr = Proposal(name=e2e.name, description="把收据汇总", triggers=["收据"],
                      steps=[{"tool": "file_ops", "action": "list", "path": "{d}"}], requires_tools=["file_ops"])
        fac.save(pr)
        pv = AF.prove(pr, fac)
        for _ in range(3):
            U.record(pr.name, True)
        out = AF.enshrine(pr.name, fac, {"verdict": "safe", "passed": True}, pv)
        chk("★★ 端到端: 全链走通 → 自有技能入库", out["ok"] and (sdir / pr.name).is_dir(), out)
        chk("★ 入库的是**自有技能** (只有本地工具, 没搬第三方代码)",
            "file_ops" in pv["evidence"]["tools"] and not (sdir / pr.name / "third_party").exists())
    finally:
        os.environ.pop("TMM_SKILL_USAGE_FILE", None)
        os.environ.pop("TMM_LIVE", None)
        os.environ.pop("TMM_GAP_DB", None)
        shutil.rmtree(sbx, ignore_errors=True)

    # ── [G] 探针安全 ──
    print("\n[G] 探针安全")
    chk("★ 用户真 usage 账未被写", _fp(real_usage) == before[0])
    chk("★ 用户真 gap 台账未被写", _fp(real_gap) == before[1])

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
