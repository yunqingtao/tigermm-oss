"""优雅停引擎 —— 避免强杀毁掉 SQLite WAL。

★ 为什么要有这个 (2026-09-20 实测踩坑):
   之前停引擎用 `taskkill /F` (强杀), 而引擎在 WAL 模式下持有 data/chat_sessions.db。
   强杀后留下**过期 -shm** + 0 字节 -wal, 后续任何连接执行 PRAGMA 都报
   `sqlite3.OperationalError: disk I/O error` —— 门禁里 verify_user_turn_dedup 因此
   直接崩成 0 PASS / 1 FAIL, 看起来像"门禁坏了", 实际是库被强杀搞脏了。
   修: 先**优雅**停 (taskkill 不带 /F, 相当于发关闭请求) → 等它自己收尾
   (tmm_app.py 的 shutdown() 会关 MCP 子进程) → 实在不走再强杀 →
   **最后做一次 WAL checkpoint** 把 -wal 收干净 (顺手清理过期 -shm)。

用法:
    python stop_web_ui.py            # 优雅停 (默认等 12 秒)
    python stop_web_ui.py --timeout 20
    python stop_web_ui.py --quiet    # 只出错才说话
退出码: 0 = 已停/本来就没跑 · 1 = 停不掉 (需要人工看)
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "chat_sessions.db"


def _pids_on(port: int = 8800) -> list:
    """找到监听该端口的进程 (只读 netstat, 不动系统)。"""
    try:
        out = subprocess.run(f'netstat -ano | findstr ":{port} " | findstr LISTENING',
                             shell=True, capture_output=True, text=True,
                             encoding="gbk", errors="replace").stdout
    except Exception:
        return []
    pids = []
    for line in (out or "").split("\n"):
        parts = line.split()
        if len(parts) >= 5 and parts[-1].isdigit():
            pids.append(parts[-1])
    return sorted(set(pids))


def _alive(pid: str) -> bool:
    r = subprocess.run(f'tasklist /FI "PID eq {pid}" /FO CSV /NH', shell=True,
                       capture_output=True, text=True, encoding="gbk", errors="replace")
    return pid in (r.stdout or "")


# 窗口标题 (tmm_app.py 的 create_window(title=...)) —— 靠它精确认人
WINDOW_TITLE_HINT = "Tiger.M.M"


def _app_windows() -> list:
    """找出**我们自己的**顶层窗口 (按标题认, 不看进程)。

    ★ 为什么按标题 (2026-09-20 踩坑后改的): 第一版按"所有 pythonw 进程"找窗口/杀进程
    —— 实测那会列出**全机器 10 个 pythonw** (别的会话、别的 Agent 都在里面),
      降级强杀时会把它们一起杀掉。这是很危险的做法, 已彻底移除。
      现在只认标题含 "Tiger.M.M" 的窗口 → 只会碰到我们自己的界面。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    user32 = ctypes.windll.user32
    out = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if WINDOW_TITLE_HINT.lower() in (buf.value or "").lower():
            out.append(hwnd)
        return True

    user32.EnumWindows(CB(_cb), 0)
    return out


def _wm_close() -> int:
    """★ 真正的优雅关闭: 给我们自己窗口发 WM_CLOSE (走 pywebview shutdown())。

    实测的为什么: `taskkill` 不带 /F 对**无控制台的 pythonw 进程无效** (没有窗口/消息循环
    可接收), 于是"优雅停"实际一直落到强杀, 只是事后 checkpoint 救了库 ——
    输出里那句 "优雅停没走, 强杀" 就是证据。改成 WM_CLOSE 后实测
    "[方式: 窗口关闭 (WM_CLOSE)]", 真走 shutdown() 收尾 (关 MCP + 让 uvicorn 退出)。
    """
    hwnds = _app_windows()
    if not hwnds:
        return 0
    try:
        import ctypes
    except Exception:
        return 0
    for h in hwnds:
        ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
    return len(hwnds)


def _stop(pid: str, hard: bool) -> None:
    flag = "/F " if hard else ""
    subprocess.run(f"taskkill {flag}/PID {pid}", shell=True, capture_output=True,
                   text=True, encoding="gbk", errors="replace")


def _tidy_db() -> str:
    """收尾: WAL checkpoint 收干净 + 清过期 -shm (库脏了也能救)。"""
    notes = []
    if DB.is_file():
        try:
            c = sqlite3.connect(str(DB))
            try:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                c.commit()
                notes.append("wal 已 checkpoint")
            finally:
                c.close()
        except sqlite3.DatabaseError as e:
            # ★ catch DatabaseError (父类), 不只 OperationalError ——
            #   坏库抛的是 "file is not a database" (DatabaseError), 只捕 OperationalError
            #   会直接漏出去把收尾打断 (被单元测试抓出来的)。
            notes.append(f"checkpoint 跳过 ({type(e).__name__}: {e})")
    shm = Path(str(DB) + "-shm")
    if shm.exists():
        try:
            shm.unlink()
            notes.append("清理过期 -shm")
        except OSError:
            notes.append("-shm 被占用, 未清")
    # 收尾复检
    if DB.is_file():
        try:
            c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            try:
                ic = c.execute("PRAGMA integrity_check").fetchone()[0]
                n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                notes.append(f"库 integrity={ic} · messages={n}")
            finally:
                c.close()
        except Exception as e:
            notes.append(f"复检失败: {type(e).__name__}: {e}")
    return " · ".join(notes)


def main() -> int:
    ap = argparse.ArgumentParser(description="优雅停 TMM 引擎")
    ap.add_argument("--timeout", type=int, default=12, help="优雅等待秒数 (默认 12)")
    ap.add_argument("--quiet", action="store_true", help="只输出一行结果")
    ap.add_argument("--port", type=int, default=8800)
    a = ap.parse_args()

    pids = _pids_on(a.port)
    if not pids:
        msg = "引擎没在跑"
        print(msg if a.quiet else f"{msg} (端口 {a.port} 无监听) · {_tidy_db()}")
        return 0

    if not a.quiet:
        print(f"找到引擎 PID: {', '.join(pids)} —— 先优雅停 (等 {a.timeout}s)")

    # 第 1 级: WM_CLOSE 给我们自己的窗口 (真优雅 —— 走 pywebview shutdown() 收尾)
    #   ★ 只按窗口标题认人, 不碰进程列表 (见 _app_windows 的踩坑说明)
    how = ""
    n_closed = _wm_close()
    if n_closed and not a.quiet:
        print(f"已向 {n_closed} 个自有窗口发 WM_CLOSE (标题含 {WINDOW_TITLE_HINT!r})")
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if not _pids_on(a.port):
            break
        time.sleep(1)
    if not _pids_on(a.port):
        how = "窗口关闭 (WM_CLOSE)"

    # 第 2 级: 软停 (不带 /F; 只对**端口属主**)
    if not how:
        for pid in pids:
            _stop(pid, hard=False)
        deadline = time.time() + max(4, a.timeout // 3)
        while time.time() < deadline:
            if not _pids_on(a.port):
                break
            time.sleep(1)
        if not _pids_on(a.port):
            how = "软停 (taskkill 无 /F)"

    # 第 3 级: 强杀 (最后手段 —— 会留脏 WAL, 所以后面必须 checkpoint)
    #   ★ 只杀**端口属主**, 绝不按进程名批量杀 (那会误伤别的会话/别的 Agent)
    left = _pids_on(a.port)
    if left:
        for pid in left:
            _stop(pid, hard=True)
        time.sleep(2)
        left = _pids_on(a.port)
        if not left:
            how = "强杀 (留脏 WAL, 已 checkpoint 兜底)"

    notes = _tidy_db()
    if left:
        print(f"✗ 停不掉: {', '.join(left)} —— 请手工检查")
        return 1
    print((f"引擎已停 [{how}] · " if a.quiet else f"✓ 引擎已停 [方式: {how}] · ") + notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
