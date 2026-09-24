"""文本工具调用解析 (P0-A) — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景 (2026-09-24, 从 428 轮真实对话数出来):
    助手轮次 214 里有 **21 轮**的正文是一坨工具标签, 工具**从未执行**,
    用户看到的就是那坨 XML —— 全部落在 general 路由 (本地 mimo 17 次 · deepseek 4 次),
    其中 17 轮回复以标签开头、零正文。

真因: `core/pipeline.py` 的对话循环只认 OpenAI 结构化 `message.tool_calls`;
而**同一循环的提示词却要求模型"输出合法工具标签"** ⇒ 协议有头无尾, 解析器从未写过。

本门禁钉住修好的行为:
  A 解析器认 **实测两种形态** (function=/parameter= 与 DSML invoke/parameter)
  B **不误判** (普通文本 / 只提工具名 / 空串 / 半截标签 → 0 个调用)
  C 参数值类型归一 (数字→int, true/false→bool)
  D ★ 真库原文: 当年泄漏的那些消息, 现在必须解析得出来 (回放证据)
  E ★★ 端到端: 桩模型吐文本工具调用 → 工具**真被执行** + 回复**无裸露标签**
  F 判据保守: 闭合标签缺失的截断调用 → **不解析** (宁可不做, 不乱做)
  G 零污染: 全程 probe + 沙箱, 不写用户数据

跑法:
    python -B scripts/verification/verify_text_toolcall.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
DB = ROOT / "data" / "chat_sessions.db"
P, F, SKIP = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def load_pipeline_mod():
    spec = importlib.util.spec_from_file_location(
        "_pl_gate", ROOT / "core" / "pipeline.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_pl_gate"] = m
    spec.loader.exec_module(m)
    return m


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    os.chdir(str(ROOT))

    print("—— A 解析器认实测两种形态 ——")
    plm = load_pipeline_mod()
    f = plm.parse_text_tool_calls
    chk("A1 parse_text_tool_calls 存在", callable(f))

    PIPE = "\uff5c"
    dsm = PIPE + PIPE + "DSML" + PIPE + PIPE
    A = ("<tool_call>\n<function=web_search>\n"
         "<parameter=query>双色球 开奖</parameter>\n</function>\n</tool_call>")
    A2 = ("<function=file_ops>\n<parameter=action>write</parameter>\n"
          "<parameter=path>D:\\a\\b.docx</parameter>\n"
          "<parameter=content>正文</parameter>\n</function>")
    B = ("<" + dsm + "tool_calls><" + dsm + "invoke name=\"shell_exec\">"
         "<" + dsm + "parameter name=\"command\" string=\"true\">dir</"
         + dsm + "parameter></" + dsm + "invoke>")
    C = ("我需要搜索一下。<tool_call>\n<function=web_search>\n"
         "<parameter=query>2026073期 开奖号码</parameter>\n</function>\n</tool_call>")

    r = f(A)
    chk("A2 形态A(name+params) 解析出 1 个调用", len(r) == 1 and r[0]["name"] == "web_search", r)
    chk("A3 形态A 参数正确", r and r[0]["arguments"].get("query", "").startswith("双色球"), r)
    r = f(A2)
    chk("A4 形态A 多参数", r and set(r[0]["arguments"].keys()) == {"action", "path", "content"}, r)
    r = f(B)
    chk("A5 形态B(DSML) 解析出 1 个调用",
        len(r) == 1 and r[0]["name"] == "shell_exec", r)
    chk("A6 形态B 参数正确", r and r[0]["arguments"].get("command") == "dir", r)
    r = f(C)
    chk("A7 前言+调用 也能解析 (不丢正文外的调用)", len(r) == 1, r)

    print("—— B 不误判 ——")
    for label, txt in (("普通文本", "北京今天晴，26度。"),
                       ("只提工具名", "我可以用 web_search 帮你查，但需要你确认。"),
                       ("空串", ""),
                       ("半截标签", "让我调用工具标签来搜索")):
        chk(f"B 不误判 [{label}]", len(f(txt)) == 0, f(txt))

    print("—— C 值类型归一 ——")
    r = f('<function=file_ops><parameter=limit>20</parameter>'
          '<parameter=flag>true</parameter><parameter=path>D:\\x.txt</parameter></function>')
    a = (r[0]["arguments"] if r else {})
    chk("C1 数字→int", isinstance(a.get("limit"), int), a)
    chk("C2 true→bool", isinstance(a.get("flag"), bool), a)
    chk("C3 路径原样保留", a.get("path") == "D:\\x.txt", a)

    print("—— F 判据保守 (宁可不做, 不乱做) ——")
    trunc = "<tool_call>\n<function=file_ops>\n<parameter=action>read</parameter>"
    chk("F1 闭合标签缺失的截断调用 → 不解析", len(f(trunc)) == 0, f(trunc))

    print("—— D ★ 真库原文回放 (当年泄漏的消息现在必须解析得出) ——")
    if not DB.exists():
        SKIP.append("D 真库回放")
        print("SKIP D 真库回放  <- 找不到 chat_sessions.db")
    else:
        try:
            con = sqlite3.connect(f"file:{DB.as_posix()}?immutable=1", uri=True)
            rows = [x[0] for x in con.execute(
                "select content from messages where content like '%tool_call%' "
                "or content like '%invoke name%' order by id")]
            con.close()
            parsed = sum(1 for ct in rows if f(ct))
            chk(f"D1 真库命中的消息解析成功率 >= 90% "
                f"({parsed}/{len(rows)})", len(rows) > 0 and parsed >= 0.9 * len(rows),
                f"{parsed}/{len(rows)}")
        except Exception as e:
            # ★ 读不了真库属**环境状态**(如 -shm 陈旧), 不是代码缺陷 → SKIP 而非 FAIL,
            #   否则会把"库临时读不了"误报成本次改动回归 (踩过: 一次环境问题红了一整轮)。
            SKIP.append("D 真库回放")
            print(f"SKIP D 真库回放  <- 库当前读不了: {type(e).__name__}: {str(e)[:70]}")

    print("—— E ★★ 端到端: 工具真被执行 + 回复无裸露标签 ——")
    SBX = Path(tempfile.gettempdir()) / "_txttc_gate_sbx"
    shutil.rmtree(SBX, ignore_errors=True)
    SBX.mkdir(parents=True, exist_ok=True)
    os.environ["TMM_LIVE"] = "1"
    os.environ.pop("TMM_PROBE", None)
    for k, v in [("TMM_TASK_DB", "task_ledger.db"), ("TMM_SESSION_DB", "chat_sessions.db"),
                 ("TMM_SKILL_USAGE_FILE", "skill_usage.jsonl"), ("TMM_ARTIFACTS_FILE", "artifacts.jsonl"),
                 ("TMM_GAP_DB", "gap_ledger.db"), ("TMM_PERCEPTION_FILE", "perception.json"),
                 ("TMM_USAGE_LOG", "usage.jsonl"), ("TMM_SESSION_SNAPSHOT", "session_snapshot.json")]:
        os.environ[k] = str(SBX / v)
    _sd = None
    try:
        from _probe_env import activate
        _sd = activate()
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from core.knowledge import KnowledgeEngine
        from config.settings import DATA_DIR

        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl._ke = KnowledgeEngine(DATA_DIR)
        pl.modes.path = SBX / "mode.json"

        LEAK = ("<tool_call>\n<function=system_info>\n"
                "<parameter=action>time</parameter>\n</function>\n</tool_call>")
        calls, state = [], {"n": 0}

        _orig = pl.gateway.call

        async def spy(tool_name, **kw):
            calls.append(tool_name)
            return await _orig(tool_name, **kw)

        pl.gateway.call = spy

        async def stub(model_name, messages, temperature=0.7, max_tokens=4096,
                       tools=None, think=None):
            state["n"] += 1
            if state["n"] == 1:
                return {"text": LEAK, "tool_calls": None, "error": None, "think": None,
                        "usage": {"total_tokens": 10}, "elapsed": 0.1}
            return {"text": "时间我通过 system_info 拿到了。", "tool_calls": None,
                    "error": None, "think": None, "usage": {"total_tokens": 10}, "elapsed": 0.1}

        pl.model_client.generate = stub
        res = asyncio.run(pl.process("谈谈你对效率工具的理解", probe=True)) or {}
        resp = str(res.get("response") or "")
        chk("E0 路由落 general (正是泄漏发生的那条路)",
            (res.get("intent") or "") == "general", res.get("intent"))
        chk("E1 ★ 文本格式的调用真被执行了", "system_info" in calls, calls)
        chk("E2 ★ 回复里没有裸露的工具标签",
            "<tool_call>" not in resp and "<function=" not in resp, resp[:140])
        chk("E3 拿到工具结果后又总结了一轮 (模型调用 >= 2 次)", state["n"] >= 2, state)
    except Exception as e:
        chk("E 端到端", False, f"{type(e).__name__}: {e}")
    finally:
        if _sd:
            shutil.rmtree(_sd, ignore_errors=True)
        shutil.rmtree(SBX, ignore_errors=True)

    print("—— G 零污染 ——")
    try:
        shutil.rmtree(SBX, ignore_errors=True)
        chk("G1 沙箱已清", not SBX.exists())
    except Exception as e:
        chk("G1 沙箱已清", False, e)

    print()
    print("=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
