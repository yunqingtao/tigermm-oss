"""_lay_state 铺状态契约 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景: canonical 长期 `3 failed` —— 测试断言 `_lay_state` 静默, 生产实现却在打印
"虎哥理解"。**常驻红的基线会淹没真回归**; 且用户明确要求输出干净
(memory: 无"虎哥理解"/调试行) —— 测试是对的, 实现是错的。

契约 (2026-09-18 显式化):
    默认(未打开)  → (None, False)                  输出干净, 无"虎哥理解"噪声
    /state on     → (state_msg, needs_confirm)     打印理解摘要; 任务模式需确认
审计日志 tmm_output.log 不管开关都写。

改动:
  core/pipeline.py  `_lay_state_enabled()` 新开关(default False) + `_lay_state` 默认静默
                    + 抽出 `set_lay_state(arg)` 供 CLI 调用 (可测)
  core/cli.py       `/state | /state on | /state off`
  tests/            TestLayState 契约**双向**化 + test_cli_regression 加接线守卫

★ 为什么抽成方法: CLI REPL 有 getch 密码门, 管道和 PTY 都驱动不了
  (实测管道 420s 超时、PTY 卡在密码输入) → 把逻辑抽到 pipeline 才能真测。

跑法:
    python -B scripts/verification/verify_lay_state_contract.py

副作用: 阶段3 会真写 data/prefs.json 再**逐字节还原** (验证不得污染用户数据)。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
PREFS = ROOT / "data" / "prefs.json"

P, F = [], []
def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def md5f(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest() if Path(p).exists() else "<none>"


def run_probe(src, timeout=240):
    fd, tmp = tempfile.mkstemp(suffix=".py"); os.close(fd)
    Path(tmp).write_text(_fill(src), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        return json.loads(r.stdout.split("<<<J>>>")[1].strip().splitlines()[0])
    finally:
        Path(tmp).unlink(missing_ok=True)


BOOT = r'''
import sys, os, json
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
import logging; logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg)
'''


def main():
    orig_prefs = PREFS.read_bytes() if PREFS.exists() else None
    try:
        pl = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        cli = (ROOT / "core" / "cli.py").read_text(encoding="utf-8")
        mn = (ROOT / "main.py").read_text(encoding="utf-8")
        chk("_lay_state_enabled 存在", "def _lay_state_enabled" in pl)
        chk("默认关", 'self._prefs.get("lay_state", False)' in pl)
        chk("静默早退存在", "if not self._lay_state_enabled():" in pl)
        chk("审计日志仍写", 'log_line = f"[{ts}] 虎哥理解' in pl)
        chk("set_lay_state 存在", "def set_lay_state" in pl)
        chk("CLI /state 接线", "pipeline_obj.set_lay_state(" in cli)
        # 可发现性: 命令必须登记在帮助块里 (否则用户不知道有这东西)
        _hb = re.search(r"命令：(.*?)\"\"\"\)", cli, re.S)
        _block = _hb.group(1) if _hb else ""
        chk("/state 登记在帮助块内", bool(_block and "/state" in _block))
        # ★ 辨别力: 抽掉那一行必须判定为假 (证明断言绑真实内容, 不是恒真)
        chk("帮助断言有辨别力 (抽掉即假)",
            '/state' in _block
            and "/state" not in re.sub(r"^\s*\{GOLD\}/state.*$", "", _block, flags=re.M))
        chk("活跃入口 = core.cli", "from core.cli import run_cli" in mn)
        chk("无'虎哥理解'直接 print", 'print(f"{GOLD}[虎哥理解]' not in pl)

        print("        —— 单元 (内存, 不写盘) ——")
        d = run_probe(BOOT + r'''
pl._save_prefs = lambda: None
out = {}
orig = pl._prefs.get("lay_state", None)
pl._prefs.pop("lay_state", None)
out["default"] = list(pl._lay_state("你好"))
out["query_off"] = pl.set_lay_state("")
out["on_msg"] = pl.set_lay_state("on")
out["on_chat"] = list(pl._lay_state("你好"))
out["on_task"] = list(pl._lay_state("请帮我写一份非常详细的架构文档覆盖所有方面" * 8))
out["off_msg"] = pl.set_lay_state("off")
out["off_state"] = list(pl._lay_state("你好"))
if orig is None: pl._prefs.pop("lay_state", None)
else: pl._prefs["lay_state"] = orig
print("<<<J>>>" + json.dumps(out, ensure_ascii=False))
''')
        chk("默认 → [None, False]", d["default"] == [None, False], str(d["default"]))
        chk("查询(关) 报'当前: 关'", "当前: 关" in d["query_off"], d["query_off"])
        chk("'on' 报'已打开'", "已打开" in d["on_msg"], d["on_msg"])
        chk("打开+闲聊 → 摘要, 不需确认",
            d["on_chat"][0] is not None and "虎哥理解" in d["on_chat"][0]
            and d["on_chat"][1] is False, str(d["on_chat"])[:110])
        chk("打开+任务 → 需确认", d["on_task"][1] is True, str(d["on_task"])[:110])
        chk("'off' 报'已关闭'", "已关闭" in d["off_msg"], d["off_msg"])
        chk("关闭后回落静默", d["off_state"] == [None, False], str(d["off_state"]))

        print("        —— ★ 真实持久化往返 (写盘→读回→还原) ——")
        before = md5f(PREFS)
        run_probe(BOOT + 'pl.set_lay_state("on"); print("<<<J>>>" + json.dumps({}))')
        chk("'on' 真写进磁盘", json.loads(PREFS.read_text(encoding="utf-8")).get("lay_state") is True)
        chk("磁盘确实变了 (前提成立)", md5f(PREFS) != before)
        d3 = run_probe(BOOT + r'''
out = {"enabled": pl._lay_state_enabled(), "state": list(pl._lay_state("你好"))[:1],
       "msg": pl.set_lay_state("off")}
print("<<<J>>>" + json.dumps(out, ensure_ascii=False))
''')
        chk("★ 新进程读到开关为开", d3["enabled"] is True, str(d3))
        chk("★ 新进程里返回摘要 (持久化真实生效)",
            d3["state"] and d3["state"][0] is not None, str(d3))
        chk("'off' 写回磁盘", json.loads(PREFS.read_text(encoding="utf-8")).get("lay_state") is False)

        print("        —— canonical ——")
        rp = subprocess.run([PY, "-B", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
                            cwd=str(ROOT), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=580)
        tail = [l for l in rp.stdout.split("\n") if "passed" in l or "failed" in l]
        print(f"        {tail[-1] if tail else rp.stdout.strip()[-70:]}")
        chk("canonical 全绿 (0 failed)", rp.returncode == 0 and "failed" not in rp.stdout,
            rp.stdout.strip()[-150:])
    finally:
        if orig_prefs is not None:
            PREFS.write_bytes(orig_prefs)
            chk("prefs.json 逐字节还原", PREFS.read_bytes() == orig_prefs,
                md5f(PREFS) + " vs " + hashlib.md5(orig_prefs).hexdigest())

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
