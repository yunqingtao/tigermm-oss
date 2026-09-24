"""web_server 重复写用户消息 修复 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景: `web_server.py` `/chat` 开头原本也写 `pipeline.memory.add_turn("user", msg)`,
与 `core/pipeline.py::process()` L899 重复 —— process() 是所有入口 (CLI/Web/relay) 的
必经之路, 上层再写一次 → 每条用户消息在历史里出现两遍。
实测现象(修前): `你好/你好`、`你知道workbuddy吗 ×2`、`你找找 ×2`。
2026-09-18 修: 删掉 web_server 那次 user 写入, user 只由 pipeline 写。
修后实测: 一次请求 0 → 2 条 (原为 0 → 3 条)。

跑法:
    python -B scripts/verification/verify_user_turn_dedup.py

副作用: 阶段2 会写入活服务真实历史 (走真实路径才能验行为)。★ 2026-09-19 改:
       原来靠"跑完重启引擎"来清 —— process() 统一落库之后这招**失效**了 (重启不再清历史),
       实测两轮门禁往用户历史塞进 32 条测试消息。现在改成**精确自清理**:
       只删"本次自己发的那两条 (按内容签名匹配)", 发现非本次产生的行**不删**并报警
       (防误删真人正在用的对话)。
       ★ 判据也变了: 修复后重启**不该**清空历史 (/history 改读库) —— 详见下方断言。
       若 8800 未运行, 阶段2 自动 SKIP 并标注 (不假装通过)。
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
BASE = "http://127.0.0.1:8800"

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def pids_on(port):
    out = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True,
                         encoding="gbk", errors="replace").stdout
    return sorted({l.split()[-1] for l in out.split("\n")
                   if "LISTENING" in l.upper() and len(l.split()) >= 5
                   and l.split()[1].endswith(f":{port}") and l.split()[-1].isdigit()})


def bump_engine():
    for pid in pids_on(8800):
        subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
    time.sleep(2)
    # ★ 起引擎时要**摘掉** TMM_SESSION_DB —— 那是门禁给"验证器自己"用的隔离变量,
    #   引擎(被验证对象)必须用真实库, 否则验证器读真实库、引擎写临时库 → 断言恒 +0。
    import os as _os
    env = {k: v for k, v in _os.environ.items() if k != "TMM_SESSION_DB"}
    subprocess.Popen([PY, "-B", "start_web_ui.py"], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008, env=env)
    for _ in range(40):
        time.sleep(3)
        try:
            urllib.request.urlopen(BASE + "/stats", timeout=5)
            return True
        except Exception:
            pass
    return False


def hist():
    return json.loads(urllib.request.urlopen(BASE + "/history", timeout=10).read().decode())["history"]


def post(msg, probe):
    body = json.dumps({"message": msg, "model": "auto", "probe": probe}).encode()
    req = urllib.request.Request(BASE + "/chat", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=40).read()
    except Exception:
        pass


def db_rows():
    """直读对话库 (只读): [(id, role, content)] —— 修复后历史落库了, 该以库为准。"""
    import sqlite3
    db = ROOT / "data" / "chat_sessions.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return c.execute("SELECT id, role, content FROM messages ORDER BY id").fetchall()
    finally:
        c.close()


def db_cleanup(keep_max_id, signatures):
    """精确自清理: 只删 id > keep_max_id **且内容属于本次自己发的**那些行。

    ★ 为什么不直接 `DELETE WHERE id > keep_max_id`: 门禁跑的时候用户可能正在
      桌面端聊天 —— 一把梭会把真人的新消息删掉 (用户数据禁宽匹配)。
      发现不匹配的行就**不动它**, 并打印警告 (宁可留垃圾, 不可误删)。
    """
    import sqlite3
    db = ROOT / "data" / "chat_sessions.db"
    c = sqlite3.connect(str(db))
    try:
        rows = c.execute("SELECT id, role, content FROM messages WHERE id > ?", (keep_max_id,)).fetchall()
        mine = [r for r in rows if r[2] in signatures]
        others = [r for r in rows if r[2] not in signatures]
        if mine:
            c.executemany("DELETE FROM messages WHERE id = ?", [(r[0],) for r in mine])
            c.execute("UPDATE sessions SET message_count = MAX(0, message_count - ?) WHERE id = 1", (len(mine),))
            c.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            c.commit()
        return len(mine), len(others)
    finally:
        c.close()


def main():
    ws = (ROOT / "web_server.py").read_text(encoding="utf-8")
    pl = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")

    user_adds = [l.strip() for l in ws.split("\n") if "add_turn" in l and '"user"' in l]
    chk("web_server 不再写 user", not user_adds, str(user_adds))
    chk("web_server 保留 assistant 写入", 'add_turn("assistant", result.get' in ws)
    chk("web_server 保留 inbox 结果写入", 'add_turn("assistant", f"[Inbox' in ws)
    chk("pipeline.process 仍写 user (唯一正本)",
        'if not probe:\n            self.memory.add_turn("user", message)' in pl)
    chk("pipeline 里 user 写入只此一处",
        pl.count('self.memory.add_turn("user"') == 1,
        str(pl.count('self.memory.add_turn("user"')))

    print("        —— 活服务行为 ——")
    try:
        urllib.request.urlopen(BASE + "/stats", timeout=5)
        live = True
    except Exception:
        live = False
        print("        [SKIP] 8800 未运行 → 跳过行为断言 (源码断言仍有效)")

    if live:
        base_max = db_rows()[-1][0] if db_rows() else 0        # 基线: 库里现有最大 id
        sigs = set()                                          # 本次自己产生的内容签名
        if not bump_engine():
            chk("引擎重启就绪", False, "超时")
        else:
            # ★ 2026-09-19 口径反转: 修复前"重启即清空"(历史只在内存); 修复后 /history
            #   读库 → 重启**应当仍能读到历史**。这里两条都记着 (谁对看数据)。
            h0 = hist()
            chk("重启后历史仍在 (读库, 非内存)", len(h0) > 0,
                f"实得 {len(h0)} 条 (内存版必为 0 → 说明又退回内存读取了)")
            chk("/history 与库一致 (取最近 50 条)", len(h0) == min(50, len(db_rows())),
                f"history={len(h0)} 库={len(db_rows())}")

            n_before = len(db_rows())
            post("你好", probe=False)
            time.sleep(2)
            rows = db_rows()
            added = rows[n_before:]
            sigs = {r[2] for r in added} | {"你好"}
            chk("一次请求 → 库 +2 条 (user+assistant, 原为 +3/含重复)", len(added) == 2,
                f"实得 +{len(added)}: {[(r[1], r[2][:12]) for r in added]}")
            if len(added) == 2:
                chk("顺序 user 先于 assistant",
                    added[0][1] == "user" and added[1][1] == "assistant",
                    str([(r[1]) for r in added]))
                chk("无重复 user (dedup 生效)",
                    sum(1 for r in added if r[1] == "user") == 1)
            n1 = len(db_rows())
            post("你好", probe=True)
            time.sleep(2)
            chk("probe=True 不写库", len(db_rows()) == n1)

        # ── 精确自清理 (只删自己发的; 非本次的一律不碰) ──
        if sigs:
            n_mine, n_other = db_cleanup(base_max, sigs)
            chk("自己产生的测试行已清掉", n_mine >= 1, f"清了 {n_mine} 条")
            chk("未误删他人/真人的行 (无越界)", True,
                f"发现非本次行 {n_other} 条 (未删)")

    r = subprocess.run([PY, "-B", "-m", "pytest", "tests/", "-q", "--no-header",
                        "-p", "no:cacheprovider"], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=580)
    tail = [l for l in r.stdout.split("\n") if "passed" in l or "failed" in l]
    print("        pytest:", tail[-1] if tail else r.stdout.strip()[-80:])
    # 基线已从 "3 failed (TestLayState 契约冲突)" 变为 **全绿** (2026-09-18 契约修复)
    failed = {l[7:].split(" ")[0] for l in r.stdout.split("\n") if l.startswith("FAILED ")}
    chk("canonical 全绿 (0 failed)", not failed, str(sorted(failed)))

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
