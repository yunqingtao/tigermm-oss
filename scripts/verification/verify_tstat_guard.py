"""ui/app.html 顶栏状态守卫 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景: `<b id="tStat">连接中…</b>` 是 HTML 初始值, 但 JS 只在发消息时改写它,
`init()` 只在有历史时才写 → **空会话下顶栏永远卡"连接中…"** (2026-09-18 修)。
修法: pollStats() 首次探测成功后置 '已连接', 用一次性标志 statInit, 不覆盖用户操作后的状态。

跑法:
    python -B scripts/verification/verify_tstat_guard.py

技术要点 (可复用于任何前端 JS 逻辑验证):
    **用 node 执行从 HTML 里逐字提取的函数源码**, 在 stub DOM 下断言行为 ——
    验的是真发出去的代码, 不是重写的一份; 无需 playwright/selenium。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
NODE = r"D:\tools\nodejs\node.exe"
BASE = "http://127.0.0.1:8800"
UI = ROOT / "ui" / "app.html"

P, F, SKIP = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def skip(name, why=""):
    """★ 条件不具备 → 跳过 (不算 FAIL, 但打印醒目 SKIP 行, 绝不静默)。"""
    SKIP.append(name)
    print(f"SKIP {name}" + (f"  <- {why}" if why else ""))


def build_harness(ui_src: str) -> str:
    fmt_src = "function fmt(" + ui_src.split("function fmt(", 1)[1].split("\n", 1)[0]
    poll_src = "async function pollStats(){" + ui_src.split(
        "async function pollStats(){", 1)[1].split("async function pollInbox", 1)[0]
    return "\n".join([
        "const OK = {uptime:100, total:3, errors:0, sessions:2};",
        "const els = { stats: {innerHTML:''}, tStat: {textContent:'连接中…'} };",
        "const document = { getElementById: (id) => els[id] };",
        "const esc = (s) => String(s);",
        "let statInit = true;",
        "let __resp = OK;",
        "const fetch = async () => ({ json: async () => __resp });",
        "const tStat = els.tStat;",
        fmt_src,
        poll_src,
        "(async () => {",
        "  const out = {};",
        "  out.a_before = els.tStat.textContent;",
        "  await pollStats();",
        "  out.a_after = els.tStat.textContent; out.a_flag = statInit;",
        "  els.tStat.textContent = '完成';",
        "  await pollStats();",
        "  out.b_after = els.tStat.textContent;",
        "  statInit = true; els.tStat.textContent = '连接中…';",
        "  await pollStats();",
        "  out.c_after = els.tStat.textContent;",
        "  statInit = true; els.tStat.textContent = '连接中…'; __resp = {error:'boom'};",
        "  await pollStats();",
        "  out.d_after = els.tStat.textContent; out.d_flag = statInit;",
        "  __resp = OK;",
        "  await pollStats();",
        "  out.e_after = els.tStat.textContent; out.e_stats = els.stats.innerHTML;",
        "  console.log(JSON.stringify(out));",
        "})();",
    ])


def main():
    ui = UI.read_text(encoding="utf-8")

    chk("初始文案未误删", 'id="tStat">连接中…<' in ui)
    chk("statInit 已声明", re.search(r"busy=false,\s*statInit=true", ui) is not None)
    chk("守卫存在且唯一", ui.count("if(statInit)") == 1, str(ui.count("if(statInit)")))
    blk = ui.split("async function pollStats(){", 1)[1].split("async function pollInbox", 1)[0]
    chk("守卫在 d.error 提前 return 之后", -1 < blk.find("if(d.error)") < blk.find("if(statInit)"))

    try:
        served = urllib.request.urlopen(BASE + "/", timeout=10).read().decode("utf-8", "replace")
        chk("服务端含守卫", "if(statInit){statInit=false; tStat.textContent='已连接';}" in served)
        chk("服务端 == 磁盘", served.strip() == ui.strip())
    except Exception as e:
        # ★ 2026-09-19 修 (误报): 引擎没跑时这是"没得可验", 不是"验失败"。原来记 FAIL,
        #   会让整个门禁因为"引擎没起来"变红 —— 误报会掩盖真缺陷 (实测: 清理数据时停了
        #   引擎, 门禁就 860 PASS/1 FAIL)。改成 SKIP: 不静默 (打印醒目 SKIP 行),
        #   引擎一旦在跑立刻恢复严格断言 (能过也会叫)。
        skip("活服务断言 (服务端含守卫 / 服务端==磁盘)", f"引擎未跑: {str(e)[:60]}")

    fd, npath = tempfile.mkstemp(suffix=".mjs")
    os.close(fd)
    Path(npath).write_text(build_harness(ui), encoding="utf-8")
    try:
        r = subprocess.run([NODE, npath], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        chk("node harness 可跑", False, f"{e} | {r.stdout[-120:] if 'r' in dir() else ''}")
        d = None
    finally:
        Path(npath).unlink(missing_ok=True)

    if d:
        chk("A 首轮 连接中… → 已连接", d["a_after"] == "已连接", d["a_after"])
        chk("A 标志已消耗", d["a_flag"] is False, str(d["a_flag"]))
        chk("B 一次性: 用户态'完成'不被覆盖", d["b_after"] == "完成", d["b_after"])
        chk("C 反向对照: 重置标志后确实改写", d["c_after"] == "已连接", d["c_after"])
        chk("D 报错路径不谎报已连接", d["d_after"] == "连接中…", d["d_after"])
        chk("D 报错时标志不被消耗", d["d_flag"] is True, str(d["d_flag"]))
        chk("E 恢复后正常显示'已连接'", d["e_after"] == "已连接", d["e_after"])
        chk("E 侧栏渲染统计", "运行" in d["e_stats"] and "请求" in d["e_stats"], d["e_stats"][:60])

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
