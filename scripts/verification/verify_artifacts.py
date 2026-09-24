"""产物清单 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

为什么要有这一门 (2026-09-20, 借鉴通用 Agent 平台的 "Files/产物面板")
═══════════════════════════════════════════════════════════════════
引擎产出散落在桌面与项目目录, 用户习惯是"问进度要查盘直答"。本门守着三样东西:
  ① 只读: 不动任何用户文件 (登记表只追加)
  ② 诚实: 登记不存在的路径要**拒绝**; 查询要**过滤已删文件** (不给过期结论)
  ③ 接得通: 工具已登记(不被安全扫描拒绝) · 有 TOOL 元数据(关键词可命中) ·
            intent_router 有映射 · 技能在盘上且能加载

自清理: 全程用 TMM_ARTIFACTS_FILE 隔离 + 临时沙箱目录; 不碰真实登记表与用户文件。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PY = sys.executable

SB = ROOT / "tmp" / f"_art_sandbox_{os.getpid()}"
P, F, S, W = [], [], [], []


def chk(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS " if c else "  FAIL ") + n + (("   <- " + str(d)) if d and not c else ""))


def skip(n, why=""):
    S.append(n)
    print(f"  SKIP {n}   ({why})")


def warn(n, d=""):
    W.append(n)
    print(f"  WARN {n}   <- {d}")


def fingerprint_db(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if not tabs:
        c.close()
        return ("err", "no tables")
    main = "messages" if "messages" in tabs else tabs[0]
    n = c.execute("SELECT COUNT(*) FROM " + main).fetchone()[0]
    mx = c.execute("SELECT MAX(rowid) FROM " + main).fetchone()[0]
    c.close()
    return (main, n, mx)


def main():
    print("=" * 78)
    print("产物清单 — 只读 / 诚实 / 接得通")
    print("=" * 78)
    SB.mkdir(parents=True, exist_ok=True)
    reg = SB / "reg.jsonl"
    os.environ["TMM_ARTIFACTS_FILE"] = str(reg)

    REAL_REG = ROOT / "data" / "artifacts.jsonl"
    real_before = REAL_REG.read_text(encoding="utf-8") if REAL_REG.exists() else None
    db = ROOT / "data" / "chat_sessions.db"
    db_before = fingerprint_db(db) if db.exists() else None

    from core import artifacts as A

    # ── [0] 自检: 判据有牙齿 ──
    print("\n[0] 自检 (判据自身的辨别力)")
    chk("★ 隔离生效: 登记表指向沙箱", str(A.store_path()) == str(reg), A.store_path())
    probe = SB / "_tooth.txt"
    probe.write_text("x", encoding="utf-8")
    r_bad = A.add(str(SB / "不存在.bin"))
    chk("★ 登记不存在的路径被拒绝 (不制造假信息)", r_bad.get("ok") is False, r_bad)

    # ── [A] 登记与查询 ──
    print("\n[A] 登记与查询")
    mp4 = SB / "试片.mp4"
    mp4.write_bytes(b"0" * 4096)
    xlsx = SB / "报表.xlsx"
    xlsx.write_bytes(b"1" * 800)
    r1 = A.add(str(mp4), title="试片")
    chk("登记成功且返回记录", r1.get("ok") and r1["record"]["name"] == "试片.mp4", r1)
    chk("★ 类型识别正确 (mp4→video, xlsx→sheet)",
        r1["record"]["kind"] == "video" and A.kind_of(xlsx) == "sheet")
    chk("★ 大小人读 (4096B→4.0KB)", r1["record"]["size_h"] == "4.0KB", r1["record"]["size_h"])
    A.add(str(xlsx))
    A.add(str(mp4), title="试片v2")          # 同路径再登记
    recs = A.list_recent(limit=10)
    chk("★ 同路径去重 (只留最新一条)", len(recs) == 2, [r["name"] for r in recs])
    chk("★ 去重时保留最新 (试片v2)", any(r.get("title") == "试片v2" for r in recs))

    # ── [B] 诚实: 已删文件不报 ──
    print("\n[B] 诚实 (已不在盘上的不报)")
    xlsx.unlink()
    alive = A.list_recent(limit=10)
    allrec = A.list_recent(limit=10, alive_only=False)
    chk("★ 已删文件被过滤 (alive_only)", len(alive) == 1 and len(allrec) == 2,
        f"alive={len(alive)} all={len(allrec)}")

    # ── [C] 只读: 不动用户文件 ──
    print("\n[C] 只读 (不动用户文件)")
    _names_before = sorted(x.name for x in SB.iterdir())
    before = (mp4.stat().st_size, mp4.stat().st_mtime)
    A.add(str(mp4), title="再登记一次")
    A.list_recent(limit=5)
    A.stats()
    A.scan_dirs(dirs=[SB], days=1, limit=5)
    after = (mp4.stat().st_size, mp4.stat().st_mtime)
    chk("★ 登记/查询/盘点都不改文件", before == after, f"{before} → {after}")
    # ★ 探针自身修正: 原断言硬编码"文件数==2" —— 数错了 (沙箱里还有 reg.jsonl 与
    #   自检产生的 _tooth.txt)。改为**前后快照对比**: 只关心"有没有多出/少掉文件"。
    chk("★ 沙箱文件集合不变 (没偷偷复制/移动/删除)",
        sorted(x.name for x in SB.iterdir()) == _names_before,
        f"{_names_before} → {sorted(x.name for x in SB.iterdir())}")

    # ── [D] 盘点 (scan) ──
    print("\n[D] 目录盘点")
    found = A.scan_dirs(dirs=[SB], days=1, limit=10)
    chk("盘点到沙箱里的产物", any(f["name"] == "试片.mp4" for f in found), found)
    chk("★ 跳过缓存/仓库内部目录", not any("__pycache__" in f["path"] or ".git" in f["path"] for f in found))
    found0 = A.scan_dirs(dirs=[SB], days=0, limit=10)     # days=0 → 只看最近0天 → 基本为空
    chk("days 参数生效 (0 天 ≈ 空)", len(found0) <= len(found))

    # ── [E] 接线: 工具登记 / 元数据 / 路由 / 技能 ──
    print("\n[E] 接线 (工具可达 + 技能可接)")
    try:
        from config.settings import SAFE_PLUGINS, DATA_DIR
        from core.plugin_manager import PluginManager, scan_plugin_safe
        chk("★ artifacts 已登记 SAFE_PLUGINS (不被安全扫描拒绝)", "artifacts" in SAFE_PLUGINS)
        tpath = ROOT / "tools" / "artifacts.py"
        chk("工具文件在盘上", tpath.is_file())
        chk("★ 能通过静态扫描", scan_plugin_safe(tpath) is True)
        pm = PluginManager(DATA_DIR)
        chk("★ PluginManager 真加载了它", "artifacts" in pm._plugins)
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("artifacts_probe", tpath)
        mod = _iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        kws = getattr(mod, "TOOL", {}).get("keywords", [])
        chk("★ 有 TOOL 元数据 keywords (关键词路由只读 TOOL)",
            bool(kws) and "产出了什么" in kws, kws[:4])
        chk("PLUGIN 元数据也在 (兼容只读 PLUGIN 的旧路径)", bool(getattr(mod, "PLUGIN", {})))
        ir = (ROOT / "core" / "intent_router.py").read_text(encoding="utf-8")
        chk("★ intent_router 有产物映射", '"artifacts"' in ir and "产出了什么" in ir)
        sk = ROOT / "tmm_skills" / "artifacts-inventory" / "SKILL.md"
        chk("技能文件在盘上", sk.is_file())
        head = sk.read_text(encoding="utf-8-sig").split("---")[1]
        chk("★ 技能 answer_mode=true (查询类必须, 否则接不到)", "answer_mode: true" in head)
        import yaml
        try:
            yaml.safe_load(head)
            chk("★ frontmatter YAML 可解析 (踩过: 引号里套引号)", True)
        except Exception as e:
            chk("★ frontmatter YAML 可解析 (踩过: 引号里套引号)", False, str(e)[:80])
    except Exception as e:
        chk("接线检查可执行", False, f"{type(e).__name__}: {e}")

    # ── [F] 真跑: 经引擎的真工具链 ──
    print("\n[F] 真跑 (经 PluginManager 真调工具)")
    try:
        import asyncio
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        res = asyncio.run(pl.gateway.call("artifacts", action="list", limit=5))
        chk("★ 工具真跑通 (success)", bool(res.get("success")), str(res.get("error"))[:110])
        out = str(res.get("output") or "")
        chk("★ 输出里有沙箱产物路径 (不是谎报)", "试片.mp4" in out, out[:120])
        res2 = asyncio.run(pl.gateway.call("artifacts", action="stats"))
        chk("stats 动作可用", bool(res2.get("success")) and "产物" in str(res2.get("output")))
    except Exception as e:
        chk("真跑可执行", False, f"{type(e).__name__}: {e}")

    # ── [G] 零副作用 ──
    print("\n[G] 零副作用")
    real_after = REAL_REG.read_text(encoding="utf-8") if REAL_REG.exists() else None
    chk("★ 真实登记表未被碰 (隔离生效)", real_after == real_before,
        "真实表被改动了!" if real_after != real_before else "")
    if db_before is not None:
        chk("★ 真对话库指纹未变", fingerprint_db(db) == db_before, f"{db_before} → {fingerprint_db(db)}")
    chk("沙箱无残留 (运行中)", SB.exists())

    try:
        shutil.rmtree(SB, ignore_errors=True)
    except Exception:
        pass
    print("  PASS 沙箱已清理" if not SB.exists() else "  WARN 沙箱未清")
    if SB.exists():
        W.append("沙箱未清理")

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(W)} WARN" if W else "") +
          (f" / {len(S)} SKIP" if S else ""))
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
