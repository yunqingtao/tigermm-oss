"""本轮两处工具修复 — 测试 (2026-09-20)

① tiger_office 提取 PDF: 机器上装的是 **pypdf 6.x**, 而代码只认 PyPDF2/pdfplumber
   → "提取 PDF" 直接报 "pdfplumber or PyPDF2 required" (明明有库却用不了)。
   修: _DEPS 加 pypdf + 提取加回退分支。行为级验证用 pypdf 自己造一个空白 PDF。

② whisper 副产物污染: whisper CLI 把 .txt/.srt/.vtt/.tsv/.json 写在**输入文件旁边**,
   旧代码还 cwd=dirname(path) → 用户给一个视频, 他的文件夹立刻多 5 个文件。
   修: --output_dir 指到临时目录, 用完删。这条做静态断言 (真跑要下模型, 不适合进套件;
   已另做真跑验证: 用户目录 0 新增、临时目录 0 残留)。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestPdfExtractionFallback:
    def test_deps_sees_pypdf(self):
        from tools import tiger_office as TO
        assert "pypdf" in TO._DEPS, "_DEPS 里没有 pypdf, 提取 PDF 会白报缺库"

    def test_extract_text_works_with_installed_lib(self, tmp_path):
        """★ 用 pypdf 造一个单页 PDF → 提取必须成功 (修前必然报缺库)。"""
        pypdf = pytest.importorskip("pypdf")
        from tools import tiger_office as TO
        p = tmp_path / "blank.pdf"
        w = pypdf.PdfWriter()
        w.add_blank_page(width=200, height=200)
        with open(p, "wb") as f:
            w.write(f)
        r = asyncio.run(TO.run(action="extract_pdf_text", path=str(p)))
        assert r.get("success"), r
        # ★ 注意 page_count 的既有语义是"**含文字的**页数" (实现里 `if t: append`)
        #   → 空白页不计入, 所以这里应为 0。初版我断言 1 → 假红 (测试期望写错, 非代码错)。
        #   本用例的真正目的是: **pypdf 回退分支能跑通且不报缺库** (修前必报
        #   "pdfplumber or PyPDF2 required")。
        assert r.get("page_count") == 0, r
        assert isinstance(r.get("output"), str)

    def test_extract_text_missing_file_honest(self, tmp_path):
        from tools import tiger_office as TO
        r = asyncio.run(TO.run(action="extract_pdf_text", path=str(tmp_path / "nope.pdf")))
        assert r.get("success") is False and "not found" in str(r.get("error")).lower()

    def test_extract_text_no_path_honest(self):
        from tools import tiger_office as TO
        r = asyncio.run(TO.run(action="extract_pdf_text", path=""))
        assert r.get("success") is False and "路径" in str(r.get("error"))


class TestWhisperNoLitter:
    def test_output_dir_passed(self):
        """★ 必须显式给 --output_dir, 否则副产物落在用户目录。"""
        src = (Path(__file__).parent.parent / "tools" / "whisper.py").read_text(encoding="utf-8")
        assert '"--output_dir"' in src or "'--output_dir'" in src
        assert "mkdtemp" in src, "应有临时输出目录"

    def test_txt_lookup_uses_tmp_not_user_dir(self):
        """★ 读结果也要去临时目录找, 否则会去读用户目录里的残留 .txt。"""
        src = (Path(__file__).parent.parent / "tools" / "whisper.py").read_text(encoding="utf-8")
        assert "_outdir" in src
        assert 'os.path.splitext(path)[0] + ".txt"' not in src, "还在用输入路径拼 .txt (会读用户目录)"

    def test_cwd_is_tmp(self):
        src = (Path(__file__).parent.parent / "tools" / "whisper.py").read_text(encoding="utf-8")
        assert "cwd=_outdir" in src, "cwd 应指向临时目录"
        assert 'cwd=os.path.dirname(path) or "."' not in src

    def test_cleanup_in_finally(self):
        """★ 收尾必须删临时目录 (finally 里, 有异常也要删)。"""
        src = (Path(__file__).parent.parent / "tools" / "whisper.py").read_text(encoding="utf-8")
        assert "finally:" in src and "rmtree(_outdir" in src
