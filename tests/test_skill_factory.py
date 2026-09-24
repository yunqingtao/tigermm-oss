"""tests for core/skill_factory.py — 技能工厂 (编译→质检→自测→人工确认).

全部**离线**: generate 是注入的 stub, 候选池/技能目录都在 tmp_path, 不碰真 data/ 与 tmm_skills/。
守的关键点:
  · 质检门: 未知工具 / 坏的 $params 引用 / 重复 id / 名字非法 / 重名 / 触发词冲突 / 单字触发词
  · ★ 自测门: 用 stub 真跑 DAG, 每步工具都必须被调用 (多重集比对)
  · ★★ 自测必须**还原** engine._gateway —— 忘了还原 = 真管线被 stub 永久接管
  · 人工确认门: 候选不生效; promote 会复检; 重名拒绝; meta.json 不带进正式技能
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.skill_factory import SkillFactory, Proposal, BUILTIN_STEPS


# ─────────────────────────── 夹具 ───────────────────────────

GOOD_DAG = {
    "name": "weekly-csv-report",
    "description": "把桌面上的 csv 汇总成 Word 周报",
    "tags": ["csv", "周报"],
    "triggers": ["汇总csv周报", "把csv做成周报"],
    "params": {"out_path": {"type": "path", "required": False, "desc": "输出路径"}},
    "steps": [
        {"id": "draft", "tool": "llm", "input": {"prompt": "$message"},
         "output": "text", "on_error": "stop"},
        {"id": "save", "tool": "tiger_office", "action": "write_word",
         "input": {"path": "$params.out_path", "content": "$steps.draft.text"},
         "on_error": "stop"},
    ],
}


def mk_generate(text=None, payload=None):
    """异步 stub: 返回 payload 的 JSON (或给定文本)。"""
    body = text if text is not None else json.dumps(payload or GOOD_DAG, ensure_ascii=False)

    async def _g(model, messages, **kw):
        return {"text": body, "error": None}
    return _g


@pytest.fixture
def engine():
    from core.skill_loader import SkillWorkflowEngine
    return SkillWorkflowEngine()


@pytest.fixture
def factory(tmp_path, engine):
    """候选池/技能目录都指向 tmp —— 绝不碰真 data/ 与 tmm_skills/。"""
    skills = tmp_path / "skills"
    skills.mkdir()
    return SkillFactory(generate=mk_generate(), engine=engine,
                        candidate_dir=tmp_path / "cand", skills_dir=skills)


def build_prop(**over):
    d = json.loads(json.dumps(GOOD_DAG))
    d.update(over)
    return Proposal(name=d["name"], description=d["description"], tags=d["tags"],
                    triggers=d["triggers"], params=d["params"], steps=d["steps"])


# ─────────────────────────── 编译 ───────────────────────────

class TestPropose:
    def test_parses_plain_json(self, factory):
        p = asyncio.run(factory.propose("把csv汇总成周报"))
        assert p.name == "weekly-csv-report" and len(p.steps) == 2 and not p.errors

    def test_tolerates_fenced_json(self, tmp_path, engine):
        f = SkillFactory(generate=mk_generate(text="好的：\n```json\n" + json.dumps(GOOD_DAG, ensure_ascii=False) + "\n```\n以上"),
                         engine=engine, candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        p = asyncio.run(f.propose("汇总csv"))
        assert p.name == "weekly-csv-report" and not p.errors

    def test_tolerates_leading_prose(self, tmp_path, engine):
        f = SkillFactory(generate=mk_generate(text="这是结果 " + json.dumps(GOOD_DAG, ensure_ascii=False) + " 完毕"),
                         engine=engine, candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        assert asyncio.run(f.propose("x")).name == "weekly-csv-report"

    def test_bad_json_is_honest_error(self, tmp_path, engine):
        f = SkillFactory(generate=mk_generate(text="抱歉我不会"),
                         engine=engine, candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        p = asyncio.run(f.propose("x"))
        assert p.errors and "JSON" in p.errors[0]

    def test_model_error_surfaces(self, tmp_path, engine):
        async def bad(model, messages, **kw):
            return {"error": "网络挂了"}
        f = SkillFactory(generate=bad, engine=engine, candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        p = asyncio.run(f.propose("x"))
        assert p.errors and "网络挂了" in p.errors[0]

    def test_requires_tools_derived(self, factory):
        p = asyncio.run(factory.propose("x"))
        assert p.requires_tools == ["tiger_office"]        # llm 是内建, 不计

    def test_no_generate_injected(self, tmp_path, engine):
        f = SkillFactory(generate=None, engine=engine, candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        p = asyncio.run(f.propose("x"))
        assert p.errors and "generate" in p.errors[0]

    def test_catalog_comes_from_disk(self, factory):
        assert len(factory.catalog()) > 5
        assert "tiger_office" in factory.known_tools()
        assert BUILTIN_STEPS <= factory.known_tools()


# ─────────────────────────── 质检门 ───────────────────────────

class TestValidateGates:
    def test_good_passes(self, factory):
        assert factory.validate(build_prop()).errors == []

    def test_unknown_tool_rejected(self, factory):
        p = build_prop(steps=[{"id": "s1", "tool": "coffee_machine", "input": {}}])
        errs = factory.validate(p).errors
        assert any("未知工具" in e for e in errs), errs

    def test_bad_param_ref_rejected(self, factory):
        p = build_prop(steps=[{"id": "s1", "tool": "llm", "input": {"prompt": "$params.nope"}}])
        assert any("$params.nope" in e for e in factory.validate(p).errors)

    def test_bad_step_ref_rejected(self, factory):
        p = build_prop(steps=[{"id": "s1", "tool": "llm", "input": {"prompt": "$steps.ghost.text"}}])
        assert any("ghost" in e for e in factory.validate(p).errors)

    def test_duplicate_step_id(self, factory):
        p = build_prop(steps=[{"id": "a", "tool": "llm", "input": {"prompt": "x"}},
                              {"id": "a", "tool": "llm", "input": {"prompt": "y"}}])
        assert any("重复" in e for e in factory.validate(p).errors)

    @pytest.mark.parametrize("bad", ["Bad Name", "1abc", "abc_def", "", "-x"])
    def test_bad_name_rejected(self, factory, bad):
        assert any("name" in e for e in factory.validate(build_prop(name=bad)).errors)

    def test_no_triggers_rejected(self, factory):
        assert any("triggers" in e for e in factory.validate(build_prop(triggers=[])).errors)

    def test_no_steps_rejected(self, factory):
        assert any("steps" in e for e in factory.validate(build_prop(steps=[])).errors)

    def test_single_char_trigger_rejected(self, factory):
        """单字触发词会抢走别的技能的句子 (diagram 的裸'画'踩过)。"""
        assert any("太短" in e for e in factory.validate(build_prop(triggers=["画"])).errors)

    def test_name_clash_with_existing_skill(self, tmp_path, engine):
        skills = tmp_path / "skills"; (skills / "weekly-csv-report").mkdir(parents=True)
        (skills / "weekly-csv-report" / "SKILL.md").write_text(
            "---\nname: weekly-csv-report\ntriggers: [x]\n---\n", encoding="utf-8")
        f = SkillFactory(generate=mk_generate(), engine=engine,
                         candidate_dir=tmp_path / "c", skills_dir=skills)
        assert any("重名" in e for e in f.validate(build_prop()).errors)

    def test_trigger_exact_clash_detected(self, tmp_path, engine):
        skills = tmp_path / "skills"; (skills / "other").mkdir(parents=True)
        (skills / "other" / "SKILL.md").write_text(
            "---\nname: other\ntriggers: [汇总csv周报]\n---\n", encoding="utf-8")
        f = SkillFactory(generate=mk_generate(), engine=engine,
                         candidate_dir=tmp_path / "c", skills_dir=skills)
        errs = f.validate(build_prop()).errors
        assert any("完全相同" in e for e in errs), errs

    def test_trigger_substring_is_warning_not_error(self, tmp_path, engine):
        skills = tmp_path / "skills"; (skills / "other").mkdir(parents=True)
        (skills / "other" / "SKILL.md").write_text(
            "---\nname: other\ntriggers: [帮我汇总csv周报吧]\n---\n", encoding="utf-8")
        f = SkillFactory(generate=mk_generate(), engine=engine,
                         candidate_dir=tmp_path / "c", skills_dir=skills)
        p = f.validate(build_prop())
        assert not p.errors and any("子串" in w for w in p.warnings)


# ─────────────────────────── 自测门 ───────────────────────────

class TestSelfTest:
    def test_every_step_tool_called(self, factory):
        """llm 是内建步骤 → 走 generate(不走 gateway); 其余工具走 gateway。"""
        st = asyncio.run(factory.self_test(build_prop()))
        assert st["ran"] and st["ok"], st
        assert st["missing"] == []
        assert [c[0] for c in st["called"]] == ["tiger_office"], st["called"]

    def test_action_is_passed_through(self, factory):
        """★ 与生产同一条调用路径 (execute_dag 会带 action=) —— 能抓严格签名的工具。"""
        st = asyncio.run(factory.self_test(build_prop()))
        assert any(c[0] == "tiger_office" and c[1] == "write_word" for c in st["called"]), st["called"]

    def test_missing_step_detected(self, factory):
        """★ 多重集比对: 少跑一步必须被发现。"""
        class _E:
            _gateway = None
            def set_gateway(self, g): self._gateway = g
            async def execute_dag(self, dag, params, mgr):
                await self._gateway.call("llm")          # 只跑第一步, 漏 tiger_office
                return {"success": True}
        f = SkillFactory(generate=mk_generate(), engine=_E(),
                         candidate_dir=factory.candidate_dir, skills_dir=factory.skills_dir)
        st = asyncio.run(f.self_test(build_prop()))
        assert st["ran"] and not st["ok"] and st["missing"] == ["tiger_office"], st

    def test_gateway_restored_on_success(self, factory):
        """★★ 自测必须还原 engine._gateway —— 否则真管线被 stub 永久接管。"""
        before = factory.engine._gateway
        asyncio.run(factory.self_test(build_prop()))
        assert factory.engine._gateway is before

    def test_gateway_restored_on_exception(self, tmp_path):
        """异常路径也必须还原 (finally)。"""
        sentinel = object()

        class _E:
            _gateway = sentinel
            def set_gateway(self, g): self._gateway = g
            async def execute_dag(self, dag, params, mgr):
                raise RuntimeError("炸")
        f = SkillFactory(generate=mk_generate(), engine=_E(),
                         candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        st = asyncio.run(f.self_test(build_prop()))
        assert not st["ok"] and "炸" in st["error"]
        assert f.engine._gateway is sentinel, "异常路径也必须还原 gateway!"

    def test_not_run_when_validation_failed(self, factory):
        p = build_prop(steps=[{"id": "s1", "tool": "coffee_machine", "input": {}}])
        factory.validate(p)
        st = asyncio.run(factory.self_test(p))
        assert st["ran"] is False


# ─────────────────────────── 候选池 + 人工确认门 ───────────────────────────

class TestCandidateAndApproval:
    def test_build_all_stages_then_saved(self, factory):
        r = asyncio.run(factory.build("把csv汇总成周报"))
        assert r["ok"] and r["stage"].startswith("候选")
        assert Path(r["path"]).is_file()
        assert [c["name"] for c in factory.list_candidates()] == ["weekly-csv-report"]

    def test_build_stops_at_validation(self, tmp_path, engine):
        f = SkillFactory(generate=mk_generate(payload={**GOOD_DAG, "steps": [
            {"id": "s1", "tool": "coffee_machine", "input": {}}]}),
            engine=engine, candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        r = asyncio.run(f.build("煮咖啡"))
        assert not r["ok"] and r["stage"] == "质检"
        assert f.list_candidates() == []

    def test_build_stops_at_compile(self, tmp_path, engine):
        f = SkillFactory(generate=mk_generate(text="no json"), engine=engine,
                         candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        r = asyncio.run(f.build("x"))
        assert not r["ok"] and r["stage"] == "编译" and f.list_candidates() == []

    def test_candidate_not_active_before_approval(self, factory):
        """★ 人工确认门: 编译完候选, 正式技能目录里**不该**有它。"""
        asyncio.run(factory.build("把csv汇总成周报"))
        assert not (factory.skills_dir / "weekly-csv-report").exists()
        assert factory.existing_skills() == {}

    def test_promote_activates(self, factory):
        asyncio.run(factory.build("把csv汇总成周报"))
        r = factory.promote("weekly-csv-report")
        assert r["ok"], r
        assert (factory.skills_dir / "weekly-csv-report" / "SKILL.md").is_file()
        assert not (factory.skills_dir / "weekly-csv-report" / "meta.json").exists(), "候选元数据不该带进正式技能"

    def test_promote_rechecks(self, factory):
        """promote 必须**复检** —— 候选文件可能被手改坏 (在候选池里改)。"""
        asyncio.run(factory.build("把csv汇总成周报"))
        f = factory.candidate_dir / "weekly-csv-report" / "SKILL.md"     # ★ 候选池, 不是技能目录
        f.write_text(f.read_text(encoding="utf-8").replace("tiger_office", "coffee_machine"),
                     encoding="utf-8")
        r = factory.promote("weekly-csv-report")
        assert not r["ok"] and any("未知工具" in e for e in (r.get("errors") or [])), r
        assert not (factory.skills_dir / "weekly-csv-report").exists(), "复检没过就绝不能生效"

    def test_promote_refuses_duplicate(self, factory):
        """目标已存在同名技能 → 拒绝覆盖 (不静默替换用户已有技能)。
        注意: build() 会先在质检阶段拦下重名, 所以这条必须**手工造候选**才能测到 promote 自己的检查。
        """
        asyncio.run(factory.build("把csv汇总成周报"))
        assert factory.promote("weekly-csv-report")["ok"]
        # 手工再造一个同名候选 (绕过质检对重名的拦截, 直击 promote 的 dest.exists 检查)
        d = factory.candidate_dir / "weekly-csv-report"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            "---\nname: weekly-csv-report\ndescription: x\ntriggers: [汇总csv周报]\n"
            "params: {}\nsteps:\n  - id: s1\n    tool: llm\n    input: {prompt: hi}\n---\n",
            encoding="utf-8")
        r = factory.promote("weekly-csv-report")
        # 复检里"与现有技能重名"先于 dest.exists 命中 —— 两种拦法都算拒绝, 关键是**不覆盖**
        assert not r["ok"], r
        assert ("已存在" in str(r.get("error")) or "重名" in str(r.get("errors"))), r
        # 原有技能文件未被破坏
        assert "tiger_office" in (factory.skills_dir / "weekly-csv-report" / "SKILL.md").read_text(encoding="utf-8")

    def test_promote_missing_candidate(self, factory):
        r = factory.promote("nope")
        assert not r["ok"] and "不存在" in r["error"]

    def test_reject_removes(self, factory):
        asyncio.run(factory.build("把csv汇总成周报"))
        assert factory.reject("weekly-csv-report") is True
        assert factory.list_candidates() == []
        assert factory.reject("weekly-csv-report") is False

    def test_candidate_pool_is_outside_skills_dir(self, factory):
        """★ 候选池必须在技能目录**之外** —— 否则未确认的技能会污染技能发现/路由。"""
        assert factory.candidate_dir != factory.skills_dir
        assert factory.skills_dir not in factory.candidate_dir.parents
        assert factory.candidate_dir not in factory.skills_dir.parents


# ─────────────────────────── 工具目录 (两种声明约定) ───────────────────────────

class TestToolCatalog:
    """★ 回归: 工具模块有两种历史声明约定 —— TOOL = {...} 和 PLUGIN = {...}。
    早先只认 TOOL → 漏掉 13 个工具 (tiger_office / send_email / whisper …),
    而 12 个正式技能里 9 个靠 tiger_office → 工厂会看不到最常用的积木。
    """

    def test_declared_via_plugin_convention_is_found(self):
        from core.tool_schema import load_tool_metadata
        from core.skill_factory import TOOLS_DIR
        m = load_tool_metadata(TOOLS_DIR)
        assert "tiger_office" in m, "PLUGIN 约定声明的工具必须被收录"
        assert (m["tiger_office"] or {}).get("declared_as") == "PLUGIN"

    def test_keywords_alias_trigger(self):
        """PLUGIN 用 'trigger' 字段, TOOL 用 'keywords' —— 都要归一成 keywords。"""
        from core.tool_schema import load_tool_metadata
        from core.skill_factory import TOOLS_DIR
        m = load_tool_metadata(TOOLS_DIR)
        assert m["tiger_office"]["keywords"], "trigger 应被归一成 keywords"
        assert "表格" in m["tiger_office"]["keywords"]

    def test_other_plugin_declared_tools_present(self):
        from core.tool_schema import load_tool_metadata
        from core.skill_factory import TOOLS_DIR
        m = load_tool_metadata(TOOLS_DIR)
        for nm in ("send_email", "check_mail", "shell_exec", "whisper", "voice"):
            assert nm in m, f"{nm} 应被收录 (PLUGIN 约定)"

    def test_catalog_includes_runtime_known_names(self, tmp_path):
        """目录里若有模块没声明 TOOL/PLUGIN, 也要有名字兜底 —— 否则误判'未知工具'。"""
        class _E:
            _KNOWN_TOOLS = {"ghost_tool"}
        f = SkillFactory(generate=mk_generate(), engine=_E(),
                         candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        assert "ghost_tool" in f.known_tools()
        assert "ghost_tool" in f.catalog()

    def test_no_recursion_between_catalog_and_known_tools(self, factory):
        """★ 回归: catalog() 与 known_tools() 互相调用曾导致无限递归。"""
        assert factory.known_tools()
        assert factory.catalog()


# ─────────────────────────── 工具动作抽取 ───────────────────────────

class TestActionExtraction:
    """★ 回归: 工具的 PLUGIN/TOOL 声明**不写动作**, 但动作才是真入口。
    不告诉 LLM 动作名, 它会**编** (实测编出 excel_read, 真名 scan_excel)。
    """

    def test_tiger_office_actions_found(self, factory):
        """字典分发 (handlers = {...}) 要能抽到。"""
        acts = factory.catalog()["tiger_office"]["actions"]
        assert "write_word" in acts and "scan_excel" in acts
        assert "excel_read" not in acts

    def test_if_chain_actions_found(self, factory):
        """if action == "x" 链也要能抽到。"""
        acts = factory.catalog()["file_ops"]["actions"]
        assert "list" in acts and "read" in acts and "write" in acts

    def test_builtin_has_no_actions(self, factory):
        assert factory.catalog()["llm"]["actions"] == []

    def test_prompt_lists_actions(self, factory):
        """动作名必须进提示词 (否则 LLM 又会编)。"""
        pr = factory._prompt("做个 Word", "")
        assert "write_word" in pr and "scan_excel" in pr

    def test_prompt_forbids_posix_paths(self, factory):
        """回归: LLM 曾写 ~/Desktop → 工具不认 → 实跑门拦下。"""
        assert "~/" in factory._prompt("x", "")          # 作为反例出现在规则里


# ─────────────────────────── 实跑门 (真工具) ───────────────────────────

class TestSmokeGate:
    """★ stub 自测只能验"链路可达", 验不出"选错工具" —— 必须有真跑门。"""

    class _FakeEngine:
        """模拟 execute_dag: 真调 gateway; 第一次(自测)成功, 第二次(实跑)按设定。"""

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
                    continue                      # 内建, 不走 gateway
                r = await self._gateway.call(s["tool"], action=s.get("action", ""))
                res[s.get("id")] = {"success": bool(r.get("success"))}
            if self.calls >= 2 and not self.smoke_ok:      # 实跑阶段故意失败
                return {"success": False, "error": "step 'save': Access denied (模拟)",
                        "results": res}
            return {"success": True, "results": res}

    class _OkGW:
        """假的真 gateway: 工具调用一律成功 (只用来验"实跑这一段被走到了")。"""
        async def call(self, tool, action="", **kw):
            return {"success": True, "output": "[fake]", "text": "[fake]"}

    def _factory(self, tmp_path, engine, gateway=None):
        return SkillFactory(generate=mk_generate(), engine=engine,
                            gateway=(gateway if gateway is not None else self._OkGW()),
                            candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")

    def test_smoke_failure_blocks_candidate(self, tmp_path):
        """真跑报失败 → 不许落候选 (自测是过的, 所以卡在实跑门)。"""
        f = self._factory(tmp_path, self._FakeEngine(smoke_ok=False))
        r = asyncio.run(f.build("把话扩写成通知并存 Word"))
        assert not r["ok"] and r["stage"] == "实跑", r
        assert "Access denied" in str(r["errors"])
        assert f.list_candidates() == [], "实跑没过就绝不能落候选"

    def test_smoke_success_saves_candidate(self, tmp_path):
        f = self._factory(tmp_path, self._FakeEngine(smoke_ok=True))
        r = asyncio.run(f.build("把话扩写成通知并存 Word"))
        assert r["ok"] and f.list_candidates(), r
        assert r["smoke"]["ran"] and r["smoke"]["ok"]

    def test_smoke_can_be_skipped(self, tmp_path):
        f = self._factory(tmp_path, self._FakeEngine(), gateway=None)
        r = asyncio.run(f.build("把话扩写成通知并存 Word", smoke=False))
        assert r["ok"] and r["smoke"]["ran"] is False

    def test_smoke_restores_gateway_and_generate(self, tmp_path):
        """★★ 实跑换了 gateway **和** generate, 两个都必须还原。"""
        sentinel_gw, sentinel_gen = object(), object()

        class E:
            _gateway = sentinel_gw
            _generate = sentinel_gen
            def set_gateway(self, g): self._gateway = g
            def set_generate(self, g): self._generate = g
            async def execute_dag(self, dag, params, mgr): return {"success": True, "results": {}}
        e = E()
        f = SkillFactory(generate=mk_generate(), engine=e, gateway=object(),
                         candidate_dir=tmp_path / "c", skills_dir=tmp_path / "s")
        asyncio.run(f.smoke_test(build_prop()))
        assert e._gateway is sentinel_gw and e._generate is sentinel_gen


# ─────────────────────────── DAG 执行必须诚实 ───────────────────────────

class TestDagHonesty:
    """★ 回归: execute_dag 曾**硬编码 success=True**(工具失败也报成功), 且不支持 llm 步骤。"""

    def _eng(self):
        from core.skill_loader import SkillWorkflowEngine
        return SkillWorkflowEngine()

    def test_tool_failure_propagates(self):
        from core.skill_loader import SkillDAG
        e = self._eng()
        prev = e._gateway
        class GW:
            async def call(self, tool, action="", **kw):
                return {"success": False, "error": "Access denied: 演示"}
        try:
            e.set_gateway(GW())
            r = asyncio.run(e.execute_dag(
                SkillDAG(name="t", steps=[{"id": "a", "tool": "file_ops",
                                           "input": {"path": "x"}, "on_error": "stop"}]),
                {}, GW()))
        finally:
            e.set_gateway(prev)
        assert r["success"] is False and "Access denied" in str(r["error"]), r

    def test_llm_step_supported(self):
        """llm 是内建步骤 (走 generate, 不走 gateway) —— execute_dag 必须支持。"""
        from core.skill_loader import SkillDAG
        e = self._eng()
        prev_g, prev_gen = e._gateway, e._generate
        seen = []

        async def gen(model, messages, **kw):
            seen.append(model)
            return {"text": "生成的内容", "error": None}

        class GW:
            async def call(self, tool, action="", **kw):
                raise AssertionError("llm 不该走 gateway")
        try:
            e.set_gateway(GW()); e.set_generate(gen)
            r = asyncio.run(e.execute_dag(
                SkillDAG(name="t", steps=[{"id": "a", "tool": "llm",
                                           "input": {"prompt": "hi"}, "on_error": "stop"}]),
                {}, GW()))
        finally:
            e.set_gateway(prev_g); e.set_generate(prev_gen)
        assert r["success"] is True and seen and "生成的内容" in str(r["results"]), r

    def test_llm_without_generate_is_honest_failure(self):
        from core.skill_loader import SkillDAG
        e = self._eng()
        prev_g, prev_gen = e._gateway, e._generate

        class GW:
            async def call(self, tool, action="", **kw): return {"success": True}
        try:
            e.set_gateway(GW()); e.set_generate(None)
            r = asyncio.run(e.execute_dag(
                SkillDAG(name="t", steps=[{"id": "a", "tool": "llm",
                                           "input": {"prompt": "hi"}, "on_error": "stop"}]),
                {}, GW()))
        finally:
            e.set_gateway(prev_g); e.set_generate(prev_gen)
        assert r["success"] is False and "generate" in str(r["error"]), r
