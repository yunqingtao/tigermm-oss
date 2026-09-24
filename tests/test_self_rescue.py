"""本地自救 (core/self_rescue.py) 的测试。

守四组:
  A plan 纯函数 —— 七类缺口各自的自救可行性与做法 (可本地救 vs 该出门找)
  B 依赖提示 —— 从真实报错里认出依赖名, 给**可照抄**的装法 (不自动装)
  C attempt —— 真试一次: 本地可救→交给工厂; 救不了→如实说; 工厂失败≠该出门找
  D 状态映射 —— ★ 救不了必须标 blocked_local, 不许粉饰成已处理
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.self_rescue import (STRATEGIES, RescueResult, attempt,
                             classify_factory_failure, dep_hint,
                             local_unsalvageable, plan, render_plan, status_for)
from core.gap_ledger import GapLedger


# ───────────────────── A. plan: 七类分流 ─────────────────────

class TestPlan:
    def test_no_tool_is_local(self):
        p = plan({"id": 1, "category": "no_tool", "sample": "帮我做个视频"})
        assert p["local"] and p["tries_factory"] and not p["needs_human"], p

    def test_skill_failed_is_local(self):
        """技能失败多半是 DAG 缺步骤 → 重编一版, 属本地可救。"""
        p = plan({"id": 2, "category": "skill_failed"})
        assert p["local"] and p["tries_factory"], p

    def test_missing_dep_is_local_but_no_factory(self):
        """缺依赖该装, 不该拿去编技能 (编了也跑不起来)。"""
        p = plan({"id": 3, "category": "missing_dep", "detail": "No module named 'docx'"})
        assert p["local"] and not p["tries_factory"], p
        assert "pip install python-docx" in p["hint"]

    @pytest.mark.parametrize("cat", ["no_entry", "missing_arg", "unknown_action", "model_refuse"])
    def test_manual_categories_need_human(self, cat):
        p = plan({"id": 4, "category": cat})
        assert not p["local"] and p["needs_human"] and p["hint"], p

    def test_unknown_category_defaults_to_human(self):
        p = plan({"id": 5, "category": "什么鬼"})
        assert p["needs_human"] and not p["local"], p

    def test_all_seven_categories_covered(self):
        for cat in ("no_tool", "skill_failed", "missing_dep", "no_entry",
                    "missing_arg", "unknown_action", "model_refuse"):
            assert cat in STRATEGIES, f"策略表漏了 {cat}"

    def test_plan_is_pure(self):
        g = {"id": 1, "category": "missing_dep", "detail": "No module named 'docx'"}
        assert plan(g) == plan(g)

    def test_render_plan_marks_outside_need(self):
        txt = render_plan({"id": 7, "category": "no_entry", "sample": "调用 office_cli"})
        assert "#7" in txt and "该出门找" in txt, txt
        txt2 = render_plan({"id": 8, "category": "no_tool", "sample": "做个视频"})
        assert "本地可救" in txt2, txt2


# ───────────────────── B. 依赖提示 ─────────────────────

class TestDepHint:
    def test_python_module(self):
        assert "pip install pytesseract" in dep_hint("No module named 'pytesseract'")

    def test_module_submodule_uses_top(self):
        assert "pip install openpyxl" in dep_hint("No module named 'openpyxl.workbook'")

    def test_binary_not_recognized(self):
        h = dep_hint("'ffmpeg' is not recognized as an internal command")
        assert "winget install Gyan.FFmpeg" in h, h

    def test_binary_chinese_form(self):
        h = dep_hint("找不到 tesseract 可执行文件")
        assert "TesseractOCR" in h, h

    def test_unknown_dep_says_so(self):
        h = dep_hint("出错了但没说清缺啥")
        assert "认不出" in h or "手动确认" in h, h

    def test_unknown_python_module_generic(self):
        h = dep_hint("No module named 'somelib'")
        assert "pip install somelib" in h, h

    def test_never_auto_installs(self):
        """★ 硬规矩: 只生成文本, 不含任何执行动作。"""
        h = dep_hint("No module named 'pytesseract'")
        assert "pip install" in h
        assert "正在安装" not in h and "已安装" not in h


# ───────────────────── C. attempt: 真试一次 ─────────────────────

class FakeFactory:
    """假工厂: 记录被调用的 description, 返回可配置结果。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def build(self, description, name_hint="", smoke=True):
        self.calls.append(description)
        return self.result


OK_RESULT = {"ok": True, "name": "weekly-report", "path": "tmp/x/weekly-report",
             "self_test": {"steps": 3}, "errors": []}
BAD_RESULT = {"ok": False, "stage": "质检", "errors": ["step1 用了未知工具: magic"]}


class TestAttempt:
    def test_no_tool_goes_to_factory(self):
        f = FakeFactory(OK_RESULT)
        r = asyncio.run(attempt({"id": 1, "category": "no_tool", "sample": "帮我做周报"}, f))
        assert r.ok and r.kind == "factory_candidate", r
        assert f.calls == ["帮我做周报"], f.calls
        assert "approve" in r.next_step

    def test_factory_uses_user_words(self):
        """★ 交给工厂的必须是**用户原话** (最能表达真实需求)。"""
        f = FakeFactory(OK_RESULT)
        asyncio.run(attempt({"id": 1, "category": "no_tool", "sample": "把报表转成 Excel"}, f))
        assert f.calls == ["把报表转成 Excel"]

    def test_factory_failed_unknown_tool_is_blocked_local(self):
        """真缺积木 (用了未知工具) → 本地救不了 → 该出门找。"""
        f = FakeFactory(BAD_RESULT)
        r = asyncio.run(attempt({"id": 2, "category": "no_tool", "sample": "做个视频"}, f))
        assert not r.ok and r.kind == "factory_failed"
        assert "质检" in r.detail
        assert status_for(r)[0] == "blocked_local"

    def test_factory_failed_input_limited_stays_open(self):
        """★ 实测挖出: 实跑门因**沙箱没给输入文件**失败 → 本地可能仍可救, 不许判"救不了"。
        (真跑验证时把 "CSV 转 Excel" 判成了 blocked_local —— 那会污染"该出门找"的依据。)"""
        f = FakeFactory({"ok": False, "stage": "实跑",
                         "errors": ["step 'read_csv': File not found: tmp\\_factory_smoke\\x\\out.txt"
                                    " (resolved to tmp\\_factory_smoke\\x\\out.txt) · 失败步骤: ['read_csv']"]})
        r = asyncio.run(attempt({"id": 9, "category": "no_tool", "sample": "把 CSV 转 Excel"}, f))
        assert r.kind == "factory_failed"
        assert status_for(r)[0] == "open", f"不该判 blocked_local: {status_for(r)}"
        assert "input_limited" in r.detail

    def test_factory_exception_handled(self):
        class Boom:
            async def build(self, *a, **k):
                raise RuntimeError("模型炸了")
        r = asyncio.run(attempt({"id": 3, "category": "no_tool", "sample": "x"}, Boom()))
        assert r.kind == "factory_error" and "模型炸了" in r.detail
        assert status_for(r)[0] == "open", "工厂自身出错不该算'本地救不了'"

    def test_missing_dep_no_factory_call(self):
        f = FakeFactory(OK_RESULT)
        r = asyncio.run(attempt({"id": 4, "category": "missing_dep",
                                 "detail": "No module named 'pytesseract'"}, f))
        assert f.calls == [], "缺依赖不该去编技能"
        assert r.kind == "dep_hint" and "pip install pytesseract" in r.detail
        assert status_for(r)[0] == "open", "待装依赖仍是 open (装完才算解决)"

    def test_manual_category_cannot(self):
        r = asyncio.run(attempt({"id": 5, "category": "no_entry",
                                 "detail": "工具 'x' 没有可调用入口"}, None))
        assert r.kind == "cannot" and not r.ok
        assert status_for(r)[0] == "blocked_local"

    def test_no_factory_only_plans(self):
        """不带工厂 → 只出方案, **不花模型调用**。"""
        r = asyncio.run(attempt({"id": 6, "category": "no_tool", "sample": "做个视频"}, None))
        assert not r.attempted and r.kind == "factory_skipped"
        assert "/gap rescue" in r.next_step

    def test_empty_sample_no_input(self):
        f = FakeFactory(OK_RESULT)
        r = asyncio.run(attempt({"id": 7, "category": "no_tool", "sample": ""}, f))
        assert r.kind == "no_input" and f.calls == []


# ───────────────────── C2. 工厂失败细分 (实测必需) ─────────────────────

class TestClassifyFactoryFailure:
    def test_input_limit_is_open(self):
        c = classify_factory_failure({"stage": "实跑", "errors": [
            "step 'read_csv': File not found: ...out.txt (resolved to ...out.txt)"]})
        assert c["status"] == "open" and c["reason"] == "input_limited", c

    def test_chinese_input_limit(self):
        c = classify_factory_failure({"stage": "实跑", "errors": ["读不到输入文件: D:/x.csv"]})
        assert c["status"] == "open" and c["reason"] == "input_limited", c

    def test_unknown_tool_is_blocked(self):
        c = classify_factory_failure({"stage": "质检", "errors": ["step1 用了未知工具: magic"]})
        assert c["status"] == "blocked_local" and c["reason"] == "no_local", c

    def test_no_entry_is_blocked(self):
        c = classify_factory_failure({"stage": "质检",
                                      "errors": ["工具 'office_cli' 没有可调用入口 (需 run/execute), 不能用"]})
        assert c["status"] == "blocked_local", c

    def test_missing_dep_is_blocked(self):
        c = classify_factory_failure({"stage": "实跑", "errors": ["No module named 'pytesseract'"]})
        assert c["status"] == "blocked_local", c

    def test_unclear_is_open_not_blocked(self):
        """★ 说不清就**不许**判'救不了' —— 误判会把人推向外来代码。"""
        c = classify_factory_failure({"stage": "自测", "errors": ["链路不可达, 缺调用: ['x']"]})
        assert c["status"] == "open" and c["reason"] == "unknown", c

    def test_pure(self):
        r = {"stage": "实跑", "errors": ["File not found: a.txt"]}
        assert classify_factory_failure(r) == classify_factory_failure(r)


# ───────────────────── D. 状态映射 ─────────────────────

class TestStatusFor:
    def test_ok_is_building_not_built(self):
        """★ 候选就绪 ≠ 已解决 (还没人工确认生效) → 只能是 building。"""
        r = RescueResult(ok=True, kind="factory_candidate", detail="候选 x")
        st, note = status_for(r)
        assert st == "building" and "候选" in note

    def test_blocked_is_explicit(self):
        st, note = status_for(RescueResult(kind="cannot", detail="需改代码"))
        assert st == "blocked_local" and "救不了" in note

    def test_never_fakes_built(self):
        """任何自救结果都不该直接给 built —— 那要人确认后才由 /gap done 标记。"""
        for kind, ok in (("factory_candidate", True), ("factory_failed", False),
                         ("cannot", False), ("dep_hint", False), ("factory_skipped", False),
                         ("factory_error", False), ("no_input", False)):
            st, _ = status_for(RescueResult(kind=kind, ok=ok))
            assert st != "built", f"{kind} 不该自己标 built"


# ───────────────────── E. 与账本联动 ─────────────────────

class TestLedgerIntegration:
    def test_blocked_local_accepted_by_ledger(self, tmp_path):
        led = GapLedger(db_path=tmp_path / "g.db")
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert led.set_status(r["id"], "blocked_local", "本地救不了") is True
        assert led.get(r["id"])["status"] == "blocked_local"

    def test_local_unsalvageable_lists_blocked(self, tmp_path):
        led = GapLedger(db_path=tmp_path / "g.db")
        g1 = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        led.record("识别图片", "No module named 'pytesseract'", "tool")
        led.set_status(g1["id"], "blocked_local", "本地救不了")
        out = local_unsalvageable(led)
        assert [g["id"] for g in out] == [g1["id"]], out

    def test_blocked_not_in_worth_building(self, tmp_path):
        """出门找的那批不该再混进"本地值得造"的清单 (两类要分得开)。"""
        led = GapLedger(db_path=tmp_path / "g.db")
        for _ in range(3):
            r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        led.set_status(r["id"], "blocked_local", "本地救不了")
        assert led.worth_building() == []

    def test_render_shows_outside_count(self, tmp_path):
        led = GapLedger(db_path=tmp_path / "g.db")
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        led.set_status(r["id"], "blocked_local")
        assert "该出门找 1 项" in led.render(), led.render()

    def test_full_loop_factory_ok(self, tmp_path):
        """端到端: 记缺口 → 自救(工厂成功) → 账本状态 building + 有产物。"""
        led = GapLedger(db_path=tmp_path / "g.db")
        g = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        gap = led.get(g["id"])
        f = FakeFactory(OK_RESULT)
        res = asyncio.run(attempt(gap, f))
        st, note = status_for(res)
        led.set_status(gap["id"], st, note)
        assert led.get(gap["id"])["status"] == "building"
        assert res.artifacts and "weekly-report" in res.artifacts[0]
        assert led.worth_building()[0]["id"] == gap["id"], "building 仍算值得造"
