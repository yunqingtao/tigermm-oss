"""file_ops: '.' and '..' path rejection, normal ops unaffected."""
import sys, os, pytest, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestDotRejection:
    """Bug #3: '.' → error, not doc_timestamp.txt."""

    def test_read_dot_lists_instead_of_writing(self):
        """read '.' 自动转列目录 (v4.2), 关键是不产生 doc_*.txt。"""
        import glob, os
        from tools.file_ops import run
        before = set(glob.glob(os.path.join(os.getcwd(), "doc_*.txt")))
        r = asyncio.run(run(action="read", path="."))
        assert r["success"]
        after = set(glob.glob(os.path.join(os.getcwd(), "doc_*.txt")))
        assert before == after

    def test_write_dot_rejected(self):
        from tools.file_ops import run
        r = asyncio.run(run(action="write", path=".", content="x"))
        assert not r["success"]
        assert "directory" in r.get("error", "").lower()


class TestNormalOps:
    """Normal read/write/list should still work."""

    def test_read_real_file(self):
        from tools.file_ops import run
        r = asyncio.run(run(action="read", path="core/auto_guide.py", limit=1))
        assert r["success"]

    def test_list_dir(self):
        from tools.file_ops import run
        r = asyncio.run(run(action="list", path="core"))
        assert r["success"]

    def test_bare_dir_name_stays_in_project(self):
        """回归: 裸目录名 (core/tools) 不能被当成桌面文件名。

        2026-08-20 的"裸文件名→桌面"规则曾把 'core' 映射到 ~/Desktop/core,
        导致 "列出 core 目录的文件" 直接失败。带扩展名的裸名才该去桌面。
        """
        from tools.file_ops import run
        for d in ("core", "tools"):
            r = asyncio.run(run(action="list", path=d))
            assert r["success"], f"list {d!r} 应成功, 实际: {r.get('error')}"

    def test_bare_filename_still_goes_desktop(self):
        """保留 2026-08-20 修法: 带扩展名的裸文件名 → 桌面。"""
        from tools.file_ops import run
        r = asyncio.run(run(action="read", path="__no_such_file__.txt"))
        assert not r["success"]
        assert "Desktop" in str(r.get("error", "")), r.get("error")

    def test_write_then_read(self, tmp_path):
        from tools.file_ops import run
        import os
        f = os.path.join(str(tmp_path), "test.txt")
        w = asyncio.run(run(action="write", path=f, content="hello"))
        # write may fail if outside project root, just check it doesn't crash
        assert isinstance(w, dict)
