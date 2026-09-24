"""感知写入的 probe 守卫 + 纠正检测 — 集成测试 (2026-09-20 修)

两个真问题一起守:

  ① ★ 纠正检测误报: 裸 `别` 命中"识别/特别/区别", 裸 `不是` 命中"是不是"(提问)。
     危害: get_correction_context() 会把纠正注入**系统提示词** → 直接改变模型行为。
     (单元层见 tests/test_perception_corrections.py)

  ② ★ 感知写入没有 probe 守卫: probe=True 本该是"验证流量, 不写用户数据",
     但 _process_external() 的两处 self.perception.analyze(...) 完全没检查 probe
     → 验证器跑真 pipeline 时, 探针句子被写进用户真实 data/perception.json。
     实测证据: 用户库里出现 2 条假纠正 ("你能识别我的电脑吗" / "帮我识别图片文字"),
     时间戳正好对得上验证器运行时刻。
     修法: 三个触及感知写入的方法 (_process_external / _ir_chain_exec /
     _analysis_preread) 都收 `probe` 参数并一路传下去 (加性: 默认 False → 旧调用者不变)。
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.perception import PerceptionEngine  # noqa: E402


# ───────────────────── ① 纠正检测 (误报守卫) ─────────────────────

class TestCorrectionDetection:
    @pytest.fixture
    def pe(self):
        return PerceptionEngine.__new__(PerceptionEngine)

    @pytest.mark.parametrize("msg", [
        "你能识别我的电脑吗", "帮我识别图片文字", "这个和那个有什么区别",
        "特别想知道天气", "级别不够吧", "是不是该换个方案",
        "把文件转换成 PDF", "分别处理这两个", "告别过去", "别人怎么说", "换个字体",
    ])
    def test_innocent_not_a_correction(self, pe, msg):
        assert pe._detect_corrections(msg) == [], msg

    @pytest.mark.parametrize("msg", ["不对，重来", "不是这样", "错了", "别再忘了", "换成苏州"])
    def test_real_correction_detected(self, pe, msg):
        assert pe._detect_corrections(msg) != [], msg

    def test_marker_not_regex(self, pe):
        """存进库的 marker 必须是人可读词, 不是正则 (它会被展示/注入)。"""
        for word, _pat in PerceptionEngine._CORRECTION_MARKERS:
            assert "?" not in word and "(" not in word, word


# ───────────────────── ② probe 守卫 (不污染用户数据) ─────────────────────

class TestProbeGuardOnPerception:
    """★★ 核心不变量: probe=True 的运行**不得**写感知库。"""

    def _pipeline(self, sandbox: Path):
        import core.mcp_client as _mc
        _mc.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
            load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [],
            tools=[], get_tool=lambda *a, **k: None)
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from storage.session_store import SessionStore
        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl.sessions = SessionStore(sandbox / "s.db")
        return pl

    def _fake_perception(self, sandbox: Path, calls: list):
        """替身: 记录 analyze 被调用的 (message, response)。"""
        class _Spy:
            def analyze(self, msg, resp, memory=None):
                calls.append((msg, (resp or "")[:60]))
                return {}
        return _Spy()

    def test_probe_true_skips_analysis(self, tmp_path):
        pl = self._pipeline(tmp_path)
        calls: list = []
        pl.perception = self._fake_perception(tmp_path, calls)

        async def _run():
            pl._process_external = pl._process_external  # 真方法
            # 直接调真 _process_external, probe=True → 不应写感知
            import core.pipeline as P
            # 用一个最小桩替掉模型调用, 只验证收尾的感知写入被跳过
            async def _fake_generate(*a, **k):
                return {"text": "ok"}
            pl.model_client.generate = _fake_generate
            res = await pl._process_external("你能识别我的电脑吗", "ollama", 0.0, probe=True)
            return res
        asyncio.run(_run())
        assert calls == [], f"probe=True 仍写了感知: {calls}"

    def test_signatures_accept_probe(self):
        """★ 三个触及感知写入的方法都必须收 probe (否则守卫传不进来)。"""
        import inspect as _inspect
        from core.pipeline import Level4Pipeline
        for name in ("_process_external", "_ir_chain_exec", "_analysis_preread"):
            sig = _inspect.signature(getattr(Level4Pipeline, name))
            assert "probe" in sig.parameters, f"{name} 缺 probe 参数"
            assert sig.parameters["probe"].default is False, f"{name} 默认值应为 False (加性)"

    def test_every_analyze_site_is_guarded(self):
        """★★ 静态守卫: pipeline 里每个 perception.analyze 调用点前面必须有 probe 判断。

        这条是"加性改动"的回归网: 以后有人新增一个感知写入点却忘了 probe,
        这个测试会红 (它当初就该拦下那两处漏网)。
        """
        src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
        lines = src.split("\n")
        sites = [i for i, l in enumerate(lines) if "perception.analyze(" in l
                 and not l.strip().startswith("#")]
        assert sites, "没找到 perception.analyze 调用点 —— 代码结构变了, 请更新本测试的定位方式"
        unguarded = []
        for i in sites:
            window = "\n".join(lines[max(0, i - 6):i + 1])
            if "probe" not in window:
                unguarded.append((i + 1, lines[i].strip()[:70]))
        assert not unguarded, f"这些感知写入点没有 probe 守卫: {unguarded}"
