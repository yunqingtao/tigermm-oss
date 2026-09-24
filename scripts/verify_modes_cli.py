"""verify_modes_cli.py — 真机 CLI 三模式验证 (复用真实 core/cli.py 登录门)

做法: 喂脚本化输入 (input + msvcrt.getch 都被替换) 驱动真实 run_cli,
      走完整登录 → /mode → /ask → /plan → 取消 → /craft → /exit 链路。

铁律: 不改生产文件; mode.json 改前留底 finally 还原。
用法: python scripts/verify_modes_cli.py
"""
import asyncio
import builtins
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(r"str(Path(__file__).resolve().parents[1])")
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

# ── ★ 2026-09-21 探针隔离 (第三次污染真库后的加固) ──────────────────────────
# 本脚本会**真跑 pipeline**。不隔离的话, 测试对话会写进用户的真实 chat_sessions.db
# (实测踩过: 2026-09-21 01:52 跑本脚本 → 真库 +16 行, 全是 ask/plan_proposal/plan_exec)。
# 隔离变量集中在 scripts/verification/_probe_env.py; 拿不到就**醒目告警**, 不静默继续。
try:
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    from _probe_env import activate as _activate_probe_isolation
    _activate_probe_isolation()
    print("[隔离] 已启用探针沙箱 (不会污染真实数据)")
except Exception as _e:
    print(f"[★ 告警] 探针隔离未生效: {type(_e).__name__}: {_e}")
    print("         继续跑可能把测试对话写进真实库! 建议先修好 _probe_env 再跑。")
# ───────────────────────────────────────────────────────────────────────────


MODE_JSON = ROOT / "data" / "mode.json"
MODE_BAK = ROOT / "tmp" / "mode.json.cli_bak"
TMPD = ROOT / "tmp"
TMPD.mkdir(exist_ok=True)
TARGET = TMPD / "cli_mode_test.txt"
TARGET2 = TMPD / "cli_mode_exec.txt"
TARGET2_DESK = Path.home() / "Desktop" / "cli_mode_exec.txt"

USER = "TMM"
# ★ 2026-09-20 分发整改: 登录口令**不再硬编码** (原来写在这里, 而本文件会被复制进分发仓库)。
#   从环境变量或 keys.json["cli"] 读; 读不到就留空 —— 此时 CLI 会跳过登录, 脚本照样能跑。
def _cli_pwd() -> str:
    import json as _json_vm
    import os as _os_vm
    p = _os_vm.environ.get("TMM_CLI_PASSWORD", "")
    if p:
        return p
    try:
        from config.settings import PROJECT_ROOT as _PR
        with open(_PR / "keys.json", encoding="utf-8") as f:
            return str((_json_vm.load(f).get("cli") or {}).get("password", "") or "")
    except Exception:
        return ""


PWD = _cli_pwd()

_TARGET_MSG = ("在 " + str(TMPD) + " 新建 cli_mode_test.txt 写入 hi")
_TARGET_MSG2 = ("在 " + str(TMPD) + " 新建 cli_mode_exec.txt 写入 hi")
SCRIPT = [
    USER,                 # 用户名 (input)
    # 密码走 msvcrt.getch
    "/mode",
    "/ask",
    "用一句话说明什么是二分查找",
    "/plan",
    _TARGET_MSG,
    "取消",
    "/plan",
    _TARGET_MSG2,
    "",                    # 空回车 = 执行该方案
    "/craft",
    "/exit",
]

_answers = list(SCRIPT)
_pwd_q = list(PWD) + ["\r"]
LOG = []


def fake_input(prompt=""):
    if not _answers:
        raise EOFError
    v = _answers.pop(0)
    # 真实 input() 会回显 prompt → harness 也要写出来, 否则断言看不到提示符
    try:
        sys.stdout.write(f"{prompt}{v}\n")
        sys.stdout.flush()
    except Exception:
        pass
    return v


def fake_getch():
    if not _pwd_q:
        return b"\r"
    return _pwd_q.pop(0).encode()


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def write(self, s):
        try:
            self.f.write(s)
        except Exception:
            pass

    def flush(self):
        pass


def load_config():
    cfg = {}
    for kf in [ROOT / "data" / "keys.json", ROOT / "keys.json"]:
        if kf.exists():
            try:
                d = json.loads(kf.read_text(encoding="utf-8"))
                for k, v in d.items():
                    if isinstance(v, dict) and ("key" in v or "api_key" in v):
                        cfg[k] = v
            except Exception:
                pass
    return cfg


if MODE_JSON.exists():
    shutil.copy2(MODE_JSON, MODE_BAK)
TARGET.unlink(missing_ok=True)
TARGET2.unlink(missing_ok=True)
TARGET2_DESK.unlink(missing_ok=True)

try:
    import msvcrt
    msvcrt.getch = fake_getch          # 跳过真实键盘
    builtins.input = fake_input        # 脚本化 stdin

    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.cli import run_cli

    cfg = load_config()
    mc = ModelClient(cfg)
    pipeline = Level4Pipeline(mc, cfg)

    tee = Tee(ROOT / "tmp" / "cli_verify_output.txt")
    real_out = sys.stdout.write

    def _w(s):
        try:
            real_out(s)
        except Exception:
            pass
        tee.write(s)
    sys.stdout.write = _w

    t0 = time.time()
    try:
        asyncio.run(run_cli(pipeline, mc))
    except SystemExit:
        pass
    except Exception as e:
        print(f"[harness] run_cli 抛出: {type(e).__name__}: {e}")
    elapsed = time.time() - t0
    try:
        tee.f.flush()          # 断言前必须落盘
        tee.f.close()
    except Exception:
        pass

    # ── 断言 (读回放日志) ──
    body = (ROOT / "tmp" / "cli_verify_output.txt").read_text(encoding="utf-8", errors="replace")
    PASS, FAIL = [], []

    def chk(n, c, extra=""):
        (PASS if c else FAIL).append(n)
        print(("  PASS " if c else "  FAIL ") + n + (f"   <- {extra}" if extra else ""))

    print("\n" + "=" * 62)
    print(f"CLI 真机回放 ({elapsed:.0f}s)")
    print("=" * 62)
    chk("登录通过 (出现 Tiger 分隔)", "Tiger" in body)
    chk("提示符带模式标记 [实干] 或 [问答] 或 [规划]",
        any(k in body for k in ("[实干]", "[问答]", "[规划]")), body[-400:])
    chk("/mode 列出三模式面板", "运行模式" in body and "问答" in body and "规划" in body and "实干" in body)
    chk("/ask 切换生效", "已切到 问答" in body)
    chk("/plan 切换生效", "已切到 规划" in body)
    chk("/craft 切换生效", "已切到 实干" in body)
    chk("ask 模式回答了问题", "二分查找" in body)
    chk("plan 模式出方案 + 确认提示", ("确认后开始执行" in body) or ("执行该方案" in body))
    chk("取消路径生效", ("已取消" in body) or ("方案作废" in body))
    chk("取消后未执行文件操作", not TARGET.exists(), str(TARGET))
    _landed = TARGET2 if TARGET2.exists() else (TARGET2_DESK if TARGET2_DESK.exists() else None)
    chk("plan 确认后真执行 (文件落地)", _landed is not None,
        f"TMPD={TARGET2.exists()} 桌面={TARGET2_DESK.exists()}")
    if _landed:
        _c = _landed.read_text(encoding="utf-8", errors="replace")
        chk("plan 执行内容正确", "hi" in _c, _c[:60])
        print(f"  落地位置: {_landed}")

finally:
    if MODE_BAK.exists():
        shutil.copy2(MODE_BAK, MODE_JSON)
        MODE_BAK.unlink()
        print("[cleanup] data/mode.json 已还原")
    TARGET.unlink(missing_ok=True)
    TARGET2.unlink(missing_ok=True)
    TARGET2_DESK.unlink(missing_ok=True)
    print("[cleanup] 测试文件已删除")

sys.exit(1 if FAIL else 0)
