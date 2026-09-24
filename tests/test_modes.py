"""Tests for ModeManager (Ask / Plan / Craft 三模式闸门)."""
import json
import time

import pytest

from core.modes import ModeManager, MODES, DEFAULT_MODE


@pytest.fixture
def mm(tmp_path):
    return ModeManager(tmp_path)


class TestModes:
    def test_three_modes(self):
        assert set(MODES) == {"ask", "plan", "craft"}
        assert [MODES[k]["cn"] for k in ("ask", "plan", "craft")] == ["问答", "规划", "实干"]

    def test_default_is_craft(self, mm):
        assert mm.get() == DEFAULT_MODE == "craft"

    def test_info(self, mm):
        i = mm.info()
        assert i["mode"] == "craft" and i["en"] == "Craft" and i["order"] == ["ask", "plan", "craft"]

    def test_set_valid(self, mm):
        assert mm.set("ask") and mm.get() == "ask"
        assert mm.set("plan") and mm.get() == "plan"

    def test_set_invalid_rejected(self, mm):
        mm.set("plan")
        assert mm.set("godmode") is False
        assert mm.set("") is False
        assert mm.get() == "plan"
        assert "ask / plan / craft" in mm.set_error()

    def test_persists_across_instances(self, mm, tmp_path):
        mm.set("ask")
        assert ModeManager(tmp_path).get() == "ask"

    def test_corrupt_state_falls_back(self, tmp_path):
        (tmp_path / "mode.json").write_text("{not json", encoding="utf-8")
        assert ModeManager(tmp_path).get() == "craft"


class TestWordJudgement:
    @pytest.mark.parametrize("w", ["执行", "开始", "确认", "可以", "好", "行", "y", "yes", "ok", "继续"])
    def test_confirm_words(self, w):
        assert ModeManager.is_confirm(w)

    @pytest.mark.parametrize("w", ["取消", "不", "算了", "停", "cancel", "n", "no"])
    def test_cancel_words(self, w):
        assert ModeManager.is_cancel(w)

    def test_phrasing_tolerated(self):
        assert ModeManager.is_confirm("  开始。 ")
        assert ModeManager.is_cancel("取消！")

    def test_blank_is_neither(self):
        assert not ModeManager.is_confirm("   ")
        assert not ModeManager.is_cancel("\n")

    def test_no_cross_talk(self):
        assert not ModeManager.is_confirm("取消")
        assert not ModeManager.is_cancel("执行")


class TestPendingPlan:
    STEPS = [{"step": 1, "tool": "file_ops", "action": "write", "params": {"path": "a.txt"}}]

    def test_roundtrip(self, mm):
        mm.set_pending(self.STEPS, "写个文件", "方案文本")
        p = mm.get_pending()
        assert p["steps"] == self.STEPS
        assert p["message"] == "写个文件"
        assert p["text"] == "方案文本"

    def test_persists(self, mm, tmp_path):
        mm.set_pending(self.STEPS, "原话", "文本")
        assert ModeManager(tmp_path).get_pending()["text"] == "文本"

    def test_clear(self, mm):
        mm.set_pending(self.STEPS, "原话", "文本")
        mm.clear_pending()
        assert mm.get_pending() is None

    def test_text_only_plan_counts(self, mm):
        mm.set_pending([], "原话", "纯文本方案")
        assert mm.get_pending()["text"] == "纯文本方案"

    def test_empty_pending_is_none(self, mm):
        mm.set_pending([], "", "")
        assert mm.get_pending() is None

    def test_ttl_expiry(self, mm):
        mm.set_pending(self.STEPS, "原话", "文本")
        mm.state["pending_at"] = time.time() - 4000
        assert mm.get_pending() is None

    def test_mode_switch_voids_pending(self, mm):
        """真换模式必须作废方案 — 防止跨模式误执行攒下的操作。

        pending 只可能存在于 plan 模式 (闸门只在 ask/plan 分支消费它),
        所以"从 plan 切走"是唯一需要防守的路径。
        """
        mm.set("plan")
        mm.set_pending(self.STEPS, "原话", "文本")
        mm.set("craft")
        assert mm.get_pending() is None

    def test_plan_to_ask_also_voids(self, mm):
        """plan → ask 再回 plan 不能捞到旧方案。"""
        mm.set("plan")
        mm.set_pending(self.STEPS, "原话", "文本")
        mm.set("ask")
        mm.set("plan")
        assert mm.get_pending() is None

    def test_same_mode_keeps_pending(self, mm):
        """重复 set 同一模式不算切换 → 方案保留 (set() 无变化即 no-op)。"""
        mm.set("plan")
        mm.set_pending(self.STEPS, "原话", "文本")
        mm.set("plan")
        assert mm.get_pending() is not None
