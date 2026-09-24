"""数据面 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

为什么要有这一门 (2026-09-20, 借鉴通用 Agent 平台的 UI 面)
═══════════════════════════════════════════════════════════════════
架构图里 Chat / Task / Files / Skills / Experts / MCP / Permission 这些面对用户的价值
不是"有能力", 而是"能看见、能干预"。TMM 原来只有 9 条路由 (chat/mode/history/inbox/stats),
产物与专家的数据**根本露不出来**。本次补了三条**只读**数据面:
    GET /artifacts  —— 产物/交付清单 (可 ?scan=1 附带目录盘点)
    GET /experts    —— 角色专家清单 (可 ?name= 看某位会注入什么)
    GET /audit      —— 工具调用审计尾巴 + 汇总 (权限/任务可视化的数据)

本门守三件事:
  ① 三个接口**真能返回** (不是 404/500) 且结构对得上
  ② **只读**: 不写状态、不动用户文件、不触发任何工具
  ③ 边界诚实: 查不存在的专家 → 明说没有 (不返回别人的)

做法: 用 FastAPI TestClient 直接打应用 (不起端口, 不与用户正在跑的 8800 抢)。
自清理: 全程只读; 用 TMM_ARTIFACTS_FILE 隔离产物登记表。
"""
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F, W = [], [], []


def chk(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS " if c else "  FAIL ") + n + (("   <- " + str(d)) if d and not c else ""))


def warn(n, d=""):
    W.append(n)
    print(f"  WARN {n}" + (f"   <- {d}" if d else ""))


def fp_db(p):
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if not tabs:
        c.close()
        return ("err",)
    main = "messages" if "messages" in tabs else tabs[0]
    n = c.execute("SELECT COUNT(*) FROM " + main).fetchone()[0]
    mx = c.execute("SELECT MAX(rowid) FROM " + main).fetchone()[0]
    c.close()
    return (main, n, mx)


def main():
    print("=" * 78)
    print("数据面 — 产物 / 专家 / 审计 三条只读接口")
    print("=" * 78)
    iso = ROOT / "tmp" / f"_web_iso_{os.getpid()}"
    iso.mkdir(parents=True, exist_ok=True)
    os.environ["TMM_ARTIFACTS_FILE"] = str(iso / "reg.jsonl")

    db = ROOT / "data" / "chat_sessions.db"
    db_before = fp_db(db)

    # ── [0] 源码层: 三条路由都注册了 ──
    print("\n[0] 路由已注册")
    src = (ROOT / "web_server.py").read_text(encoding="utf-8", errors="replace")
    for r in ("/artifacts", "/experts", "/audit", "/dash"):
        chk(f"{r} 已定义", f'@app.get("{r}")' in src)
    chk("三条都是 GET 只读 (没有写操作)",
        not re.search(r'@app\.post\("/artifacts"|@app\.post\("/experts"|@app\.post\("/audit"', src))

    # ── [A] 真打接口 ──
    print("\n[A] 真打接口 (TestClient, 不占端口)")
    try:
        from fastapi.testclient import TestClient
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("web_probe", ROOT / "web_server.py")
        mod = _iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        client = TestClient(mod.app)
    except Exception as e:
        chk("可加载 web_server 并建 TestClient", False, f"{type(e).__name__}: {e}")
        return 1
    chk("可加载 web_server 并建 TestClient", True)

    # /artifacts
    r1 = client.get("/artifacts?limit=5")
    chk("/artifacts 返回 200", r1.status_code == 200, r1.status_code)
    try:
        j1 = r1.json()
    except Exception as e:
        j1 = {}
        chk("/artifacts 返回 JSON", False, str(e)[:80])
    chk("★ /artifacts 结构含 count/items/stats",
        all(k in j1 for k in ("count", "items", "stats")), list(j1)[:6])
    chk("items 是列表且长度受 limit 约束", isinstance(j1.get("items"), list) and len(j1["items"]) <= 5,
        len(j1.get("items") or []))
    r1b = client.get("/artifacts?scan=1&days=0&limit=1")
    chk("★ /artifacts?scan=1 附带目录盘点结果", "scanned" in r1b.json() or "scanned_error" in r1b.json(),
        list(r1b.json())[:6])

    # /experts
    r2 = client.get("/experts")
    chk("/experts 返回 200", r2.status_code == 200, r2.status_code)
    j2 = r2.json()
    chk("★ /experts 有专家清单", isinstance(j2.get("items"), list) and j2.get("count", 0) >= 1,
        j2.get("count"))
    names = [x.get("name") for x in (j2.get("items") or [])]
    chk("★ 清单里有点名可用的专家 (如 数据分析)", "数据分析" in names, names[:6])
    r2b = client.get("/experts?name=数据分析")
    j2b = r2b.json()
    chk("★ ?name= 返回会注入的人设块", "block" in j2b and "现在的角色" in str(j2b.get("block")),
        list(j2b)[:6])
    r2c = client.get("/experts?name=不存在的专家")
    j2c = r2c.json()
    chk("★ 查不存在的专家 → 明说没有 (不返回别人的)",
        "error" in j2c and "没有叫" in str(j2c.get("error")), j2c)

    # /audit
    r3 = client.get("/audit?n=5")
    chk("/audit 返回 200", r3.status_code == 200, r3.status_code)
    j3 = r3.json()
    chk("★ /audit 返回 entries 列表", isinstance(j3.get("entries"), list), list(j3)[:5])
    chk("审计条数受 n 约束", len(j3.get("entries") or []) <= 5, len(j3.get("entries") or []))
    if j3.get("error"):
        warn("引擎未就绪时 /audit 的诚实报错", str(j3["error"])[:60])

    # /dash —— 只读数据面页面 (★ 2026-09-21 加)
    r4 = client.get("/dash")
    chk("/dash 返回 200", r4.status_code == 200, r4.status_code)
    h = r4.text or ""
    chk("★ /dash 是黑金风页面 (背景 #0a0502 + 主色 #FFAC02)", "#0a0502" in h and "#FFAC02" in h)
    chk("★ /dash 不出现白底 (用户视觉标准: 禁白色)", "#fff" not in h.lower() and "#ffffff" not in h.lower())
    chk("★ /dash 真去读那三条只读接口",
        all(k in h for k in ("/artifacts", "/experts", "/audit")), "页面没引用只读接口")
    chk("★ /dash 不写任何状态 (只有 GET)",
        not re.search(r'method\s*:\s*"(POST|PUT|DELETE)"', h), "页面里有写操作")

    # 只读性: 连打几次, 内容不应产生副作用
    before = (ROOT / "data" / "artifacts.jsonl")
    b_snap = before.read_text(encoding="utf-8") if before.exists() else None
    for _ in range(3):
        client.get("/artifacts?scan=1")
        client.get("/experts")
        client.get("/audit")
    after = (ROOT / "data" / "artifacts.jsonl")
    a_snap = after.read_text(encoding="utf-8") if after.exists() else None
    chk("★ 反复调用不写真实产物登记表 (隔离生效)", b_snap == a_snap)

    # ── [B] 零副作用 ──
    print("\n[B] 零副作用")
    if db_before is not None:
        chk("★ 真对话库指纹未变", fp_db(db) == db_before, f"{db_before} → {fp_db(db)}")
    # ★ 探针自身修正: 原先在**清理之前**就断言"无残留" —— 必然失败 (顺序错)。
    #   先清自己的沙箱, 再断言 (而且只清自己这次的, 不碰别人的)。
    try:
        shutil.rmtree(iso, ignore_errors=True)
    except Exception:
        pass
    junk = [p.name for p in (ROOT / "tmp").glob("_web_*")]
    chk("无 tmp 残留 (本次沙箱已清)", not junk, junk)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(W)} WARN" if W else ""))
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    import shutil
    rc = main()
    shutil.rmtree(Path(__file__).resolve().parents[2] / "tmp" / f"_web_iso_{os.getpid()}",
                  ignore_errors=True)
    sys.exit(rc)
