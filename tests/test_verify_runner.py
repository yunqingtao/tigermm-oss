"""tests for scripts/hermes_verify.py — the single verification entry point.

守什么 (全离线, 不跑任何真实门禁):
  · ★ 自动发现: 门禁清单来自磁盘 glob, **源码里不得写死任何门禁名**
    (教训: 曾有验证器写死"5 个命令路由/8 个名字", 加一条就假 FAIL)
  · 结果行解析: 取**最后一条**"结果: N PASS / M FAIL" (汇总行)
  · green 判据: 有 FAIL / 非零退出 / 0 PASS / 超时 → 一律不算绿
  · 状态守卫覆盖 mode.json + prefs.json + perception.json + 规则库行数 (验证不得污染用户数据)
"""
import importlib.util
import json
import re
import sys
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "hermes_verify.py"


@pytest.fixture(scope="module")
def H():
    """按路径导入 runner (它不是包模块)。"""
    spec = importlib.util.spec_from_file_location("hermes_verify_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDiscoveryIsDynamic:
    """★ 门禁清单必须来自磁盘, 不得写死在源码里。"""

    def test_matches_disk_glob(self, H):
        on_disk = sorted(p for p in H.V_DIR.glob("verify_*.py"))
        assert H.discover() == on_disk, "discover() 必须等于磁盘上的实际文件"
        assert len(on_disk) > 0, "至少要有一个门禁"

    def test_no_hardcoded_gate_names(self, H):
        """源码里出现具体门禁名 = 写死清单的信号 (排除注释里的举例)。"""
        src = RUNNER.read_text(encoding="utf-8")
        code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
        # 允许出现在 SLOW_HINTS (那是"跑得慢"的标注, 不是清单本身)
        hits = [n for n in re.findall(r"verify_[a-z_]+", code) if n not in H.SLOW_HINTS]
        assert not hits, f"源码里写死了门禁名: {hits} —— 应改为 glob 自动发现"

    def test_new_file_is_picked_up(self, H, tmp_path, monkeypatch):
        """新放进目录的验证器必须被自动纳入 (不依赖任何注册表)。"""
        monkeypatch.setattr(H, "V_DIR", tmp_path)
        (tmp_path / "verify_aaa.py").write_text("print('结果: 1 PASS / 0 FAIL')", encoding="utf-8")
        (tmp_path / "verify_zzz.py").write_text("print('结果: 1 PASS / 0 FAIL')", encoding="utf-8")
        (tmp_path / "not_a_gate.py").write_text("pass", encoding="utf-8")
        names = [p.stem for p in H.discover()]
        assert names == ["verify_aaa", "verify_zzz"], "只收 verify_*.py, 且排序稳定"


class TestResultParsing:
    def test_takes_last_summary_line(self, H):
        out = "结果: 1 PASS / 5 FAIL\n(中间输出)\n结果: 9 PASS / 0 FAIL\n"
        ms = list(H.RESULT_RE.finditer(out))
        assert ms and ms[-1].group(1) == "9" and ms[-1].group(2) == "0"

    def test_parses_spacing_variants(self, H):
        for s in ("结果: 3 PASS / 1 FAIL", "结果:3 PASS/1 FAIL", "结果: 12 PASS /  0 FAIL"):
            m = H.RESULT_RE.search(s)
            assert m, s

    def test_no_result_line_means_crash(self, H, tmp_path, monkeypatch):
        monkeypatch.setattr(H, "ROOT", tmp_path)
        p = tmp_path / "verify_boom.py"
        p.write_text("raise RuntimeError('x')", encoding="utf-8")
        r = H.run_gate(p, timeout=30)
        assert r["pass"] == 0 and r["fail"] == 1 and not r["green"]


class TestGreenCriteria:
    """绿 = 有 PASS 且零 FAIL 且退出 0 且没超时。任何一条不满足都不算绿。"""

    def _mk(self, H, tmp_path, monkeypatch, body, timeout=30):
        monkeypatch.setattr(H, "ROOT", tmp_path)
        p = tmp_path / "verify_x.py"
        p.write_text(body, encoding="utf-8")
        return H.run_gate(p, timeout=timeout)

    def test_green_when_all_pass(self, H, tmp_path, monkeypatch):
        r = self._mk(H, tmp_path, monkeypatch, "print('结果: 4 PASS / 0 FAIL')")
        assert r["green"] and r["pass"] == 4 and r["fail"] == 0

    def test_red_when_any_fail(self, H, tmp_path, monkeypatch):
        r = self._mk(H, tmp_path, monkeypatch,
                     "print('FAIL 某断言')\nprint('结果: 3 PASS / 1 FAIL')\nimport sys; sys.exit(1)")
        assert not r["green"] and r["fail"] == 1
        assert any("某断言" in x for x in r["failed_items"])

    def test_red_when_zero_pass(self, H, tmp_path, monkeypatch):
        r = self._mk(H, tmp_path, monkeypatch, "print('结果: 0 PASS / 0 FAIL')")
        assert not r["green"], "0 PASS 不算绿 (空壳验证器)"

    def test_red_on_timeout_and_marked(self, H, tmp_path, monkeypatch):
        # 超时设小 (2s) 免得拖慢 canonical
        r = self._mk(H, tmp_path, monkeypatch, "import time; time.sleep(30)", timeout=2)
        assert not r["green"] and r.get("timeout") is True
        assert any("TIMEOUT" in x for x in r["failed_items"]), "超时必须可辨认 (诊断缺口, 曾修)"


class TestStateGuard:
    """验证不得污染用户真实数据 —— runner 自己兜底对比, 不只靠门禁自查。"""

    def test_covers_live_state_files(self, H):
        assert "data/mode.json" in H.STATE_FILES
        assert "data/prefs.json" in H.STATE_FILES
        # ★ perception.json 也会被门禁写 (实测) + 规则库按行数比 (sqlite 字节非确定)
        assert "data/perception.json" in H.STATE_JSON   # 内容比 (格式差异不算污染)
        assert "data/learned_rules.db" in H.STATE_DB

    def test_snapshot_detects_change(self, H, tmp_path, monkeypatch):
        monkeypatch.setattr(H, "ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        f = tmp_path / "data/mode.json"
        f.write_text('{"mode":"craft"}', encoding="utf-8")
        before = H.snapshot_state()
        f.write_text('{"mode":"ask"}', encoding="utf-8")
        after = H.snapshot_state()
        assert before != after, "改了用户状态必须能被 md5 快照看出来"

class TestModePin:
    """★ 2026-09-19 实测: 用户把 TMM 切到 ask(问答) 后, 门禁红了 **7 条假 FAIL**
    (三模式闸门拦在所有工具路由之前 → 门禁看到"问答模式拒绝执行")。
    所以 runner 必须**跑前钉 craft、跑完还原** —— 验证结果不能依赖用户当前状态。
    """

    def _mk(self, tmp_path, mode):
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        f = tmp_path / "data" / "mode.json"
        f.write_text(json.dumps({"mode": mode, "updated": 1, "pending": None},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        return f

    def test_pins_craft_then_restores(self, H, tmp_path, monkeypatch):
        f = self._mk(tmp_path, "ask")
        monkeypatch.setattr(H, "ROOT", tmp_path)
        orig = H._pin_gate_mode()
        assert json.loads(f.read_text(encoding="utf-8"))["mode"] == "craft", "跑门禁前必须钉成 craft"
        H._restore_gate_mode(orig)
        assert json.loads(f.read_text(encoding="utf-8"))["mode"] == "ask", "跑完必须还原用户的模式"

    def test_restore_is_byte_exact(self, H, tmp_path, monkeypatch):
        f = self._mk(tmp_path, "plan")
        before = f.read_bytes()
        monkeypatch.setattr(H, "ROOT", tmp_path)
        H._restore_gate_mode(H._pin_gate_mode())
        assert f.read_bytes() == before, "还原必须字节一致 (不能只改 mode 字段)"

    def test_no_mode_file_is_safe(self, H, tmp_path, monkeypatch):
        monkeypatch.setattr(H, "ROOT", tmp_path)
        assert H._pin_gate_mode() is None          # 没有文件也不炸
        H._restore_gate_mode(None)                 # 还原 None 安全

    def test_craft_stays_untouched(self, H, tmp_path, monkeypatch):
        f = self._mk(tmp_path, "craft")
        before = f.read_bytes()
        monkeypatch.setattr(H, "ROOT", tmp_path)
        H._restore_gate_mode(H._pin_gate_mode())
        assert f.read_bytes() == before, "已经是 craft 就不该改写 (无谓的写盘也是污染)"
