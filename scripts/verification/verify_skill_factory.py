"""技能工厂 (core/skill_factory.py + /skill 命令) — 常驻验证器

为什么要有这一份: 技能工厂是"让 TMM 越用越强"的入口 —— 它会把**模型生成的东西**
写进技能目录。这类"由生成物改写系统能力"的通路, 一旦门禁漏了, 后果是
**不可信的能力悄悄上线**(而所有门禁还显示全绿)。所以它的每道门都必须被实测。

跑法:
    python -B scripts/verification/verify_skill_factory.py

本验证器守什么:
  A. 四道门各拦一次 (编译 → 质检 → 自测 → 人工确认)
  B. ★★ 自测必须**还原** engine._gateway (忘了还原 = 真管线被 stub 永久接管, 最阴)
  C. ★ 候选池在 tmm_skills **之外** (未确认的技能不得污染技能发现/路由/可达性)
  D. ★ 邻居方法类型守卫 —— 往类里插新方法时, 别把相邻方法的 @staticmethod 挪走
     (真实事故: 插 /skill 处理器时把 _guard_kind 的 @staticmethod 吃掉了 →
      self._guard_kind(msg) 会把 self 当 message → 输入守卫静默失效; py_compile 抓不到)
  E. cmd.skill 路由: 优先级/标志位 + **probe 流量被硬不变量跳过** (验证不得造候选)
  F. 真 tmm_skills 目录未被候选污染 + 工厂对真工具目录的认知正确

离线: generate 用 stub, 候选池/技能目录都用沙箱; 不调任何真模型。
"""
import asyncio
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
sys.path.insert(0, str(ROOT))

from core import skill_factory as SF                                    # noqa: E402
from core.skill_factory import SkillFactory, CANDIDATE_DIR, SKILLS_DIR  # noqa: E402

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


GOOD = {
    "name": "zz-factory-demo", "description": "把一段话扩写成通知并存 Word",
    "tags": ["通知", "扩写"], "triggers": ["扩写成通知", "把这段话写成通知"],
    "params": {"out_path": {"type": "path", "required": False, "desc": "输出路径"}},
    "steps": [
        {"id": "draft", "tool": "llm", "input": {"prompt": "$message"}, "output": "text",
         "on_error": "stop"},
        {"id": "save", "tool": "tiger_office", "action": "write_word",
         "input": {"path": "$params.out_path", "content": "$steps.draft.text"}, "on_error": "stop"},
    ],
}
BAD_TOOL = {**GOOD, "name": "zz-factory-bad",
            "steps": [{"id": "s1", "tool": "coffee_machine", "input": {}, "on_error": "stop"}]}


def mk_gen(payload=None, text=None):
    body = text if text is not None else json.dumps(payload or GOOD, ensure_ascii=False)

    async def _g(model, messages, **kw):
        return {"text": body, "error": None}
    return _g


SBX = Path(tempfile.mkdtemp(prefix="_vfy_factory_"))
SK = SBX / "skills"; SK.mkdir()
CAND = SBX / "cand"


def mk_factory(payload=None, text=None):
    from core.skill_loader import SkillWorkflowEngine
    return SkillFactory(generate=mk_gen(payload, text), engine=SkillWorkflowEngine(),
                        candidate_dir=CAND, skills_dir=SK)


def main() -> int:
    try:
        print("=" * 74)
        print("A. 四道门各拦一次")
        print("=" * 74)
        # ① 编译门: 模型没吐出 JSON
        r = asyncio.run(mk_factory(text="抱歉我不会").build("x"))
        chk("编译门: 非 JSON → 停在第 1 道门", r["stage"] == "编译" and not r["ok"], r)
        # ② 质检门: 未知工具
        r = asyncio.run(mk_factory(payload=BAD_TOOL).build("x"))
        chk("质检门: 未知工具 → 停在第 2 道门",
            r["stage"] == "质检" and any("未知工具" in e for e in r["errors"]), r)
        # ③ 自测门: 引擎把工具调用吞掉 → 链路不可达 (stub 引擎只跑第一步)
        class _HalfEngine:
            _gateway = None
            _KNOWN_TOOLS = set()
            def set_gateway(self, g): self._gateway = g
            async def execute_dag(self, dag, params, mgr):
                await self._gateway.call("llm")
                return {"success": True}
        f = SkillFactory(generate=mk_gen(), engine=_HalfEngine(), candidate_dir=CAND, skills_dir=SK)
        r = asyncio.run(f.build("x"))
        chk("自测门: 链路缺调用 → 停在第 3 道门",
            r["stage"] == "自测" and "不可达" in str(r["errors"]), r)
        # ④ 人工确认门: 全过只是"候选", 正式技能目录必须还是空的
        f_ok = mk_factory()
        r = asyncio.run(f_ok.build("把这段话扩写成通知并存成 Word"))
        chk("全过 → 只落候选 (不生效)", r["ok"] and r["stage"].startswith("候选"), r.get("stage"))
        chk("★ 确认前正式技能目录为空 (人工确认门生效)",
            not (SK / "zz-factory-demo").exists() and f_ok.existing_skills() == {},
            str(list(SK.iterdir())))
        # 确认后生效
        pr = f_ok.promote("zz-factory-demo")
        chk("确认后生效 (移入技能目录)", pr["ok"] and (SK / "zz-factory-demo" / "SKILL.md").is_file(), pr)
        chk("候选元数据不带进正式技能", not (SK / "zz-factory-demo" / "meta.json").exists())

        print()
        print("=" * 74)
        print("B. ★★ 自测必须还原 engine._gateway")
        print("=" * 74)
        from core.skill_loader import SkillWorkflowEngine
        eng = SkillWorkflowEngine()
        sentinel = object()
        eng._gateway = sentinel
        f2 = SkillFactory(generate=mk_gen(), engine=eng, candidate_dir=CAND, skills_dir=SK)
        from core.skill_factory import Proposal
        prop = Proposal(name="zz-gw", description="d", tags=["t"], triggers=["测试触发词甲"],
                        params={}, steps=[{"id": "s1", "tool": "llm", "input": {"prompt": "hi"}}])
        st = asyncio.run(f2.self_test(prop))
        chk("自测跑完 gateway 已还原 (成功路径)", eng._gateway is sentinel, f"{type(eng._gateway)}")

        class _Boom:
            _gateway = sentinel
            def set_gateway(self, g): self._gateway = g
            async def execute_dag(self, dag, params, mgr): raise RuntimeError("炸")
        fb = SkillFactory(generate=mk_gen(), engine=_Boom(), candidate_dir=CAND, skills_dir=SK)
        st2 = asyncio.run(fb.self_test(prop))
        chk("自测异常时 gateway 也已还原 (finally 路径)", fb.engine._gateway is sentinel,
            f"error={st2.get('error')}")

        print()
        print("=" * 74)
        print("C. ★ 候选池在 tmm_skills 之外 (未确认不得污染)")
        print("=" * 74)
        chk("默认候选池 = data/skill_candidates (不在 tmm_skills 内)",
            CANDIDATE_DIR != SKILLS_DIR and SKILLS_DIR not in CANDIDATE_DIR.parents
            and CANDIDATE_DIR not in SKILLS_DIR.parents,
            f"cand={CANDIDATE_DIR} skills={SKILLS_DIR}")
        real_skills = sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())
        chk("真 tmm_skills 里没有候选残留 (名字带 zz- 的)",
            not [n for n in real_skills if n.startswith("zz-")], str(real_skills))

        print()
        print("=" * 74)
        print("D. ★ 邻居方法类型守卫 (往类里插方法时别挪走相邻的 @staticmethod)")
        print("=" * 74)
        from core.pipeline import Level4Pipeline
        must_static = ["_guard_kind", "_is_explicit_model", "_is_pure_producer",
                       "_mode_pick_model", "_fmt_plan_step"]
        bad = [n for n in must_static
               if not isinstance(inspect.getattr_static(Level4Pipeline, n, None), staticmethod)]
        chk("★ 既有 staticmethod 全部保持 staticmethod", not bad, f"被挪走了: {bad}")
        # 行为校验 (比类型更直接): 输入守卫对噪声/空白要给对结论
        got = [Level4Pipeline._guard_kind(x) for x in ["", "   ", "？？？", "写份周报"]]
        chk("★ _guard_kind 行为正确 (self 没被当 message)",
            got[0] == "empty" and got[1] == "empty" and got[2] == "noise" and got[3] is None, str(got))
        chk("_run_cmd_skill 是普通方法 (要用 self.model_client)",
            not isinstance(inspect.getattr_static(Level4Pipeline, "_run_cmd_skill", None), staticmethod))

        print()
        print("=" * 74)
        print("E. cmd.skill 路由: 标志位 + probe 被硬不变量跳过")
        print("=" * 74)
        from main import _load_config
        from core.model_client import ModelClient
        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        rs = {r.name: r for r in pl.routes.routes()}
        rt = rs.get("cmd.skill")
        chk("cmd.skill 已注册", rt is not None)
        if rt:
            chk("优先级 = 命令级 (1000)", rt.priority == 1000, str(rt.priority))
            chk("★ writes_state=True (它会落候选/改技能)", rt.writes_state is True)
            chk("★ probe_safe=False (验证流量不得造候选)", rt.probe_safe is False)
            chk("匹配 /skill 与 /skill build x", rt.match("/skill", {}) and rt.match("/skill build x", {}))
            chk("不误匹配 /skills 与 skillx", not rt.match("/skills", {}) and not rt.match("skillx", {}))
        # probe 端到端: 不造候选 + 不落到 cmd.skill
        cand_before = sorted(p.name for p in CAND.iterdir()) if CAND.is_dir() else []
        async def _fake(model, messages, **kw): return {"text": "stub", "error": None}
        pl.model_client.generate = _fake
        res = asyncio.run(pl.process("/skill build 把这段话扩写成通知", probe=True, mode_override="craft"))
        cand_after = sorted(p.name for p in CAND.iterdir()) if CAND.is_dir() else []
        chk("★ probe 流量不产生候选 (硬不变量拦住)", cand_before == cand_after, f"{cand_before} → {cand_after}")
        chk("★ probe 流量没落到 cmd.skill", res.get("route") != "cmd.skill", str(res.get("route")))

        print()
        print("=" * 74)
        print("G. ★ 实跑门: 真工具跑一遍 (stub 自测验不出'选错工具')")
        print("=" * 74)

        class _FakeEng:
            """模拟 execute_dag: 真调 gateway; 第二轮(实跑)按设定成败。"""
            def __init__(self, smoke_ok=True):
                self._gateway = None
                self._generate = None
                self.calls = 0
                self.smoke_ok = smoke_ok
            def set_gateway(self, g): self._gateway = g
            def set_generate(self, g): self._generate = g
            async def execute_dag(self, dag, params, mgr):
                self.calls += 1
                res = {}
                for s in dag.steps:
                    if s.get("tool") in ("llm", "knowledge"):
                        continue
                    r = await self._gateway.call(s["tool"], action=s.get("action", ""))
                    res[s.get("id")] = {"success": bool(r.get("success"))}
                if self.calls >= 2 and not self.smoke_ok:
                    return {"success": False, "error": "step 'save': Access denied (模拟)",
                            "results": res}
                return {"success": True, "results": res}

        class _OkGW:
            async def call(self, tool, action="", **kw):
                return {"success": True, "output": "[fake]"}

        # ★ 用独立沙箱: A 段已把 zz-factory-demo 提升为正式技能, 同名会被质检挡在"重名"
        SK2 = SBX / "skills_smoke"; SK2.mkdir(exist_ok=True)
        CAND2 = SBX / "cand_smoke"; CAND2.mkdir(exist_ok=True)
        ok_eng = _FakeEng(smoke_ok=True)
        f_ok = SkillFactory(generate=mk_gen(), engine=ok_eng, gateway=_OkGW(),
                            candidate_dir=CAND2, skills_dir=SK2)
        r_ok = asyncio.run(f_ok.build("把这段话扩写成通知并存成 Word"))
        chk("实跑通过 → 落候选", r_ok["ok"] and r_ok.get("smoke", {}).get("ok"), r_ok.get("stage"))
        # 坏用例用**独立候选池** (否则会被上一条成功用例留下的候选干扰判断)
        CAND3 = SBX / "cand_smoke_bad"; CAND3.mkdir(exist_ok=True)
        bad_eng = _FakeEng(smoke_ok=False)
        f_bad = SkillFactory(generate=mk_gen(), engine=bad_eng, gateway=_OkGW(),
                             candidate_dir=CAND3, skills_dir=SK2)
        r_bad = asyncio.run(f_bad.build("把这段话扩写成通知并存成 Word"))
        chk("★ 实跑失败 → 卡在'实跑'门, 不落候选",
            (not r_bad["ok"]) and r_bad["stage"] == "实跑"
            and not (CAND3 / "zz-factory-demo").exists()
            and f_bad.list_candidates() == [], r_bad.get("stage"))
        sent_gw, sent_gen = object(), object()

        class _Restore:
            _gateway = sent_gw
            _generate = sent_gen
            def set_gateway(self, g): self._gateway = g
            def set_generate(self, g): self._generate = g
            async def execute_dag(self, dag, params, mgr): return {"success": True, "results": {}}
        e_r = _Restore()
        f_r = SkillFactory(generate=mk_gen(), engine=e_r, gateway=_OkGW(),
                           candidate_dir=CAND2, skills_dir=SK2)
        asyncio.run(f_r.smoke_test(SF.Proposal(
            name="zz-r", description="d", tags=["t"], triggers=["测试触发词丁"], params={},
            steps=[{"id": "s1", "tool": "llm", "input": {"prompt": "hi"}}])))
        chk("★ 实跑后 gateway+generate **两个都**还原",
            e_r._gateway is sent_gw and e_r._generate is sent_gen)

        print()
        print("=" * 74)
        print("H. ★ 动作名必须给 LLM (不给就会编: 编出 excel_read, 真名 scan_excel)")
        print("=" * 74)
        f_a = SkillFactory(generate=mk_gen(), engine=eng)
        cat_a = f_a.catalog()
        tiger_acts = cat_a["tiger_office"]["actions"]
        chk("字典分发(handlers)动作抽到", "write_word" in tiger_acts and "scan_excel" in tiger_acts, str(tiger_acts[:5]))
        chk("if 链动作抽到 (file_ops)", "list" in cat_a["file_ops"]["actions"], str(cat_a["file_ops"]["actions"][:5]))
        chk("内建步骤无动作", cat_a["llm"]["actions"] == [])
        pr = f_a._prompt("做个 Word", "")
        chk("★ 动作名进了提示词", "write_word" in pr and "scan_excel" in pr)
        chk("★ 提示词禁用 POSIX 路径 (~/Desktop 工具不认)", "~/" in pr)
        # ★ 2026-09-19 更新: 原来这条断言"office_cli 不该出现", 因为**当年它没有入口**。
        #   本轮给 office_cli / win32_input 补齐了 run()+action 派发 → 全库 32 个工具**都**有入口,
        #   旧断言的前提不存在了。改成更强的不变量: catalog 给出的工具集
        #   == 实际有 run/execute 入口的工具集 (一个不多一个不少), 且名字都进了提示词。
        import re as _re2
        _tools_dir = ROOT / "tools"
        _callable = set()
        for _f in _tools_dir.glob("*.py"):
            if _f.name.startswith("_"):
                continue
            _s = _f.read_text(encoding="utf-8", errors="replace")
            if _re2.search(r"^(async )?def (run|execute)\(", _s, _re2.M) or \
                    _re2.search(r"^(run|execute) = \w+", _s, _re2.M):
                _callable.add(_f.stem)
        _in_cat = {k for k in cat_a if k not in ("llm", "knowledge")}
        chk("★ catalog 工具集 == 有入口的工具集 (无入口不许进提示词)",
            _in_cat == _callable,
            f"只在catalog={sorted(_in_cat - _callable)} 只在磁盘={sorted(_callable - _in_cat)}")
        chk("★ office_cli 现已可调用 (本轮补的入口) 且动作名被抽到",
            "office_cli" in _in_cat and "write_text" in (cat_a.get("office_cli", {}).get("actions") or []),
            str(cat_a.get("office_cli", {}).get("actions")))

        print()
        print("=" * 74)
        print("I. ★ DAG 执行必须诚实 (曾硬编码 success=True + 不支持 llm)")
        print("=" * 74)
        from core.skill_loader import SkillWorkflowEngine, SkillDAG
        e2 = SkillWorkflowEngine()
        prev_gw2, prev_gen2 = e2._gateway, e2._generate

        class _FailGW:
            async def call(self, tool, action="", **kw):
                return {"success": False, "error": "Access denied: 演示"}
        try:
            e2.set_gateway(_FailGW())
            r_f = asyncio.run(e2.execute_dag(
                SkillDAG(name="t", steps=[{"id": "a", "tool": "file_ops",
                                           "input": {"path": "x"}, "on_error": "stop"}]), {}, _FailGW()))
        finally:
            e2.set_gateway(prev_gw2)
        chk("★ 工具失败 → DAG 必须报失败 (不再硬编码成功)",
            r_f.get("success") is False and "Access denied" in str(r_f.get("error")), str(r_f)[:150])

        seen = []

        async def _gen(model, messages, **kw):
            seen.append(model)
            return {"text": "生成内容", "error": None}

        class _NoCall:
            async def call(self, tool, action="", **kw):
                raise AssertionError("llm 不该走 gateway")
        try:
            e2.set_gateway(_NoCall()); e2.set_generate(_gen)
            r_l = asyncio.run(e2.execute_dag(
                SkillDAG(name="t", steps=[{"id": "a", "tool": "llm",
                                           "input": {"prompt": "hi"}, "on_error": "stop"}]), {}, _NoCall()))
        finally:
            e2.set_gateway(prev_gw2); e2.set_generate(prev_gen2)
        chk("★ llm 步骤真被支持 (走 generate 不走 gateway)",
            r_l.get("success") is True and seen, str(r_l)[:150])
        try:
            e2.set_gateway(_NoCall()); e2.set_generate(None)
            r_l2 = asyncio.run(e2.execute_dag(
                SkillDAG(name="t", steps=[{"id": "a", "tool": "llm",
                                           "input": {"prompt": "hi"}, "on_error": "stop"}]), {}, _NoCall()))
        finally:
            e2.set_gateway(prev_gw2); e2.set_generate(prev_gen2)
        chk("llm 缺 generate → 诚实失败 (不静默)", r_l2.get("success") is False, str(r_l2)[:120])

        print()
        print("=" * 74)
        print("F. 工厂对真工具目录的认知 + 现有技能未被动")
        print("=" * 74)
        f3 = SkillFactory(generate=mk_gen(), engine=eng)
        cat = f3.catalog()
        chk("工具目录读到了 (>5 个真实工具)", len(cat) > 5, str(len(cat)))
        chk("已知工具含内建步骤 llm/knowledge", {"llm", "knowledge"} <= f3.known_tools())
        chk("现有正式技能仍是 12 个 (工厂没动它们)",
            len([p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()]) >= 12,
            str(len([p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()])))
    finally:
        shutil.rmtree(SBX, ignore_errors=True)
        left = sorted(p.name for p in CANDIDATE_DIR.iterdir()) if CANDIDATE_DIR.is_dir() else []
        print()
        print(f"[cleanup] 沙箱已删 · 真候选池内容: {left or '空 ✓'}")

    print()
    print("=" * 74)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    if F:
        print("失败: " + " · ".join(F))
    print("=" * 74)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
