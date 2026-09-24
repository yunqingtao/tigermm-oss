"""MCP 子进程回收 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

审计发现 **两个** 真泄漏 (2026-09-18):
  ① `MCPClient.shutdown()` **全仓无人调用** → 子进程永不回收; 且 `proc.kill()` 后
     asyncio 传输留到 GC 才收, 那时 loop 已关 → `RuntimeError: Event loop is closed`
     (表现为 pytest 的 PytestUnraisableExceptionWarning)
  ② `refresh_all` → `_discover_stdio` 里 `self._processes[name] = proc` **直接覆盖旧进程
     而不杀** → 每次刷新漏一个 (引擎后台刷新会持续漏)

修法:
  core/mcp_client.py  `_reap(name)` = kill + **await proc.wait()**(释放传输)
                      `aclose()` 逐个 reap; `_discover_stdio` 替换前先 reap
  core/pipeline.py    `aclose_resources()` 收口 (返回 awaitable)
  main.py             run_cli 的 finally → await aclose_resources()
  web_server.py       uvicorn.run 的 finally → asyncio.run(aclose_resources())
  tmm_app.py          `taskkill /F` → `/F /T` (引擎挂 MCP 子进程, 强杀会留孤儿)

★ 断言口径的教训(本会话第 7 次同类错): v1 用**全局进程集合 diff** 判"无孤儿",
  结果被引擎的后台刷新干扰(它持续 spawn/换 pid) → 误报。改为**追踪探针自己的具体 pid**。

跑法:
    python -B scripts/verification/verify_mcp_reap.py

副作用: 会起/杀几个探测用 MCP 子进程 (自己的, 不碰引擎的); 跑完自清。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

def _fill(s: str) -> str:
    """把探针模板里的 @@PROJ@@ 换成真实项目根。

    ★ 2026-09-21: 探针脚本写在临时目录里, 自己推不出项目根 (`__file__` 指向 %TEMP%)。
      原来是**写死绝对路径** —— 公开/换机就会指向别处。现在用占位符, 写入时填充。
    """
    # ★ 用**正斜杠**注入: 探针里的路径常被拼进中文话术再交给引擎解析, 反斜杠会与 "/"
    #   混用导致解析失败 (实测: 会当成"没给目录"而落到桌面)。正斜杠在 Windows 上同样合法。
    return s.replace("@@PROJ@@", str(ROOT).replace("\\", "/"))


PY = sys.executable

P, F = [], []
def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def alive(pid):
    if not pid:
        return False
    r = subprocess.run(f'tasklist /FI "PID eq {pid}" /FO CSV /NH', shell=True,
                       capture_output=True, text=True, encoding="gbk", errors="replace", timeout=60)
    return str(pid) in (r.stdout or "")


PROBE = r'''
import sys, os, json, asyncio, logging
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from core.mcp_client import MCPClient
from config.settings import DATA_DIR
MODE = sys.argv[1]
out = {}

async def main():
    c = MCPClient(DATA_DIR)
    c.load_config([{"name": "probe", "transport": "stdio", "command": "python",
                    "args": ["@@PROJ@@/_test_mcp_server.py"]}])
    t1 = await c.discover_tools("probe")
    p1 = c._processes.get("probe")
    out["tools"] = len(t1); out["child_pid_1"] = p1.pid if p1 else None
    if MODE == "refresh":
        t2 = await c.discover_tools("probe")
        p2 = c._processes.get("probe")
        out["child_pid_2"] = p2.pid if p2 else None
        out["p1_returncode"] = p1.returncode if p1 else None
        out["pid_changed"] = bool(p1 and p2 and p1.pid != p2.pid)
        out["tools2"] = len(t2)
    if MODE == "kill_only":
        c.shutdown()
        out["rc_immediately"] = p1.returncode if p1 else None
    else:
        await c.aclose()
        out["rc_after"] = p1.returncode if p1 else None
        out["cleared"] = (len(c._processes) == 0)
    out["ok"] = True

asyncio.run(main())
print("<<<J>>>" + json.dumps(out, ensure_ascii=False))
'''


def run_probe(mode):
    fd, tmp = tempfile.mkstemp(suffix=".py"); os.close(fd)
    Path(tmp).write_text(_fill(PROBE), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp, mode], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=200, cwd=str(ROOT))
        return json.loads(r.stdout.split("<<<J>>>")[1].strip().splitlines()[0]), r.stdout + r.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    mc = (ROOT / "core" / "mcp_client.py").read_text(encoding="utf-8")
    pl = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    mn = (ROOT / "main.py").read_text(encoding="utf-8")
    ws = (ROOT / "web_server.py").read_text(encoding="utf-8")
    ap = (ROOT / "tmm_app.py").read_text(encoding="utf-8")

    chk("_reap 存在 (kill + await wait)",
        "async def _reap" in mc and "await asyncio.wait_for(old.wait()" in mc)
    chk("aclose 复用 _reap", "await self._reap(name)" in mc)
    chk("替换前 reap (refresh 泄漏已修)", "await self._reap(server_name)" in mc)
    chk("shutdown() 保留兼容", "def shutdown" in mc)
    chk("pipeline.aclose_resources", "def aclose_resources" in pl)
    chk("main.py 退出收口", "await pipeline.aclose_resources()" in mn)
    chk("web_server 退出收口", "pipeline.aclose_resources()" in ws)
    chk("tmm_app taskkill /T", "taskkill /F /T /PID" in ap and "taskkill /F /PID" not in ap)

    print("        —— 行为: aclose 回收自己的子进程 ——")
    d, raw = run_probe("aclose")
    pid = d.get("child_pid_1")
    chk("探针连上并起了子进程", d.get("ok") is True and bool(pid) and (d.get("tools") or 0) > 0)
    chk("returncode 已置 (证明 await 到了)", d.get("rc_after") is not None, str(d.get("rc_after")))
    chk("内部表已清空", d.get("cleared") is True)
    time.sleep(2)
    chk(f"子进程 pid={pid} 已消失", not alive(pid), f"pid {pid} 仍在")

    print("        —— 行为: 重复 refresh 不漏进程 ——")
    d3, _ = run_probe("refresh")
    chk("两次 discover 都成功", (d3.get("tools") or 0) > 0 and (d3.get("tools2") or 0) > 0)
    chk("第二次换新进程 (前提成立)", d3.get("pid_changed") is True)
    chk("旧进程 returncode 非 None (已被 reap)", d3.get("p1_returncode") is not None,
        str(d3.get("p1_returncode")))
    time.sleep(2)
    chk(f"旧进程 pid={d3.get('child_pid_1')} 已消失", not alive(d3.get("child_pid_1")))
    chk(f"最新进程 pid={d3.get('child_pid_2')} 已回收", not alive(d3.get("child_pid_2")))

    print("        —— A/B: 无事件循环噪声 ——")
    noise = [l for l in raw.split("\n") if "Event loop is closed" in l or "BaseSubprocessTransport" in l]
    chk("aclose 路径无噪声", not noise, str(noise[:2]))
    d4, raw2 = run_probe("kill_only")
    chk("对照 kill-only 未 await", d4.get("rc_immediately") is None, str(d4.get("rc_immediately")))
    n2 = [l for l in raw2.split("\n") if "Event loop is closed" in l or "BaseSubprocessTransport" in l]
    print(f"        A/B — aclose 噪声 {len(noise)} 条 | kill-only 噪声 {len(n2)} 条")
    if d4.get("child_pid_1") and alive(d4.get("child_pid_1")):
        subprocess.run(f"taskkill /F /PID {d4['child_pid_1']}", shell=True, capture_output=True)

    print("        —— canonical ——")
    r = subprocess.run([PY, "-B", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=580)
    tail = [l for l in r.stdout.split("\n") if "passed" in l or "failed" in l]
    print(f"        {tail[-1] if tail else r.stdout.strip()[-70:]}")
    chk("canonical 全绿 (0 failed)", r.returncode == 0 and "failed" not in r.stdout,
        r.stdout.strip()[-150:])
    chk("pytest 无 unraisable 传输警告", "BaseSubprocessTransport" not in r.stdout)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
