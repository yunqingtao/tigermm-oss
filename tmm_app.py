"""
Tiger.M.M 桌面应用外壳 — 双击即用 (对标 WorkBuddy 的桌面端形态)

做三件事:
  1. 引擎自启: 8800 没跑就拉起 web_server.py (脱离进程, 不占本程序生命周期)
  2. 原生窗口: pywebview + Edge WebView2 渲染现有三模式界面
  3. 优雅收尾: 若是本程序启动的引擎, 关窗时精确停掉; 本来就在跑的则不动

用法:
  python tmm_app.py            # 直接跑
  双击 Tiger.M.M.bat          # 无控制台窗口 (pythonw)
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# ★ 真人会话标记: 记账类功能(技能使用账/产物台账)只认这个信号 ——
#   测试/门禁没有它, 所以永远写不进用户真数据 (fail-closed)。
import os as _os_live; _os_live.environ.setdefault("TMM_LIVE", "1")

ROOT = Path(__file__).resolve().parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

PORT = 8800
URL = f"http://127.0.0.1:{PORT}"
ICON = ROOT / "tigermm.ico"
LOG = ROOT / "tmm_app.log"

BG = "#0a0502"
GOLD = "#FFAC02"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# 引擎管理
# ═══════════════════════════════════════════════════════════

def port_open(port: int = PORT, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def is_tmm_engine(timeout: float = 3.0) -> bool:
    """8800 上跑的确实是我们自己的引擎吗 (而不是别的程序占着端口)。"""
    try:
        with urllib.request.urlopen(URL + "/stats", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return isinstance(d, dict) and "uptime" in d
    except Exception:
        return False


def pid_on_port(port: int = PORT) -> list[str]:
    """精确定位监听端口的 PID (禁止 taskkill /IM python.exe —— 会杀掉 Hermes 自己)。

    ⚠ 必须按列解析后比对端口号 —— 用 `f":{port}" in line` 会子串误命中:
      ":8800" 会匹配到 ":88001"..":88009"; ":1" 会匹配到所有 "1xxxx" 端口。
    """
    pids = set()
    want = f":{port}"
    try:
        out = subprocess.run("netstat -ano", shell=True, capture_output=True,
                             text=True, encoding="gbk", errors="replace", timeout=20).stdout
        for line in out.split("\n"):
            if "LISTENING" not in line.upper():
                continue
            parts = line.split()
            # 形如: TCP  127.0.0.1:8800  0.0.0.0:0  LISTENING  10572
            if len(parts) < 5 or parts[0].upper() not in ("TCP", "UDP"):
                continue
            if parts[1].endswith(want) and parts[-1].isdigit():
                pids.add(parts[-1])
    except Exception as e:
        log(f"pid_on_port 失败: {e}")
    return sorted(pids)


def start_backend() -> bool:
    """以脱离进程方式启动引擎 (不随本程序退出而死)。"""
    try:
        out = open(ROOT / "web_ui.out.log", "a", encoding="utf-8")
        err = open(ROOT / "web_ui.err.log", "a", encoding="utf-8")
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= getattr(subprocess, name, 0)
        p = subprocess.Popen(
            [sys.executable, "-B", "web_server.py"],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=flags, close_fds=True)
        log(f"引擎已启动 pid={p.pid}")
        return True
    except Exception as e:
        log(f"引擎启动失败: {e}")
        return False


def wait_ready(timeout: int = 120) -> bool:
    """轮询直到引擎真的能响应 (不只是端口开着)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_tmm_engine(timeout=2):
            log(f"引擎就绪 ({time.time()-t0:.1f}s)")
            return True
        time.sleep(1.5)
    log("引擎等待超时")
    return False


# ═══════════════════════════════════════════════════════════
# 启动页 / 错误页
# ═══════════════════════════════════════════════════════════

SPLASH_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0502;color:#FFAC02;height:100vh;display:flex;flex-direction:column;
     align-items:center;justify-content:center;font:15px/1.7 'Microsoft YaHei',sans-serif}
#t{font-size:1.6em;font-weight:700;letter-spacing:5px}
#s{color:#5a5147;font-size:.78em;letter-spacing:2px;margin-top:8px}
#bar{margin-top:34px;width:250px;height:2px;background:rgba(255,172,2,.14);overflow:hidden;border-radius:2px}
#bar i{display:block;height:100%;width:35%;background:#FFAC02;animation:run 1.15s ease-in-out infinite}
@keyframes run{0%{margin-left:-35%}100%{margin-left:100%}}
#msg{color:#5a5147;font-size:.72em;margin-top:20px;letter-spacing:1px}
</style></head><body>
<div id="t">TIGER.M.M</div>
<div id="s">星港驾驶舱 · MARY III</div>
<div id="bar"><i></i></div>
<div id="msg">正在启动引擎…</div>
</body></html>"""


def error_html(detail: str) -> str:
    import html as _h
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{background:#0a0502;color:#cfc7bb;height:100vh;display:flex;flex-direction:column;
align-items:center;justify-content:center;font:14px/1.8 'Microsoft YaHei',sans-serif;padding:40px}}
h1{{color:#d0674f;font-size:1.2em;letter-spacing:2px;margin-bottom:14px}}
pre{{background:rgba(255,172,2,.05);border:1px solid rgba(255,172,2,.13);border-radius:8px;
padding:14px 18px;color:#847a6c;font-size:.86em;max-width:620px;white-space:pre-wrap;margin:10px 0 20px}}
.t{{color:#FFAC02;font-size:.9em}}
</style></head><body>
<h1>引擎没能起来</h1>
<pre>{_h.escape(detail)}</pre>
<div class="t">排查:</div>
<pre>1. 看日志  {LOG}
2. 看引擎日志  {ROOT}\\web_ui.err.log
3. 手动验证  python web_server.py
4. 端口被占  netstat -ano | findstr :{PORT}</pre>
</body></html>"""


# ═══════════════════════════════════════════════════════════
# 应用
# ═══════════════════════════════════════════════════════════

class App:
    def __init__(self):
        self.window = None
        self.owned_pids = []      # 本程序启动的引擎 PID (关窗时才停)
        self.ready = False

    def boot(self):
        """窗口已显示 → 后台起引擎 → 就绪后切到界面。"""
        try:
            if is_tmm_engine():
                log("复用已在运行的引擎")
            else:
                if port_open():
                    log(f"警告: 端口 {PORT} 被非 TMM 程序占用")
                    self.window.load_html(error_html(
                        f"端口 {PORT} 被别的程序占用。\n"
                        f"请先关掉它, 或改 config 里的端口。"))
                    return
                if not start_backend():
                    self.window.load_html(error_html("引擎进程启动失败, 见日志。"))
                    return
                time.sleep(1.0)
                self.owned_pids = pid_on_port()
            if wait_ready(timeout=120):
                self.ready = True
                self.window.load_url(URL)
            else:
                self.window.load_html(error_html(
                    "引擎 120 秒内没响应 /stats。\n"
                    "常见原因: 依赖缺失 (fastapi/uvicorn) 或 端口被占用。"))
        except Exception as e:
            log(f"boot 异常: {e}")
            try:
                self.window.load_html(error_html(f"{type(e).__name__}: {e}"))
            except Exception:
                pass

    def shutdown(self):
        """关窗: 只停本程序启动的引擎。

        `/T` = 连**子进程树**一起杀 —— 引擎会挂 MCP 子进程(_test_mcp_server.py),
        不加 /T 会留孤儿 (实测)。
        """
        if not self.owned_pids:
            log("引擎非本程序启动 → 保持运行")
            return
        for pid in self.owned_pids:
            try:
                subprocess.run(f"taskkill /F /T /PID {pid}", shell=True,
                               capture_output=True, timeout=20)
                log(f"已停止引擎 pid={pid} (含子进程树)")
            except Exception as e:
                log(f"停止 pid={pid} 失败: {e}")

    # ── 菜单动作 ──
    def reload_ui(self):
        try:
            self.window.load_url(URL)
        except Exception as e:
            log(f"reload 失败: {e}")

    def open_in_browser(self):
        try:
            import webbrowser
            webbrowser.open(URL)
        except Exception as e:
            log(f"打开浏览器失败: {e}")

    def show_log(self):
        try:
            os.startfile(str(ROOT / "web_ui.out.log"))
        except Exception as e:
            log(f"打开日志失败: {e}")

    def quit_app(self):
        try:
            self.window.destroy()
        except Exception:
            pass


def main():
    log("=" * 50)
    log("Tiger.M.M 桌面应用启动")

    app = App()

    menu_items = []
    try:
        import webview
        menu_items = [
            webview.menu.Menu("操作", [
                webview.menu.MenuAction("重新加载界面", app.reload_ui),
                webview.menu.MenuAction("在浏览器中打开", app.open_in_browser),
                webview.menu.MenuSeparator(),
                webview.menu.MenuAction("查看日志", app.show_log),
                webview.menu.MenuAction("退出", app.quit_app),
            ]),
        ]
    except Exception:
        menu_items = []

    kwargs = dict(
        title="Tiger.M.M · 星港驾驶舱",
        html=SPLASH_HTML,
        width=1340, height=880,
        min_size=(1000, 640),
        background_color=BG,
        text_select=True,
        confirm_close=False,
        resizable=True,
    )
    if menu_items:
        kwargs["menu"] = menu_items

    import webview
    app.window = webview.create_window(**kwargs)

    start_kwargs = dict(
        func=app.boot,
        private_mode=False,                       # 保留登录态/偏好
        storage_path=str(ROOT / "data" / "webview_store"),
    )
    if ICON.exists():
        start_kwargs["icon"] = str(ICON)

    try:
        webview.start(**start_kwargs)
    finally:
        app.shutdown()
        log("桌面应用退出")


if __name__ == "__main__":
    main()
