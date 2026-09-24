"""fix_context_guard: vague fix requests → options, contextual → pass."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest


# Mirror of _guard_fix_context logic — same as in pipeline.py
import re

FIX_KW = ['修复', '修一下', '改一下', '改改', '修修', '解决', '处理一下',
          'fix', 'debug', '修bug', '改bug', '修这个', '改这个',
          '帮我修', '帮我改', '帮我修复', '帮我解决']

SYMPTOM_KW = ['报错', '错误', 'error', '崩了', '不行', '不对', '打不开',
              '没反应', '闪退', '卡住', '没显示', '失败', '出问题', '异常',
              '坏了', '不正常', '报错信息', '日志', '提示', '警告']


def _should_intercept(msg):
    if not any(kw in msg for kw in FIX_KW):
        return False
    has_file = bool(re.search(r'[\w/\-]+\.(py|json|md|txt|yaml|toml|cfg)', msg))
    has_symptom = any(kw in msg for kw in SYMPTOM_KW)
    has_line = bool(re.search(r'(第?\d+行|line\s*\d+|L\d+)', msg, re.IGNORECASE))
    return not (has_file or has_symptom or has_line)


class TestFixContextIntercept:
    """Vague fix/modify requests lacking context → intercept."""

    def test_vague_fix_bug(self):
        assert _should_intercept("帮我修复一个bug")

    def test_vague_xiu(self):
        assert _should_intercept("修一下")

    def test_vague_fix_english(self):
        assert _should_intercept("fix this")

    def test_vague_debug(self):
        assert _should_intercept("帮我debug一下")


class TestFixContextPass:
    """Requests with filename/symptom/line → let model handle."""

    def test_has_file(self):
        assert not _should_intercept("帮我修复 core/pipeline.py")

    def test_has_symptom(self):
        assert not _should_intercept("帮我修复 decode error报错")

    def test_has_line_number(self):
        assert not _should_intercept("改改 line 800 的问题")

    def test_has_error_keyword(self):
        assert not _should_intercept("修修那个 崩了")


class TestNonFix:
    """Non-fix messages → pass through."""

    def test_chat(self):
        assert not _should_intercept("你好")

    def test_search(self):
        assert not _should_intercept("搜索北京天气")
