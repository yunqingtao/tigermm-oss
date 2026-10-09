# -*- coding: utf-8 -*-
"""后台观察者 + 协调器 (observers) —— 抄 Claude Code 的 observer/reconciler 分工。

要解决的问题 (TMM 的真痛点)
═══════════════════════════════════════════════════════════════════
TMM 现在有三套"自知"的东西 (SelfRegulation 的元认知、task_flow 的在途台账、
task_ledger 的干活账), 但**没有一条路把它们送回当前这一轮**。后果:
  · 手里还有没关的在途单, 却一本正经地收尾;
  · 说"修好了/门禁绿了", 而没人当场要它拿证据 (用户明确定过: 门禁绿不算, 要拉真引擎);
  · 有据可查的长期记忆躺在库里, 不如"我印象里你说过"好使 —— 于是用印象答。

Claude Code 的做法值得抄, 关键是**分工**而不是"多几个 agent":
  后台观察者各自盯**一个轴**, 只**提案** (propose), 绝不替你答;
  一个**协调器** (reconciler) 决定哪些提案能进主线;
  只有被采纳的提案才进主 agent 的下一轮 —— 而且不打断它。
这套分工的价值: 观察者可以放宽 (多提没事), 因为**闸在后面**; 主线的干净程度
由协调器一处负责。

本机版四条轴 (对应它那四条, 内容换成 TMM 真正有的证据)
───────────────────────────────────────────────────────────────
  goal      在途单没收 (core/task_flow)         → 提醒别收尾
  evidence  声称完成但拿不出证据                  → 提醒"这话没证据撑"
  memory    与本轮相关、且**有据**的记忆           → 提醒对着库说, 别凭印象
  skill     该先加载的技能 (triggers/tags 命中)    → 提醒先读哪份技能

铁律 (与记忆证据层同一条: 无凭据不许进)
───────────────────────────────────────────────────────────────
1. **无证据的提案一律丢弃**。每条提案必须带 evidence (文件路径/行号/账本数/引文),
   拿不出来的当噪音 —— 否则这套东西就退化成"多几个 agent 抢话"。
2. **提案 ≠ 注入**。只有协调器采纳的才进主线; 采纳要过四道:
   有证据 → 不重复(跨轮记已呈过的) → 与本轮相关(或该轴恒相关) → 预算内。
3. **不许提案里出现"结论"**。提案是提醒 (你还有单没关 / 这句话缺证据),
   不是替用户下判断。
4. 零依赖、纯离线、确定性: 同输入必同输出 (可被门禁逐条断言)。

对外接口
───────────────────────────────────────────────────────────────
    get_observers()                        -> Observers
    obs.observe(message, limit=3)          -> {accepted:[...], dropped:[...], text}
    obs.axes()                             -> 四条轴的自述
    obs.stats()
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("core.observers")

ROOT = Path(__file__).resolve().parent.parent

#: 协调器的预算 (抄它"短建议、不打断"的形态, 收得更紧)
MAX_ACCEPTED = 3
MAX_CHARS = 600

#: 相关性比较用: 中文按 2-gram, 英文/数字按词
_PUNCT = "，。！？；：、（）《》“”‘’ \t\r\n.,!?;:()[]{}\"'/\\|-_=+*&^%$#@~`<>"


def _content_units(s: str):
    """抽"内容单位": 中文 2-gram + 英文/数字词 (不用分词库)。"""
    s = str(s or "")
    units = set()
    han = re.findall(r"[\u4e00-\u9fff]{2,}", s)
    for seg in han:
        for i in range(len(seg) - 1):
            units.add(seg[i:i + 2])
    for w in re.findall(r"[A-Za-z0-9_]{3,}", s):
        units.add(w.lower())
    return units


def _relevant(a: str, b: str) -> int:
    return len(_content_units(a) & _content_units(b))


class Proposal:
    __slots__ = ("axis", "kind", "text", "evidence", "strength", "always", "dedupe")

    def __init__(self, axis, text, evidence, kind="advisory", strength=0.5,
                 always=False, dedupe=""):
        self.axis = axis
        self.kind = kind
        self.text = text
        self.evidence = evidence
        self.strength = float(strength)
        self.always = bool(always)
        self.dedupe = dedupe or f"{axis}:{kind}:{text[:40]}"

    def to_dict(self):
        return {"axis": self.axis, "kind": self.kind, "text": self.text,
                "evidence": self.evidence, "strength": self.strength,
                "dedupe": self.dedupe}


class Reconciler:
    """唯一决定"什么能进主线"的地方。观察者放宽, 闸在这里。"""

    def __init__(self, seen_path: Path = None, max_accepted=MAX_ACCEPTED, max_chars=MAX_CHARS):
        self.seen_path = Path(seen_path) if seen_path else self._default_seen()
        self.max_accepted = int(max_accepted)
        self.max_chars = int(max_chars)
        self._seen = self._load_seen()

    @staticmethod
    def _default_seen() -> Path:
        p = os.environ.get("TMM_OBSERVERS_SEEN")
        return Path(p) if p else ROOT / "data" / "observers_seen.jsonl"

    def _load_seen(self):
        out = {}
        try:
            if self.seen_path.exists():
                with open(self.seen_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        out[d.get("dedupe", "")] = d.get("ts", 0)
        except Exception as e:
            logger.warning("observers: 已呈记录读不了 %s", e)
        return out

    def _mark_seen(self, p: Proposal):
        self._seen[p.dedupe] = time.time()
        try:
            self.seen_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.seen_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"dedupe": p.dedupe, "ts": time.time(),
                                    "axis": p.axis}, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("observers: 已呈记录写不了 %s", e)

    def decide(self, proposals, message="", ttl_s=6 * 3600):
        """-> (accepted: list[Proposal], dropped: list[(Proposal, 原因)])"""
        accepted, dropped, chars = [], [], 0
        now = time.time()
        for p in sorted(proposals, key=lambda x: -x.strength):
            # ① 铁律 1: 无证据 → 丢
            if not p.evidence:
                dropped.append((p, "无证据 (不许进主线)"))
                continue
            # ② 跨轮去重 (同一个提醒不反复念; TTL 后允许再提)
            last = self._seen.get(p.dedupe)
            if last and now - float(last) < ttl_s:
                dropped.append((p, "重复 (本 TTL 内已呈过)"))
                continue
            # ③ 相关性 (恒相关的轴跳过这一关)
            if not p.always and message and _relevant(message, p.text) == 0:
                dropped.append((p, "与本轮不相关"))
                continue
            if not p.always and not message:
                dropped.append((p, "没有本轮内容可比, 非恒相关轴不注入"))
                continue
            # ④ 预算
            if len(accepted) >= self.max_accepted:
                dropped.append((p, f"超出条数预算 {self.max_accepted}"))
                continue
            if chars + len(p.text) > self.max_chars:
                dropped.append((p, f"超出字数预算 {self.max_chars}"))
                continue
            accepted.append(p)
            chars += len(p.text)
        return accepted, dropped

    def commit(self, accepted):
        for p in accepted:
            self._mark_seen(p)
        return len(accepted)

    def forget(self, axis=None):
        """清已呈记忆 (想让它再提一遍时用)。"""
        if axis:
            self._seen = {k: v for k, v in self._seen.items() if not k.startswith(axis + ":")}
        else:
            self._seen = {}
        try:
            if self.seen_path.exists():
                if axis:
                    keep = [json.dumps({"dedupe": k, "ts": v, "axis": k.split(":")[0]},
                                       ensure_ascii=False) for k, v in self._seen.items()]
                    self.seen_path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
                else:
                    self.seen_path.write_text("", encoding="utf-8")
        except Exception as e:
            logger.warning("observers: 清记录失败 %s", e)
        return len(self._seen)


# ══════════════════════════════════════════════════════════════════
# 四条轴
# ══════════════════════════════════════════════════════════════════
class GoalObserver:
    """在途单没收 → 提醒别收尾 (抄 Claude Code 的 goal tracking: 拒绝提前收尾)。"""
    axis = "goal"
    always = True

    def __init__(self, root=ROOT):
        self.root = Path(root)

    def observe(self, message="", ctx=None):
        out = []
        try:
            import sys
            if str(self.root) not in sys.path:
                sys.path.insert(0, str(self.root))
            from core import task_flow
            items = task_flow.inflight() or []
        except Exception as e:
            logger.warning("observers.goal: task_flow 不可用 %s", e)
            return out
        for it in items[:2]:
            tid = it.get("id") or it.get("task_id") or "?"
            txt = str(it.get("text") or it.get("title") or "")[:40]
            steps = it.get("steps")
            out.append(Proposal(
                self.axis,
                f"还有在途单没关: {txt}（#{tid}）—— 收尾前先确认它是真完成还是搁置。",
                evidence=f"core/task_flow.py inflight() 返回 #{tid}",
                strength=0.9, always=True, dedupe=f"goal:{tid}"))
        return out


class EvidenceObserver:
    """声称完成但没证据 → 提醒 (用户定过的规矩: 门禁绿不算, 要拉真引擎跑一遍)。"""
    axis = "evidence"
    always = True

    #: "我干完了"类断言
    CLAIM_PAT = re.compile(
        r"(已(经)?(修好|修完|完成|搞定|解决|跑通|通过|验证)|修好了|搞定了|通过了|"
        r"全部通过|门禁(全)?绿|已确认|测试通过|没问题了)")

    def __init__(self, root=ROOT):
        self.root = Path(root)

    def _has_evidence(self):
        """本轮有没有留下证据: 出去跑过门禁/命令, 或账本里刚记过一笔。"""
        ev = []
        try:
            p = Path(os.environ.get("TMM_TASK_DB") or (self.root / "data" / "task_ledger.db"))
            if p.exists():
                import sqlite3
                c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=8)
                row = c.execute("SELECT COUNT(*) FROM turns WHERE ts > ?",
                                (time.time() - 1800,)).fetchone()
                c.close()
                if row and row[0]:
                    ev.append(f"干活账近 30 分钟有 {row[0]} 轮")
        except Exception:
            pass
        try:
            p = Path(os.environ.get("TMM_EGRESS_AUDIT") or (self.root / "data" / "egress_audit.jsonl"))
            if p.exists():
                n = 0
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        try:
                            ts = time.mktime(time.strptime(e.get("ts", ""), "%Y-%m-%d %H:%M:%S"))
                        except Exception:
                            continue
                        if time.time() - ts < 1800:
                            n += 1
                if n:
                    ev.append(f"出口闸门近 30 分钟 {n} 笔决策 (说明真动过工具)")
        except Exception:
            pass
        return ev

    def observe(self, message="", ctx=None):
        text = str(message or "")
        m = self.CLAIM_PAT.search(text)
        if not m:
            return []
        ev = self._has_evidence()
        if ev:
            return []
        return [Proposal(
            self.axis,
            f"你说了「{m.group(0)}」, 但近 30 分钟没有可查的证据 (没跑过门禁、账本没记)。"
            f"按既定规矩: 拿真引擎跑一遍再下结论, 门禁绿不算、印象不算。",
            evidence="data/task_ledger.db 与 data/egress_audit.jsonl 近 30 分钟均无记录",
            strength=0.95, always=True, dedupe="evidence:unbacked_claim")]


class MemoryObserver:
    """与本轮相关、且**有据**的记忆 → 提醒 (对着库说, 别凭印象)。"""
    axis = "memory"
    always = False

    def __init__(self, root=ROOT, min_confidence=0.6):
        self.root = Path(root)
        self.min_confidence = float(min_confidence)

    def observe(self, message="", ctx=None):
        if not str(message or "").strip():
            return []
        try:
            import sys
            if str(self.root) not in sys.path:
                sys.path.insert(0, str(self.root))
            from core.memory_claims import get_claims
            m = get_claims()
        except Exception as e:
            logger.warning("observers.memory: claims 不可用 %s", e)
            return []
        rows = [r for r in m.search("", limit=200) if r.get("confidence", 0) >= self.min_confidence]
        scored = []
        for r in rows:
            rel = _relevant(message, str(r.get("text") or ""))
            if rel:
                scored.append((rel, r))
        scored.sort(key=lambda x: (-x[0], -float(x[1].get("confidence") or 0)))
        out = []
        for rel, r in scored[:2]:
            verified = r.get("verified") == 1
            tag = "引文已核对" if verified else "引文未核对 (只能算你说过)"
            out.append(Proposal(
                self.axis,
                f"库里有一条与你这次相关的记忆: 「{str(r.get('text'))[:50]}」(#{r['id']}, {tag}) —— 要引用就先 explain 看引文。",
                evidence=f"data/memory_claims.db#{r['id']} (来源 {r.get('source_kind')}, 置信 {float(r.get('confidence') or 0):.2f})",
                strength=0.7 if verified else 0.5, always=False,
                dedupe=f"memory:{r['id']}"))
        return out


class SkillObserver:
    """该先加载的技能 → 提醒 (抄它的 skill recall)。"""
    axis = "skill"
    always = False

    def __init__(self, root=ROOT):
        self.root = Path(root)

    def observe(self, message="", ctx=None):
        msg = str(message or "")
        if len(msg) < 2:
            return []
        hits = []
        try:
            import sys
            if str(self.root) not in sys.path:
                sys.path.insert(0, str(self.root))
            from core.skill_loader import SkillWorkflowEngine
            eng = SkillWorkflowEngine()
            eng.scan()
            idx = eng.get_index()
            for name, si in (idx or {}).items():
                for kw in (getattr(si, "triggers", None) or []) + (getattr(si, "tags", None) or []):
                    if kw and str(kw) in msg:
                        hits.append((len(str(kw)), name, str(kw)))
        except Exception as e:
            logger.warning("observers.skill: 技能索引不可用 %s", e)
            return []
        hits.sort(reverse=True)
        if not hits:
            return []
        ln, name, kw = hits[0]
        return [Proposal(
            self.axis,
            f"这句话命中了技能「{name}」(触发词: {kw}) —— 走它的流程比临场想更稳, 先读那份 SKILL.md。",
            evidence=f"tmm_skills/{name}/SKILL.md (triggers 命中「{kw}」)",
            strength=0.6, always=False, dedupe=f"skill:{name}")]


class Observers:
    AXES = {
        "goal": ("在途单没收吗", GoalObserver),
        "evidence": ("声称完成有证据吗", EvidenceObserver),
        "memory": ("有相关的有据记忆吗", MemoryObserver),
        "skill": ("该先读哪份技能", SkillObserver),
    }

    def __init__(self, root=ROOT, reconciler=None, enabled=None):
        self.root = Path(root)
        self.reconciler = reconciler or Reconciler()
        self._obs = {}
        self.enabled = enabled or list(self.AXES)

    def axes(self):
        return [{"axis": k, "问的是什么": v[0]} for k, v in self.AXES.items()]

    def _get(self, axis):
        if axis not in self._obs:
            self._obs[axis] = self.AXES[axis][1](root=self.root)
        return self._obs[axis]

    def observe(self, message="", limit=None, commit=True, ctx=None):
        props = []
        for axis in self.enabled:
            try:
                props.extend(self._get(axis).observe(message=message, ctx=ctx) or [])
            except Exception as e:
                logger.warning("observers: 轴 %s 出错 %s (其余照跑)", axis, e)
        if limit:
            self.reconciler.max_accepted = int(limit)
        accepted, dropped = self.reconciler.decide(props, message=message)
        if commit:
            self.reconciler.commit(accepted)
        return {
            "accepted": [p.to_dict() for p in accepted],
            "dropped": [{"axis": p.axis, "text": p.text[:60], "why": why} for p, why in dropped],
            "text": self.render(accepted),
        }

    @staticmethod
    def render(accepted):
        """进主线的那一小块 (没有就返回空串, 一个字的噪音都不留)。"""
        if not accepted:
            return ""
        lines = ["[观察者提醒] (后台各轴只提案, 以下是被采纳的)"]
        for p in accepted:
            lines.append(f"· [{p.axis}] {p.text}")
            lines.append(f"   证据: {p.evidence}")
        return "\n".join(lines)

    def stats(self):
        return {"axes": list(self.enabled), "seen": len(self.reconciler._seen),
                "seen_path": str(self.reconciler.seen_path)}


_OBS = None


def get_observers(**kw) -> Observers:
    global _OBS
    if _OBS is None:
        _OBS = Observers(**kw)
    return _OBS


def reload_observers():
    global _OBS
    _OBS = None
    return get_observers()
