"""缺信息自查 + 行动判定 门禁 (2026-09-21 立)

挡的是什么 (实测的短板)
═══════════════════════════════════════════════════════════════════
短板1 "停在门口": "帮我看看这100个Excel怎么归类" → 原来直接要路径, 不自己找。
短板B "只会说不会动": 分析类请求最后交给模型, 它可以干聊一整段方案而不碰工具。

本门禁钉三件:
  [A] 判定纯函数: 该自查/不该自查、该动手/不该动手 (正例+反例, 含"问概念不动手")
  [B] 只读且受控: discover 只在白名单内、有上限、**绝不创建/修改任何文件**
  [C] 端到端(隔离沙箱): 真跑 pipeline, 抓**实际喂给模型的提示** ——
      沙箱里有 xlsx → 提示里必须出现真实候选路径; 沙箱空 → 必须说"没找到"且不许编
      收口: TMM_SELF_LOOKUP_DIRS 指向沙箱, 所以**不碰用户真实桌面**

判据自身有效性: 正控(沙箱有文件→必现候选) + 负控(沙箱空→不得编造路径)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))

P, F = [], []


def skip(name, reason=""):
    """依赖不在 → 跳过 (不是 FAIL)。本门禁原来没有 skip, 加它是因为:
    在**分发包**里跑时本地会话库不存在 (data/ 按设计不进包) —— 那是"依赖不在",
    报成 FAIL 就等于把环境问题算成代码坏 (项目口径: 依赖不可用一律 SKIP 带原因)。"""
    print(f"  SKIP  {name}" + (f"   <- {reason}" if reason else ""))


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def main() -> int:
    print("=" * 78)
    print("缺信息自查 + 行动判定 门禁")
    print("=" * 78)

    from core import self_lookup as SL

    # ── [A] 判定纯函数 ──
    print("\n[A] 判定 (纯函数)")
    chk("★ 缺路径提对象 → 要自查", SL.wants_lookup("帮我看看这100个Excel怎么归类"))
    chk("★ 有路径 → 不自查 (那是'读它')", not SL.wants_lookup("读一下 D:/x/a.xlsx"))
    chk("★ 问概念 → 不触发", not SL.wants_lookup("什么是闭包"))
    chk("★ 要动手 (归类/汇总/做成)", SL.wants_action("帮我看看这100个Excel怎么归类")
        and SL.wants_action("把表格汇总成报告"))
    chk("★ 问概念/求解释 → 不动手", not SL.wants_action("什么是闭包")
        and not SL.wants_action("解释一下什么是RAG"))
    # 牙齿: 判定必须**有区分度** (不能恒真/恒假)
    _pos = sum(1 for m in ("把Excel归类", "整理这批文件", "做成报告") if SL.wants_action(m))
    _neg = sum(1 for m in ("什么是闭包", "解释一下RAG", "为什么天是蓝的") if not SL.wants_action(m))
    chk("★ 判据有区分度 (正例3/3 反例3/3)", _pos == 3 and _neg == 3, f"{_pos}/{_neg}")

    # ── [B] 只读 + 受控 ──
    print("\n[B] 只读 & 受控")
    sbx = Path(tempfile.mkdtemp(prefix="_vfy_lookup_"))
    (sbx / "sub").mkdir()
    for n in ("a.xlsx", "b.xlsx", "c.csv"):
        (sbx / n).write_bytes(b"x" * 10)
    (sbx / "sub" / "deep.xlsx").write_bytes(b"x" * 10)
    (sbx / "note.txt").write_text("hi", encoding="utf-8")
    os.environ["TMM_SELF_LOOKUP_DIRS"] = str(sbx)
    try:
        snap0 = sorted(p.relative_to(sbx).as_posix() for p in sbx.rglob("*"))
        got = SL.discover("帮我看看Excel")
        snap1 = sorted(p.relative_to(sbx).as_posix() for p in sbx.rglob("*"))
        chk("★ discover 只读 (目录快照逐项未变)", snap0 == snap1, f"{snap0} -> {snap1}")
        chk("★ 只挑匹配后缀 (xlsx/csv, 不含 note.txt)",
            {Path(c["path"]).name for c in got} == {"a.xlsx", "b.xlsx", "c.csv", "deep.xlsx"},
            [Path(c["path"]).name for c in got])
        chk("★ 下钻两层拿到 sub/deep.xlsx", any("sub" in c["path"] for c in got))
        chk("★ 上限生效", len(SL.discover("帮我看看Excel", cap=2)) <= 2)
        chk("★ 收口生效: TMM_SELF_LOOKUP_DIRS 设了就只用它 (不碰家目录)",
            all(str(sbx) in c["path"] for c in got), got[:2])
        _empty = Path(tempfile.mkdtemp(prefix="_vfy_empty_"))
        os.environ["TMM_SELF_LOOKUP_DIRS"] = str(_empty)
        chk("★ 空目录 → 空候选", SL.discover("帮我看看Excel") == [])
        _msg = SL.format_candidates("看看Excel", [])
        chk("★ 找不到时明说'没找到' + 禁止编造", "没找到" in _msg and "不要编造" in _msg)
        chk("★ 动作指示只在要动手时给 (负例为空串)",
            SL.format_action_directive("什么是闭包") == ""
            and "优先调用工具" in SL.format_action_directive("把Excel归类"))
        os.environ["TMM_SELF_LOOKUP_DIRS"] = str(sbx)
    finally:
        os.environ.pop("TMM_SELF_LOOKUP_DIRS", None)

    # ── [C] 端到端 (隔离沙箱, 抓实际提示) ──
    print("\n[C] 端到端: 真跑 pipeline, 抓喂给模型的提示")
    try:
        from _probe_env import activate
        activate()
    except Exception as e:
        chk("探针隔离可加载 (缺它不许跑 e2e)", False, str(e))
        _cleanup([sbx])
        return 1
    db = ROOT / "data" / "chat_sessions.db"
    # ★ 2026-09-22 修 (产物级实测): 本机会话库**按设计不进分发包** (data/ 属个人数据) ⇒
    #   在分发包里跑这道门时 `sqlite3.connect` 会抛 `unable to open database file`,
    #   整门 exit=1 且无结果行 —— 把"依赖不在"报成了"代码坏", 与项目口径相悖
    #   (依赖不可用 → SKIP 带原因, 不是 FAIL)。
    #   实测: 从公开站 clone 后在包里跑, 只有这道门如此 (其余 7 道都跑得起来)。
    if not db.is_file():
        skip("端到端零污染核对", f"本机会话库不在 ({db.name}) —— 分发包里按设计没有本地数据, 这层跳过")
        # ★ 必须**照常打标准结果行** (`结果: N PASS / M FAIL`): runner 靠它判分, 认不出
        #   结果行就按"崩溃"记 `0 PASS / 1 FAIL`, 而且 green 还要求 npass > 0。
        #   (我第一版在这里打了自定义文案 + 提前 return ⇒ 直接跑是对的(exit 0),
        #    通过 runner 跑却判红 —— "在包里跑全套"才暴露出来。)
        print("\n" + "=" * 78)
        print(f"结果: {len(P)} PASS / {len(F)} FAIL")
        for x in F:
            print("  -", x)
        print("=" * 78)
        _cleanup([sbx])
        return 1 if F else 0

    def fp():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute("SELECT id, role, content FROM messages ORDER BY id").fetchall()
        c.close()
        h = hashlib.sha1()
        for r in rows:
            h.update((str(r[0]) + "|" + str(r[1]) + "|" + str(r[2])).encode("utf-8", "replace"))
        return len(rows), h.hexdigest()[:10]

    before = (fp(), (ROOT / "data" / "mode.json").read_bytes())
    try:
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from core.knowledge import KnowledgeEngine
        from config.settings import DATA_DIR

        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl._ke = KnowledgeEngine(DATA_DIR)
        seen = {}

        async def fake(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
            seen["msgs"] = messages
            return {"text": "(模型回复)"}

        pl.model_client.generate = fake
        MSG = "帮我看看这100个Excel怎么归类"

        # 正控: 沙箱有 xlsx → 提示里必须出现真实候选
        os.environ["TMM_SELF_LOOKUP_DIRS"] = str(sbx)
        r_pos = asyncio.run(pl.process(MSG, probe=True))
        blob = "\n".join(str(m.get("content", "")) for m in (seen.get("msgs") or []))
        chk("★ 正控: 提示里出现沙箱真实候选路径 (含 a.xlsx)", "a.xlsx" in blob and str(sbx) in blob, blob[-300:])
        chk("★ 提示里带上'只读自查结果'块", "只读自查结果" in blob)
        chk("★ 判定该动手 → 提示里带'框架判定'指示", "框架判定" in blob)
        # ★ 路由归属: 否则语料只记 intent/model, 分不出这条路由是否真生效
        chk("★ 该消息由 lookup.prefill 裁决 (不是兜底)", r_pos.get("route") == "lookup.prefill",
            f"route={r_pos.get('route')}")

        # 不抢既有能力: "整理文件"有专门的链路, 不该被本路由接走
        seen.clear()
        r_org = asyncio.run(pl.process("把这批文件整理一下", probe=True))
        chk("★ 不抢既有能力 (整理类仍走原链路, 非 lookup.prefill)",
            r_org.get("route") != "lookup.prefill", f"route={r_org.get('route')}")

        # 负控: 空沙箱 → 必须说没找到, 不得编造路径
        empty = Path(tempfile.mkdtemp(prefix="_vfy_empty2_"))
        os.environ["TMM_SELF_LOOKUP_DIRS"] = str(empty)
        seen.clear()
        asyncio.run(pl.process(MSG, probe=True))
        blob2 = "\n".join(str(m.get("content", "")) for m in (seen.get("msgs") or []))
        chk("★ 负控: 空沙箱 → 提示里明说'没找到'", "没找到" in blob2, blob2[-260:])
        chk("★ 负控: 不得凭空给出候选路径", str(empty) not in blob2.replace("# 用户消息", ""), blob2[-200:])
        os.environ.pop("TMM_SELF_LOOKUP_DIRS", None)
        _cleanup([empty])

        # 不越界: 问概念不该触发自查
        seen.clear()
        asyncio.run(pl.process("解释一下什么是闭包", probe=True))
        blob3 = "\n".join(str(m.get("content", "")) for m in (seen.get("msgs") or []))
        chk("★ 问概念 → 不注入自查块", "只读自查结果" not in blob3)

        chk("★ 零副作用 (对话库指纹 + mode.json 未变)", before == (fp(), (ROOT / "data" / "mode.json").read_bytes()))
    finally:
        os.environ.pop("TMM_SELF_LOOKUP_DIRS", None)
        _cleanup([sbx])

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


def _cleanup(dirs):
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
