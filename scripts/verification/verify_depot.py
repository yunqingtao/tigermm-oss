"""对外检索与安全评估 (core/depot.py + /depot 接线) — 常驻验证器

为什么要有这一份: 它管的是「从外面拿东西进这台机器」—— 全项目最高风险的一条路。
用户的要求很明确: **先测安全, 再操作; 安全且适用才做成自己的**。
所以这一份守的不是"功能有没有", 而是**闸门有没有牙齿**:

  A 阻断项 —— .pth (解释器启动即执行) / setup.py 混入 / 没许可证 → 一律 reject
  B ★★ 静态扫描**绝不执行**候选代码 (canary 文件必须不存在)
  C ★ 可达性分档 —— 入口路径风险 → reviewable (人可放行) 但**不等于 safe**
  D ★★ 真跑接线 —— /depot help|installed 离线跑; assess 联网 (不通则 SKIP 不误报)
  E ★★ adopt 闸门 —— 非 safe 且未核对 → **绝不安装** (真跑负例, 零副作用)
  F 可追溯 —— 已装清单 append 事件 + 记核对结论; 真清单不被验证器改动
  G ★ 变异 —— 拆 .pth 阻断 / 拆"缺证据不给 safe" / 拆 reviewable 限制 → 必红
  H 零副作用 —— 真 depot_installed.json / 对话库指纹不变 · 下载沙箱无残留
"""
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F, S = [], [], []
REAL_INSTALLED = ROOT / "data" / "depot_installed.json"


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def skip(name, why=""):
    S.append(name)
    print(f"  SKIP {name}" + (f"   ({why})" if why else ""))


def _net_ok(host="pypi.org", port=443, t=4) -> bool:
    try:
        socket.create_connection((host, port), timeout=t).close()
        return True
    except Exception:
        return False


def _fp_file(p: Path):
    try:
        return ((p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest()[:12])
                if p.is_file() else None)
    except Exception:
        return None


def build(sandbox: Path):
    import core.mcp_client as _mc
    _mc.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
        load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [],
        tools=[], get_tool=lambda *a, **k: None)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.knowledge import KnowledgeEngine
    from config.settings import DATA_DIR
    import core.gap_ledger as _gl
    from core.gap_ledger import GapLedger
    from storage.session_store import SessionStore

    cfg = _load_config()
    pl = Level4Pipeline(ModelClient(cfg), cfg)
    try:
        pl._ke = KnowledgeEngine(DATA_DIR)
    except Exception:
        pass
    sb = GapLedger(db_path=sandbox / "gaps.db", data_dir=sandbox)
    _gl._ledger = sb
    pl.gap_ledger = sb
    pl.sessions = SessionStore(sandbox / "sessions.db")     # ★ 真跑 process 必须沙箱
    pl._depot_dir = lambda: (sandbox / "dl")                # 下载落沙箱
    return pl


def make_wheel(tmp: Path, files: dict, name="pkg.whl") -> Path:
    p = tmp / name
    with zipfile.ZipFile(p, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return p


def main() -> int:
    SB = Path(tempfile.mkdtemp(prefix="_vfy_depot_"))
    inst_before = _fp_file(REAL_INSTALLED)
    chat = ROOT / "data" / "chat_sessions.db"
    chat_before = None
    try:
        c = sqlite3.connect(f"file:{chat}?mode=ro", uri=True)
        chat_before = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        c.close()
    except Exception:
        pass
    try:
        from core import depot as DP
        from core.depot import BLOCKERS, risk_reach, scan_wheel, verdict
        pl = build(SB)
        net = _net_ok()

        EV_OK = {"name": "x", "license": "MIT", "requires_python": ">=3.9", "scanned": True,
                 "latest_upload": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                 "risks": [], "blockers": []}

        # ── A 阻断项 ──
        print("        —— A 阻断项 (一票否决) ——")
        chk("★ .pth 在阻断清单里", "pth_file" in BLOCKERS)
        chk("★ 带 .pth → reject", verdict({**EV_OK, "blockers": ["pth_file"]})["level"] == "reject")
        chk("wheel 带 setup.py → reject",
            verdict({**EV_OK, "blockers": ["setup_py_in_wheel"]})["level"] == "reject")
        v = verdict({**EV_OK, "license": ""})
        chk("★ 没许可证 → reject", v["level"] == "reject" and "no_license" in v["blockers"], v["level"])
        chk("yanked 版本 → reject", verdict({**EV_OK, "yanked": True})["level"] == "reject")
        w = make_wheel(SB, {"pkg/__init__.py": "x=1\n", "evil.pth": "import os\n"}, "a1.whl")
        chk("★ 扫描器能发现 .pth", "pth_file" in scan_wheel(w)["blockers"])
        w2 = make_wheel(SB, {"pkg/__init__.py": "x=1\n", "setup.py": "setup()\n"}, "a2.whl")
        chk("扫描器能发现 setup.py", "setup_py_in_wheel" in scan_wheel(w2)["blockers"])

        # ── B ★★ 绝不执行 ──
        print("        —— B ★★ 静态扫描绝不执行代码 ——")
        canary = SB / "CANARY.txt"
        w3 = make_wheel(SB, {"pkg/__init__.py": "open(r'%s','w').write('x')\n" % canary}, "b1.whl")
        s3 = scan_wheel(w3)
        chk("★ canary 未被创建 (没执行候选代码)", not canary.exists(), "扫描执行了代码!")
        chk("扫描确实读到内容 (不是空转)", s3["ok"] and s3["py_files"] == 1, str(s3)[:120])
        for label, src, kind in [
            ("exec+base64", "import base64\nexec(base64.b64decode('cHJpbnQ='))\n", "base64_exec"),
            ("子进程", "import subprocess\nsubprocess.run(['whoami'])\n", "subprocess"),
            ("凭据路径", "p='~/.ssh/id_rsa'\n", "credential_paths"),
            ("注册表", "import winreg\n", "winreg"),
            ("网络", "import requests\nrequests.post('http://x')\n", "network"),
        ]:
            wq = make_wheel(SB, {"pkg/a.py": src}, "bq.whl")
            kinds = [r["kind"] for r in scan_wheel(wq)["risks"]]
            chk(f"扫出 {label}", kind in kinds, str(kinds))

        # ── C ★ 可达性 ──
        print("        —— C ★ 风险点可达性 (实测驱动) ——")
        r = risk_reach([{"kind": "subprocess", "files": ["ddgs/cli.py"], "count": 1}])
        chk("cli.py → 入口路径", bool(r["entry_only"]) and not r["lib"], str(r))
        r = risk_reach([{"kind": "network", "files": ["pkg/http_client.py"], "count": 1}])
        chk("库路径 → lib", bool(r["lib"]) and not r["entry_only"], str(r))
        r = risk_reach([{"kind": "x", "files": ["pkg/core.py", "pkg/cli.py"], "count": 2}])
        chk("混合能拆开", r["lib"][0]["files"] == ["pkg/core.py"]
            and r["entry_only"][0]["files"] == ["pkg/cli.py"], str(r))
        v_entry = verdict({**EV_OK, "risks": [{"kind": "subprocess", "files": ["x/cli.py"], "count": 1}]})
        chk("★ 入口路径风险 → caution + reviewable (不自动 safe)",
            v_entry["level"] == "caution" and v_entry["reviewable"] is True, str(v_entry))
        v_lib = verdict({**EV_OK, "risks": [{"kind": "network", "files": ["x/http_client.py"], "count": 1}]})
        chk("★ 库路径风险 → 不可 reviewable", v_lib["reviewable"] is False, str(v_lib))
        chk("★ 缺证据(没扫内容) → 不给 safe",
            verdict({**EV_OK, "scanned": False})["level"] != "safe")

        # ── D 真跑接线 ──
        print("        —— D 真跑 /depot ——")
        o = asyncio.run(pl.process("/depot", mode_override="craft"))
        chk("★ /depot 被 cmd.depot 接手", o.get("route") == "cmd.depot", str(o.get("route"))[:50])
        body = o.get("response") or ""
        chk("help 列出 search/assess/adopt/installed",
            all(x in body for x in ("search", "assess", "adopt", "installed")), body[:120])
        chk("help 声明只统计不执行", ("不执行" in body) or ("只统计" in body))
        o2 = asyncio.run(pl.process("/depot installed", mode_override="craft"))
        chk("installed 可读 (真清单有 ddgs)", "ddgs" in (o2.get("response") or ""),
            (o2.get("response") or "")[:100])
        chk("用法提示 (不乱执行)",
            "用法" in (asyncio.run(pl.process("/depot 乱写", mode_override="craft")).get("response") or ""))
        pr = asyncio.run(pl.process("/depot installed", probe=True, mode_override="craft"))
        chk("★ probe 下 /depot 被拦 (不下载不安装)",
            pr.get("route") == "probe.skip", str(pr.get("route"))[:50])
        if net:
            txt = asyncio.run(pl.process("/depot assess ddgs", mode_override="craft")).get("response") or ""
            chk("★ 真联网 assess 有裁决", "评分" in txt and "许可证" in txt, txt[:120])
            chk("assess 标出入口路径风险 + 放行路径",
                ("入口路径" in txt) and ("reviewed" in txt), txt[:200])
        else:
            skip("真联网 assess ddgs", "网络不通")

        # ── E ★★ adopt 闸门 (真跑负例) ──
        print("        —— E ★★ adopt 闸门 (真跑负例, 零副作用) ——")
        t5 = asyncio.run(pl.process("/depot adopt zzz-not-a-real-pkg-tmm-9x",
                                    mode_override="craft")).get("response") or ""
        chk("★ 不存在的包 → 不采用", ("不予采用" in t5) or ("没有" in t5), t5[:100])
        if net:
            t6 = asyncio.run(pl.process("/depot adopt ddgs", mode_override="craft")).get("response") or ""
            chk("★★ caution 未核对 → 不予采用 (闸门有牙齿)", "不予采用" in t6, t6[:160])
            chk("★ 并提示放行方式 (reviewable)", "--reviewed" in t6, t6[:200])
        else:
            skip("caution 未核对的放行闸门", "网络不通")
        events0 = []
        if REAL_INSTALLED.is_file():
            events0 = json.loads(REAL_INSTALLED.read_text(encoding="utf-8")).get("ddgs", {}).get("events", [])
            chk("★ 负例没往真清单里写事件", len(events0) == 1, f"事件数 {len(events0)}")

        # ── F 可追溯 ──
        print("        —— F 可追溯 (已装清单) ——")
        chk("真清单记了核对结论", bool(events0 and events0[0].get("reviewed")), str(events0[:1])[:160])
        chk("真清单记了来源/许可证/版本",
            all(events0[0].get(k) for k in ("source", "license", "version")) if events0 else False)
        sb_log = SB / "log"
        DP.record_installed("t", {"kind": "dep", "verdict": "safe"}, data_dir=sb_log)
        DP.record_installed("t", {"kind": "dep", "verdict": "safe", "note": "第二次"}, data_dir=sb_log)
        chk("★ 同一项多次动作 append 不覆盖",
            len(DP.load_installed(sb_log)["t"]["events"]) == 2)

        # ── G ★ 变异 ──
        print("        —— G ★ 变异 (闸门有没有牙齿) ——")
        tgt = ROOT / "core" / "depot.py"
        orig = tgt.read_bytes()
        h0 = hashlib.md5(orig).hexdigest()
        nl = "\r\n" if b"\r\n" in orig else "\n"
        src = orig.decode("utf-8").replace("\r\n", "\n")

        def run_case(label, old, new, is_bad):
            if src.count(old) != 1:
                print(f"  (跳过 锚点{src.count(old)}: {label})")
                return
            tgt.write_bytes(src.replace(old, new, 1).replace("\n", nl).encode("utf-8"))
            importlib.reload(DP)
            bad = bool(is_bad(DP))
            tgt.write_bytes(orig)
            importlib.reload(DP)
            chk(f"★ 变异被抓住: {label}", bad)

        run_case("拆 .pth 阻断 (扫描不再标 blocker)",
                 '    if out["pth_files"]:\n        out["blockers"].append("pth_file")',
                 '    if False:\n        out["blockers"].append("pth_file")',
                 lambda d: "pth_file" not in d.scan_wheel(
                     make_wheel(SB, {"pkg/__init__.py": "x=1\n", "m.pth": "import os\n"}, "g1.whl"))["blockers"])
        run_case("拆『缺证据不给 safe』",
                 '    if not ev.get("scanned"):\n        score -= 1',
                 '    if False:\n        score -= 1',
                 lambda d: d.verdict({**EV_OK, "scanned": False})["level"] == "safe")
        run_case("入口路径风险也直接判 safe",
                 '    elif score >= 3:\n        level = "safe"',
                 '    elif score >= 1:\n        level = "safe"',
                 lambda d: d.verdict({**EV_OK, "risks": [{"kind": "subprocess",
                                                          "files": ["x/cli.py"], "count": 1}]})["level"] == "safe")
        chk("变异后已还原 (md5 一致)", hashlib.md5(tgt.read_bytes()).hexdigest() == h0)
        chk("还原后 .pth 仍被阻断",
            scan_wheel(make_wheel(SB, {"pkg/__init__.py": "x=1\n", "m.pth": "import os\n"}, "g2.whl"))["blockers"] == ["pth_file"])
        chk("还原后缺证据仍不给 safe",
            verdict({**EV_OK, "scanned": False})["level"] != "safe")

        # ── H 零副作用 ──
        print("        —— H 零副作用 ——")
        chk("★ 真已装清单未被验证器改动", _fp_file(REAL_INSTALLED) == inst_before,
            f"{inst_before} -> {_fp_file(REAL_INSTALLED)}")
        if chat_before is not None:
            try:
                c = sqlite3.connect(f"file:{chat}?mode=ro", uri=True)
                n = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                c.close()
                chk("★ 真对话库指纹未变", n == chat_before, f"{chat_before} -> {n}")
            except Exception:
                pass
        dl = SB / "dl"
        chk("下载沙箱无残留", (not any(dl.glob("*"))) if dl.is_dir() else True)
    finally:
        shutil.rmtree(SB, ignore_errors=True)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
