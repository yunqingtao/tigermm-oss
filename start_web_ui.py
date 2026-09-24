"""
启动 Tiger.M.M Web UI（独立进程）。

为什么用独立进程: Hermes 后台(background=true)启动的进程, 其 stdin/stdout
走会话管道, 运行 3-4 分钟后句柄失效触发 SIGSEGV (exit 139)。
用 DETACHED_PROCESS + DEVNULL 完全脱离后稳定。

用法: python start_web_ui.py   (或双击 start_web_ui.bat)
"""
import subprocess, sys, os, socket

# ★ 真人会话标记: 记账类功能(技能使用账/产物台账)只认这个信号 ——
#   测试/门禁没有它, 所以永远写不进用户真数据 (fail-closed)。
import os as _os_live; _os_live.environ.setdefault("TMM_LIVE", "1")

os.chdir(os.path.dirname(os.path.abspath(__file__)))


def port_in_use(port=8800):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def engine_ready(port=8800, timeout=90):
    """等引擎真的能应答 /stats (不只是端口开着) 再返回 True。"""
    import time as _t, urllib.request as _u
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        if port_in_use(port):
            try:
                with _u.urlopen(f"http://127.0.0.1:{port}/stats", timeout=2) as r:
                    if "uptime" in r.read().decode("utf-8", "replace"):
                        return True
            except Exception:
                pass
        _t.sleep(1)
    return False


def main():
    # --open: 起完(或已在跑)后自动打开默认浏览器 (加性: 不带参数行为完全不变)
    open_browser = "--open" in sys.argv
    # ★ 2026-09-21 加性: --dash 让浏览器打开**只读数据面**页面 (缺省行为一个字节没变)
    _ui_path = "/dash" if "--dash" in sys.argv else ""
    if port_in_use(8800):
        print("Tiger.M.M Web UI 已在运行 -> http://localhost:8800")
        if open_browser:
            engine_ready(8800, 15)
            import webbrowser
            webbrowser.open("http://localhost:8800" + _ui_path)
        return

    out = open("web_ui.out.log", "a", encoding="utf-8")
    err = open("web_ui.err.log", "a", encoding="utf-8")

    p = subprocess.Popen(
        [sys.executable, "web_server.py"],
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        creationflags=subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    print(f"Tiger.M.M Web UI 已启动 PID={p.pid} -> http://localhost:8800")
    if open_browser:
        if engine_ready(8800, 90):
            import webbrowser
            webbrowser.open("http://localhost:8800" + _ui_path)
        else:
            print("引擎启动超时 —— 请看 web_ui.err.log")


if __name__ == "__main__":
    main()
