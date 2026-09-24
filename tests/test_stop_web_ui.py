"""stop_web_ui.py — 优雅停引擎的测试 (2026-09-20)

为什么要有这一份: 停引擎看着简单, 实测踩了两个坑, 都是**会伤数据/伤别人**的那种:

  ① 强杀 (taskkill /F) 引擎 → WAL 残留 → 之后任何 PRAGMA 报 `disk I/O error`
     (门禁曾被它搞崩: verify_user_turn_dedup 0 PASS / 1 FAIL)
  ② ★ 第一版按"所有 pythonw 进程"找窗口/杀进程 —— 实测列出**全机器 10 个 pythonw**
     (别的会话、别的 Agent 都在里面), 降级强杀会把它们一起杀掉。**危险, 已移除。**

所以本份守三条不变量:
  A 不按进程名批量杀 (只认端口属主 + 自有窗口标题)
  B 有 WM_CLOSE 优雅路径, 且靠**窗口标题**认人
  C 收尾一定做 WAL checkpoint + 清 -shm + 复检 integrity
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PY = sys.executable
SRC_PATH = ROOT / "stop_web_ui.py"


@pytest.fixture(scope="module")
def sw():
    """加载 stop_web_ui 模块 (只导入, 不执行 main)。"""
    spec = importlib.util.spec_from_file_location("tmm_stop_web_ui", str(SRC_PATH))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def src():
    return SRC_PATH.read_text(encoding="utf-8", errors="replace")


class TestNoMassKill:
    """★★ 安全不变量: 绝不按进程名批量杀 (那会误伤别的会话/别的 Agent)。"""

    def test_no_shell_pids_helper(self, src):
        assert "_shell_pids" not in src, "按 pythonw 进程列表批量收尾的做法必须保持移除"

    def test_no_kill_by_imagename(self, src):
        """tasklist 按镜像名枚举 + 杀 = 危险组合, 不许出现。"""
        assert 'IMAGENAME eq pythonw.exe' not in src
        assert 'IMAGENAME eq python.exe' not in src

    def test_kills_only_port_owner(self, src):
        """强杀只允许出现在 `for pid in left` 这种『端口属主』循环里。"""
        assert "for pid in left:" in src
        assert "_stop(pid, hard=True)" in src
        # 不许出现"遍历某个名单再杀"
        for bad in ("for pid in shell:", "for pid in _shell_pids", "for pid in others:"):
            assert bad not in src, f"出现了按名单批量杀: {bad}"


class TestGracefulPath:
    """★ WM_CLOSE 优雅路径存在, 且靠窗口标题认人 (不靠进程)。"""

    def test_has_wm_close(self, sw):
        assert callable(getattr(sw, "_wm_close", None))

    def test_title_hint_defined(self, sw):
        assert getattr(sw, "WINDOW_TITLE_HINT", "") == "Tiger.M.M"

    def test_app_windows_uses_title_not_pid(self, src):
        assert "GetWindowTextW" in src, "应按窗口标题认人"
        assert "EnumWindows" in src
        # 不该用 PID 过滤窗口
        assert "GetWindowThreadProcessId" not in src, "按 PID 过滤窗口正是误伤的来源"

    def test_app_windows_returns_list(self, sw):
        got = sw._app_windows()
        assert isinstance(got, list)          # 没开窗口时为空列表, 不报错

    def test_levels_declared(self, src):
        """三级降级要有明确标注 (发生什么要能被看见)。"""
        assert "窗口关闭 (WM_CLOSE)" in src
        assert "软停 (taskkill 无 /F)" in src
        assert "强杀" in src
        assert "方式:" in src                  # 输出里报告靠哪级停下来的


class TestTidyDb:
    """★ 收尾必须 checkpoint + 清 -shm + 复检 (脏库也要能救回来)。"""

    def test_tidy_reports_integrity(self, sw, tmp_path, monkeypatch):
        db = tmp_path / "t.db"
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
        c.executemany("INSERT INTO messages(content) VALUES(?)", [("x",), ("y",)])
        c.commit()
        c.close()
        (tmp_path / "t.db-shm").write_bytes(b"stale-shm")   # 伪造过期 shm
        monkeypatch.setattr(sw, "DB", db)
        note = sw._tidy_db()
        assert "checkpoint" in note
        assert "integrity=ok" in note
        assert not (tmp_path / "t.db-shm").exists(), "过期 -shm 应被清掉"
        # 数据没丢
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        c.close()

    def test_tidy_handles_broken_db(self, sw, tmp_path, monkeypatch):
        """库读不了时不许抛异常 (收尾要尽量做完)。"""
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"not a sqlite file at all")
        monkeypatch.setattr(sw, "DB", bad)
        note = sw._tidy_db()
        assert isinstance(note, str) and note


class TestCli:
    def test_help_works(self):
        r = subprocess.run([PY, "-B", str(SRC_PATH), "--help"], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        assert r.returncode == 0
        assert "--timeout" in r.stdout and "--quiet" in r.stdout

    def test_pids_on_returns_list(self, sw):
        got = sw._pids_on(port=59999)          # 不可能有人监听
        assert isinstance(got, list) and got == []

    def test_has_no_side_effect_on_import(self, src):
        """导入不许自动停引擎 (只有 __main__ 才跑 main)。"""
        assert 'if __name__ == "__main__":' in src
        assert src.index('if __name__ == "__main__":') > src.index("def main()")


class TestBatLauncher:
    def test_bat_exists_and_ascii(self):
        b = ROOT / "stop_web_ui.bat"
        assert b.is_file()
        txt = b.read_text(encoding="utf-8", errors="replace")
        assert all(ord(c) < 128 for c in txt), "bat 里出现非 ASCII 会在某些控制台崩"
        assert "stop_web_ui.py" in txt

    def test_bat_uses_shared_python_probe(self):
        """★ 2026-09-21 更新: 原断言是 `set "PY=` (写死绝对路径)。

        那样在别人机器上必然指错 —— 而"别人拿到能跑"是分发版的硬门槛。
        现在正确的做法: 走共用的 `_find_python.bat` 探测 (它内部才 set PY),
        既保留"绝不裸 python"这条原始意图, 又不用把作者的家目录写死。
        """
        txt = (ROOT / "stop_web_ui.bat").read_text(encoding="utf-8", errors="replace")
        low = txt.lower()
        assert "_find_python.bat" in low, "必须用共用的解释器探测 (否则写死路径 = 别人拿到跑不了)"
        assert '"%py%"' in low, "必须用 %PY% 调用, 不能裸 python (会落到别的解释器)"
        assert "python.exe" not in low, "不该再写死任何 python.exe 绝对路径"
