# -*- coding: utf-8 -*-
"""记忆证据层 (memory claims) —— 抄 Meta Muse 记忆子系统的三件硬东西。

为什么要有这个东西 (要解决的真问题)
═══════════════════════════════════════════════════════════════════
TMM 的记忆现在有两条路: `data/memory.db` (工序记忆) 与 long_term.db 的 facts 表
(`/remember` 直接 INSERT 一句话)。两条路的共同毛病: **记下来的话没有出处**。
后果是真缺陷, 不是洁癖:
  · 记错了没人能查。用户问"你凭什么这么说", 系统答不出来 —— 只能说"我记得"。
  · 记重了没人能合。同一条事被记 5 遍, 字面还各不相同, 越用越糊。
  · 记反了没人能换。"以后别用 A 了" 之后 A 还在库里, 下次又用。

Muse 记忆子系统的三件做法 (值得抄):
  ① **每条 claim 带引文**, 能回到原始消息的那一行 (它是 Postgres + 向量, 但
     引文那一列是硬的)
  ② **claim 能被新 claim 覆盖** (supersedes), 覆盖是**显式关系**, 不是删旧留新
     —— 旧的不消失, 才能回答"我什么时候改的口"
  ③ **置信度**不是装饰: 会衰减, 会被引用行为抬升, 低到阈值自动过期
本模块把这三件落成 SQLite (零依赖, 与项目其余库同构)。

铁律
───────────────────────────────────────────────────────────────
1. **无凭据不入库**: 没有引文 (quote) 也没有来源 (source_ref) 的 claim 一律拒绝。
   宁缺勿编 —— 一条说不出出处的记忆, 比没有记忆更危险。
2. 覆盖**不删行**: 旧 claim 转 `superseded` 并指向新 claim; 搜索默认不出旧的,
   `explain()` 仍能整条追溯 (谁在什么时候把哪句换成了哪句)。
3. 引文可**回原库核对**: message_id 给了就去会话库里把那行读回来 (只读打开),
   对不上就报 `evidence_unverified` —— 不许"引文是编的"还标记为有据。
4. 置信度只走真实行为: `touch()` 被引用才涨, 时间衰减按天, 低到阈值才过期。
   不许手改高 (要给高置信度就在入库时写清楚来源)。

对外接口
───────────────────────────────────────────────────────────────
    get_claims()                     -> MemoryClaims
    add(text, quote=, source_kind=, source_ref=, message_id=, confidence=, tags=, supersedes_id=)
    search(q, limit=, include_superseded=False)
    explain(id)                      -> {claim, evidence, lineage, verified}
    supersede(old_id, text, quote=, ...)
    touch(id) / expire() / stats()
    harvest_teachings(messages)      -> 从真实对话里捞出"用户教的规矩" (带原话)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("core.memory_claims")

ROOT = Path(__file__).resolve().parent.parent

#: 置信度衰减: 每天乘这个系数 (Muse 的 claim 会随新 claim 重新核对)
DECAY_PER_DAY = 0.97
#: 低于这个值 + 从未被引用 + 超过 EXPIRE_DAYS 天 → 过期
EXPIRE_FLOOR = 0.2
EXPIRE_DAYS = 60
#: 置信度上限 (没有来源核对的 claim 不许报到"确定")
CONF_CEILING = {"user": 0.95, "tool": 0.8, "model": 0.6, "inferred": 0.5}

#: 用户"教规矩"的句式 —— 这些句子才是该长期记的 (抄用户自己的话, 不改写)
TEACH_PATTERNS = (
    "以后", "默认", "记住", "不要再", "别再", "不许", "必须", "一律",
    "我说的是", "跟你说过", "跟你说过多少", "从今往后", "下次",
)


def _db_path() -> Path:
    p = os.environ.get("TMM_CLAIMS_DB")
    return Path(p) if p else ROOT / "data" / "memory_claims.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.6,
    status TEXT NOT NULL DEFAULT 'active',      -- active / superseded / expired
    source_kind TEXT NOT NULL DEFAULT 'user',   -- user / tool / model / inferred
    source_ref TEXT DEFAULT '',
    quote TEXT DEFAULT '',
    session_id INTEGER,
    message_id INTEGER,
    tags TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,        -- 引文回原库核对过 = 1
    supersedes_id INTEGER,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_text ON claims(text);
"""


class MemoryClaims:
    def __init__(self, db_path: Path = None, session_db: Path = None):
        self.db_path = Path(db_path) if db_path else _db_path()
        self.session_db = Path(session_db) if session_db else self._session_db_default()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @staticmethod
    def _session_db_default() -> Path:
        p = os.environ.get("TMM_SESSION_DB")
        return Path(p) if p else ROOT / "data" / "chat_sessions.db"

    def _conn(self):
        c = sqlite3.connect(str(self.db_path), timeout=8)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ── 入 ────────────────────────────────────────────────
    def add(self, text, quote="", source_kind="user", source_ref="",
            session_id=None, message_id=None, confidence=0.6, tags=None,
            supersedes_id=None):
        text = (text or "").strip()
        quote = (quote or "").strip()
        source_ref = (source_ref or "").strip()
        if not text:
            return {"success": False, "error": "空记忆不入库"}
        # 铁律 1: 无凭据不入库
        if not quote and not source_ref:
            return {"success": False,
                    "error": "无凭据的记忆不许入库 (要 quote 原话或 source_ref 来源) —— 宁缺勿编"}
        kind = (source_kind or "user").strip().lower()
        ceiling = CONF_CEILING.get(kind, 0.5)
        conf = max(0.0, min(float(confidence or 0.6), ceiling))
        now = time.time()
        verified = 0
        if message_id:
            verified = 1 if self._verify_quote(message_id, quote) else 0
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO claims (text, confidence, status, source_kind, source_ref, quote,"
                " session_id, message_id, tags, created_at, updated_at, ref_count, verified,"
                " supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (text, conf, "active", kind, source_ref, quote, session_id, message_id,
                 ",".join(tags or []), now, now, verified, supersedes_id))
            new_id = cur.lastrowid
            if supersedes_id:
                old = c.execute("SELECT id, status FROM claims WHERE id=?",
                                (supersedes_id,)).fetchone()
                if not old:
                    return {"success": False, "error": f"要覆盖的 #{supersedes_id} 不存在"}
                c.execute("UPDATE claims SET status='superseded', superseded_by=?, updated_at=?"
                          " WHERE id=?", (new_id, now, supersedes_id))
        return {"success": True, "id": new_id, "confidence": conf, "verified": bool(verified),
                "output": f"已记 (#{new_id})" + ("" if verified else " —— 注: 引文未回原库核对")}

    def _verify_quote(self, message_id, quote):
        """铁律 3: 引文回原库核对 (只读打开, 绝不写用户的会话库)。"""
        try:
            if not self.session_db.exists():
                return False
            c = sqlite3.connect(f"file:{self.session_db.as_posix()}?mode=ro", uri=True, timeout=8)
            row = c.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()
            c.close()
            if not row:
                return False
            return (quote[:24] in str(row[0])) if quote else True
        except Exception as e:
            logger.warning("memory_claims: 引文核对失败 %s", e)
            return False

    # ── 查 ────────────────────────────────────────────────
    def search(self, q="", limit=10, include_superseded=False):
        where, params = [], []
        if not include_superseded:
            where.append("status='active'")
        if q:
            where.append("(text LIKE ? OR quote LIKE ? OR tags LIKE ?)")
            params += [f"%{q}%"] * 3
        sql = "SELECT * FROM claims" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        params.append(int(limit or 10))
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(sql, params)]
        return rows

    def explain(self, cid):
        """一条 claim 的完整交代: 正文 + 引文 + 来源 + 覆盖链 + 核对结果。"""
        with self._conn() as c:
            row = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
            if not row:
                return {"success": False, "error": f"没有 #{cid} 这条记忆"}
            claim = dict(row)
            lineage = []
            cur = claim
            while cur.get("supersedes_id"):
                prev = c.execute("SELECT id, text, status, created_at FROM claims WHERE id=?",
                                 (cur["supersedes_id"],)).fetchone()
                if not prev:
                    break
                lineage.append(dict(prev))
                cur = dict(prev)
            fwd = c.execute("SELECT id, text, created_at FROM claims WHERE supersedes_id=?",
                            (cid,)).fetchall()
        verified = claim.get("verified") == 1
        if claim.get("message_id") and not verified:
            verified = self._verify_quote(claim["message_id"], claim.get("quote") or "")
        return {
            "success": True, "claim": claim,
            "evidence": {"quote": claim.get("quote"), "source_kind": claim.get("source_kind"),
                         "source_ref": claim.get("source_ref"),
                         "message_id": claim.get("message_id"),
                         "session_id": claim.get("session_id"),
                         "verified": bool(verified)},
            "lineage_older": lineage,             # 它覆盖了谁
            "lineage_newer": [dict(x) for x in fwd],  # 谁覆盖了它
            "output": (f"#{cid}「{claim['text'][:40]}」 置信 {claim['confidence']:.2f} · "
                       f"状态 {claim['status']} · "
                       + ("引文已回原库核对 ✓" if verified else "引文未核对/不可核对")),
        }

    def touch(self, cid):
        with self._conn() as c:
            c.execute("UPDATE claims SET ref_count=ref_count+1, updated_at=?,"
                      " confidence=MIN(0.95, confidence+0.02) WHERE id=?", (time.time(), cid))
        return {"success": True, "id": cid}

    def supersede(self, old_id, text, **kw):
        kw["supersedes_id"] = old_id
        kw.setdefault("source_kind", "user")
        r = self.add(text, **kw)
        if r.get("success"):
            r["output"] = f"#{old_id} 已被 #{r['id']} 覆盖 (旧的留档, 不删)"
        return r

    # ── 治理 ──────────────────────────────────────────────
    def decay(self):
        """按天衰减 + 过期 (只碰 active)。返回 (衰减条数, 过期条数)。"""
        now = time.time()
        decayed = expired = 0
        with self._conn() as c:
            for r in c.execute("SELECT id, confidence, created_at, ref_count, updated_at"
                               " FROM claims WHERE status='active'").fetchall():
                days = max(0.0, (now - float(r["updated_at"] or r["created_at"])) / 86400.0)
                new_conf = float(r["confidence"]) * (DECAY_PER_DAY ** days)
                if new_conf < float(r["confidence"]) - 1e-9:
                    decayed += 1
                old_enough = (now - float(r["created_at"])) / 86400.0 > EXPIRE_DAYS
                if new_conf < EXPIRE_FLOOR and int(r["ref_count"] or 0) == 0 and old_enough:
                    c.execute("UPDATE claims SET status='expired', updated_at=? WHERE id=?",
                              (now, r["id"]))
                    expired += 1
                else:
                    c.execute("UPDATE claims SET confidence=? WHERE id=?", (new_conf, r["id"]))
        return decayed, expired

    def stats(self):
        with self._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM claims GROUP BY status").fetchall()
            by_kind = c.execute("SELECT source_kind, COUNT(*) n FROM claims GROUP BY source_kind").fetchall()
            unverified = c.execute("SELECT COUNT(*) n FROM claims WHERE verified=0").fetchone()["n"]
        return {"by_status": {r["status"]: r["n"] for r in rows},
                "by_kind": {r["source_kind"]: r["n"] for r in by_kind},
                "unverified": unverified, "db": str(self.db_path)}


# ── 从真实对话里捞"用户教的规矩" ──────────────────────────────
def harvest_teachings(messages):
    """messages: [(id, role, content), ...] → [{message_id, quote, text, hits}]

    只捞**用户自己说的**、含教规矩句式的句子, 原话照抄当引文 (不改写、不总结 ——
    这不是模型的活, 是"把人说过的话原样钉住"的活)。
    """
    out = []
    for mid, role, content in messages or []:
        if role != "user":
            continue
        text = str(content or "").strip()
        if not (4 <= len(text) <= 300):
            continue
        hits = [p for p in TEACH_PATTERNS if p in text]
        if not hits:
            continue
        out.append({"message_id": mid, "quote": text, "text": text, "hits": hits})
    return out


_CLAIMS = None


def get_claims(**kw) -> MemoryClaims:
    global _CLAIMS
    if _CLAIMS is None:
        _CLAIMS = MemoryClaims(**kw)
    return _CLAIMS


def reload_claims():
    global _CLAIMS
    _CLAIMS = None
    return get_claims()
