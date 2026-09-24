"""技能参数抽取 + 产出守卫 + shell_exec 安全规则 — 测试 (2026-09-20)

对照 OpenClaw 补技能时暴露的三处**真缺陷**, 每处都有实跑证据:

  ① URL 抽不到: text 参数只从「引号内 / '内容:' 后」抽 →
     "总结这个网页 https://x" 的 url 参数**恒空** → 网页类技能根本跑不起来
  ② 媒体路径抽不到: path 抽取的文件后缀表只有文档/图片 → ".mp3/.mp4" 不认
     → "转写这段录音 D:/a.mp3" / "抽帧 D:/v.mp4" 拿不到路径
  ③ 产出守卫过严: 含 llm 步骤的技能被"必须有写/生成字样"挡着 →
     "总结这个网页 https://x"(有素材、要成品) 永远接不到, 落到通用兜底
     (修法: 补"带明确来源=链接或文件路径"这一条; 而"总结一下这个文档"仍须被排除)

另含 shell_exec 两条**误杀**回归 (裸 `format` 一词造成的):
  ④ `ffprobe -show_entries format=duration` 被 DANGEROUS_COMMANDS 的 `\\bformat\\b` 拦
  ⑤ 同一条命令因路径含 `/bin/` + 词里有 `format` 又被"保护路径"规则拦
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pipeline import Level4Pipeline as P


class _Dag:
    def __init__(self, schema):
        self.params_schema = schema


@pytest.fixture
def pl():
    return P.__new__(P)


def _params(pl, msg, schema):
    pl._ke = None
    return pl._skill_params(msg, _Dag(schema))


# ───────────────── ① URL 抽取 ─────────────────

class TestUrlExtraction:
    SCHEMA = {"url": {"type": "text"}, "style": {"type": "text"}}

    @pytest.mark.parametrize("msg,want", [
        ("总结这个网页 https://example.com/a", "https://example.com/a"),
        ("把这篇整理成摘要 https://news.site.cn/x?y=1 存到 D:/报告/", "https://news.site.cn/x?y=1"),
        ("文章摘要 http://a.b.com/p/1。", "http://a.b.com/p/1"),
    ])
    def test_url_filled(self, pl, msg, want):
        got = _params(pl, msg, self.SCHEMA)
        assert got.get("url") == want, got

    def test_no_url_leaves_empty(self, pl):
        assert "url" not in _params(pl, "整理成摘要 把那份文档处理一下", self.SCHEMA)

    def test_non_url_param_untouched(self, pl):
        """参数名不暗示网址时不该被塞 URL (别抢别的东西)。"""
        got = _params(pl, "写份摘要 https://example.com/a", {"style": {"type": "text"}})
        assert "style" not in got or "http" not in str(got.get("style"))


# ───────────────── ② 媒体路径抽取 ─────────────────

class TestMediaPathExtraction:
    SCHEMA = {"path": {"type": "path"}, "out": {"type": "path"}}

    @pytest.mark.parametrize("msg,want", [
        ("转写这段录音 D:/a.mp3", "D:/a.mp3"),
        ("把这个 D:/v/clip.mp4 抽帧", "D:/v/clip.mp4"),
        ("语音转文字 C:/x/y.m4a", "C:/x/y.m4a"),
        ("转写 D:/rec/wav1.wav 存到 D:/out/", "D:/rec/wav1.wav"),
        ("抽帧 D:/v/a.mov", "D:/v/a.mov"),
    ])
    def test_media_path_filled(self, pl, msg, want):
        got = _params(pl, msg, self.SCHEMA)
        assert got.get("path") == want, got

    def test_doc_path_still_works(self, pl):
        assert _params(pl, "审阅 D:/docs/plan.docx", self.SCHEMA).get("path") == "D:/docs/plan.docx"

    def test_output_dir_still_detected(self, pl):
        got = _params(pl, "转写 D:/a.mp3 存到 D:/out/", self.SCHEMA)
        assert got.get("out", "").rstrip("/\\") == "D:/out", got


# ───────────────── ③ 产出守卫 ─────────────────

class TestCreationGuard:
    @pytest.mark.parametrize("msg", [
        "总结这个网页 https://example.com/a",
        "转写这段录音 D:/a.mp3",
        "从视频里抽帧 D:/v/clip.mp4",
        "双模型审阅 D:/docs/draft.md",
    ])
    def test_source_counts_as_production(self, msg):
        assert P._has_explicit_source(msg), msg
        assert P._looks_like_creation(msg) or P._has_explicit_source(msg)

    @pytest.mark.parametrize("msg", [
        "总结一下这个文档",          # ★ 既有负例, 必须仍然被排除
        "帮我看看这份周报",
        "这个图是什么意思",
        "这段代码讲什么",
    ])
    def test_readonly_still_excluded(self, msg):
        assert not P._looks_like_creation(msg)
        assert not P._has_explicit_source(msg), f"{msg} 被误判成带来源"

    def test_creation_words_unchanged(self):
        for m in ("写份周报", "画个架构图", "做个流程图"):
            assert P._looks_like_creation(m), m


# ───────────────── ④⑤ shell_exec 误杀回归 ─────────────────

class TestShellExecFormatFalsePositive:
    def test_ffprobe_format_allowed(self):
        """★ 裸 `format` 曾拦下 ffprobe 的合法用法 (实测被拦死)。"""
        from tools.shell_exec import _check_security
        ok, why = _check_security(
            "ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 D:/v/a.mp4")
        assert ok, why

    def test_ffmpeg_with_bin_path_allowed(self):
        """★ 路径含 /bin/ + 词里有 format → 曾被"保护路径"规则拦 (同一个词两处误杀)。"""
        from tools.shell_exec import _check_security
        cmd = ("python -c \"import subprocess as S;S.run(['c:/ffmpeg/bin/ffmpeg.exe','-y',"
               "'-i','D:/v/a.mp4','-show_entries','format=duration'])\"")
        ok, why = _check_security(cmd)
        assert ok, why

    @pytest.mark.parametrize("cmd", [
        "format c:",
        "format D: /q",
        "cmd /c format e:",
    ])
    def test_disk_format_still_blocked(self, cmd):
        """★ 真危险形态 (格式化盘) 必须继续拦 —— 别为了修误杀把闸门拆了。"""
        from tools.shell_exec import _check_security
        ok, why = _check_security(cmd)
        assert not ok, f"{cmd} 竟然放行了"

    @pytest.mark.parametrize("cmd", [
        "del /f C:\\Windows\\x.dll", "rd /s C:\\Windows", "shutdown /s /t 0",
        "taskkill /F /PID 1", "reg add HKLM\\x",
    ])
    def test_other_dangerous_still_blocked(self, cmd):
        from tools.shell_exec import _check_security
        ok, _why = _check_security(cmd)
        assert not ok, f"{cmd} 竟然放行了"
