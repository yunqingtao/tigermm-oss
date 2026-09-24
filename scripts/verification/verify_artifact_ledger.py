"""产物台账门禁 (2026-09-21 立) —— 归档线的第一条真闭环

挡的是什么
═══════════════════════════════════════════════════════════════════
core/artifacts.py 有 add()/list_recent()/stats() 一整套, tools/artifacts.py 有工具,
web_server 有 /artifacts 端点 —— 但**没有任何生成路径调用 add()** →
台账永远是空的 ("今天产出了什么"只能诚实答"登记表空")。
又一处"文件在、功能不在"。

现在挂在**所有工具调用的唯一成功出口** (ToolGateway.call) 自动登记。

判据
───────────────────────────────────────────────────────────────
  [A] add() 诚实: 不存在的路径**拒绝登记** (登记不存在的文件 = 制造假信息)
  [B] 只在"真产出"时记: 刚变的文件记; 10 分钟前的旧文件**不记**; 不认识的扩展名**不记**
  [C] 端到端: 真调 file_ops 写文件 → 台账里出现它 (且 list_recent/stats 能查到)
  [D] 探针安全: 台账落盘路径受 TMM_ARTIFACTS_FILE 控制 → 隔离时**绝不动用户真台账**
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def main() -> int:
    print("=" * 78)
    print("产物台账门禁 —— 归档线闭环")
    print("=" * 78)

    from core import artifacts as A

    sandbox = Path(tempfile.mkdtemp(prefix="_vfy_art_"))
    ledger = sandbox / "artifacts.jsonl"
    real_ledger = ROOT / "data" / "artifacts.jsonl"

    def _real_fp():
        return real_ledger.read_bytes() if real_ledger.exists() else b""

    real_before = _real_fp()
    os.environ["TMM_ARTIFACTS_FILE"] = str(ledger)
    # ★★ 记账是 fail-closed 的: 只有真人会话 (TMM_LIVE=1) 才登记。
    #   本门禁要测"登记真的会发生"这条正路 → 显式开真人信号 (负控见下面 [D] 段)。
    os.environ["TMM_LIVE"] = "1"
    try:
        # ── [A] add() 诚实 ──
        print("\n[A] 登记器本身 (诚实)")
        miss = A.add(str(sandbox / "不存在.xlsx"))
        chk("★ 不存在的路径拒绝登记 (不制造假信息)",
            isinstance(miss, dict) and miss.get("ok") is False and "不存在" in str(miss.get("error", "")), miss)
        f1 = sandbox / "real.xlsx"
        f1.write_bytes(b"x" * 100)
        r1 = A.add(str(f1), source="test")
        chk("★ 存在的文件登记成功", isinstance(r1, dict) and r1.get("ok") and r1["record"]["path"] == str(f1))
        chk("★ 记录带 kind/size/来源", r1["record"]["kind"] != "other"
            and r1["record"]["size"] == 100 and r1["record"]["source"] == "test", r1.get("record"))

        # ── [B] 自动登记的启发式 ──
        print("\n[B] 自动登记: 只记'刚产出的'")
        # ★ 真身在 core/pipeline.py (core/tool_gateway.py 那份生产路径不用 —— 见门禁报告)
        from core.pipeline import ToolGateway

        gw = ToolGateway()
        gw.plugin_mgr = None                      # 只用 _register_artifacts, 不经过工具
        fresh = sandbox / "fresh_report.docx"
        fresh.write_bytes(b"y" * 50)
        n = gw._register_artifacts("file_ops", {"path": str(fresh), "content": "y"}, {"success": True})
        chk("★ 刚产出的文件被登记", n == 1, f"n={n}")

        n2 = gw._register_artifacts("file_ops", {"path": str(fresh)}, {"success": True})
        chk("★ 同路径不重复登记 (本进程去重)", n2 == 0, f"n={n2}")

        old = sandbox / "old_report.docx"
        old.write_bytes(b"z" * 10)
        os.utime(old, (time.time() - 3600, time.time() - 3600))     # 一小时前
        gw2 = ToolGateway()
        n3 = gw2._register_artifacts("file_ops", {"path": str(old)}, {"success": True})
        chk("★★ 旧文件(1h前)不登记 —— 那是'读到的'不是'产出的'", n3 == 0, f"n={n3}")

        weird = sandbox / "noise.qqq"
        weird.write_bytes(b"n" * 10)
        gw3 = ToolGateway()
        n4 = gw3._register_artifacts("file_ops", {"path": str(weird)}, {"success": True})
        chk("★ 不认识的扩展名不登记 (避免噪声)", n4 == 0, f"n={n4}")

        n5 = gw3._register_artifacts("file_ops", {"path": "relative/path.xlsx"}, {"success": True})
        chk("★ 相对路径不登记", n5 == 0, f"n={n5}")

        # ★ 生产模式必须拦临时/沙箱路径 (否则门禁与探针产生的临时文件会灌进用户真台账)
        _saved_env = os.environ.pop("TMM_ARTIFACTS_FILE", None)
        try:
            tmpf = ROOT / "tmp" / "_art_skip_probe.xlsx"
            tmpf.parent.mkdir(parents=True, exist_ok=True)
            tmpf.write_bytes(b"t" * 20)
            gw4 = ToolGateway()
            n6 = gw4._register_artifacts("file_ops", {"path": str(tmpf)}, {"success": True})
            chk("★★ 生产模式: tmp/ 下的新文件不进台账", n6 == 0, f"n={n6}")
        finally:
            if _saved_env is not None:
                os.environ["TMM_ARTIFACTS_FILE"] = _saved_env
            try:
                (ROOT / "tmp" / "_art_skip_probe.xlsx").unlink(missing_ok=True)
            except Exception:
                pass

        # ★ 硬探针闸: TMM_PROBE=1 时**绝不登记** (验证流量不写用户台账)
        os.environ["TMM_PROBE"] = "1"
        try:
            gw5 = ToolGateway()
            n7 = gw5._register_artifacts("file_ops", {"path": str(fresh)}, {"success": True})
            chk("★★ 探针流量: TMM_PROBE=1 时绝不登记 (不写用户台账)", n7 == 0, f"n={n7}")
        finally:
            os.environ.pop("TMM_PROBE", None)

        # ★★ 失败即闭的负控: 没有真人信号 (测试/门禁/脚本) → 一律不登记。
        #   这一条是本项目用两次"新功能污染用户数据"换来的: 只拦 TMM_PROBE 会漏,
        #   因为门禁直接调 process() 时不传 probe。规矩倒过来写才不漏。
        _live = os.environ.pop("TMM_LIVE", None)
        try:
            gw6 = ToolGateway()
            n8 = gw6._register_artifacts("file_ops", {"path": str(fresh)}, {"success": True})
            chk("★★ 失败即闭: 无 TMM_LIVE 时不登记 (门禁/测试写不进用户台账)", n8 == 0, f"n={n8}")
        finally:
            if _live is not None:
                os.environ["TMM_LIVE"] = _live

        recs = [A.public_rec(r) for r in A.list_recent(limit=0)]
        paths = [r["path"] for r in recs]
        chk("★ 台账里出现刚产出的那个 (fresh_report.docx)", str(fresh) in paths, paths[:6])
        chk("★ 台账里**没有**旧文件 / 怪扩展名",
            str(old) not in paths and str(weird) not in paths)

        # ── [C] 端到端: 真调工具 ──
        print("\n[C] 端到端: 真调 file_ops 写文件")
        try:
            from main import _load_config
            from core.model_client import ModelClient
            from core.pipeline import Level4Pipeline
            from core.knowledge import KnowledgeEngine
            from config.settings import DATA_DIR

            cfg = _load_config()
            pl = Level4Pipeline(ModelClient(cfg), cfg)
            pl._ke = KnowledgeEngine(DATA_DIR)
            # ★ 写在**项目内**临时目录: file_ops 的护栏拒绝写项目外路径 (这是对的, 别绕过它)。
            #   首次写在这里被判 Access denied —— 不是缺陷, 是护栏; 测试要顺着护栏走。
            wt = ROOT / "tmp" / "_vfy_art_e2e"
            wt.mkdir(parents=True, exist_ok=True)
            out = wt / "e2e_output.csv"
            payload = "a,b\n1,2\n"
            res = asyncio.run(pl.gateway.call("file_ops", action="write",
                                              path=str(out), content=payload))
            chk("工具调用本身成功", bool(isinstance(res, dict) and res.get("success")), str(res)[:200])
            chk("文件真的写出来了", out.exists() and out.read_text(encoding="utf-8") == payload)
            got = [A.public_rec(r) for r in A.list_recent(limit=0)]
            hit = [r for r in got if r["path"] == str(out)]
            chk("★★ 台账自动登记了它 (无需任何手工调用)",
                bool(hit), [r["path"] for r in got][:8])
            if hit:
                chk("★ 来源标成本次工具 (tool:file_ops)", hit[0].get("source") == "tool:file_ops", hit[0])
            st = A.stats()
            chk("★ stats 能统计到它", st["count"] >= 1 and st["total_size"] > 0, st)
        except Exception as e:
            chk("端到端跑通 (真调工具)", False, f"{type(e).__name__}: {e}")

        # ── [D] 探针安全 ──
        print("\n[D] 探针安全: 隔离时不动用户真台账")
        chk("★ 真台账 (data/artifacts.jsonl) 全程未被写",
            _real_fp() == real_before, "用户真台账被污染了!")
        os.environ.pop("TMM_ARTIFACTS_FILE", None)
        chk("★ 去掉隔离变量后, store_path 回到真路径",
            str(A.store_path()) == str(real_ledger), str(A.store_path()))
    finally:
        os.environ.pop("TMM_ARTIFACTS_FILE", None)
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(ROOT / "tmp" / "_vfy_art_e2e", ignore_errors=True)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
