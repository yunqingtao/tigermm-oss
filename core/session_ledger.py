# -*- coding: utf-8 -*-
"""会话账本: 可举证的 append-only 哈希链 —— 补「真正落后的八件」第 4 条 (2026-09-26 立)

问题 (原文档第 4 条)
═══════════════════════════════════════════════════════════════
    Claude Code: 会话是 append-only 外部日志
    TMM: `chat_sessions.db` 可改可删, "当时什么样" 举不了证

`data/chat_sessions.db` 是一张普通表 (无触发器无视图, 而且 WAL), 一条 UPDATE 就能把
"我到底说过什么/它到底答过什么"抹平, 事后无从分辨 —— 包括**我自己**改过没改过。

做法: 旁路一份带前后哈希链的日志 (**一行一条, 只追加**)
───────────────────────────────────────────────────────────────
不碰现有库 (风险为零, 现有功能一行不改), 另开 `data/session_ledger.jsonl`:

    {"n":1, "prev":"", "sha":"…", "ts":…, "session":"default",
     "user":"…", "reply_sha":"…", "route":"…", "tools":[…], "reply":"…"}

  · `prev` = 上一条的 `sha`; `sha` = sha256(定序后的本条内容 + prev)
  · 于是**改一个字/删一行/插一行/换顺序** 都会在下一条对不上 ⇒ `verify()` 指得出断点
  · 只 append: 从不 UPDATE / DELETE (源码级有一条门禁盯着这件事)

它给什么、不给什么 (诚实边界)
───────────────────────────────────────────────────────────────
给: 从某条记录之后**任何改动都可被发现** —— 这正好是"举证"要的性质。
不给: 它**不是**不可伪造的 (谁都能整份重写并重算哈希链)。真要做到防伪造需要外部锚
     (把链尾哈希存到别处/签名), 那是下一步, 本模块不含。**不许把它说成"防篡改"**,
     只能说"**可发现改动**、可举证"。

隐私: 账本存用户原话 (这是它的用途), 因此
  · 落盘前过一遍凭据遮挡 (钥匙/口令形态一律替换, 只留名字)
  · 文件在 `data/` 下, 与其它用户数据同级; 门禁跑的时候走隔离路径, 不碰真账本
"""
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("core.session_ledger")

#: 凭据形态 (落盘前替换成占位符 —— 账本可以留话, 但不留钥匙)
_SECRET_PAT = (
    re.compile(r"\b(sk|pk|ghp|gho|xox[baprs])[-_A-Za-z0-9]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b(?=[^\n]{0,20}(?:key|token|secret|密钥|口令))", re.I),
)
_REDACTED = "[REDACTED]"


def _redact(s: str) -> str:
    out = str(s or "")
    for p in _SECRET_PAT:
        out = p.sub(_REDACTED, out)
    return out


def _canon(d: dict) -> str:
    """定序序列化 —— 哈希必须不受键顺序/空格影响。"""
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(d: dict) -> str:
    return hashlib.sha256(_canon(d).encode("utf-8")).hexdigest()[:16]


class SessionLedger:
    def __init__(self, path: Path | str | None = None):
        ovr = os.environ.get("TMM_SESSION_LEDGER")
        self.path = Path(ovr) if ovr else (Path(path) if path else None)
        self.stats = {"append": 0, "verify_ok": 0, "verify_bad": 0}

    # ────────── 只追加 ──────────
    def append(self, *, user: str = "", reply: str = "", session: str = "default",
               route: str = "", tools=None, extra: dict | None = None) -> dict | None:
        """追加一条。**只 append**, 从不改旧行。返回写进去的记录 (失败返回 None)。"""
        if not self.path:
            return None
        try:
            prev, n = self._tail()
            rec = {
                "n": n + 1,
                "prev": prev,
                "ts": round(time.time(), 3),
                "session": _redact(session)[:64],
                "route": _redact(route)[:80],
                "tools": [_redact(str(t))[:40] for t in (tools or [])][:20],
                "user": _redact(user)[:4000],
                "reply": _redact(reply)[:4000],
            }
            rec["reply_sha"] = _sha({"r": rec["reply"]})
            rec["sha"] = _sha({k: v for k, v in rec.items() if k != "sha"})
            if extra:
                rec["extra"] = {k: _redact(str(v))[:200] for k, v in extra.items()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(_canon(rec) + "\n")
            self.stats["append"] += 1
            return rec
        except Exception as e:                       # noqa: BLE001
            logger.debug("session_ledger 追加失败: %s", e)
            return None

    def _tail(self) -> tuple[str, int]:
        """最后一条的 (sha, n)。"""
        if not self.path or not self.path.exists():
            return "", 0
        last = None
        with self.path.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    last = ln
        if not last:
            return "", 0
        try:
            d = json.loads(last)
            return str(d.get("sha") or ""), int(d.get("n") or 0)
        except Exception:
            return "", 0

    # ────────── 校验: 改动/删除/插入/换序 全都指得出来 ──────────
    def verify(self) -> dict:
        """走一遍链。返回 {ok, n, broken:[{n, why}], tail}。"""
        res = {"ok": True, "n": 0, "broken": [], "tail": ""}
        if not self.path or not self.path.exists():
            return res
        prev = ""
        expect = 1
        with self.path.open("r", encoding="utf-8") as fh:
            for i, ln in enumerate(fh, 1):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    res["broken"].append({"n": expect, "why": f"第 {i} 行不是合法 JSON (被改坏?)"})
                    res["ok"] = False
                    prev, expect = "", expect + 1
                    continue
                n = d.get("n")
                if n != expect:
                    res["broken"].append(
                        {"n": expect, "why": f"序号断档: 期望 {expect}, 实际 {n} (删过/插过?)"})
                    res["ok"] = False
                if str(d.get("prev") or "") != prev:
                    res["broken"].append(
                        {"n": n, "why": "prev 与上一条的 sha 对不上 (改过/换过顺序?)"})
                    res["ok"] = False
                calc = _sha({k: v for k, v in d.items() if k != "sha"})
                if calc != d.get("sha"):
                    res["broken"].append({"n": n, "why": f"内容哈希对不上 (期望 {calc}, 实际 {d.get('sha')})"})
                    res["ok"] = False
                if d.get("reply_sha") != _sha({"r": d.get("reply", "")}):
                    res["broken"].append({"n": n, "why": "回复正文与 reply_sha 不符 (回复被改过)"})
                    res["ok"] = False
                prev = str(d.get("sha") or "")
                expect = (n or 0) + 1
                res["n"] = n or res["n"]
        res["tail"] = prev
        self.stats["verify_ok" if res["ok"] else "verify_bad"] += 1
        return res


_L = None


def get_ledger(path=None) -> SessionLedger:
    global _L
    if _L is None or path is not None:
        _L = SessionLedger(path)
    return _L


def reset_ledger():
    global _L
    _L = None
