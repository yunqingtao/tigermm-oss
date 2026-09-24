# -*- coding: utf-8 -*-
r"""TMM 自然语言理解「考卷」—— 出题考它, 而不是自编自答。

为什么单独做成考卷 (与常驻门禁分开):
  门禁 = 回归防线 (钉住已知缺陷, 每次改动都跑, 必须全绿)。
  考卷 = **压力测试** (专挑会翻车的说法: 口语变体/省略/否定/混合意图/带错别字),
         允许出现"没做对", 那是发现问题的信号 —— 分数低不代表门禁红。
         用户原话: "你出个卷子, 让他做"。

判分口径 (客观, 可自动化):
  save    必须**真落盘** · 内容 >= RICH_MIN 字 · 不含指令碎片 · 文件名不是自动命名
  noban   一个字都不许落盘 (查询/读目录/问概念/否定)
  reply   必须有实质回复 (>= 40 字) —— 是否落盘不限
  infer   省略句: 两种情况都算对 —— (a) 合理推断并干成  (b) 诚实反问"你指什么";
          但**任何**垃圾产物都判错

跑法: python -B scripts/tmm_exam.py            (全部 22 题)
      python -B scripts/tmm_exam.py --only A   (只跑某一类)
      python -B scripts/tmm_exam.py --quiet    (只出成绩单)
★ 全程隔离: 真账本/会话库/缺口库/技能账全部指向临时目录, 桌面产物跑完即清。
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))
os.chdir(ROOT)

RICH_MIN = 20          # "有真内容" 的起点 (字)
REPLY_MIN = 40         # "有实质回复" 的起点 (字)
#: 指令碎片特征: 正文里出现这些 = 把用户的话当内容写进去了
TAIL_WORDS = ("放桌面", "搁桌面", "扔桌面", "丢桌面", "摆桌面上", "存桌面", "保存到桌面",
              "存到文档", "写入桌面", "到桌面")

# ═══════════════════════════════════════════════════════════════════
# 卷子 (用户说话的方式; 挑会翻车的)
# ═══════════════════════════════════════════════════════════════════
EXAM: list[tuple[str, str, str, str]] = [
    # (题号, 题目, 类别, 期望)
    ("A1", "写一首诗放桌面",                     "A.落盘-口语", "save"),
    ("A2", "写一首诗保存到桌面",                 "A.落盘-口语", "save"),
    ("A3", "弄一首诗搁桌面上",                   "A.落盘-口语", "save"),
    ("A4", "给我来首诗扔桌面",                   "A.落盘-口语", "save"),
    ("A5", "随手写个小故事存桌面",               "A.落盘-口语", "save"),

    ("B1", "写一首关于秋天的七言绝句存到桌面",   "B.落盘-带题材", "save"),
    ("B2", "写个教小朋友学英语的小剧本, 保存到桌面", "B.落盘-带题材", "save"),
    ("B3", "帮我弄份周报放文档里",               "B.落盘-带题材", "save"),

    ("C1", "写一首关于春天的诗",                 "C.生成-不指定落盘", "reply"),
    ("C2", "给我写个三人对话的小短剧",           "C.生成-不指定落盘", "reply"),

    ("D1", "北京天气",                           "D.查询-禁落盘", "noban"),
    ("D2", "今天几号",                           "D.查询-禁落盘", "noban"),
    ("D3", "双色球7+1复式多少钱",                "D.查询-禁落盘", "noban"),

    ("E1", "看看桌面有什么",                     "E.读目录-禁落盘", "noban"),
    ("E2", "桌面上有哪些文件",                   "E.读目录-禁落盘", "noban"),

    ("F1", "什么是向量数据库",                   "F.问概念-禁落盘", "noban"),
    ("F2", "解释一下什么是RAG",                  "F.问概念-禁落盘", "noban"),

    ("G1", "别放桌面",                           "G.否定-禁落盘", "noban"),
    ("G2", "先别保存, 我看看",                   "G.否定-禁落盘", "noban"),

    ("H1", "整一个",                             "H.省略-最难", "infer"),
    ("H2", "就那个, 弄一下",                     "H.省略-最难", "infer"),
    ("H3", "帮我弄一下昨天说的那个",             "H.省略-最难", "infer"),
]

DESK = Path(os.path.expanduser("~/Desktop"))
DOCS = Path(os.path.expanduser("~/Documents"))


def snapshot():
    """★ 用 **mtime+size** 快照, 不用"文件集合差集" —— 差集抓不到**覆盖写**。
    踩坑实录 (2026-09-23): 考卷先跑"写一首诗放桌面"(建了 小诗.txt),
    再跑"弄一首诗搁桌面上"(规划器同样选 小诗.txt → **覆盖**),
    文件集合没变 ⇒ 被判"没落盘" —— 假红, 实际 TMM 干得好好的。
    用户看不到这一层, 只会以为 TMM 不行 —— 所以判分工具本身的坑必须堵死。
    """
    snap = {}
    for p in list(glob.glob(str(DESK / "*"))) + list(glob.glob(str(DOCS / "*"))):
        try:
            st = os.stat(p)
            snap[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return snap


def changed(before: dict, after: dict) -> list:
    """新增 **或 被覆盖** 的文件 (覆盖 = mtime/size 变了)。"""
    return sorted(p for p, v in after.items() if before.get(p) != v)


def judge(kind_expect: str, before: dict, after: dict, resp: str) -> tuple[bool, str]:
    """判一题。返回 (对错, 理由)。纯函数 —— 只看输入。

    before/after 是 snapshot() 的 mtime 快照 (能看见**覆盖写**)。
    """
    new = changed(before, after)
    resp = str(resp or "")
    # 任何情况下, 自动命名的垃圾文件都算错
    junk = [Path(p).name for p in new
            if Path(p).name.startswith(("doc_", "output_", "untitled"))]
    if junk:
        return False, f"自动命名垃圾 {junk}"
    if kind_expect == "save":
        if not new:
            return False, "没落盘"
        for p in new:
            try:
                t = Path(p).read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                return False, f"{Path(p).name} 读不出"
            if len(t) < RICH_MIN:
                return False, f"{Path(p).name} 只有 {len(t)} 字 (内容是 {t[:24]!r})"
            hit = [w for w in TAIL_WORDS if w in t]
            if hit:
                return False, f"{Path(p).name} 把指令当正文 ({hit})"
        return True, f"落盘 {len(new)} 个 · 内容合格"
    if kind_expect == "noban":
        if new:
            names = [Path(p).name for p in new]
            return False, f"不该落盘却写了 {names}"
        return True, "未落盘 ✓"
    if kind_expect == "reply":
        if len(resp.strip()) >= REPLY_MIN:
            return True, f"有实质回复 ({len(resp.strip())} 字)" + \
                         (f" · 另落盘 {len(new)} 个" if new else "")
        return False, f"回复太短 ({len(resp.strip())} 字): {resp[:40]!r}"
    if kind_expect == "infer":
        # 省略句: 干成 或 诚实反问 都对; 写垃圾错 (上面已判)
        asking = re.search(r"你指的是|是指什么|想让我|哪一首|哪一个|具体说|请问|不明白|不清楚", resp)
        did = bool(new)
        if did:
            return True, f"合理推断并执行 ({len(new)} 个产物)"
        if asking:
            return True, "诚实反问 ✓"
        if len(resp.strip()) >= 8:
            return True, f"作了回应 ({len(resp.strip())} 字)"
        return False, "既没干也没问"
    return False, f"未知期望 {kind_expect}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某一类 (如 A / B / H)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    iso = Path(tempfile.mkdtemp(prefix="tmm_exam_"))
    _env_keys = ("TMM_TASK_DB", "TMM_SESSION_DB", "TMM_GAP_DB", "TMM_PERCEPTION_FILE",
                 "TMM_USAGE_LOG", "TMM_SKILL_USAGE_FILE", "TMM_ARTIFACTS_FILE", "TMM_LIVE")
    _saved = {k: os.environ.get(k) for k in _env_keys}
    for k, f in (("TMM_TASK_DB", "task.db"), ("TMM_SESSION_DB", "sess.db"), ("TMM_GAP_DB", "gap.db"),
                 ("TMM_PERCEPTION_FILE", "p.json"), ("TMM_USAGE_LOG", "u.jsonl"),
                 ("TMM_SKILL_USAGE_FILE", "s.jsonl"), ("TMM_ARTIFACTS_FILE", "a.jsonl")):
        os.environ[k] = str(iso / f)
    os.environ.pop("TMM_PROBE", None)
    os.environ["TMM_LIVE"] = "1"          # 走"真人"路径 (账本也一起考)
    os.environ["TMM_EXAM"] = "1"

    import logging
    logging.disable(logging.CRITICAL)
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.task_ledger import get_task_ledger, reset_task_ledger
    from main import _load_config

    MJ = ROOT / "data" / "mode.json"
    _mbak = MJ.read_bytes() if MJ.exists() else None
    reset_task_ledger()

    cfg = _load_config()
    PL = Level4Pipeline(ModelClient(cfg), cfg)
    # ★ 考试前提: 钉住实干模式 (与每题 mode_override 双保险) ——
    #   否则用户态漂到 ask, 全部题都"不执行", 成绩单毫无意义 (实测踩过)。
    try:
        PL.modes.set("craft")
    except Exception:
        pass
    lg = get_task_ledger()
    mode = PL.modes.get()

    print("=" * 96)
    print("TMM 自然语言理解 · 考卷")
    print(f"  题量 {len(EXAM)} · 模式 {mode} · 账本 {lg.db_path.name} (隔离)")
    print("=" * 96)

    rows, made = [], []
    todo = [q for q in EXAM if not args.only or q[2].startswith(args.only)]
    _mode_guard = []                      # ★ 谁改了 mode.json → 逐题记下来
    try:
        for no, q, cat, expect in todo:
            _mj0 = MJ.read_bytes() if MJ.exists() else b""
            before = snapshot()
            _gw0 = getattr(PL.gateway, "turn_tools", [])
            t0 = time.time()
            try:
                r = asyncio.run(PL.process(q, probe=True, mode_override="craft")) or {}
                resp = str(r.get("response") or "")
                route = str(r.get("route") or r.get("intent") or "")
                tools = list(getattr(PL.gateway, "turn_tools", []) or [])
            except Exception as e:
                resp, route, tools = f"EXC {type(e).__name__}: {e}", "EXC", []
            el = round(time.time() - t0, 1)
            after = snapshot()
            # ★ 逐题盯 mode.json: 用户态被谁改了, 考试过程里就能看见
            if (MJ.read_bytes() if MJ.exists() else b"") != _mj0:
                try:
                    _m1 = json.loads(MJ.read_text(encoding="utf-8")).get("mode")
                except Exception:
                    _m1 = "?"
                _mode_guard.append((no, q, _m1))
                print(f"     ★★ 本题改了 mode.json → {_m1}")
            ok, why = judge(expect, before, after, resp)
            made.extend(changed(before, after))
            rows.append({"no": no, "q": q, "cat": cat, "expect": expect, "route": route,
                         "tools": tools, "ok": ok, "why": why, "el": el,
                         "new": [Path(p).name for p in changed(before, after)],
                         "resp": resp[:110]})
            if not args.quiet:
                print(f"\n[{no}] {q}")
                print(f"     期望 {expect:<6} route={route:<22} {el}s  tools={tools or '[]'}")
                print(f"     {'✓' if ok else '✗'} {why}")
                if not ok:
                    print(f"     回复: {resp[:150]!r}")
    finally:
        for p in set(made):
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        if _mbak is not None:
            MJ.write_bytes(_mbak)
        try:
            PL.modes.set("craft")        # 考卷跑完把真实模式钉回实干 (用户态别再漂)
        except Exception:
            pass
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_task_ledger()
        shutil.rmtree(iso, ignore_errors=True)
        os.environ.pop("TMM_EXAM", None)

    # ── 成绩单 ──────────────────────────────────────────────
    total, right = len(rows), sum(1 for r in rows if r["ok"])
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["cat"], []).append(r)
    print("\n" + "=" * 96)
    print(f"成绩单: {right}/{total}  ({right / total * 100:.0f} 分)" if total else "成绩单: 空")
    print("=" * 96)
    for cat, rs in by_cat.items():
        got = sum(1 for r in rs if r["ok"])
        bar = "█" * got + "░" * (len(rs) - got)
        print(f"  {cat:<20} {got}/{len(rs)}  {bar}")
    if _mode_guard:
        print("\n  ★★ 有题目改动了 mode.json (用户状态!):")
        for no, q, m in _mode_guard:
            print(f"     [{no}] {q[:30]} → mode={m}")
    bad = [r for r in rows if not r["ok"]]
    if bad:
        print("\n  没做对的:")
        for r in bad:
            print(f"    [{r['no']}] {r['q'][:34]:<36} {r['why'][:70]}")
        print("\n  用的路由:", json.dumps({r["no"]: r["route"] for r in bad}, ensure_ascii=False))
    else:
        print("\n  全对 ✓")
    Path(ROOT / "tmp").mkdir(exist_ok=True)
    out = ROOT / "tmp" / f"_exam_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"right": right, "total": total, "mode": mode, "rows": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n  明细: {out}")
    print("=" * 96)
    return 0 if right == total else 1


if __name__ == "__main__":
    sys.exit(main())
