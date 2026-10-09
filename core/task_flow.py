# -*- coding: utf-8 -*-
r"""在途任务台账 —— 「你问的那件活, 到底做完了没」的底账 (2026-09-25 立)

为什么必须另立一本 (而不是加到 core/task_ledger.py)
═══════════════════════════════════════════════════════════════════
干活账 (`core/task_ledger.py`) 记的是**聚合**: 每天几轮 / 成多少败多少 / 哪条路由在失败。
它**故意不存原话、不存单件状态** (隐私口径), 所以用户问
「做完了吗」「继续跑」「到哪了」时它**只能编**。这一点已经钉成反向用例
(`verify_task_ledger` [E] 段: 账本答不了的不许接账本)。

真缺口是: 系统**确实**经常在替用户办一件多步的活 (链式任务), 但**没有任何地方记着
"这件事现在处于什么状态"**。实测: 121 轮兜底里 14 轮就是这类问句
(「做完了吗」「完成了？」「继续跑 B 组」「到哪了」), 全掉大模型闲聊。

本模块只做一件事: **开一条记录 → 记步骤 → 关掉**, 并回答"现在在跑什么"。
它跟干活账的分工是清楚的:
    干活账   = 统计 (这台机器攒下了什么)         ← 不存原话
    在途台账 = 单件任务的生命周期 (这件事到哪了)   ← **存原话**(不然用户问不出来)

隐私口径 (写在这里, 改之前先看这句)
───────────────────────────────────────────────────────────────
存的是**用户自己那句任务原话**, 截断 100 字符, 落在本机 `data/task_flow.jsonl`,
不出机器、不进分发包 (data/ 本来就不进包)。这不是"收集凭据", 是"记住你让我干的活" ——
不存它, "做完了吗"就永远只能编。

诚实边界 (门禁里有反向用例)
───────────────────────────────────────────────────────────────
- 没在跑的任务 ⇒ 必须**明说没有**, 不许拿"最近完成的那件"冒充"在跑的";
- 链式任务**抛异常**也必须关记录 (否则永远显示"在跑", 那是最坏的一种骗);
- 只有**真人会话**才记 (探针/门禁不写), 且探针进程一律拒写 (见 core/probe_guard.py)。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "task_flow.jsonl"

#: 任务原话最多存这么多字符 (够用户认出是哪件事, 又不至于把整段粘进来)
TEXT_MAX = 100
#: 读的时候最多回看多少行事件 (够最近几十件, 又不会越读越慢)
READ_MAX = 4000


def store_path() -> Path:
    """台账路径: 环境变量优先 (门禁/探针必须指到临时文件)。"""
    return Path(os.environ.get("TMM_TASK_FLOW") or DEFAULT_PATH)


# ── 写盘闸 (与 core/task_ledger.py 同一条, 见 core/probe_guard.py 文件头) ────────
def _writable() -> str:
    """能不能写。返回空串=能写; 非空=拒写理由。"""
    if os.environ.get("TMM_PROBE") in ("1", "true", "yes"):
        return "probe 流量不记任务"
    if os.environ.get("TMM_LIVE") not in ("1", "true", "yes"):
        return "非真人会话不记任务 (缺 TMM_LIVE)"
    try:
        from core import probe_guard
        return probe_guard.block_reason(store_path(), what="在途任务台账")
    except Exception:                                    # noqa: BLE001
        return ""


def _append(ev: dict) -> bool:
    """追加一条事件。写不进去不抛 (记账失败不该弄挂用户的活)。"""
    why = _writable()
    if why:
        logger.debug("task_flow 不记: %s", why)
        return False
    p = store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return True
    except Exception as e:                               # noqa: BLE001
        logger.warning("task_flow 写失败: %s: %s", type(e).__name__, e)
        return False


def _events():
    """读全部事件 (只读; 文件不在 = 空)。坏行跳过, 不因一行脏数据全废。"""
    p = store_path()
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-READ_MAX:]
    except Exception as e:                               # noqa: BLE001
        logger.warning("task_flow 读失败: %s: %s", type(e).__name__, e)
        return []
    out = []
    for ln in lines:
        ln = (ln or "").strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
            if isinstance(d, dict) and d.get("ev"):
                out.append(d)
        except Exception:                                # noqa: BLE001
            continue
    return out


def _fold() -> list:
    """把事件折叠成任务列表 (按开单顺序)。"""
    tasks: dict = {}
    order: list = []
    for e in _events():
        tid = e.get("id")
        if not tid:
            continue
        if e["ev"] == "open":
            if tid not in tasks:
                order.append(tid)
            tasks[tid] = {"id": tid, "task": e.get("task") or "", "source": e.get("source") or "",
                          "steps": int(e.get("steps") or 0), "done": 0,
                          "started_at": float(e.get("ts") or 0), "ended_at": 0.0,
                          "ok": None, "note": e.get("note") or "",
                          "tools": [], "last_at": float(e.get("ts") or 0)}
        elif tid in tasks:
            t = tasks[tid]
            if e["ev"] == "step":
                t["done"] += 1
                sk = e.get("tool") or ""
                if sk and sk not in t["tools"]:
                    t["tools"].append(sk)
                t["last_at"] = float(e.get("ts") or t["last_at"])
                if e.get("steps"):
                    t["steps"] = max(t["steps"], int(e["steps"]))
            elif e["ev"] == "close":
                t["ended_at"] = float(e.get("ts") or 0)
                t["ok"] = bool(e.get("ok"))
                if e.get("note"):
                    t["note"] = e.get("note")
                if e.get("steps"):
                    t["steps"] = max(t["steps"], int(e.get("steps")))
    return [tasks[i] for i in order]


# ── 写侧 API ──────────────────────────────────────────────────────────────
def open_task(text: str, steps: int = 0, source: str = "") -> str:
    """开一条任务记录。返回 id (没写进去也返回 id —— 调用方照常传下去)。"""
    tid = uuid.uuid4().hex[:12]
    _append({"ev": "open", "id": tid, "ts": round(time.time(), 3),
             "task": (text or "").strip()[:TEXT_MAX], "steps": int(steps or 0),
             "source": (source or "")[:40]})
    return tid


def step(task_id: str, tool: str = "", ok: bool = True, steps: int = 0) -> None:
    """记一步 (第几个工具、成没成)。"""
    if not task_id:
        return
    _append({"ev": "step", "id": task_id, "ts": round(time.time(), 3),
             "tool": (tool or "")[:40], "ok": bool(ok), "steps": int(steps or 0)})


def close_task(task_id: str, ok: bool = True, note: str = "", steps: int = 0) -> None:
    """关掉任务。**抛异常路径也必须调** (否则永远显示'在跑')。"""
    if not task_id:
        return
    _append({"ev": "close", "id": task_id, "ts": round(time.time(), 3),
             "ok": bool(ok), "note": (note or "")[:80], "steps": int(steps or 0)})


# ── 读侧 API ──────────────────────────────────────────────────────────────
def inflight() -> list:
    """**现在真在跑**的任务 (开了没收的)。"""
    return [t for t in _fold() if not t["ended_at"]]


def recent(n: int = 5) -> list:
    """最近**已完成**的任务, 新的在前。"""
    done = [t for t in _fold() if t["ended_at"]]
    done.sort(key=lambda t: t["ended_at"], reverse=True)
    return done[:max(1, int(n or 1))]


def last_update_ago() -> float:
    """最后一笔事件距今多少秒 (没有任何事件 ⇒ -1)。用来判断"是不是卡住了"。"""
    evs = _events()
    if not evs:
        return -1.0
    return max(0.0, time.time() - float(evs[-1].get("ts") or 0))
