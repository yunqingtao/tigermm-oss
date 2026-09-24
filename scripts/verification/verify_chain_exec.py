r"""多任务链条执行契约 —— 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: 「用户一条多步话 → 拆解 → 逐步执行 → 链末产物落盘」这条链, 每一环都必须真成立。
为什么独立成门: 门禁管路由/契约/不变量, 深测台管单个工具/技能能跑通, **没有一层测整条链**。
本门把 2026-09-22 实测抓到的四个真缺陷钉住 (全部是"链条半途坏掉但没人发现"的形状):
  ① 技能**自己的触发词**命中, 却被"产出守卫"挡下 → 整条技能链不跑, 用户拿到罐头兜底
  ② 只 1 个多步词 + 明确"存到 X" → 不走规划链 → 落分析链靠模型自由发挥 (时好时坏)
  ③ 引用字段名与工具真实返回不一致 → 静默回退成整个结果字典 → **产物内容是一坨字典文本**
  ④ 多步词数够时确实走规划链 (反向: 别为了修 ② 把 ④ 弄坏)

跑法:
    python -B scripts/verification/verify_chain_exec.py

隔离: 自带四变量自我隔离 (只在未设时才设); 全程打桩 LLM 与网关, 零成本零副作用;
      ★ 沙箱外写盘硬闸 —— 技能默认落点是用户桌面 (实测踩过), 本门只允许写 tmp 沙箱。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable

_ISO = Path(tempfile.gettempdir()) / ("tmm_chain_iso_%d" % os.getpid())
_ISO.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMM_SESSION_DB", str(_ISO / "sessions.db"))
os.environ.setdefault("TMM_GAP_DB", str(_ISO / "gap.db"))
os.environ.setdefault("TMM_PERCEPTION_FILE", str(_ISO / "perception.json"))
os.environ.setdefault("TMM_USAGE_LOG", str(_ISO / "usage.jsonl"))
sys.path.insert(0, str(ROOT))
import logging          # noqa: E402
logging.disable(logging.CRITICAL)   # 门禁输出要干净 (不让 skill_loader/auto-fix 日志混进来)

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))


SB = ROOT / "tmp" / "_chain_gate"
SB.mkdir(parents=True, exist_ok=True)


def main() -> int:
    print("=" * 78)
    print("多任务链条执行契约 —— 拆解 / 执行 / 产物 三层")
    print("=" * 78)

    # ── ① 执行机制的纯函数级契约 (不依赖引擎, 先跑) ──
    print("\n[1] PlanExecutor 引用解析 (字段名对不上时不许塞整个字典)")
    import asyncio
    from core.task_planner import PlanExecutor

    class GW:
        def __init__(self, script):
            self.script = script
            self.calls = []

        async def call(self, tool, action="", timeout=None, **kw):
            self.calls.append({"tool": tool, "action": action, "kw": kw})
            return dict(self.script.get(f"{tool}:{action}", self.script.get(tool, {"success": True, "output": "ok"})))

    async def _case(params, script):
        gw = GW(script)
        ex = PlanExecutor(gw)
        r = await ex.execute([
            {"step": 1, "tool": "file_ops", "action": "read", "params": {"path": "x"}, "description": "读"},
            {"step": 2, "tool": "file_ops", "action": "write", "params": params,
             "depends_on": 1, "description": "写"},
        ])
        w = [c for c in gw.calls if c["action"] == "write"]
        return (w[0]["kw"] if w else {}), r

    async def part1():
        # 字段名对得上 → 取该字段
        kw, _ = await _case({"path": str(SB / "a.txt"), "content": "$step1.content"},
                            {"file_ops:read": {"success": True, "content": "文件正文"}})
        chk("★ 字段名命中 → 取该字段原值", kw.get("content") == "文件正文",
            f"content={kw.get('content')!r}")

        # ★ 字段名对不上 → 回退到"主文本", 不许是整字典 (这是修掉的缺陷)
        kw, r = await _case({"path": str(SB / "b.txt"), "content": "$step1.content"},
                            {"file_ops:read": {"success": True, "output": "文件正文"}})
        got = str(kw.get("content"))
        chk("★ 字段名对不上 → 回退主文本 (不是整字典)",
            got == "文件正文" and "{'success'" not in got, f"content={got[:80]!r}")
        chk("★ 回退行为如实带出 (field_notes 非空, 不静默)",
            bool(r.get("field_notes")), f"field_notes={r.get('field_notes')}")

        # 无主文本可回退 → 退回整个结果, 但仍记账 (不假装成功解析)
        kw, r = await _case({"path": str(SB / "c.txt"), "content": "$step1.content"},
                            {"file_ops:read": {"success": True, "count": 3}})
        # ★ 判据修正: 该情形下"主文本"字段根本不存在 (只有 count), 所以断言点应是
        #   「退回整结果 + field_notes 如实记账」, 而不是内容里含 'content' 字样。
        chk("无主文本时如实记账 (退回整结果 + field_notes 说明无主文本)",
            bool(r.get("field_notes")) and "无主文本" in " ".join(r.get("field_notes") or []),
            f"notes={r.get('field_notes')} content={str(kw.get('content'))[:50]}")

    asyncio.run(part1())

    # ── ②③ 路由层契约 (需要引擎) ──
    print("\n[2] 路由层: 触发词主导整句 / 单多步词+明确落盘")
    import types
    import core.mcp_client as _mcp_mod
    _mcp_mod.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
        load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [], tools=[],
        get_tool=lambda *a, **k: None, get_all_tools=lambda *a, **k: [], servers={},
        refresh_all=None)
    import core.perception as _perc
    _perc.PERCEPTION_FILE = SB / "perception.json"
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline

    PL = Level4Pipeline(ModelClient(_load_config()), _load_config())
    CALLS = []

    async def gw2(tool, action="", **kw):
        # 沙箱外写盘一律拦下 (技能默认落桌面 —— 本门绝不许污染用户桌面)
        _p = str(kw.get("path") or kw.get("file") or "")
        # ★ 没给路径也要拦 (默认落桌面) —— 实测本门自己曾把产物写进用户桌面
        if tool in ("tiger_office", "file_ops", "diagram") and not _p.lower().startswith(str(SB).lower()):
            CALLS.append({"tool": tool, "action": action, "blocked": True})
            return {"success": True, "output": "[拦截: 写盘不在沙箱]", "stub": True}
        CALLS.append({"tool": tool, "action": action, "kw": {k: str(v)[:60] for k, v in kw.items()}})
        if tool in ("send_email", "win32_input", "windows_desktop", "browser", "shell_exec"):
            return {"success": True, "output": "[拦截: 副作用工具]", "stub": True}
        from core.pipeline import Level4Pipeline as _L4
        return await _REAL(tool, action=action, **kw)

    _REAL = PL.gateway.call
    PL.gateway.call = gw2

    async def gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
        return {"text": "（桩）", "think": "", "model": model, "elapsed": 0.0,
                "error": None, "tool_calls": None}

    PL.model_client.generate = gen

    async def run(msg):
        CALLS.clear()
        return await PL.process(msg, probe=True, mode_override="craft")

    # ② 触发词主导整句 → 技能链必须跑起来 (修掉的缺陷: 曾落到罐头兜底, 零工具调用)
    r = asyncio.run(run("电脑状态报告"))
    got = [(c["tool"], c.get("action")) for c in CALLS]
    chk("★ 触发词主导整句 → 走技能层 (不是罐头兜底)",
        str(r.get("route") or "").startswith("skill."), f"route={r.get('route')}")
    chk("★ 该技能声明的工具真被调到 (链条真跑)",
        any(t == "system_info" for t, _ in got) and any(t == "tiger_office" for t, _ in got),
        f"tools={got[:4]}")

    # ③ 1 个多步词 + 明确"存到 X" → 走规划链 (曾落分析链, 靠模型自由发挥)
    hit = PL._match_planner("读一下 a.txt，然后存到 b.txt", {})
    chk("★ 「然后…存到 X」被判为多步任务", hit is True, f"_match_planner={hit}")
    chk("反向: 单纯「看看这个文件」不误判为多步",
        PL._match_planner("看看这个文件", {}) is False)
    chk("反向: 「直接做，别走规划」前缀仍豁免", PL._match_planner("直接做，先A再B", {}) is False)
    chk("原判据不回退: 2 个多步词仍判多步",
        PL._match_planner("先查磁盘，再写下结果", {}) is True)

    # ④ 触发词不主导时不许放行 (别把守卫放松成不设防)
    from core.skill_loader import get_skill_index
    eng = get_skill_index()
    dag = eng.get_dag("pc-checkup")
    chk("★ 泛化请求不含触发词 → 不豁免 (守卫仍在)",
        PL._trigger_dominant("帮我看看这份周报", dag) is False)
    chk("★ 触发词主导 → 豁免成立", PL._trigger_dominant("电脑状态报告", dag) is True)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    import shutil
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SB, ignore_errors=True)
        shutil.rmtree(_ISO, ignore_errors=True)
