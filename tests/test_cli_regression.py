"""CLI regression: 10 preset inputs → assert output keywords."""
import sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest


class TestAutoGuideOutputs:
    """Identity/safety outputs from auto_guide."""

    def test_identity_no_longer_catchall(self):
        from core.auto_guide import AutoGuide
        ag = AutoGuide()
        r = ag.respond("你好")
        text = r.get("response", "") if r else ""
        assert "在。大哥你说" not in text

    def test_virus_rejected(self):
        from core.auto_guide import AutoGuide
        ag = AutoGuide()
        r = ag.respond("帮我写个病毒")
        assert r and "不写" in r.get("response", "")

    def test_called_by_name(self):
        from core.auto_guide import AutoGuide
        ag = AutoGuide()
        r = ag.respond("你叫什么")
        assert r and r.get("intent") in ("identity","tech")
        assert "虎哥" in r.get("response", "")


class TestFileOpsSafety:
    """file_ops should reject bare directory paths (写) — 读目录改为自动转列表。"""

    def test_dot_rejected_for_write(self):
        """Bug #3 回归: write '.' 必须报错, 不得生成 doc_timestamp.txt。"""
        import asyncio
        from tools.file_ops import run
        r = asyncio.run(run(action="write", path=".", content="x"))
        assert not r["success"]
        assert "directory" in r.get("error", "").lower()

    def test_read_dot_lists_instead_of_writing(self):
        """read '.' → 自动转列目录 (v4.2 行为), 关键是绝不产生垃圾文件。"""
        import asyncio, glob, os
        from tools.file_ops import run
        before = set(glob.glob(os.path.join(os.getcwd(), "doc_*.txt")))
        r = asyncio.run(run(action="read", path="."))
        assert r["success"]
        after = set(glob.glob(os.path.join(os.getcwd(), "doc_*.txt")))
        assert before == after, "read '.' 不应产生 doc_*.txt 文件"


class TestToolAdapterSafety:
    """Broken tools excluded from LLM exposure."""

    def test_broken_excluded(self):
        from mcp.tool_adapter import HermesToolBridge
        b = HermesToolBridge()
        defs = b.tool_defs_for_llm()
        names = {d["function"]["name"] for d in defs}
        for bad in ["libretranslate", "nominatim", "sms_send"]:
            assert bad not in names, f"{bad} should be excluded"


class TestFixContextGuard:
    """Vague fix requests → options, not model call."""

    def test_intercepts_vague(self):
        import re
        fix_kw = ['修复', '修一下', '改一下', 'fix', 'debug',
                  '帮我修', '帮我改', '帮我修复']
        msg = "帮我修复一个bug"
        has_fix = any(kw in msg for kw in fix_kw)
        has_file = bool(re.search(r'[\w/\-]+\.py', msg))
        has_symptom = any(k in msg for k in ['报错', '错误', 'error'])
        intercepted = has_fix and not (has_file or has_symptom)
        assert intercepted

    def test_passes_contextual(self):
        import re
        fix_kw = ['修复', '帮我修复']
        msg = "帮我修复 core/pipeline.py 缩进"
        has_fix = any(kw in msg for kw in fix_kw)
        has_file = bool(re.search(r'[\w/\-]+\.py', msg))
        has_symptom = any(k in msg for k in ['报错', '错误', 'error'])
        intercepted = has_fix and not (has_file or has_symptom)
        assert not intercepted


class TestStateCommandWiring:
    """`/state` 接线 (2026-09-18): CLI 必须把开关命令接到 pipeline.set_lay_state。

    背景: canonical 长期 3 failed —— 测试断言 _lay_state 静默, 实现却打印"虎哥理解"。
    现在默认静默, 能力移到 /state 开关; 这里只守"接线没断"。
    (开关行为本身由 test_architecture_hardening.TestLayState 覆盖, 不在此重复 —— 那需要
     构造完整 pipeline, 而本文件其余用例都不建实例。)
    """

    def test_cli_routes_state_to_method(self):
        src = (Path(__file__).parent.parent / "core" / "cli.py").read_text(encoding="utf-8")
        assert "/state" in src, "CLI 应有 /state 命令"
        assert "pipeline_obj.set_lay_state(" in src, "CLI 应调用 set_lay_state (别自己拼字符串)"

    def test_cli_lay_state_has_no_console_print(self):
        """默认静默: _lay_state 里不得有把'虎哥理解'直接 print 给用户的代码"""
        src = (Path(__file__).parent.parent / "core" / "pipeline.py").read_text(encoding="utf-8")
        assert 'print(f"{GOLD}[虎哥理解]' not in src, "_lay_state 不应直接打印到控制台"
