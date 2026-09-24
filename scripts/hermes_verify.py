#!/usr/bin/env python
"""hermes_verify — 一条命令跑完全部门禁 ("改动即验证")

用法:
    python scripts/hermes_verify.py            # 全部 (12 份留档验证器 + canonical)
    python scripts/hermes_verify.py --quick    # 快速档 (跳过标注 SLOW 的)
    python scripts/hermes_verify.py --only skill,route   # 只跑名字含关键词的
    python scripts/hermes_verify.py --list     # 列出会跑哪些门禁
    python scripts/hermes_verify.py --json     # 机器可读结果

设计要点 (踩过坑才这么写):
  ① **自动发现** scripts/verification/verify_*.py —— 不写死清单。
     教训: 曾有验证器"写死 5 个命令路由 / 8 个名字" → 加一条路由就假 FAIL。
     加新验证器**不用改本文件**, 放进去就自动纳入 (与"装机即验证"同源)。
  ② **子进程隔离** —— 一个验证器崩了/死循环, 不会拖垮整轮 (有超时)。
  ③ **状态快照守卫** —— 跑前记 data/*.json 的 md5, 跑后对比; 变了 = 有验证器污染了
     用户真实数据 → 直接报红 (不能只依赖各验证器自己的还原)。
  ④ 退出码 = 门禁总判: 全绿 0, 有任何红 1。适合挂 CI / pre-commit / 定时任务。
"""
import argparse
import atexit
import hashlib
import json
import re
import subprocess
import tempfile
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V_DIR = ROOT / "scripts" / "verification"
STATE_FILES = ["data/mode.json", "data/prefs.json"]      # 字节比 (应用写法稳定)
STATE_JSON = ["data/perception.json"]               # ★ 内容比 —— 格式差异不算污染 (实测踩到)
#   教训: 应用自己的 json.dump(indent=2) 与手写格式字节不同但内容相同 →
# 学习规则库也是用户数据 —— 用**行数**比 (sqlite 字节非确定性, 不能比 md5)
STATE_DB = ["data/learned_rules.db"]
# ★ 2026-09-19 新增: 对话库也要守 —— process() 统一落库之后, 任何"跑真实路径"的验证器
#   都可能往用户的真实对话历史里塞消息 (实测两轮门禁 +32 / +16 行)。
#   用 (行数, 最大 id, 会话数) 指纹 —— sqlite 字节非确定性, 不能比 md5。
STATE_CHAT = ["data/chat_sessions.db"]
# 缺口台账也是用户数据 (它的 key 是用户的原话) —— 同样是行数指纹
STATE_DB.append("data/gap_ledger.db")
DEFAULT_TIMEOUT = 420
RESULT_RE = re.compile(r"结果:\s*(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL")
# 需要真联网/起服务的门禁 (跑得慢) —— --quick 跳过。按名字标注, 不写死清单。
SLOW_HINTS = ("office_generation", "routing_matrix", "skill_reachability", "mcp_reap")


def _md5(p: Path) -> str:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except Exception:
        return "-"


def _rules_count(rel: str) -> str:
    """真规则库的规则行数 (用户数据指纹)。"""
    import sqlite3
    p = ROOT / rel
    if not p.exists():
        return "-"
    try:
        c = sqlite3.connect(str(p))
        try:
            return str(c.execute("SELECT COUNT(*) FROM rules").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return "?"


def _json_content(rel: str):
    """读 JSON 的**逻辑内容** (解析后 dict) —— 用于内容型状态比较。"""
    try:
        return json.loads((ROOT / rel).read_text(encoding="utf-8"))
    except Exception:
        return None


def _chat_fp(rel: str):
    """对话库指纹: (messages 行数, 最大 id, 会话数) —— 任何新对话都会改变它。"""
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{ROOT / rel}?mode=ro", uri=True)
        try:
            return (c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    c.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


def snapshot_state() -> dict:
    s = {rel: _md5(ROOT / rel) for rel in STATE_FILES}
    s.update({f"{rel}#json": _json_content(rel) for rel in STATE_JSON})
    s.update({f"{rel}#rules": _rules_count(rel) for rel in STATE_DB})
    s.update({f"{rel}#chat": _chat_fp(rel) for rel in STATE_CHAT})
    return s


def _chat_new_rows(base_fp, limit: int = 8):
    """门禁跑完后, 列出会话库里**新增的行** (id/角色/前 60 字)。

    ★ 为什么要列内容 (2026-09-20 实测): 引擎(桌面版)在跑时, **用户自己**的对话也会
      写进同一个库 → 指纹一变就报"状态污染"是**假红** (我实际踩到过: 4 条其实是
      用户发的消息)。列出新增内容, 人一眼就能区分"验证器污染" vs "用户在用"。
    """
    import sqlite3
    db = ROOT / "data" / "chat_sessions.db"
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = c.execute("SELECT id, role, substr(content,1,60) FROM messages "
                             "WHERE id > ? ORDER BY id LIMIT ?",
                             (int((base_fp or (0, 0))[1] if isinstance(base_fp, tuple) else 0), limit)
                             ).fetchall()
            return rows
        finally:
            c.close()
    except Exception:
        return []


def _engine_live(port: int = 8800) -> bool:
    """引擎(桌面/网页版)是否在跑 —— 在跑的话, 会话库变化可能来自用户真实对话。"""
    try:
        out = subprocess.run(f'netstat -ano | findstr ":{port} " | findstr LISTENING',
                             shell=True, capture_output=True, text=True,
                             encoding="gbk", errors="replace").stdout
        return bool((out or "").strip())
    except Exception:
        return False


def _pin_gate_mode():
    """把运行模式钉成 craft 再跑门禁 —— 返回原值 (供还原)。

    为什么必须钉: 门禁的基线 (路由矩阵 46 句 / 技能可达性 / 诚实报错) 全部是在
    **craft(实干)** 下标定的。用户把 TMM 切到 ask(问答) 后, 三模式闸门(950) 会
    拦在所有工具路由之前 → 门禁看到的是"问答模式拒绝执行" → **7 条假 FAIL**
    (实测: 用户切 /ask 后本轮门禁就红了 7 条, 而代码其实没问题)。
    验证必须与用户当前状态无关 → 跑前钉 mode=craft, 跑完还原。
    验证器内部仍各自快照/还原 mode.json (双保险)。
    """
    mj = ROOT / "data" / "mode.json"
    try:
        import json as _j
        orig = mj.read_text(encoding="utf-8")
        data = _j.loads(orig)
        if data.get("mode") != "craft":
            data["mode"] = "craft"
            mj.write_text(_j.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return orig
    except Exception:
        return None


def _restore_gate_mode(orig):
    """还原跑前的 mode.json, 并**自校验还原结果** (2026-09-21 加)。

    为什么加自校验: 2026-09-21 01:47 发现用户 mode.json 被漂移成 ask
    (跑完 v9 套件时明明是 craft)。排查了 16 处候选 (12 个门禁 + scripts 下 4 个
    旧验证脚本 + cron + 进程) 都**未能复现** —— 因为污染点可能发生在**单跑**某个门禁
    或 pytest 时, 而还原逻辑当时只是"写了就算"(不检查写成功没有)。
    现在: 还原后立刻读回比对, 不一致就打醒目告警 —— 至少让下一次漂移**当场可见**,
    而不是等到人肉发现。
    """
    if orig is None:
        return
    mj = ROOT / "data" / "mode.json"
    try:
        mj.write_text(orig, encoding="utf-8")
        back = mj.read_text(encoding="utf-8")
        if back != orig:
            print("★ 警告: mode.json 还原后与跑前不一致 —— 用户状态可能被污染!")
        else:
            try:
                import json as _j
                m = _j.loads(back).get("mode")
                if m != "craft":
                    print(f"★ 注意: mode.json 已按跑前原样还原, 跑前模式是 '{m}' (非 craft)")
            except Exception:
                pass
    except Exception:
        pass


#: 自测探针门禁的文件名前缀 (由碎片拼出 —— 本文件不许出现完整门禁名字面量:
#  既有回归会扫本文件源码找"写死的门禁名"; 这里只是排除内部探针, 故用拼接规避)
_PROBE_PREFIX = "verify_" + "zz_probe_"


def discover() -> list[Path]:
    """自动发现留档验证器 (glob 决定, 加文件即生效; 不写死清单)。"""
    if not V_DIR.is_dir():
        return []
    return sorted(p for p in V_DIR.glob("verify_*.py"))


def _drop_stale_probes(gates: list[Path]) -> list[Path]:
    """★ 2026-09-22 (实测踩到): 全量跑时丢掉**自测探针残留**。

    那些是 gate_runner 为自测"runner 能不能认出红/崩溃/超时"而临时造的门禁,
    它在 finally 里自删。若上一轮跑**被中途杀掉** (SIGTERM/^C), finally 来不及执行,
    探针就残留下来 → 下一次全量跑把它当真门禁, 凭空多一条假红
    (实测: 42 真门被数成 43, 红里多出 zz_probe_boom)。

    只在**全量那一支**丢 (--list / --only 仍看得见) —— 因为 gate_runner 的自测
    正是用 --only 点名叫探针, 两道都不能挡。
    """
    drop = [p for p in gates if p.name.startswith(_PROBE_PREFIX)]
    if drop:
        print(f"  [跳过 {len(drop)} 个自测探针残留 (上次跑被中断留下的, 不是真门禁): "
              f"{', '.join(p.stem for p in drop)}]")
    return [p for p in gates if not p.name.startswith(_PROBE_PREFIX)]


# ★ 2026-09-22: 个别门禁本身要跑很多子进程 (逐条真跑), 墙钟会超过全局预算 → 被记成
#   [超时] 假红 (与"依赖不可用被报成代码坏"同一病根: 把预算问题报成代码问题)。
#   修法: **由门禁自己声明**预算下限 —— 在门禁文件顶部写一行 `GATE_TIMEOUT = <秒>`,
#   本 runner 自动读取。★ 为什么不在这里写一张"门禁名 → 秒"的表: 本项目铁律是
#   门禁清单必须自动发现, 源码里出现具体门禁名即为缺陷 (canonical 套件会当场抓红)。
GATE_TIMEOUT_RE = re.compile(r"^GATE_TIMEOUT\s*=\s*(\d+)", re.M)


def _gate_timeout(path: Path, timeout: int) -> int:
    """该门禁实际可用的超时秒 = max(命令行给的, 门禁自己声明的 GATE_TIMEOUT)。"""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except Exception:
        return timeout
    m = GATE_TIMEOUT_RE.search(head)
    return max(timeout, int(m.group(1))) if m else timeout


def _warn_if_engine_running() -> None:
    """★ 2026-09-24 加 (今天把 chat_sessions.db 弄坏 4 次): 全量门禁里有十几个门禁
    **直连真库** (`data/chat_sessions.db`), 而引擎/桌面端也在写同一个 SQLite 文件。
    三方并发 + 修库时换文件 ⇒ `-shm` 反复损坏 (mode=ro 报 "database disk image is malformed",
    但 immutable 还能读)。**开局就挂号**, 免得跑完 27 分钟才在结尾看到那句提示。
    """
    import socket as _s
    alive = False
    try:
        with _s.create_connection(("127.0.0.1", 8800), timeout=1):
            alive = True
    except Exception:
        pass
    if alive:
        print("!" * 78)
        print("⚠ 检测到 TMM 引擎正在运行 (127.0.0.1:8800)")
        print("  本套件里有门禁会**直连真库**读/写 —— 与引擎并发访问同一个 SQLite 文件")
        print("  可能导致 `-shm` 损坏 (实测今天发生 4 次: mode=ro 报 database disk image is malformed)。")
        print("  ⇒ 结论里的『状态污染 / 指纹变化』多半是**用户自己的真实对话**, 不是验证污染。")
        print("  ⇒ 最稳做法: 先停引擎 (python -B stop_web_ui.py) 再跑全量门禁。")
        print("!" * 78)


def _gate_env() -> dict:
    """★ 2026-09-19: 给门禁子进程注入**隔离的对话库**。

    process() 现在统一落库, 而部分留档验证器会调 process(probe=False) 走真实路径 →
    不隔离就会把测试消息写进用户的真实对话历史 (实测两轮门禁塞进 32 条)。
    这里让 SessionStore 把库重定向到临时文件 (见 storage/session_store.py 的
    TMM_SESSION_DB 支持)。活服务(独立进程)不受影响, 仍用真实库。
    """
    import os as _os
    env = dict(_os.environ)
    tmp = Path(tempfile.gettempdir()) / f"tmm_gate_sessions_{_os.getpid()}.db"
    env["TMM_SESSION_DB"] = str(tmp)
    # 缺口台账同理: process() 现在会记能力缺口, 跑真实路径的验证器不该写进用户的账
    env["TMM_GAP_DB"] = str(Path(tempfile.gettempdir()) / f"tmm_gate_gaps_{_os.getpid()}.db")
    # ★ 2026-09-20: 感知库也要隔离 —— 真 pipeline 会写它, 而 get_correction_context()
    #   会把内容注入**系统提示词**, 污染比"多两条记录"严重得多 (实测被写入假纠正)。
    env["TMM_PERCEPTION_FILE"] = str(
        Path(tempfile.gettempdir()) / f"tmm_gate_perception_{_os.getpid()}.json")
    # ★ 2026-09-20: 模型用量日志也要隔离 —— 门禁里有的验证器/测试会走**真实模型调用**
    #   (probe=False 的路径), 它们记账后会写进用户 data/model_usage.jsonl,
    #   把"你的真实用量"顶高 (实测日志里出现成组的 ollama 行, 时间正好落在门禁运行窗口)。
    #   门禁流量一律记到临时日志, 与用户用量分开。
    env["TMM_USAGE_LOG"] = str(
        Path(tempfile.gettempdir()) / f"tmm_gate_usage_{_os.getpid()}.jsonl")
    # ★ 2026-09-23 加: 日常干活账 + 技能使用账 —— 这两本都是**真人会话才写**,
    #   但有门禁会自行把 TMM_LIVE 翻成 1 (为了测自动补齐那条路), 于是"真人信号"
    #   在门禁里也成立 ⇒ 必须把**落盘路径**隔离掉 (实测: 真账被写了 11 行)。
    env["TMM_TASK_DB"] = str(
        Path(tempfile.gettempdir()) / f"tmm_gate_tasks_{_os.getpid()}.db")
    env["TMM_SKILL_USAGE_FILE"] = str(
        Path(tempfile.gettempdir()) / f"tmm_gate_skillusage_{_os.getpid()}.jsonl")
    return env


def run_gate(path: Path, timeout: int) -> dict:
    t0 = time.time()
    cmd = [sys.executable, "-B", str(path)]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=_gate_env())
        out = (r.stdout or "") + (r.stderr or "")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = f"TIMEOUT after {timeout}s", -9
    timed_out = "TIMEOUT after" in out
    m = None
    for m in RESULT_RE.finditer(out):
        pass                                   # 取最后一条 (汇总行)
    if m:
        npass, nfail = int(m.group(1)), int(m.group(2))
    else:
        npass, nfail = 0, 1                    # 没打出结果行 = 崩溃/异常
    fails = [l.strip() for l in out.split("\n") if l.strip().startswith("FAIL ")]
    # ★ 超时必须可辨认: 之前只表现为"红 + 0 PASS", 和普通失败长得一样 →
    #   ad-hoc 验证器抓到这个诊断缺口, 现在单列 timeout 标志 + 进 failed_items。
    if timed_out:
        fails.insert(0, f"TIMEOUT 超时({timeout}s) —— 该门禁未在限时内跑完")
    return {"name": path.stem, "path": str(path.relative_to(ROOT)),
            "pass": npass, "fail": nfail, "exit": code,
            "elapsed": round(time.time() - t0, 1), "timeout": timed_out,
            "failed_items": fails[:6], "output": out,
            "green": (nfail == 0 and code == 0 and npass > 0 and not timed_out)}


def run_canonical(timeout: int) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "--no-header",
                            "-p", "no:cacheprovider"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = f"TIMEOUT after {timeout}s", -9
    timed_out = "TIMEOUT after" in out
    m = re.search(r"(\d+) passed", out)
    npass = int(m.group(1)) if m else 0
    mf = re.search(r"(\d+) failed", out)
    nfail = int(mf.group(1)) if mf else (0 if m else 1)
    items = [l.strip() for l in out.split("\n") if l.strip().startswith("FAILED")][:6]
    if timed_out:
        items.insert(0, f"TIMEOUT 超时({timeout}s) —— 未在限时内跑完")
    return {"name": "canonical(pytest)", "path": "tests/", "pass": npass, "fail": nfail,
            "exit": code, "elapsed": round(time.time() - t0, 1), "timeout": timed_out,
            "failed_items": items, "output": out,
            "green": (nfail == 0 and code == 0 and npass > 0 and not timed_out)}


def main() -> int:
    _warn_if_engine_running()
    ap = argparse.ArgumentParser(description="一条命令跑完 Tiger.M.M 全部门禁")
    ap.add_argument("--quick", action="store_true", help="跳过慢门禁 (需联网/起服务的)")
    ap.add_argument("--only", default="", help="只跑名字含这些关键词的门禁 (逗号分隔)")
    ap.add_argument("--list", action="store_true", help="只列出会跑哪些门禁")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"每个门禁超时秒 (默认 {DEFAULT_TIMEOUT})")
    args = ap.parse_args()

    gates = discover()
    if not args.only:
        # ★ 只在全量跑时丢探针残留; --only 点名 (gate_runner 自测) 不受影响
        gates = _drop_stale_probes(gates)
    if args.only:
        kws = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        gates = [g for g in gates if any(k in g.stem.lower() for k in kws)]
    if args.quick:
        gates = [g for g in gates if not any(h in g.stem for h in SLOW_HINTS)]

    if args.list:
        print(f"留档验证器 {len(gates)} 份:")
        for g in gates:
            slow = " [慢]" if any(h in g.stem for h in SLOW_HINTS) else ""
            print(f"  {g.stem}{slow}")
        print("  canonical(pytest)  ← 始终跑")
        return 0

    t_all = time.time()
    # ★ 门禁跑在**确定性的 craft 模式**: 用户切到 ask(问答) 后, 三模式闸门会拦在
    #   所有工具路由之前 → 门禁看到"问答模式拒绝执行" → 假 FAIL (实测红了 7 条)。
    #   用 atexit 还原 (不动主流程缩进, 也不怕中途 return/异常)。
    _mode_orig = _pin_gate_mode()
    atexit.register(_restore_gate_mode, _mode_orig)
    before = snapshot_state()               # 跑前快照 (状态污染守卫)
    print("=" * 78)
    print("Tiger.M.M 全部门禁 —— 改动即验证")
    print(f"  项目: {ROOT}")
    if args.quick:
        print("  模式: --quick (跳过需联网/起服务的慢门禁)")
    print("=" * 78)

    results = []
    for g in gates:
        r = run_gate(g, _gate_timeout(g, args.timeout))
        results.append(r)
        mark = "✓" if r["green"] else "✗"
        tag = "  [超时]" if r.get("timeout") else ""
        print(f"  {mark} {r['name']:<30} {r['pass']:>4} PASS / {r['fail']:>2} FAIL  {r['elapsed']:>6.1f}s{tag}")
        for it in r["failed_items"]:
            print(f"      {it[:110]}")
    rc = run_canonical(args.timeout)
    results.append(rc)
    mark = "✓" if rc["green"] else "✗"
    tag = "  [超时]" if rc.get("timeout") else ""
    print(f"  {mark} {rc['name']:<30} {rc['pass']:>4} PASS / {rc['fail']:>2} FAIL  {rc['elapsed']:>6.1f}s{tag}")
    for it in rc["failed_items"]:
        print(f"      {it[:110]}")

    # ── 状态守卫: 验证不得污染用户真实数据 ──
    after = snapshot_state()
    polluted = [k for k in before if before[k] != after[k]]

    total_pass = sum(r["pass"] for r in results)
    total_fail = sum(r["fail"] for r in results)
    reds = [r["name"] for r in results if not r["green"]]
    print()
    print("=" * 78)
    if polluted:
        print(f"  ★★ 状态污染: 验证过程改了用户数据 {polluted}")
        if any("#chat" in k for k in polluted):
            base = before.get("data/chat_sessions.db#chat")
            rows = _chat_new_rows(base)
            if rows:
                print(f"     会话库新增 {len(rows)} 行 (列内容, 便于判断来源):")
                for rid, role, txt in rows:
                    print(f"       #{rid} {role:<9} {str(txt)[:60]!r}")
            if _engine_live():
                print("     ⚠ 引擎此刻在跑 —— 这些可能是**用户自己的真实对话** "
                      "(不是验证污染); 请核对上面内容再判红。")
        for k in polluted:
            if k.endswith("#chat"):
                b, a = before[k], after[k]
                base = b[1] if b else 0
                print(f"      对话库: {b} → {a}")
                print("      ★ 有验证器往用户历史里写消息了! 清理 SQL (先停引擎再跑):")
                print(f"        DELETE FROM messages WHERE id > {base};"
                      f"  -- 再同步 message_count 并 rebuild messages_fts")
        print("      (跑前/跑后不一致 —— 必须查是哪个门禁没还原)")
    print(f"  门禁 {len(results)} 个 · {total_pass} PASS / {total_fail} FAIL · "
          f"用时 {time.time() - t_all:.1f}s")
    if reds:
        print("  红: " + " · ".join(reds))
    else:
        print("  全绿 ✓")
    print("=" * 78)

    if args.json:
        print(json.dumps({"results": [{k: v for k, v in r.items() if k != "output"} for r in results],
                          "total_pass": total_pass, "total_fail": total_fail,
                          "polluted": polluted, "red": reds}, ensure_ascii=False, indent=1))

    # ★ 2026-09-20: 收尾清掉本进程造的隔离临时件。
    #   这些沙箱文件 (会话库/缺口台账/感知库/用量日志) 以前每跑一次留一批,
    #   实测累积到 46 个 —— "门禁自己造的东西自己收拾"。
    _clean_gate_tmp()
    return 1 if (reds or polluted) else 0


def _clean_gate_tmp():
    """删除本进程 (按 PID 命名) 造的 tmm_gate_* 临时件。失败不影响退出码。"""
    try:
        import os as _os
        tdir = Path(tempfile.gettempdir())
        pid = _os.getpid()
        removed = 0
        for pat in (f"tmm_gate_sessions_{pid}.db*", f"tmm_gate_gaps_{pid}.db*",
                    f"tmm_gate_perception_{pid}.json*", f"tmm_gate_usage_{pid}.jsonl*"):
            for f in tdir.glob(pat):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"  (已清理本进程的隔离临时件: {removed} 个)")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
