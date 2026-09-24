"""本地自救 (core/self_rescue.py + /gap rescue 接线) — 常驻验证器

为什么要有这一份: 第 1 步记账只解决"看见缺口", 第 2 步要解决"**先自己救**"。
而这条路最容易出两种错, 都必须拦住:

  ① **误判"救不了"** —— 把本地其实能造的活记成 blocked_local, 人会去联网找外来代码,
     既浪费又引入风险 (正确方向: 漏判比误判安全, 不确定就说"需人看")。
     实测踩到: 一条 "CSV 转 Excel" 因**工厂沙箱没给输入文件**而实跑失败
     → 第一版判了 blocked_local (假) → 现细分出 input_limited 记 open。
  ② **粉饰"已解决"** —— 候选就绪就标 built。候选 ≠ 生效, 必须人工确认后才算。

单元测试 (tests/test_self_rescue.py) 覆盖分类/提示/映射; 这一份专测**接线与不变量**:
  A 策略表: 七类全覆盖 · plan 分流正确 (本地可救 vs 该出门找)
  B ★★ 真跑接线: /gap rescue 三条路径 (缺依赖 / 救不了 / 只出方案不花调用)
  C ★ 工厂失败细分: input_limited→open · 未知工具→blocked_local · 说不清→open
  D ★★ 硬不变量: 不自动装依赖 · 不自动生效技能 · 不假标 built
  E ★ 出口: /gap outside 只收 blocked_local (没本地试过的不许进门)
  F 隔离: TMM_GAP_DB 生效 (验证不写用户账)
  G ★ 变异: 拆细分 (一律判 blocked_local) → 必红; 把候选标成 built → 必红
  H 零副作用: 真台账 + 真对话库指纹不变 · 不留实跑沙箱残留
"""
import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F = [], []
REAL_GAP = ROOT / "data" / "gap_ledger.db"
REAL_CHAT = ROOT / "data" / "chat_sessions.db"


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def _fp(p: Path, tbl: str):
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return (c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0],
                    c.execute(f"SELECT COALESCE(MAX(id),0) FROM {tbl}").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


def build(sandbox: Path):
    import core.mcp_client as _mc
    _mc.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
        load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [],
        tools=[], get_tool=lambda *a, **k: None)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.knowledge import KnowledgeEngine
    from config.settings import DATA_DIR
    import core.gap_ledger as _gl
    from core.gap_ledger import GapLedger
    from storage.session_store import SessionStore

    cfg = _load_config()
    pl = Level4Pipeline(ModelClient(cfg), cfg)
    try:
        pl._ke = KnowledgeEngine(DATA_DIR)
    except Exception:
        pass
    sb = GapLedger(db_path=sandbox / "gaps.db", data_dir=sandbox)
    _gl._ledger = sb
    pl.gap_ledger = sb
    pl.sessions = SessionStore(sandbox / "sessions.db")     # ★ 真跑 process → 必须沙箱
    return pl, sb


class StubFactory:
    """假工厂: 只用于"细分判定"的确定性测试 (不花真调用)。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def build(self, description, name_hint="", smoke=True):
        self.calls.append(description)
        return self.result


INPUT_LIMIT = {"ok": False, "stage": "实跑",
               "errors": ["step 'read_csv': File not found: tmp/_factory_smoke/x/out.txt"
                          " (resolved to tmp/_factory_smoke/x/out.txt)"]}
NO_LOCAL = {"ok": False, "stage": "质检", "errors": ["step1 用了未知工具: magic"]}
FACTORY_OK = {"ok": True, "name": "csv-to-excel", "path": "skill_candidates/csv-to-excel",
              "self_test": {"steps": 3}, "errors": []}


def main() -> int:
    SB = Path(tempfile.mkdtemp(prefix="_vfy_rescue_"))
    gap_b, chat_b = _fp(REAL_GAP, "gaps"), _fp(REAL_CHAT, "messages")
    smoke_before = sorted(p.name for p in (ROOT / "tmp" / "_factory_smoke").glob("*")) \
        if (ROOT / "tmp" / "_factory_smoke").is_dir() else []
    try:
        from core.self_rescue import (STRATEGIES, attempt, classify_factory_failure,
                                      dep_hint, local_unsalvageable, plan, status_for)
        pl, led = build(SB)

        # ── A 策略表 ──
        print("        —— A 策略表与分流 ——")
        cats = ("no_tool", "skill_failed", "missing_dep", "no_entry",
                "missing_arg", "unknown_action", "model_refuse")
        chk("七类全覆盖", all(c in STRATEGIES for c in cats),
            str([c for c in cats if c not in STRATEGIES]))
        local_cats = [c for c in cats if plan({"category": c})["local"]]
        chk("本地可救 = no_tool/skill_failed/missing_dep",
            sorted(local_cats) == ["missing_dep", "no_tool", "skill_failed"], str(local_cats))
        chk("人工类都给了提示 (不许空手说'救不了')",
            all(plan({"category": c})["hint"] for c in cats if not plan({"category": c})["local"]))
        chk("未知分类默认人工 (不假装能自救)", plan({"category": "??"})["needs_human"])
        chk("plan 是纯函数", plan({"category": "no_tool"}) == plan({"category": "no_tool"}))

        # ── C 工厂失败细分 ──
        print("        —— C ★ 工厂失败细分 ——")
        chk("input_limited → open",
            classify_factory_failure(INPUT_LIMIT)["status"] == "open")
        chk("未知工具 → blocked_local",
            classify_factory_failure(NO_LOCAL)["status"] == "blocked_local")
        chk("说不清 → open (不轻易判'救不了')",
            classify_factory_failure({"stage": "自测", "errors": ["链路不可达"]})["status"] == "open")
        chk("缺依赖 → blocked_local",
            classify_factory_failure({"stage": "实跑", "errors": ["No module named 'x'"]})["status"]
            == "blocked_local")

        # ── D 硬不变量 ──
        print("        —— D ★★ 硬不变量 ——")
        g = led.record("把一份 CSV 转成 Excel", "抱歉，我无法制作 Excel。", "model.fallback")
        gap = led.get(g["id"])
        # ① 候选就绪 ≠ built
        r_ok = asyncio.run(attempt(gap, StubFactory(FACTORY_OK)))
        st_ok, _ = status_for(r_ok)
        chk("★ 候选就绪只标 building (不假标 built)", st_ok == "building", st_ok)
        # ② 缺依赖只给命令, 不试工厂
        f = StubFactory(FACTORY_OK)
        g2 = led.record("识别图片里的字", "No module named 'pytesseract'", "tool")
        r_dep = asyncio.run(attempt(led.get(g2["id"]), f))
        chk("★ 缺依赖不去编技能 (不浪费调用)", f.calls == [], str(f.calls))
        chk("★ 缺依赖只给装法 (不自动装)",
            "pip install pytesseract" in r_dep.detail
            and "已安装" not in r_dep.detail and "正在安装" not in r_dep.detail)
        chk("待装依赖仍是 open (装完才算解决)", status_for(r_dep)[0] == "open")
        # ③ 救不了如实标
        g3 = led.record("调用 office_cli", "工具 'office_cli' 没有可调用入口 (需要 run()/execute()), 未执行", "skill")
        r_no = asyncio.run(attempt(led.get(g3["id"]), None))
        chk("★ 救不了 → blocked_local (如实)", status_for(r_no)[0] == "blocked_local")

        # ── B ★★ 真跑接线 ──
        print("        —— B ★★ 真跑 /gap rescue ——")
        out = asyncio.run(pl.process("/gap", mode_override="craft"))
        chk("/gap 被 cmd.gap 接手", out.get("route") == "cmd.gap", str(out.get("route"))[:50])
        # 缺依赖路径 (真跑, 不花调用)
        rr = asyncio.run(pl.process(f"/gap rescue {g2['id']}", mode_override="craft"))
        chk("★ /gap rescue 缺依赖: 给装法 + 状态不变",
            "pip install" in (rr.get("response") or "") and rr.get("gap_rescue") == "dep_hint", 
            str(rr.get("response"))[:80])
        # 救不了路径
        rr2 = asyncio.run(pl.process(f"/gap rescue {g3['id']}", mode_override="craft"))
        chk("★ /gap rescue 救不了: 明说 + 记账本",
            rr2.get("gap_rescue") == "cannot" and led.get(g3["id"])["status"] == "blocked_local",
            str(rr2.get("response"))[:80])
        # 只出方案不花调用
        rr3 = asyncio.run(pl.process(f"/gap rescue {g['id']} nf", mode_override="craft"))
        chk("★ /gap rescue <id> nf: 只出方案不花调用",
            rr3.get("gap_rescue") == "factory_skipped", str(rr3.get("gap_rescue")))
        # why 带自救方案
        why = asyncio.run(pl.process(f"/gap why {g['id']}", mode_override="craft")).get("response") or ""
        chk("why 带本地自救方案", "本地自救方案" in why, why[:80])
        # 用法提示
        usage = asyncio.run(pl.process("/gap 乱写", mode_override="craft")).get("response") or ""
        chk("用法含 rescue/outside", "rescue" in usage and "outside" in usage, usage[:80])

        # ── E 出口 ──
        print("        —— E ★ 出口 /gap outside ——")
        os_ = asyncio.run(pl.process("/gap outside", mode_override="craft"))
        body = os_.get("response") or ""
        chk("★ 只收 blocked_local 的缺口", f"#{g3['id']}" in body, body[:120])
        chk("★ 本地没试过的不进门 (open 的不在)",
            f"#{g['id']}" not in body and f"#{g2['id']}" not in body, body[:160])
        # 账本级校验
        unsal = local_unsalvageable(led)
        chk("local_unsalvageable 与 /gap outside 口径一致",
            [x["id"] for x in unsal] == [g3["id"]], str([x["id"] for x in unsal]))

        # ── D2 probe ──
        print("        —— D2 probe 硬不变量 ——")
        n0 = led.stats()["total_hits"]
        pr = asyncio.run(pl.process(f"/gap rescue {g['id']}", probe=True, mode_override="craft"))
        chk("★ probe 下 /gap rescue 被拦", pr.get("route") == "probe.skip", str(pr.get("route"))[:50])
        chk("★ probe 未改账本", led.stats()["total_hits"] == n0)

        # ── F 隔离 ──
        print("        —— F 隔离 ——")
        chk("门禁 runner 注入 TMM_GAP_DB",
            "TMM_GAP_DB" in (ROOT / "scripts" / "hermes_verify.py").read_text(encoding="utf-8", errors="replace"))

        # ── G 变异 ──
        print("        —— G ★ 变异 ——")
        tgt = ROOT / "core" / "self_rescue.py"
        orig = tgt.read_bytes(); h0 = hashlib.md5(orig).hexdigest()
        nl = "\r\n" if b"\r\n" in orig else "\n"
        src = orig.decode("utf-8").replace("\r\n", "\n")
        import importlib
        import core.self_rescue as _sr
        # 变异1: 拆掉 input_limited 细分 —— 状态仍会落到 open (安全), 但**丢掉那条
        #   精确解释** ("沙箱没给输入文件, 不代表本地造不出")。人看不到解释就会误以为
        #   自己缺能力 → 所以这条分支的价值在于**给人正确的判断依据**, 断言要看 reason。
        m1 = src.replace('    if _INPUT_LIMIT.search(txt):\n', '    if False:\n', 1)
        chk("变异锚点1命中", m1 != src)
        tgt.write_bytes(m1.replace("\n", nl).encode("utf-8"))
        importlib.reload(_sr)
        c1 = _sr.classify_factory_failure(INPUT_LIMIT)
        chk("★ 变异1 被抓: 丢掉 input_limited 判定 (解释退化成'说不清')",
            c1["reason"] != "input_limited", str(c1))
        tgt.write_bytes(orig); importlib.reload(_sr)
        # 变异3 (最危险的方向): 把"说不清"直接判成 blocked_local → 会把人推向外来代码。
        #   C 组"说不清 → open"的断言就是拦它的 (C 组已跑过, 这里单独复验会被抓到)。
        m3 = src.replace("""    return {"status": "open", "reason": "unknown",""",
                         """    return {"status": "blocked_local", "reason": "unknown",""", 1)
        chk("变异锚点3命中", m3 != src)
        tgt.write_bytes(m3.replace("\n", nl).encode("utf-8"))
        importlib.reload(_sr)
        c3 = _sr.classify_factory_failure({"stage": "自测", "errors": ["链路不可达"]})
        chk("★ 变异3 被抓: '说不清'被鲁莽判成『救不了』(会推人去联网找)",
            c3["status"] == "blocked_local", str(c3))
        tgt.write_bytes(orig); importlib.reload(_sr)
        # 变异2: 候选就绪直接标 built (粉饰已解决)
        m2 = src.replace('    if result.ok:\n        return "building",', 
                         '    if result.ok:\n        return "built",', 1)
        chk("变异锚点2命中", m2 != src)
        tgt.write_bytes(m2.replace("\n", nl).encode("utf-8"))
        importlib.reload(_sr)
        from core.self_rescue import RescueResult as _RR
        c2 = _sr.status_for(_RR(ok=True, kind="factory_candidate", detail="候选 x"))
        chk("★ 变异2 被抓: 候选就绪被粉饰成 built", c2[0] == "built", str(c2))
        tgt.write_bytes(orig); importlib.reload(_sr)
        chk("变异后已还原 (md5 一致)", hashlib.md5(tgt.read_bytes()).hexdigest() == h0)

        # ── H 零副作用 ──
        print("        —— H 零副作用 ——")
        chk("真台账指纹未变", _fp(REAL_GAP, "gaps") == gap_b, f"{gap_b} -> {_fp(REAL_GAP, 'gaps')}")
        chk("真对话库指纹未变", _fp(REAL_CHAT, "messages") == chat_b,
            f"{chat_b} -> {_fp(REAL_CHAT, 'messages')}")
        smoke_after = sorted(p.name for p in (ROOT / "tmp" / "_factory_smoke").glob("*")) \
            if (ROOT / "tmp" / "_factory_smoke").is_dir() else []
        chk("实跑沙箱无残留", smoke_after == smoke_before, f"{smoke_before} -> {smoke_after}")
        chk("项目内无本验证器沙箱残留", not list((ROOT / "tmp").glob("_vfy_rescue_*")))
    finally:
        shutil.rmtree(SB, ignore_errors=True)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
