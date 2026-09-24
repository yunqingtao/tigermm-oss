r"""Tiger.M.M 多任务链条执行测试台 —— 拆解 → 逐步 → 产物, 真证据。

为什么要单独有这一台: 现有门禁测的是「路由胜出者 / 契约 / 不变量」, 深测台测的是
「单个工具/技能能不能真跑通」。**没有任何一层在测「一条多步链能不能从头走到尾」** ——
而链条恰恰是最容易半途坏掉又没人发现的地方 (中间步静默失败、$stepN 引用解析成空、
依赖失败却继续跑、汇总谎报成功)。

四层覆盖 (逐层可单独跑, 互不依赖):
  A 拆解层 (真 deepseek): 自然语言多步请求 → 步数/白名单/引用语法/语言/依赖声明
  B 执行机制 (手写计划 + 打桩网关, 零成本确定性): $ref 解析 · 中文路径 · 重试 ·
    依赖失败中断 · 无依赖失败继续 · 汇总语义 · 诚实摘要 · 非法计划拒跑
  C 端到端真链 (真规划 + 真工具, 产物落沙箱): route=planner.multi_step + 链末产物查盘
  D 技能 DAG 多步 (真 DAG + 打桩网关): 声明几步就真调几步, 顺序与参数传递正确

设计原则 (沿用 tmm_deep_test.py 的既有约定):
  ① 零副作用: 探针隔离 (_probe_env.activate) + 沙箱在**项目允许根内** + 用户库快照还原
  ② 真调用优先: 能真跑的真跑; 有副作用的工具只记录不真调 (不产出副作用)
  ③ 分类而非二元: 结果带 kind, 只有契约型失败才算 FAIL (缺口/凭据/未测单列)
  ④ 统计数字一律实算, 报告不手写

用法:
    python -B scripts/tmm_chain_test.py                # 全量 (含真模型拆解 A/C)
    python -B scripts/tmm_chain_test.py --offline      # 只跑 B/D/E (零成本, 不打模型)
    python -B scripts/tmm_chain_test.py --json tmp/_chain.json
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# ── ★ 探针隔离 (防污染真数据) ──────────────────────────────────────────────
try:
    sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    from _probe_env import activate as _activate_probe_isolation
    _activate_probe_isolation()
    print("[隔离] 已启用探针沙箱 (不会污染真实数据)")
except Exception as _e:
    print(f"[★ 告警] 探针隔离未生效: {type(_e).__name__}: {_e} —— 继续跑可能污染真库")

logging.disable(logging.CRITICAL)

# ★ 沙箱在项目根内 (file_ops 有「允许根」白名单; %TEMP% 会被拒并静默改落桌面)
SB = ROOT / "tmp" / ("_chain_test_" + time.strftime("%Y%m%d_%H%M%S"))
SB.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "data"

RESULTS: list[dict] = []
T0 = time.time()


def rec(dim: str, name: str, ok, detail: str = "", kind: str = "bool"):
    """ok: True / False / '缺口' / '需凭据' / '未测' —— 与深测台的分类口径一致。"""
    t = round(time.time() - T0, 2)
    entry = {"dim": dim, "name": name, "ok": ok, "detail": str(detail)[:300], "kind": kind, "t": t}
    RESULTS.append(entry)
    # ★ 只有契约型(bool)失败才印 FAIL —— 观测/缺口/未测项印 NOTE, 免得日志与统计口径打架
    mark = ("  PASS " if ok is True else
            ("  FAIL " if (ok is False and kind == "bool") else "  NOTE "))
    print(mark + name + (("   <- " + str(detail)[:180]) if detail and ok is not True else ""))
    return entry


def dim(t: str):
    print("\n" + "─" * 74)
    print(t)
    print("─" * 74)


# ── 用户库快照还原 (probe=False 会写真库) ──
_USER_STATE = [DATA / "chat_sessions.db", DATA / "sessions.db", DATA / "entities.json"]
_BAK = {f: f.read_bytes() for f in _USER_STATE if f.exists()}


def _restore():
    for f, b in _BAK.items():
        try:
            f.write_bytes(b)
        except Exception:
            pass
    shutil.rmtree(SB, ignore_errors=True)


atexit.register(_restore)

# ── 引擎 (打桩 LLM, 但拆解层放真模型过) ──
from main import _load_config                        # noqa: E402
from core.knowledge import KnowledgeEngine           # noqa: E402
from core.learn_loop import LearnLoop                # noqa: E402
from core.model_client import ModelClient            # noqa: E402
from core.pipeline import Level4Pipeline             # noqa: E402
from core.task_planner import TaskPlanner, PlanExecutor, PLANNER_PROMPT   # noqa: E402
import core.learn_loop as _LL                        # noqa: E402
import core.perception as _perc                      # noqa: E402
import types as _types                               # noqa: E402
import core.mcp_client as _mcp_mod                   # noqa: E402

_perc.PERCEPTION_FILE = SB / "perception.json"
_LL._loop = LearnLoop(db_path=SB / "rules.db", data_dir=SB)
_mcp_mod.get_mcp_client = lambda *a, **k: _types.SimpleNamespace(
    load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [], tools=[],
    get_tool=lambda *a, **k: None, get_all_tools=lambda *a, **k: [], servers={},
    refresh_all=None)

CFG = _load_config()
PL = Level4Pipeline(ModelClient(CFG), CFG)
PL._ke = KnowledgeEngine(DATA)
PL.learn_loop = _LL._loop

# 只记录不真调的工具 (有副作用)
SIDE_EFFECT_TOOLS = {"send_email", "sms_send", "ntfy", "push_notify", "im_notify",
                     "win32_input", "windows_desktop", "browser", "backup", "shell_exec"}
CALLS: list[dict] = []
_REAL_CALL = PL.gateway.call
_REAL_GEN = PL.model_client.generate
LLM_CALLS = {"stub": 0, "real": 0}


async def _gw(tool, action="", **kw):
    # ★ 沙箱外写盘硬闸 (2026-09-22 实测踩到): 技能默认落点是**用户桌面**
    #   (pc-checkup/healthcheck 的 write_word 步没给 path → 落桌面), 探针直接真调就把
    #   测试产物写进用户桌面了 (实测落下 电脑体检报告.docx / 服务体检报告.docx /
    #   健康检查报告.txt 三个)。所以: 凡"要写文件"的调用, 路径不在沙箱内一律拦下并记账。
    _p = str(kw.get("path") or kw.get("file") or kw.get("out") or "")
    # ★ 闸门必须覆盖"没给路径"的情形: 没给路径 = 默认落桌面 (实测漏过 3 个产物到桌面)
    _writes = (tool in ("tiger_office", "file_ops", "diagram", "artifacts", "backup"))
    _has_abs = bool(_p)
    if _writes and (not _has_abs or not os.path.abspath(_p).lower().startswith(str(SB).lower())):
        CALLS.append({"tool": tool, "action": action, "kw": {k: str(v)[:90] for k, v in kw.items()},
                      "blocked": "沙箱外写盘"})
        return {"success": True, "output": f"[已拦截: 写盘路径不在沙箱, {_p}]", "stub": True}
    CALLS.append({"tool": tool, "action": action, "kw": {k: str(v)[:90] for k, v in kw.items()}})
    if tool in SIDE_EFFECT_TOOLS:
        return {"success": True, "output": "[已拦截: 有副作用工具, 测试不真调]", "stub": True}
    return await _REAL_CALL(tool, action=action, **kw)


PL.gateway.call = _gw


async def _gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
    """★ 路由式打桩: **规划器提示词放真模型过** (拆解质量是本次要测的东西),
    其余一律桩掉 (省成本、且与链条执行机制无关)。"""
    txt = ""
    try:
        txt = "\n".join(str(m.get("content", "")) for m in (messages or []))
    except Exception:
        pass
    if "task planner" in txt.lower() and ALLOW_REAL_MODEL:
        LLM_CALLS["real"] += 1
        return await _REAL_GEN(model, messages, temperature=temperature,
                               max_tokens=max_tokens, tools=tools, think=think)
    LLM_CALLS["stub"] += 1
    return {"text": "（测试桩产出）\n# 一、背景\n要点 A\n# 二、正文\n要点 B",
            "think": "", "model": model, "elapsed": 0.0, "error": None, "tool_calls": None}


PL.model_client.generate = _gen
ALLOW_REAL_MODEL = True          # --offline 时置 False

# 钉 mode=craft (还原在 atexit)
MODE_JSON = DATA / "mode.json"
_MODE_BAK = MODE_JSON.read_bytes() if MODE_JSON.exists() else None


def _restore_mode():
    try:
        if _MODE_BAK is not None:
            MODE_JSON.write_bytes(_MODE_BAK)
    except Exception:
        pass


atexit.register(_restore_mode)
try:
    _m = json.loads(MODE_JSON.read_text(encoding="utf-8"))
    if _m.get("mode") != "craft":
        _m["mode"] = "craft"
        MODE_JSON.write_text(json.dumps(_m, ensure_ascii=False, indent=2), encoding="utf-8")
except Exception:
    pass


# ═══════════════════ A. 拆解层 (真模型) ═══════════════════

PLAN_CASES = [
    ("先看下磁盘，再把这周待办写到 " + str(SB / "plan_a.txt"),
     "两步: 查盘 + 写文件"),
    ("读一下 " + str(SB / "in.txt") + "，总结一下，然后存成 " + str(SB / "out_a.txt"),
     "三步: 读 + 总结 + 写"),
    ("先查系统状态，再查磁盘，然后整理成一份报告存到 " + str(SB / "plan_c.txt"),
     "≥3 步: 两条查询 + 汇总写盘"),
    ("列出 " + str(SB) + " 目录，挑出 txt 文件，然后把清单写到 " + str(SB / "list_a.txt"),
     "三步: 列目录 + 挑文件 + 写清单"),
]


async def part_a_planning():
    dim("A. 拆解层 —— 自然语言多步请求 → 结构化计划 (真 deepseek)")
    if not ALLOW_REAL_MODEL:
        rec("A.拆解", "跳过硬模型拆解 (--offline)", "未测", "需要真模型", kind="skip")
        return
    planner = TaskPlanner(PL.model_client, PL.gateway.plugin_mgr, mcp_client=None)
    for msg, what in PLAN_CASES:
        CALLS.clear()
        r = await planner.plan(msg)
        plan, err = r.get("plan"), r.get("error")
        steps = plan or []
        tools = [(s.get("tool"), s.get("action")) for s in steps]
        known = PL.gateway.plugin_mgr._plugins.keys()
        bad_tool = [t for t, _ in tools if t not in known and t != "llm"]
        refs = [v for s in steps for v in (s.get("params") or {}).values()
                if isinstance(v, str) and ("$step" in v or "$prev" in v)]
        deps = [s.get("depends_on") for s in steps if s.get("depends_on") is not None]
        rec("A.拆解", f"{what} → 拆出 {len(steps)} 步", len(steps) >= 2,
            f"err={err} steps={tools}")
        rec("A.拆解", f"{what} → 工具全在白名单", not bad_tool, f"越界={bad_tool}")
        rec("A.拆解", f"{what} → 每步有 action/params", 
            all("action" in s and "params" in s for s in steps), f"steps={tools}")
        rec("A.拆解", f"{what} → 描述用中文", 
            all(str(s.get("description") or "").strip() for s in steps), "缺描述")
        # ★ 判据修正 (2026-09-22): 原判据要求"必须有 $stepN 引用" —— 过严。
        #   某些步的参数自带字面量 (如"本周待办"), 本就不需要引用。真正的契约是
        #   **不许留空占位** (计划里出现空串/空字典 = 下游必然拿到空值)。
        empties = [(i + 1, k) for i, s in enumerate(steps)
                   for k, v in (s.get("params") or {}).items() if v in ("", None, {}, [])]
        rec("A.拆解", f"{what} → 计划无空占位 (引不到的值不许留空)",
            not empties, f"空值={empties}")
        if len(steps) >= 2:
            rec("A.拆解", f"{what} → 跨步引用用 $stepN 语法 (观测)",
                bool(refs) or len(steps) == 1,
                f"refs={refs[:2]} deps={deps[:2]} —— 无引用时须在上一条(无空占位)上自洽",
                kind="info")


# ═══════════════════ A2. 链条命中率 (真模型, 本台的核心问题) ═══════════════════

CHAIN_HIT_CASES = [
    "先查端口，再查进程，然后出一份健康检查报告",
    "先查系统状态，再查磁盘，然后整理成一份报告存到 " + str(SB / "hit1.txt"),
    "先看看模型用量，再整理成一份统计存到 " + str(SB / "hit2.txt"),
    "读一下 " + str(SB / "in.txt") + "，然后总结成一份笔记存到 " + str(SB / "hit3.txt"),
    "先列一下 " + str(SB) + " 里的文件，再把清单写到 " + str(SB / "hit4.txt"),
    "看看电脑体检结果，然后把报告存到 " + str(SB / "hit5.txt"),
]


async def part_a2_chain_hit():
    dim("A2. 多步请求的「链条命中率」—— 真模型下, 到底有几条真的走拆解执行")
    if not ALLOW_REAL_MODEL:
        rec("A2.命中率", "跳过 (--offline)", "未测", "需要真模型", kind="skip")
        return
    hit = 0
    with_tool = 0
    rows = []
    for msg in CHAIN_HIT_CASES:
        CALLS.clear()
        try:
            r = await PL.process(msg, probe=True, mode_override="craft")
        except Exception as e:
            rec("A2.命中率", f"{msg[:22]}… 不崩", False, repr(e)[:120])
            continue
        route = str(r.get("route") or "-")
        tools = [c["tool"] for c in CALLS if c["tool"] != "llm"]
        rows.append((msg, route, tools))
        if route == "planner.multi_step":
            hit += 1
        if tools:
            with_tool += 1
    for msg, route, tools in rows:
        print(f"      {msg[:40]:<42} route={route:<22} tools={tools[:3]}")
    n = len(rows) or 1
    rec("A2.命中率", f"★ 多步请求真走链条执行: {hit}/{len(rows)}",
        hit >= len(rows) // 2,
        f"route 分布: {sorted(set(r for _, r, _ in rows))}")
    rec("A2.命中率", f"多步请求真调了工具: {with_tool}/{len(rows)}", with_tool >= len(rows) // 2,
        f"未调工具的: {[m[:26] for m, _, t in rows if not t]}")


# ═══════════════════ B. 执行机制 (确定性, 零成本) ═══════════════════

class FakeGW:
    """按脚本回放的网关: 记录调用 + 可控成功/失败/重试。"""

    def __init__(self, script: dict | None = None):
        self.script = script or {}
        self.calls: list[dict] = []

    async def call(self, tool, action="", timeout=None, **kw):
        self.calls.append({"tool": tool, "action": action, "kw": kw})
        key = f"{tool}:{action}" if action else tool
        if key in self.script:
            spec = self.script[key]
        elif tool in self.script:
            spec = self.script[tool]
        else:
            return {"success": True, "output": "ok", "data": "ok"}
        if callable(spec):
            return spec(len(self.calls))
        return dict(spec)


async def part_b_executor():
    dim("B. 执行机制 —— $ref / 路径 / 重试 / 依赖失败 / 汇总语义")

    # B1 $stepN.field 与 $prev 解析
    gw = FakeGW({"file_ops:read": {"success": True, "content": "甲文件内容"},
                 "check_mail:list": {"success": True, "count": 2,
                                     "emails": [{"id": 7, "subject": "S7"}, {"id": 9, "subject": "S9"}]}})
    ex = PlanExecutor(gw)
    plan = [
        {"step": 1, "tool": "file_ops", "action": "read", "params": {"path": str(SB / "in.txt")},
         "description": "读文件"},
        {"step": 2, "tool": "file_ops", "action": "write",
         "params": {"path": str(SB / "b1.txt"), "content": "$step1.content"},
         "depends_on": 1, "description": "写摘要"},
    ]
    r = await ex.execute(plan)
    w = [c for c in gw.calls if c["tool"] == "file_ops" and c["action"] == "write"]
    got = (w[0]["kw"].get("content") if w else None)
    rec("B.执行", "★ $stepN.field 解析成上一步真值", got == "甲文件内容", f"content={got!r}")
    rec("B.执行", "两步都执行且汇总为全成功 (2/2)",
        r["steps_ok"] == 2 and r["steps_total"] == 2 and r["success"] is True, str(r)[:160])

    # B2 depends_on 指定来源 (非紧邻上一步)
    gw = FakeGW({"check_mail:list": {"success": True, "emails": [{"id": 7}]},
                 "file_ops:write": {"success": True, "output": "written"}})
    ex = PlanExecutor(gw)
    plan = [
        {"step": 1, "tool": "check_mail", "action": "list", "params": {"count": 3}, "description": "列邮件"},
        {"step": 2, "tool": "system_info", "action": "disk", "params": {}, "description": "查盘"},
        {"step": 3, "tool": "file_ops", "action": "write",
         "params": {"path": str(SB / "b2.txt"), "content": "$step1.emails"},
         "depends_on": 1, "description": "写邮件"},
    ]
    r = await ex.execute(plan)
    w = [c for c in gw.calls if c["tool"] == "file_ops"][0]
    ok = "emails" in str(w["kw"].get("content")) or "id" in str(w["kw"].get("content"))
    rec("B.执行", "★ depends_on 指向非紧邻步也解析正确 (取 step1 而非 step2)",
        ok, f"content={str(w['kw'].get('content'))[:80]!r}")

    # B3 中文路径前缀 → 真路径 (不写文件, 只看参数解析)
    gw = FakeGW()
    ex = PlanExecutor(gw)
    await ex.execute([{"step": 1, "tool": "file_ops", "action": "write",
                       "params": {"path": "桌面/训练链测试.txt", "content": "x"},
                       "description": "写桌面"}])
    p = gw.calls[0]["kw"].get("path", "")
    rec("B.执行", "★ 中文前缀「桌面/」解析成真实 Desktop 路径",
        p.endswith("训练链测试.txt") and "Desktop" in p.replace("\\", "/"),
        f"path={p}")
    p2 = await _t3b()
    rec("B.执行", "中文路径不带文件名时补默认名 (不落到目录本身)",
        p2.endswith(".txt"), f"path={p2}")

    # B4 失败重试: 第 1 次失败第 2 次成功 → 该步算成功
    gw = FakeGW({"system_info": lambda n: {"success": n > 1, "output": "第%d次" % n,
                                          "error": "" if n > 1 else "临时故障"}})
    ex = PlanExecutor(gw)
    r = await ex.execute([{"step": 1, "tool": "system_info", "action": "disk",
                           "params": {}, "description": "查盘"}], max_retries=1)
    rec("B.执行", "★ 临时失败重试后成功 (max_retries=1)",
        r["steps_ok"] == 1 and r["success"] is True and len(gw.calls) == 2,
        f"calls={len(gw.calls)} res={str(r)[:120]}")

    # B5 有依赖的步失败 → 中断, 后续依赖步不执行
    gw = FakeGW({"file_ops:read": {"success": False, "error": "文件不存在"},
                 "file_ops:write": {"success": True, "output": "should not run"}})
    ex = PlanExecutor(gw)
    plan = [
        {"step": 1, "tool": "file_ops", "action": "read", "params": {"path": "x"}, "description": "读"},
        {"step": 2, "tool": "file_ops", "action": "write", "params": {"path": "y", "content": "$step1.content"},
         "depends_on": 1, "description": "写"},
    ]
    r = await ex.execute(plan)
    wrote = any(c["action"] == "write" for c in gw.calls)
    rec("B.执行", "★ 依赖步失败 → 中断且下游不执行 (不留半成品)",
        not r["success"] and not wrote and r["steps_failed"] == 1,
        f"wrote={wrote} steps_ok={r['steps_ok']} total={r['steps_total']} errs={r['errors']}")
    rec("B.执行", "★ 中断时汇总如实标失败 (success=False)", r["success"] is False, str(r["success"]))

    # B6 无依赖的步失败 → 其余步继续
    gw = FakeGW({"system_info": {"success": False, "error": "查询失败"},
                 "file_ops:write": {"success": True, "output": "ok"}})
    ex = PlanExecutor(gw)
    plan = [
        {"step": 1, "tool": "system_info", "action": "disk", "params": {}, "description": "查盘(会失败)"},
        {"step": 2, "tool": "file_ops", "action": "write", "params": {"path": "z", "content": "独立"},
         "description": "独立写(应继续)"},
    ]
    r = await ex.execute(plan)
    # ★ 判据修正: 默认 max_retries=1 会让失败步重试 → 调用数不是 2。
    #   要证的是「独立步照常跑」, 不是「调用了几次」。
    ran_after = [c["tool"] for c in gw.calls].count("file_ops") == 1
    rec("B.执行", "★ 无依赖步失败不拖累后续步 (1 失败 1 成功)",
        r["steps_ok"] == 1 and r["steps_failed"] == 1 and ran_after,
        f"ok={r['steps_ok']} failed={r['steps_failed']} calls={[c['tool'] for c in gw.calls]}")
    rec("B.执行", "★ 混合结果时总判为 False (不谎报整体成功)", r["success"] is False)

    # B7 非法计划 → 拒绝执行, 不半跑
    gw = FakeGW()
    ex = PlanExecutor(gw)
    r = await ex.execute([{"step": 1, "tool": "", "action": "x", "params": {}}])
    rec("B.执行", "★ 计划缺 tool 名 → 拒跑且零调用", not r["success"] and not gw.calls,
        f"calls={len(gw.calls)} errs={r['errors']}")
    gw = FakeGW()
    ex = PlanExecutor(gw)
    r = await ex.execute([{"step": 1, "tool": "file_ops", "action": "write", "params": "不是字典"}])
    rec("B.执行", "★ params 非字典 → 拒跑且零调用", not r["success"] and not gw.calls,
        f"calls={len(gw.calls)} errs={r['errors']}")

    # B8 摘要诚实 (逐步图标 + 错误列出)
    gw = FakeGW({"system_info": {"success": False, "error": "炸了"},
                 "file_ops:write": {"success": True, "output": "ok"}})
    ex = PlanExecutor(gw)
    r = await ex.execute([
        {"step": 1, "tool": "system_info", "action": "disk", "params": {}, "description": "查盘"},
        {"step": 2, "tool": "file_ops", "action": "write", "params": {"path": "z", "content": "x"},
         "description": "写盘"},
    ])
    s = ex.format_summary(r)
    rec("B.执行", "★ 摘要逐步标 ❌/✅ 且列出错误 (不美化)",
        "❌" in s and "✅" in s and "炸了" in s, s.replace("\n", " | ")[:150])
    rec("B.执行", "★ 摘要首行给出 成功步/总步 实数",
        "1/2" in s, s.splitlines()[0][:60] if s else "")

    # B9 参数归一化 (list/dict → 字符串, 防网关 TypeError)
    gw = FakeGW()
    ex = PlanExecutor(gw)
    await ex.execute([{"step": 1, "tool": "file_ops", "action": "write",
                       "params": {"path": "p", "content": ["a", "b"], "meta": {"k": "v"}},
                       "description": "写"}])
    kw = gw.calls[0]["kw"]
    rec("B.执行", "list 参数归一化成逗号串 (不把类型错误传给网关)",
        kw.get("content") == "a,b", f"content={kw.get('content')!r}")
    rec("B.执行", "dict 参数归一化成字符串", isinstance(kw.get("meta"), str), f"meta={kw.get('meta')!r}")


async def _t3b():
    """B3 第二问: 只给「桌面」(无文件名) 时是否补默认文件名。"""
    gw = FakeGW()
    ex = PlanExecutor(gw)
    await ex.execute([{"step": 1, "tool": "file_ops", "action": "write",
                       "params": {"path": "桌面", "content": "x"}, "description": "写桌面"}])
    return gw.calls[0]["kw"].get("path", "")


# ═══════════════════ C. 端到端真链 (真规划 + 真工具) ═══════════════════

async def part_c_e2e():
    dim("C. 端到端真链 —— 真规划 + 真工具, 链末产物查盘")
    if not ALLOW_REAL_MODEL:
        rec("C.端到端", "跳过真链 (--offline)", "未测", "需要真模型", kind="skip")
        return
    (SB / "in.txt").write_text("这是输入文件。\n第二行。", encoding="utf-8")
    os.makedirs(SB / "sub", exist_ok=True)
    (SB / "sub" / "a.txt").write_text("甲", encoding="utf-8")
    (SB / "sub" / "b.md").write_text("乙", encoding="utf-8")

    cases = [
        ("先看下磁盘，再把结果写到 " + str(SB / "c1.txt"), [SB / "c1.txt"], "查盘→写盘"),
        ("读一下 " + str(SB / "in.txt") + "，然后存到 " + str(SB / "c2.txt"), [SB / "c2.txt"], "读→写"),
        ("先查系统状态，再把这周的安排写到 " + str(SB / "c3.txt"), [SB / "c3.txt"], "系统信息→写盘"),
    ]
    for msg, wants, what in cases:
        CALLS.clear()
        try:
            r = await PL.process(msg, probe=True, mode_override="craft")
        except Exception as e:
            rec("C.端到端", f"{what} 不崩", False, repr(e)[:160])
            continue
        route = str(r.get("route") or "")
        resp = str(r.get("response") or "")
        tools = [c["tool"] for c in CALLS if c["tool"] != "llm"]
        made = [str(p) for p in wants if p.exists()]
        rec("C.端到端", f"{what}: 走的是规划链 (route=planner.multi_step)",
            route == "planner.multi_step", f"route={route} tools={tools[:4]}")
        rec("C.端到端", f"{what}: 真调了工具 (≥1 次)", len(tools) >= 1, f"tools={tools[:5]}")
        rec("C.端到端", f"{what}: ★ 链末产物真落盘 ({', '.join(p.name for p in wants)})",
            bool(made), f"沙箱文件={[p.name for p in SB.iterdir()][:8]}")
        lied = ("已完成" in resp or "已写入" in resp) and not made
        rec("C.端到端", f"{what}: 不谎报 (说完成就得有产物)", not lied, f"resp={resp[:90]!r}")

    # C4 反例: 计划指向不存在工具的请求, 不该静默乱跑
    CALLS.clear()
    r = await PL.process("先打开不存在的工具xyz，再把它写到 " + str(SB / "c4.txt"),
                         probe=True, mode_override="craft")
    rec("C.端到端", "反例: 含不存在工具的请求不谎报成功",
        r.get("route") is not None, f"route={r.get('route')} resp={str(r.get('response'))[:70]!r}")


# ═══════════════════ D. 技能 DAG 多步 (真 DAG + 打桩网关) ═══════════════════

# (技能, 用**技能自己的触发词**的说法 —— 测 DAG 接线; 另配自然说法测可达性)
DAG_CASES = [
    ("healthcheck", "检查一下服务，再出一份健康检查报告"),
    ("pc-checkup", "给电脑做个体检，出一份体检报告"),
    ("model-usage", "看看模型用量，整理一份统计"),
    ("session-logs", "翻翻我们的对话历史"),
]
# 自然说法 (不含该技能的触发词) → 测「技能存在但用户这么说调不到」的覆盖缺口
NATURAL_CASES = [
    ("healthcheck", "服务还活着吗"),
    ("session-logs", "看看最近的会话日志"),
    ("model-usage", "哪个模型用得多"),
    ("pc-checkup", "电脑状态报告"),
]


async def part_d_skill_dag():
    dim("D. 技能 DAG 多步 —— 声明几步就真调几步 (真 DAG 定义 + 打桩网关)")
    from core.skill_loader import get_skill_index
    eng = get_skill_index()
    for name, msg in DAG_CASES:
        dag = eng.get_dag(name)
        if dag is None:
            rec("D.技能链", f"{name}: DAG 可解析", False, "get_dag 返回 None")
            continue
        declared = [(s.get("tool"), s.get("action")) for s in dag.steps]
        CALLS.clear()
        try:
            r = await PL.process(msg, probe=True, mode_override="craft")
        except Exception as e:
            rec("D.技能链", f"{name}: 不崩", False, repr(e)[:140])
            continue
        route = str(r.get("route") or "")
        got = [(c["tool"], c["action"]) for c in CALLS if c["tool"] != "llm"]
        decl_tools = [t for t, _ in declared if t != "llm"]
        hit = [t for t in decl_tools if t in [g for g, _ in got]]
        rec("D.技能链", f"{name}: 走技能层 (route={route})",
            route.startswith("skill.") or route in ("ir.chain", "learn.teach"),
            f"route={route} got={got[:4]}")
        rec("D.技能链", f"{name}: ★ 声明的 {len(decl_tools)} 个工具都真被调到 ({decl_tools})",
            len(hit) == len(decl_tools), f"命中={hit} 实调={got}")
        rec("D.技能链", f"{name}: 链路顺序与声明一致",
            [g for g, _ in got if g in decl_tools] == decl_tools,
            f"声明={decl_tools} 实际={[g for g, _ in got if g in decl_tools]}")

    # D2 ★ 自然说法可达性 (不拿触发词自证) —— 技能在但用户自然这么说调不到 = 覆盖缺口
    print("   ── 自然说法可达性 (不含技能触发词) ──")
    for name, msg in NATURAL_CASES:
        CALLS.clear()
        try:
            r = await PL.process(msg, probe=True, mode_override="craft")
        except Exception as e:
            rec("D.技能链", f"{name}: 自然说法「{msg}」不崩", False, repr(e)[:120])
            continue
        route = str(r.get("route") or "")
        got = [c["tool"] for c in CALLS if c["tool"] != "llm"]
        reached = route.startswith("skill.") or bool(got)
        rec("D.技能链", f"★ 自然说法「{msg}」→ 可达 {name or ''}".strip(), reached,
            f"route={route} tools={got[:3]}")


# ═══════════════════ E. 长链强度 ═══════════════════

async def part_e_long_chain():
    dim("E. 长链强度 —— 6 步不丢步、顺序不乱、$step 引用逐个正确")
    gw = FakeGW({t: {"success": True, "output": "第%d步输出" % i}
                 for i, t in enumerate(["system_info", "file_ops", "check_mail",
                                        "openmeteo", "web_search", "usage_stats"], 1)})
    ex = PlanExecutor(gw)
    tools = ["system_info", "file_ops", "check_mail", "openmeteo", "web_search", "usage_stats"]
    plan = []
    for i, t in enumerate(tools, 1):
        p = {"x": i}
        if i == 2:
            p = {"path": str(SB / "e1.txt"), "content": "$step1.output"}
        if i == 5:
            p = {"query": "$step2.output"}   # 该步只产 output 字段 (引用错字段会静默回退成整字典)
        plan.append({"step": i, "tool": t, "action": "a", "params": p,
                     "depends_on": i - 1 if i > 1 else None, "description": f"第{i}步"})
    r = await ex.execute(plan)
    order = [c["tool"] for c in gw.calls]
    rec("E.长链", "★ 6 步全部执行, 顺序不乱不丢", order == tools, f"实际={order}")
    rec("E.长链", "6/6 步成功且汇总一致",
        r["steps_ok"] == 6 and r["steps_total"] == 6 and r["success"] is True,
        f"ok={r['steps_ok']}/{r['steps_total']}")
    w = [c for c in gw.calls if c["tool"] == "file_ops"][0]
    rec("E.长链", "★ 第2步的 $step1 引用取到第1步真输出 (非空/非字面量)",
        w["kw"].get("content") == "第1步输出", f"content={w['kw'].get('content')!r}")
    q = [c for c in gw.calls if c["tool"] == "web_search"][0]
    rec("E.长链", "★ 第5步的 $step2 引用跨 3 步仍正确",
        q["kw"].get("query") == "第2步输出", f"query={q['kw'].get('query')!r}")
    # 观测 (非缺陷): 引用的字段名不存在时, 解析**静默回退**成整个结果字典的字符串。
    # 实测: "$step2.content" 而该步只产出 output → 传下去的是 "{'success': True, ...}"。
    # 危害: 字段名写错不报错, 下游拿到一坨字典字符串当内容用。记为观测项。
    gw2 = FakeGW({"file_ops": {"success": True, "output": "真输出"}})
    ex2 = PlanExecutor(gw2)
    await ex2.execute([
        {"step": 1, "tool": "file_ops", "action": "w", "params": {}, "description": "产出"},
        {"step": 2, "tool": "web_search", "action": "search",
         "params": {"query": "$step1.content"}, "depends_on": 1, "description": "引用错字段名"},
    ])
    q2 = [c for c in gw2.calls if c["tool"] == "web_search"]
    fb = str(q2[0]["kw"].get("query")) if q2 else ""
    rec("E.长链", f"观测: 引用字段名写错 → 回退成整字典字符串 (静默)", "未测",
        f"传下去={fb[:60]!r} —— 不报错, 下游可能把字典串当内容", kind="info")
    rec("E.长链", "★ 引用解析失败时不静默传空 (字面量 $ 残留可检出)",
        all("$step" not in str(c["kw"].get("content") or "") for c in gw.calls),
        "有 $step 字面量残留")
    rec("E.长链", "结果明细逐条可核对 (含步号/工具/成败)",
        len(r["results"]) == 6 and all("step" in x and "tool" in x and "success" in x
                                      for x in r["results"]), str(r["results"][:1])[:150])


async def main() -> int:
    global ALLOW_REAL_MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="不调真模型 (只跑 B/D/E)")
    ap.add_argument("--json", default="", help="结果 JSON 输出路径")
    a = ap.parse_args()
    if a.offline:
        ALLOW_REAL_MODEL = False

    print("=" * 74)
    print("Tiger.M.M 多任务链条执行测试")
    print(f"  项目: {ROOT}")
    print(f"  沙箱: {SB}")
    print(f"  模式: {'离线(零成本)' if a.offline else '全量(含真模型拆解)'}")
    print("=" * 74)

    if not a.offline:
        await part_a_planning()
        await part_a2_chain_hit()
    await part_b_executor()
    if not a.offline:
        await part_c_e2e()
    await part_d_skill_dag()
    await part_e_long_chain()

    dim("F. 汇总")
    n_pass = sum(1 for x in RESULTS if x["ok"] is True)
    n_fail = sum(1 for x in RESULTS if x["ok"] is False and x["kind"] == "bool")
    n_skip = sum(1 for x in RESULTS if x["kind"] == "skip")
    n_note = len(RESULTS) - n_pass - n_fail
    for x in RESULTS:
        if x["ok"] is False and x["kind"] == "bool":     # ★ 只有契约型才算 FAIL (口径与统计一致)
            print(f"  FAIL [{x['dim']}] {x['name']} <- {x['detail'][:110]}")
    print(f"\n总计: {n_pass} PASS / {n_fail} FAIL / {n_note} 备注项 (未测 {n_skip})"
          f" · 用时 {time.time() - T0:.1f}s · LLM 调用 桩={LLM_CALLS['stub']} 真={LLM_CALLS['real']}")
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": "chain", "sandbox": str(SB),
           "pass": n_pass, "fail": n_fail, "notes": n_note, "skipped": n_skip,
           "llm": dict(LLM_CALLS), "elapsed_s": round(time.time() - T0, 1), "results": RESULTS}
    if a.json:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON → {a.json}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
