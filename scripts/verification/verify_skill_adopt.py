"""技能"取形式重编" (core/skill_adopt.py + /depot skill-*) — 常驻验证器

为什么要有这一份: 第 4 步是"把外面的技能做成我们自己的"。
它最容易犯两种错, 都必须拦住:
  ① **搬了别人的实现** —— 直接抄外部工具名/URL/路径/代码进我们的技能 (既不安全也不算"自己的")
  ② **做成了别人的技能却没生效/没把关** —— 绕开我们自己的技能工厂四道门

  A ★★ 抽象化: 外部实现细节 (URL/路径/代码/安装命令/文件名) 一律剥掉
  B ★ 形式提取三档: 真 SKILL.md frontmatter > 仓库简介 > README 概述段
  C ★★ 安全闸: 许可证不合格 (copyleft/无) → 不采用 (连形式都不碰)
  D ★★ 真跑接线: /depot skill-assess + skill-adopt (联网则真跑; 不通 SKIP)
  E ★★ 不搬他人: 交给工厂的描述里**没有**外部工具名/URL/路径
  F ★ 闭环: 重编失败 → 记进缺口台账 (出去找的结论不散掉)
  G ★ 变异: 拆许可证闸 / 拆抽象化 → 必红
  H 零副作用: 真台账/对话库/已装清单指纹不变 · 候选池无残留 (验证不 approve)
"""
import asyncio
import hashlib
import importlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F, S = [], [], []
REAL_GAP = ROOT / "data" / "gap_ledger.db"
REAL_CHAT = ROOT / "data" / "chat_sessions.db"
REAL_INST = ROOT / "data" / "depot_installed.json"


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def skip(name, why=""):
    S.append(name)
    print(f"  SKIP {name}   ({why})")


def _fp(p: Path, tbl=None):
    try:
        if tbl:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                return (c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0],
                        c.execute(f"SELECT COALESCE(MAX(id),0) FROM {tbl}").fetchone()[0])
            finally:
                c.close()
        return (p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest()[:12]) if p.is_file() else None
    except Exception:
        return None


def _net(host="api.github.com", port=443, t=4):
    try:
        socket.create_connection((host, port), timeout=t).close()
        return True
    except Exception:
        return False


def _gh_ok():
    """GitHub **API 可用** (不只是网络通): 未认证限流 60/h, 用完了必须 SKIP 而不是 FAIL。"""
    try:
        from core import depot as DP
        d = DP._get_json("https://api.github.com/rate_limit")
        return (d.get("resources", {}).get("core", {}).get("remaining", 0) or 0) > 3
    except Exception:
        return False


EXTERNAL = """# Web Report Generator

[![Build](https://img.shields.io/badge/x.svg)](https://ci.example.com)

Fetch data from https://api.example.com/v1/data, summarize it, write to D:/work/report.md

```python
import requests
df = requests.get('https://cdn.example.com/x.csv')
open('C:/tmp/o.csv','w').write(df.text)
```
pip install requests pandas
"""

SKILL_MD = """---
name: demo
description: "Generates demos as GIFs. Use when the user wants terminal recordings."
tags: [演示, 录屏]
---
# Demo

Parse the tape file, generate the GIF, then write it to disk.
"""


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
    pl.sessions = SessionStore(sandbox / "sessions.db")
    return pl


def main() -> int:
    SB = Path(tempfile.mkdtemp(prefix="_vfy_sadopt_"))
    gap_b, chat_b, inst_b = (_fp(REAL_GAP, "gaps"), _fp(REAL_CHAT, "messages"), _fp(REAL_INST))
    cand_before = sorted(p.name for p in (ROOT / "skill_candidates").glob("*")) \
        if (ROOT / "skill_candidates").is_dir() else []
    try:
        from core import skill_adopt as SA
        from core.skill_adopt import (ACTION_WORDS, abstract_actions, assess_external,
                                     describe_form, parse_skill_md, render_plan)
        pl = build(SB)
        net = _net() and _gh_ok()

        # ── A ★★ 抽象化 ──
        print("        —— A ★★ 只取形式, 剥掉实现细节 ——")
        acts, cap = abstract_actions(EXTERNAL)
        joined = (cap + " " + " ".join(acts)).lower()
        chk("URL 被剥掉", "http" not in joined, joined[:120])
        chk("路径被剥掉", "d:/work" not in joined and "c:/tmp" not in joined, joined[:120])
        chk("代码块被剥掉", "```" not in cap and "import " not in cap, cap)
        chk("安装命令被剥掉", "pip install" not in joined, joined[:120])
        chk("徽章不被当能力", "![" not in cap and "shields.io" not in cap, cap)
        img_cap = abstract_actions("![Logo](https://x.example/a.png)\n")[1]
        chk("★ 图片行不被当能力 (只有 ! 守卫能拦的形态)", "![" not in img_cap, repr(img_cap))
        chk("动作来自抽象词表", set(acts) <= set(ACTION_WORDS), str(acts))
        chk("动作按原文顺序", acts.index("fetch") < acts.index("write") if
            ("fetch" in acts and "write" in acts) else True, str(acts))

        # ── B 三档形式 ──
        print("        —— B ★ 形式提取三档 ——")
        r1 = parse_skill_md(SKILL_MD)
        chk("① 真 SKILL.md: 认得出", r1["is_skill_md"])
        chk("① description 只取第一句 (砍掉 Use when 说明)",
            r1["description"] == "Generates demos as GIFs.", repr(r1["description"]))
        chk("① 正文抽到动作 (Claude 风格无 steps 字段)",
            any(a in r1["steps"] for a in ("generate", "write", "parse")), str(r1["steps"]))
        chk("① 动作按正文顺序", r1["steps"].index("parse") < r1["steps"].index("generate")
            if ("parse" in r1["steps"] and "generate" in r1["steps"]) else True, str(r1["steps"]))
        p1 = assess_external({"name": "x", "license": "MIT", "text": SKILL_MD,
                              "summary": "仓库简介在此", "paths_found": ["skills/x/SKILL.md"]})
        chk("① SKILL.md 描述优先于仓库简介",
            p1.capability.startswith("Generates demos"), p1.capability)
        p2 = assess_external({"name": "x", "license": "MIT", "text": EXTERNAL, "summary": ""})
        chk("② 无 SKILL.md: 退到 README 概述段", bool(p2.capability) and "http" not in p2.capability,
            p2.capability)
        p3 = assess_external({"name": "x", "license": "MIT", "text": "",
                              "summary": "search the web then write a report"})
        chk("③ 只有简介: 动作也能从简介抽", "search" in (p3.steps_hint + []) and "write" in (p3.steps_hint + []),
            str(p3.steps_hint))

        # ── C ★★ 安全闸 ──
        print("        —— C ★★ 安全闸 (许可证) ——")
        chk("MIT → 可采用", not assess_external({"name": "x", "license": "MIT", "text": EXTERNAL}).blocked)
        chk("★ copyleft → 不采用", assess_external({"name": "x", "license": "GPL-3.0",
                                                  "text": EXTERNAL}).blocked)
        chk("★ 无许可证 → 不采用", assess_external({"name": "x", "license": "",
                                                  "text": EXTERNAL}).blocked)
        chk("★ 认不出的许可证 → 不采用", assess_external({"name": "x", "license": "Custom 1.0",
                                                      "text": EXTERNAL}).blocked)
        chk("archived → 只警告不拦", not assess_external({"name": "x", "license": "MIT",
                                                       "text": EXTERNAL, "archived": True}).blocked)
        # ★ 实测踩到: GitHub 限流 → 许可证读不到 → 旧代码报"没有许可证"(假结论)。
        #   "读不到" 与 "真的没有" 必须分开说 (项目铁律: 失败要诚实)。
        p_un = assess_external({"name": "x", "license": "", "text": EXTERNAL,
                                "license_error": "HTTPError: 403 rate limit exceeded"})
        chk("★ 许可证读不到 → 说'读不到'而不是'没有'",
            p_un.blocked and any("读不到" in r for r in p_un.reasons)
            and not any("许可证不明/没有" in r for r in p_un.reasons), str(p_un.reasons))
        chk("读不到也仍然拦 (无法判定不放行)", p_un.blocked)

        # ── E ★★ 不搬他人 ──
        print("        —— E ★★ 交给工厂的描述不含外部细节 ——")
        pp = assess_external({"name": "x", "license": "MIT", "text": EXTERNAL})
        d = describe_form(pp.capability, pp.steps_hint)
        low = d.lower()
        leaks = [b for b in ("http", "api.example", "d:/work", "c:/tmp", "pip install",
                             "requests", "```") if b in low]
        chk("★★ 描述里没有外部 URL/路径/工具名/代码", not leaks, f"泄漏: {leaks}")
        chk("描述要求我们自己的风格", "我们自己风格的" in d, d[:80])
        chk("描述要求触发词具体 + 参数非必填", "触发词" in d and "非必填" in d)

        # ── D ★★ 真跑接线 ──
        print("        —— D ★★ 真跑 /depot skill-* ——")
        o = asyncio.run(pl.process("/depot", mode_override="craft")).get("response") or ""
        chk("help 列 skill-assess / skill-adopt", "skill-assess" in o and "skill-adopt" in o)
        chk("help 说明只读文本/不搬文件", ("不搬文件" in o) or ("只读文本" in o))
        u = asyncio.run(pl.process("/depot skill-assess", mode_override="craft")).get("response") or ""
        chk("无参数给用法", "用法" in u)
        if net:
            t = asyncio.run(pl.process("/depot skill-assess daymade/claude-code-skills#cli-demo-generator",
                                       mode_override="craft")).get("response") or ""
            # ★ 2026-09-20 修 (第二层脆弱): "真网络"段必须能区分**上游打嗝**与**我们坏了**。
            #   实测背景: GitHub 未授权 API 限额 60/小时, 而本门一轮要真抓 4 次 ——
            #   撞上限后响应退化成抓不到 → 断言报红, 但那不是我们的回归
            #   (同一分钟手工连抓两次都拿到正确响应, 只是每次 ~40s)。
            #   判据: 响应里出现"上游问题"字样 **且** 没有我们的成功标记 → SKIP(带原因);
            #        有我们的标记 → 照常严格断言; 两者都不是 → 真 FAIL(代码路径坏了)。
            def _upstream_state(s: str) -> str:
                """这次真抓到底处于哪个状态 → 'ok' / 'degraded' / 'broken' (三态)。

                ★ 判据(**必须有我们自己的素材标记**才算真抓到):
                  成功 = 响应里出现 "素材是真 SKILL.md"(真解析了文件) 或 "只读了纯文本"
                  降级 = 没有成功标记, 且(命中故障关键词 或 响应里**一点素材说明都没有**)
                  坏了 = 没有成功标记, **但**有素材说明 / 有异常 → 我们自己的解析或断言路径出问题

                ★ 2026-09-22 修 (v32 实测假红): 原判据是"命中故障关键词才算降级, 否则真 FAIL" ——
                  而实测降级响应 **一个故障词都没有**, 只有一行结论
                  `✓ 可重编  <repo>  —— 只取形式, 自己重编` (许可门降级后仍打这行, 本函数上方的
                  注释早就记下了这个现象)。于是它落到"真 FAIL"分支 → 门禁 47 PASS / 2 FAIL,
                  而**单跑同一门禁是 42 PASS / 0 FAIL / 2 SKIP** (串扰+上游限额, 不是代码回归)。
                  修法用"正向要求"而不是继续加关键词 —— 关键词表永远补不全。
                """
                if ("素材是真 SKILL.md" in s) or ("只读了纯文本" in s):
                    return "ok"
                if ("Traceback" in s) or ("未捕获" in s) or ("Internal Server Error" in s):
                    return "broken"
                hiccup = ("rate limit", "API rate", "403", "限流", "超时", "timeout",
                          "取不到", "无法访问", "网络不通", "连不上", "读不到")
                if any(h in s for h in hiccup):
                    return "degraded"
                # 没有成功标记、也没有故障词: 再看它有没有"素材说明"这层证据。
                # 素材说明长这样: "素材是真 SKILL.md…" / "素材含 SKILL.md…" / "素材只有 … (README 级)"
                has_material = ("素材" in s) or ("README 级" in s)
                return "broken" if has_material else "degraded"

            if _upstream_state(t) != "ok":
                # 上游打嗝/降级 → 这整段(真抓)全部 SKIP 带原因; 不假装绿, 也不冤枉自己
                _why = "上游限流/降级" if _upstream_state(t) == "degraded" else "我们自己的抓取/解析路径异常"
                for _nm in ("真跑 skill-assess: 认出真 SKILL.md",
                            "真跑 skill-assess: 能力形式非空",
                            "真跑 skill-assess: 能力形式只取形式",
                            "真跑 skill-assess: 只读纯文本契约",
                            "真跑 skill-assess: 仓库级提示",
                            "真跑 skill-assess: 不存在技能要诚实"):
                    skip(_nm, f"{_why}: {t[:60]}")
                if _upstream_state(t) == "broken":
                    chk("★ 真抓拿到素材却没解析出 (这不是上游问题, 是我们坏了)", False, t[:160])
                _skip_live = True
            else:
                _skip_live = False
                chk("★ 真跑: 认出真 SKILL.md", "SKILL.md" in t, t[:160])
                # ★ 2026-09-20 修 (真脆弱点): 原断言是
                #     chk("★ 真跑: 能力是它的 description 首句",
                #         "Generates professional animated CLI demos" in t, ...)
                #   —— 它把**第三方仓库的原文**当判据。上游一改描述 / 抓取回退,
                #   这条就红 (实测: 并行两轮全量时它就 FAIL 过一次, 单跑又绿)。
                #   判据应该是**我们自己的行为契约**, 不是别人的字符串:
                #     ① 真抓到 description 并作为"能力形式"输出 (非空)
                #     ② 只取形式 —— 该行内不得出现上游 URL / 仓库名 / 署名
                #     ③ 只读文本, 不下载文件
                # ★ 判据只用**同一行**的值: 曾用 `\s*(.+)`, 而 \s 会吃掉换行 →
                #   能力为空时把**下一行**("只读了纯文本…") 当成能力值, 空值也能过 (自测抓出)。
                _m_cap = re.search(r"能力形式[:：][ \t]*([^\n]+)", t)
                _cap = (_m_cap.group(1).strip() if _m_cap else "")
                chk("★ 真跑: 能力形式 = 抓到的 description (非空首句)",
                    bool(_cap) and len(_cap) >= 10, t[:200])
                chk("★ 能力形式只取形式 (不含上游 URL/仓库名/署名)",
                    bool(_cap) and not re.search(r"https?://|daymade|claude-code-skills", _cap), _cap[:120])
                chk("★ 只读纯文本 (没下载任何文件)",
                    "只读了纯文本" in t and "没下载任何文件" in t, t[-160:])
            t_repo = asyncio.run(pl.process("/depot skill-assess daymade/claude-code-skills",
                                            mode_override="craft")).get("response") or ""
            if not _skip_live:
                # ★ 2026-09-20 修 (门禁脆弱点): 这条提示行依赖"能取到 SKILL.md 清单"
                #   (未认证限流 60/h 时取不到 → 提示行就不存在 → 断言红, 而代码是对的)。
                #   现在: 拿不到清单就 SKIP 带原因 (不假装绿也不冤枉自己)。
                #   (根因也已治: core/depot.py 现在能用 token → 限额 60→5000/h)
                if "#技能名" in t_repo and "SKILL.md" in t_repo:
                    chk("★ 真跑: 仓库级评估提示可用 #技能名 指定", True)
                elif t_repo.strip():
                    skip("★ 真跑: 仓库级评估提示可用 #技能名 指定",
                         f"取不到 SKILL.md 清单(上游限流/网络), 无法验证该提示: {t_repo[:70]}")
                else:
                    chk("★ 真跑: 仓库级评估提示可用 #技能名 指定", False, "(空响应)")
            # 不存在的技能 → 诚实, 且**不得**给出仓库级"可重编"结论
            t2 = asyncio.run(pl.process("/depot skill-assess daymade/claude-code-skills#no-such-skill-xyz",
                                        mode_override="craft")).get("response") or ""
            if not _skip_live:
                # ★ 2026-09-20 修 (门禁自身脆弱点 + 代码真缺陷):
                #   原断言只查 "没有叫" 是否**出现在响应任意位置** —— 而旧代码是把这句当
                #   "素材说明"附注挂在**仓库级结论**后面, 且响应经过 LLM 复述 → 附注时有时无,
                #   门禁时红时绿 (v6 绿 / v7 红, 代码没变)。
                #   现在代码改为主结论**短路**(诚实的拒绝成为首行), 所以断言可以钉确定性契约:
                #     ① 明确说没有这个技能  ② **不得**给"可重编"这种仓库级正面结论
                # ★ 2026-09-22 修 (门禁自己会假红): 这两条断言要求**确定性结论**
                #   ("没有叫 X 的技能"), 但结论只有在**上游数据读到了**才可能给出。
                #   读不到时正确行为是诚实说"读不到"并**不下结论** —— 那不是代码坏,
                #   却被原判据判成 FAIL (实测 v39: 红 1 条, 那一刻元数据读不到)。
                #   所以先判上游状态: 读不到 → SKIP 带原因; 读到了才钉契约。
                _UNREAD = ("读不到", "取不到", "无法判定", "限流", "rate limit", "超时", "timeout")
                if any(k in t2 for k in _UNREAD) and "没有叫" not in t2:
                    skip("★ 指定不存在的技能 → 诚实说没有 (上游数据读不到, 不下结论)",
                         t2[:90])
                else:
                    chk("★ 指定不存在的技能 → 诚实说没有这个技能 (不悄悄换成别的)",
                        "没有叫" in t2 and "可重编" not in t2, t2[:200])
                _t2a = asyncio.run(pl.process("/depot skill-adopt daymade/claude-code-skills#no-such-skill-xyz",
                                              mode_override="craft")).get("response") or ""
                if any(k in _t2a for k in _UNREAD) and "没有叫" not in _t2a:
                    skip("★ 采用不存在的技能 → 不予重编 (上游数据读不到, 不下结论)", _t2a[:90])
                else:
                    chk("★ 采用不存在的技能 → 不予重编 (不拿别的技能顶替)",
                        "没有叫" in _t2a and "不予重编" in _t2a, _t2a[:200])
        else:
            skip("真跑 skill-assess", "网络不通")

        # ── C2 adopt 闸门 (真跑负例) ──
        print("        —— C2 ★ 真跑: 许可证不合格不得重编 ——")
        if net:
            t3 = asyncio.run(pl.process("/depot skill-adopt anthropics/skills", mode_override="craft")).get("response") or ""
            chk("★★ 无许可证仓库 → 不予重编 (闸门有牙齿)",
                ("不予重编" in t3) or ("不采用" in t3), t3[:200])
        else:
            skip("真跑 skill-adopt 负例", "网络不通")

        # ── F 闭环 ──
        print("        —— F ★ 闭环: 重编失败记账 ——")
        src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
        chk("skill-adopt 失败分支接了 gap_ledger",
            "gap record (skill-adopt) failed" in src and "depot.skill-adopt" in src)
        led = pl.gap_ledger
        n0 = led.stats()["total"]
        hit = led.record("[外部形式] 录屏演示", "✗ demo 本地重编失败: step 'render': [WinError 2] 系统找不到指定的文件",
                         "depot.skill-adopt")
        chk("★ 工厂失败原文(命令不存在)被正确分类为缺依赖",
            bool(hit) and hit.get("category") == "missing_dep", str(hit))
        chk("★ 记进账本了", led.stats()["total"] == n0 + 1)
        from core.self_rescue import plan as rescue_plan
        pl2 = rescue_plan(led.get(hit["id"]))
        chk("★ 自救方案能给出装法", pl2["local"] and (pl2["hint"] or ""), str(pl2))

        # ── G ★ 变异 ──
        print("        —— G ★ 变异 ——")
        tgt = ROOT / "core" / "skill_adopt.py"
        orig = tgt.read_bytes(); h0 = hashlib.md5(orig).hexdigest()
        nl = "\r\n" if b"\r\n" in orig else "\n"
        s = orig.decode("utf-8").replace("\r\n", "\n")

        def case(label, old, new, bad):
            if s.count(old) != 1:
                print(f"  (跳过 锚点{s.count(old)}: {label})"); return
            tgt.write_bytes(s.replace(old, new, 1).replace("\n", nl).encode("utf-8"))
            importlib.reload(SA)
            caught = bool(bad(SA))
            tgt.write_bytes(orig); importlib.reload(SA)
            chk(f"★ 变异被抓住: {label}", caught)

        # ★ 锚点要跟着代码走: 加了 license_error 分支后 `if lc == "bad"` 变成 `elif`,
        #   旧锚点失效 → 变异被"跳过"(等于那条守卫没被验)。锚点漂移本身就是隐患。
        case("拆许可证闸 (认不出也放行)",
             '    elif lc == "bad":\n        p.blocked = True',
             '    elif False:\n        p.blocked = True',
             lambda m: not m.assess_external({"name": "x", "license": "Custom 1.0",
                                              "text": EXTERNAL}).blocked)
        # ★ 这一条变异**不改"拦不拦"**(拆掉分支后仍会被 lc=="bad" 拦住), 改的是**说法**:
        #   拆掉后会把"读不到"说成"没有许可证" = 假结论。所以断言要落在**说法**上,
        #   否则变异"看起来无害"(第一版就是这么漏掉的)。
        case("拆'读不到'闸 (读不到被说成'没有')",
             '    if meta.get("license_error") and not lic:\n        # ★ 读不到 → 说"读不到"',
             '    if False:\n        # ★ 读不到 → 说"读不到"',
             lambda m: any("许可证不明/没有" in r for r in m.assess_external(
                 {"name": "x", "license": "", "text": EXTERNAL,
                  "license_error": "403 rate limit"}).reasons))
        case("拆抽象化 (URL 不再剥)",
             '(re.compile(r"https?://\\S+"), "<url>"),', '(re.compile(r"ZZZNOMATCHZZZ"), "<url>"),',
             lambda m: "http" in m._strip_externals(EXTERNAL).lower())
        # ★ 变异3 要选"**只有这条守卫能拦**"的形态:
        #   徽章 `[![Build](url)](url)` 同时会被"纯链接行"守卫拦住 → 拆掉 "!" 守卫也看不出差别
        #   (第一版变异就是这么被我自己的守卫重叠给掩盖了)。改用图片行 `![Logo](<url>)`:
        #   它**只**以 "!" 开头, 只有 "!" 守卫能拦。
        #   ★ 样本必须让**图片行是唯一一行** —— 否则后面那句话会把危害掩盖掉
        #     (第一版样本后面跟了一句正常说明, 变异后 cap 仍取到那句话 → 变异显得"无害",
        #      这是断言不够精确, 不是守卫没用; 实测确认过变异后图片行确实会当上能力)
        IMG_ONLY = "![Logo](https://x.example/a.png)\n"
        case("拆 '!' 守卫 (图片行被当能力)",
             '            if t.startswith(("!", "[![", "<url>", "<path>", "<code>", "<file>", "<install>",\n'
             '                             "|", ">", "==")):',
             '            if t.startswith(("[![", "<url>", "<path>", "<code>", "<file>", "<install>",\n'
             '                             "|", ">", "==")):',
             lambda m: "![" in m.abstract_actions(IMG_ONLY)[1])
        chk("变异后已还原", hashlib.md5(tgt.read_bytes()).hexdigest() == h0)
        chk("还原后许可证闸仍拦", SA.assess_external({"name": "x", "license": "GPL-3.0",
                                                    "text": EXTERNAL}).blocked)

        # ── H 零副作用 ──
        print("        —— H 零副作用 ——")
        chk("真缺口台账指纹未变", _fp(REAL_GAP, "gaps") == gap_b, f"{gap_b} -> {_fp(REAL_GAP, 'gaps')}")
        chk("真对话库指纹未变", _fp(REAL_CHAT, "messages") == chat_b)
        chk("真已装清单未变", _fp(REAL_INST) == inst_b)
        cand_after = sorted(p.name for p in (ROOT / "skill_candidates").glob("*")) \
            if (ROOT / "skill_candidates").is_dir() else []
        chk("候选池无新增 (验证不 approve 不产生正式技能)", cand_after == cand_before,
            f"新增 {[x for x in cand_after if x not in cand_before]}")
    finally:
        shutil.rmtree(SB, ignore_errors=True)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
