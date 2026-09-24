"""三模式验证总入口 — 一条命令跑完三层。

  1. 单元+接线   verify_modes_unit.py   (源码断言 + modes 行为)
  2. HTTP 端到端 verify_modes_e2e.py    (需 8800 在跑)
  3. CLI 真机回放 verify_modes_cli.py   (真登录门 + 三模式)

用法: python scripts/verify_modes.py
退出码: 全绿 0, 否则 1。

⚠ 状态中立: 三层各自都会读写 data/mode.json (活服务也在读同一个文件)。
  单独跑某一层时, 它只保证"自己前后"一致; 连锁跑时后一层会继承前一层的遗留值。
  所以总入口在开跑前快照 mode.json、无论成败都在 finally 还原 ——
  否则验证跑完会把活服务留在 ask/plan, 重启引擎后行为静默改变。
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
# CLI 层会写真 pipeline → chat_sessions.db; 与 mode.json 一样必须跑前快照、跑后还原
CHAT_DB = ROOT / "data" / "chat_sessions.db"
SUITE = [
    ("单元+接线", "verify_modes_unit.py", False),
    ("HTTP 端到端", "verify_modes_e2e.py", True),
    ("CLI 真机回放", "verify_modes_cli.py", False),
]


def _snap_file(path: Path, tag: str):
    if path.exists():
        bak = ROOT / "tmp" / f"{path.name}.{tag}_{int(time.time())}"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bak)
        return bak
    return None


def _restore_file(bak, path: Path, label: str):
    if bak and bak.exists():
        shutil.copy2(bak, path)
        bak.unlink()
        print(f"[state] {label} 已还原")


def _snapshot():
    if MODE_JSON.exists():
        bak = ROOT / "tmp" / f"mode.json.pregate_{int(time.time())}"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MODE_JSON, bak)
        try:
            return json.loads(MODE_JSON.read_text(encoding="utf-8")).get("mode"), bak
        except Exception:
            return None, bak
    return None, None


def _restore(bak, was):
    if bak and bak.exists():
        shutil.copy2(bak, MODE_JSON)
        bak.unlink()
        print(f"[state] data/mode.json 已还原 (跑前 = {was})")
    elif was is None and MODE_JSON.exists():
        MODE_JSON.unlink()          # 跑前就没有 → 别留下一个
        print("[state] data/mode.json 已移除 (跑前不存在)")


def main() -> int:
    was, bak = _snapshot()
    db_bak = _snap_file(CHAT_DB, "pregate")
    print(f"[state] 跑前 mode = {was} (快照 → {bak.name if bak else '无'})")
    print(f"[state] chat_sessions.db 快照 → {db_bak.name if db_bak else '无'}")
    bad = []
    try:
        for label, script, needs_server in SUITE:
            p = ROOT / "scripts" / script
            print(f"\n{'=' * 68}\n{label}  ({script})\n{'=' * 68}")
            try:
                r = subprocess.run([sys.executable, "-B", str(p)], cwd=str(ROOT),
                                   timeout=580, text=True, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                print(f"  TIMEOUT {script}")
                bad.append(script)
                continue
            if needs_server and r.returncode != 0:
                print("  (HTTP 端到端需要 web_server.py 在 8800 上跑: python start_web_ui.py)")
            if r.returncode != 0:
                bad.append(script)
    finally:
        _restore(bak, was)
        _restore_file(db_bak, CHAT_DB, "chat_sessions.db")

    print(f"\n{'=' * 68}\n总入口: {len(SUITE) - len(bad)}/{len(SUITE)} 层通过")
    for s in bad:
        print("  失败:", s)
    print("=" * 68)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
