"""verify_modes_e2e.py — 三模式真实端到端验证 (HTTP 打真实服务)

铁律: 不改生产文件; data/mode.json 改前留底, finally 还原。
用法: python scripts/verify_modes_e2e.py [base_url]
"""
import json, shutil, sys, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(r"str(Path(__file__).resolve().parents[1])")

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

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"
TMPD = ROOT / "tmp"
TMPD.mkdir(exist_ok=True)

MODE_JSON = ROOT / "data" / "mode.json"
MODE_BAK = ROOT / "tmp" / "mode.json.e2e_bak"

F_ASK = TMPD / "e2e_ask_must_not_exist.txt"
F_PLAN = TMPD / "e2e_plan_target.txt"
F_CRAFT = TMPD / "e2e_craft_target.txt"
# file_ops 既有行为: 裸文件名默认落桌面 → 两个位置都算落地
DESK = Path.home() / "Desktop"
F_PLAN2 = DESK / "e2e_plan_target.txt"
F_CRAFT2 = DESK / "e2e_craft_target.txt"
F_ASK2 = DESK / "e2e_ask_must_not_exist.txt"


def landed(primary, secondary):
    return primary if primary.exists() else (secondary if secondary.exists() else None)

PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {extra}" if extra else ""))
    return cond


def http(path, payload=None, timeout=200, method=None):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def chat(msg, mode, model="auto", timeout=220):
    """发一条消息, 返回 (文本, done_payload)。probe=True → 服务不写用户历史。"""
    body = http("/chat", {"message": msg, "model": model, "mode": mode, "probe": True},
                timeout=timeout)
    text, done = "", {}
    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        try:
            p = json.loads(line[5:].strip())
        except Exception:
            continue
        if p.get("text"):
            text += p["text"]
        if p.get("error") and not p.get("done"):
            done["error"] = p["error"]
        if p.get("done"):
            done = p
            if p.get("text"):
                text = p["text"]
    return text, done


def cleanup():
    for f in (F_ASK, F_PLAN, F_CRAFT, F_ASK2, F_PLAN2, F_CRAFT2):
        try:
            f.unlink()
        except Exception:
            pass


# ── 留底 ──
if MODE_JSON.exists():
    shutil.copy2(MODE_JSON, MODE_BAK)
cleanup()

# ★ 2026-09-21 加: 服务不在跑 → **干净跳过**。
#   原来直接打 http() → 抛 URLError traceback, 看不出"只是没启引擎", 容易被当成脚本坏了。
#   本脚本会真的切 /mode (ask↔craft), 所以也必须先确认有服务再动。
try:
    urllib.request.urlopen(BASE + "/stats", timeout=3).read()
except Exception as _e:
    print(f"[跳过] 端点 {BASE} 不可达 ({type(_e).__name__}) —— 本脚本需要引擎在跑")
    print("       启动引擎: python web_server.py   (或桌面快捷方式), 然后重跑本脚本")
    try:
        cleanup()
    except Exception:
        pass
    sys.exit(2)

try:
    print("=" * 66)
    print("A. 服务与模式接口")
    print("=" * 66)
    st = json.loads(http("/stats"))
    chk("服务存活 /stats", "uptime" in st, str(st)[:80])
    md = json.loads(http("/modes"))
    chk("/modes 返回三模式", set(md.get("modes", {})) == {"ask", "plan", "craft"})

    r = json.loads(http("/mode", {"mode": "godmode"}))
    chk("/mode 拒绝非法模式", r.get("ok") is False and "ask / plan / craft" in r.get("error", ""), str(r)[:80])

    r = json.loads(http("/mode", {"mode": "ask"}))
    chk("/mode 切到 ask", r.get("ok") and r.get("current") == "ask")
    r = json.loads(http("/mode", {"mode": "craft"}))
    chk("/mode 切到 craft", r.get("ok") and r.get("current") == "craft")

    print()
    print("=" * 66)
    print("B. ASK 模式 — 只回答, 绝不动手")
    print("=" * 66)
    t0 = time.time()
    txt, done = chat("用一句话说明什么是二分查找。"
                     "另外帮我在 " + str(TMPD) + " 新建文件 e2e_ask_must_not_exist.txt 写入 ok",
                     mode="ask")
    chk("ask 返回文本", bool(txt.strip()), txt[:80])
    chk("ask 模式标记", done.get("mode") == "ask", str(done.get("mode")))
    chk("ask 无工具轨迹", not done.get("trace"), str(done.get("trace"))[:100])
    _leak = [str(x) for x in (F_ASK, F_ASK2) if x.exists()]
    chk("ask 未创建文件 (未动手)", not _leak, "文件被创建了! " + str(_leak))
    print(f"  [{time.time()-t0:.1f}s] 回复: {txt.strip()[:150]}")

    print()
    print("=" * 66)
    print("C. PLAN 模式 — 先出方案, 不执行")
    print("=" * 66)
    t0 = time.time()
    txt, done = chat("在 " + str(TMPD) + " 新建文件 e2e_plan_target.txt，写入内容 hello", mode="plan")
    chk("plan 标记", done.get("mode") == "plan", str(done.get("mode")))
    chk("plan 要求确认 (pending_confirm)", done.get("pending_confirm") is True, str(done)[:120])
    chk("plan 返回文本方案", bool(txt.strip()), txt[:80])
    _leak = [str(x) for x in (F_PLAN, F_PLAN2) if x.exists()]
    chk("plan 阶段未执行 (文件不存在)", not _leak, "方案阶段就动手了! " + str(_leak))
    pend = json.loads(http("/modes"))
    chk("/modes 反映 pending", pend.get("pending") is True, str(pend.get("pending")))
    print(f"  [{time.time()-t0:.1f}s] 方案: {txt.strip()[:220]}")

    print()
    print("=" * 66)
    print("D. PLAN 确认 → 执行")
    print("=" * 66)
    t0 = time.time()
    txt, done = chat("执行", mode="plan")
    chk("确认后返回结果", bool(txt.strip()), txt[:80])
    chk("确认后 pending 清空", json.loads(http("/modes")).get("pending") is False)
    print(f"  [{time.time()-t0:.1f}s] 执行: {txt.strip()[:200]}")
    _pf = landed(F_PLAN, F_PLAN2)
    chk("plan 确认后文件落地", _pf is not None, "执行完仍没有文件")
    print(f"  落地位置: {_pf}")

    print()
    print("=" * 66)
    print("E. PLAN 取消 → 不动手")
    print("=" * 66)
    for _f in (F_PLAN, F_PLAN2):
        _f.unlink(missing_ok=True)
    chat("在 " + str(TMPD) + " 新建文件 e2e_plan_target.txt，写入内容 hello", mode="plan")
    txt, done = chat("取消", mode="plan")
    chk("取消后 pending 清空", json.loads(http("/modes")).get("pending") is False)
    _leak = [str(x) for x in (F_PLAN, F_PLAN2) if x.exists()]
    chk("取消后未执行", not _leak, "取消后仍执行了! " + str(_leak))
    print(f"  取消回复: {txt.strip()[:120]}")

    print()
    print("=" * 66)
    print("F. CRAFT 模式 — 直接动手 (回归)")
    print("=" * 66)
    t0 = time.time()
    txt, done = chat("在 " + str(TMPD) + " 新建文件 e2e_craft_target.txt，写入内容 hello", mode="craft")
    chk("craft 标记", done.get("mode") in ("craft", None), str(done.get("mode")))
    chk("craft 未要求确认", not done.get("pending_confirm"), str(done.get("pending_confirm")))
    _cf = landed(F_CRAFT, F_CRAFT2)
    chk("craft 直接执行 (文件落地)", _cf is not None, "文件没落地; 回复=" + txt[:160])
    if _cf:
        _ct = _cf.read_text(encoding="utf-8", errors="replace")
        chk("craft 文件内容正确", "hello" in _ct, _ct[:60])
        print(f"  落地位置: {_cf}")
    print(f"  [{time.time()-t0:.1f}s] 回复: {txt.strip()[:160]}")

    print()
    print("=" * 66)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    print("=" * 66)

finally:
    # ── 还原 mode.json + 清测试文件 ──
    if MODE_BAK.exists():
        shutil.copy2(MODE_BAK, MODE_JSON)
        MODE_BAK.unlink()
        print("[cleanup] data/mode.json 已还原")
    cleanup()
    print("[cleanup] 测试文件已删除")

sys.exit(1 if FAIL else 0)
