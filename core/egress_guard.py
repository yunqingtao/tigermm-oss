# -*- coding: utf-8 -*-
"""出口闸门 + 污点追踪 (egress guard) —— 抄 Meta Muse 的两层控制里的 approval 层
(以及它叫"tainted egress"的那一件), 落成 TMM 本机版。

为什么要有这个东西 (要解决的真问题)
═══════════════════════════════════════════════════════════════════
TMM 现在什么都能干: 读本机文件/邮件/记忆, 又能发邮件/短讯/推送/发 GitHub/开浏览器。
这两类能力之间**没有任何一道闸** —— 只要模型在某一轮"读到了东西", 它就能在
同一轮或下一轮把东西发出去, 没人经手、没人知道。这不是假想: 提示注入/被污染的
网页/被投毒的知识库条目, 都能让"读到的私事"顺着出口流走。

Muse 的解法值得抄, 因为它把两个反直觉的点想清楚了:
  ① **不每步审批**。每步都问 → 审批疲劳 → 用户一路点同意 → 等于没闸。
     所以按动作**风险分级**: 只带一个查询词的动作自动放行; 能承载任意内容的
     才拦。
  ② **看的是"这个进程碰没碰过用户数据" (污点), 不是"这次像不像坏动作"**。
     内核级数据流追踪那一套本机做不了, 但**会话级**等效版做得到: 本轮/本会话
     读过私有数据 → 之后任何"能带数据出去"的动作要人批。这就是 tainted egress。

三道闸, 一个地方做 (单一出口原则)
───────────────────────────────────────────────────────────────
所有工具调用都经 `core/tool_gateway.py::ToolGateway.call`。出口决策**只在那里**,
不在各个工具里散着判 —— 散着判必然漏 (本项目已经栽过"三个工具漏登记"那种)。

动作分级 (klass)
───────────────────────────────────────────────────────────────
  read        读: 本机私有数据 (文件/邮件/记忆/会话史/截图/知识库)
  narrow      窄出口: 只送出去一个查询词/一个坐标/一个城市名 (web_search/openmeteo)
  carrier     载体出口: 能承载任意内容出去 (邮件正文+附件/短讯/推送/GitHub/浏览器提交)
  destructive 破坏性: 删/覆盖/关机/杀进程/强推
  internal    纯本地: 系统信息/记账/端口检查 (不判)

决策 (policy.mode, 与 Muse 的四档同名)
───────────────────────────────────────────────────────────────
  ask         (最严) 除只读外全要批
  auto_review (默认) read/internal 放行; narrow 干净放行; carrier 干净放行、
                     **带污点要批**; destructive 要批
  untrusted   只读放行, 其余全要批 (含 narrow)
  yolo        不拦 (只记账) —— 只该在隔离环境用

批准不是空的: 批一次 = 给一张**授权凭证** (grant)。carrier 的凭证当天有效
(不然每封邮件都问一次=审批疲劳), destructive 的凭证一次性。这就是 Muse 那句
"Muse Code remembers your permission selections" 的本机版。

铁律 / 诚实边界
───────────────────────────────────────────────────────────────
1. **闸门自身出故障时放行并记账** (不硬拦)。理由: 这是用户日常在用的 Agent,
   闸门一个 import 错误就让它全瘫, 代价比风险大。故障一律写进审计 (guard_error),
   由 19 点复盘抓 —— 不静默。
2. **不许"自动批准"**: 队列里的东西只有人 (或 `/egress approve`) 能批。
3. 污点**有半衰期** (TAINT_TTL), 不是永久记号: 读文件 30 分钟后不再算污点。
4. 复合 shell 命令**逐段**过闸 (抄 Muse 的 staged approval), 第一段过不去就整条拦,
   绝不"整条放行"或"整条拦下"。

对外接口
───────────────────────────────────────────────────────────────
    get_guard()                                    -> EgressGuard
    guard.check(tool, kwargs)                      -> {decision, klass, tainted, reason, pending_id?}
    guard.inject(tool, kwargs)                     -> (kwargs 已换真值, used, missing)  [调 secret_broker]
    guard.settle(tool, kwargs, result, success)    -> result (出向已抹真值) + 视情况打污点
    guard.list_pending() / approve(id) / deny(id)
    guard.stats() / audit_tail(n)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("core.egress_guard")

ROOT = Path(__file__).resolve().parent.parent

TAINT_TTL = 1800          # 污点半衰期: 30 分钟
AUDIT_KEEP = 2000
PENDING_KEEP = 200

# ══════════════════════════════════════════════════════════════════
# 动作分级表 —— 新增工具时必须在这里登记 (漏登记 = 默认按 carrier 处理, 从严)
# ══════════════════════════════════════════════════════════════════
READ_TOOLS = {
    "file_ops": {"read", "list", "search", "stat", "info"},
    "check_mail": {"list", "read", "stat", "search"},
    "kb_search": None, "kb_list": None, "kb_delete": None,
    "memory": None, "session_logs": None, "task_ledger": None, "task_flow": None,
    "usage_stats": None, "artifacts": None, "backup": {"list"},
    "ocr": None, "vision": None, "whisper": None, "repo_status": None,
    "vision_analyze": None, "service_check": None,
}
NARROW_TOOLS = {"web_search", "openmeteo", "nominatim", "image_gen"}
CARRIER_TOOLS = {
    "send_email", "sms_send", "push_notify", "ntfy", "im_notify",
    "github_api", "publish_pack", "hermes_bridge", "firecrawl",
    "browser", "plantuml", "shell_exec",
}
DESTRUCTIVE_TOOLS = {"shell_exec"}
INTERNAL_TOOLS = {"system_info", "chart", "ssq", "tiger_office", "office_cli",
                  "win32_input", "windows_desktop", "file_ops"}  # 见下方 action 细化

#: 会写到本机但不算破坏性 (Muse: in-workspace 写入不审批) 的 action
BENIGN_WRITE_ACTIONS = {"write", "append", "create", "save", "screenshot", "screenshots",
                        "click", "type", "key", "scroll", "move", "focus", "open", "list",
                        "read", "search", "stat", "info", "extract", "convert", "render"}

#: 破坏性 action / 关键词
DESTRUCTIVE_WORDS = (
    "delete", "remove", "rmtree", "unlink", "overwrite", "format", "shutdown",
    "restart", "kill", "truncate", "wipe", "clear_all", "purge",
)
SHELL_DANGEROUS_RE = [
    re.compile(r"\brm\s+-[a-zA-Z]*[rf]", re.I),
    re.compile(r"\bdel\s+/[sq]", re.I),
    re.compile(r"\brmdir\s+/s", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\btaskkill\b.*(/f|/im)", re.I),
    re.compile(r"\breg\s+delete\b", re.I),
    re.compile(r"\bRemove-Item\b.*-Recurse", re.I),
    re.compile(r"\bgit\s+push\b.*--force", re.I),
    re.compile(r"\bmkfs|dd\s+if=|diskpart", re.I),
    re.compile(r"\bregedit\b", re.I),
]
SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
#: carrier 里 payload 看起来像"成批搬运"的阈值 (只用于 narrow 的误判防护)
BULK_PAYLOAD_CHARS = 400

DEFAULT_POLICY = {
    "mode": "auto_review",     # ask | auto_review | untrusted | yolo
    "auto_allow_tools": [],    # 追加白名单 (用户自己加, 慎用)
    "never_allow_tools": [],   # 永久拦 (优先级最高)
}


def _p(env_key: str, default_name: str) -> Path:
    v = os.environ.get(env_key)
    return Path(v) if v else ROOT / "data" / default_name


class EgressGuard:
    def __init__(self):
        self.audit_path = _p("TMM_EGRESS_AUDIT", "egress_audit.jsonl")
        self.pending_path = _p("TMM_EGRESS_PENDING", "egress_pending.json")
        self.grants_path = _p("TMM_EGRESS_GRANTS", "egress_grants.json")
        self.taint_path = _p("TMM_EGRESS_TAINT", "egress_taint.json")
        self.policy_path = _p("TMM_EGRESS_POLICY", "egress_policy.json")
        self._taint = {}      # scope -> {"ts":..., "why":[...]}
        self._audit = []
        self._loaded = False
        self.load()

    # ── 落盘 ──────────────────────────────────────────────
    def _read_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("egress_guard: %s 读取失败 %s", path.name, e)
        return default

    def _write_json(self, path: Path, data):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception as e:
            logger.warning("egress_guard: %s 写入失败 %s", path.name, e)
            return False

    def load(self):
        self._taint = self._read_json(self.taint_path, {})
        self._pending = self._read_json(self.pending_path, [])
        self._grants = self._read_json(self.grants_path, [])
        self.policy = dict(DEFAULT_POLICY)
        self.policy.update(self._read_json(self.policy_path, {}))
        self._loaded = True
        return self.policy

    def save(self):
        self._write_json(self.taint_path, self._taint)
        self._write_json(self.pending_path, self._pending)
        self._write_json(self.grants_path, self._grants)

    # ── 污点 ──────────────────────────────────────────────
    def mark_read(self, tool: str, scope: str = "default", why: str = ""):
        """读到本机私有数据 → 给这个会话打污点。"""
        now = time.time()
        ent = self._taint.get(scope) or {"ts": now, "why": []}
        ent["ts"] = now
        if why:
            ent["why"] = (ent["why"] + [why])[-8:]
        self._taint[scope] = ent
        self._write_json(self.taint_path, self._taint)
        return ent

    def tainted(self, scope: str = "default"):
        ent = self._taint.get(scope)
        if not ent:
            return False, []
        if time.time() - float(ent.get("ts") or 0) > TAINT_TTL:
            return False, ent.get("why") or []      # 过期不算 (铁律 3)
        return True, ent.get("why") or []

    def clear_taint(self, scope: str = None):
        if scope:
            self._taint.pop(scope, None)
        else:
            self._taint = {}
        self._write_json(self.taint_path, self._taint)

    # ── 分级 ──────────────────────────────────────────────
    def classify(self, tool: str, kwargs: dict):
        """-> (klass, detail)。未登记的工具**从严**当 carrier。"""
        action = str((kwargs or {}).get("action") or "").lower()
        if tool in self.policy.get("never_allow_tools", []):
            return "blocked", "policy.never_allow_tools"
        if tool in self.policy.get("auto_allow_tools", []):
            return "allowed", "policy.auto_allow_tools"
        if tool.startswith("mcp_"):
            return "carrier", "mcp 外部工具 (未登记, 从严)"
        if tool in READ_TOOLS:
            acts = READ_TOOLS[tool]
            if acts is None or action in acts:
                return "read", f"action={action or '-'}"
        if tool in NARROW_TOOLS:
            return "narrow", f"action={action or '-'}"
        if tool in CARRIER_TOOLS:
            if tool == "shell_exec":
                # ★ 2026-09-26: 执行外部命令**一律**归破坏性 (= 一律要人批)。
                #   原来干净的 shell 命令算 carrier (干净时自动放行) —— 有了命令沙箱之后
                #   这个口径仍然太松: 沙箱只管"在哪儿跑、带什么环境、形状像不像危险",
                #   **管不了这条命令到底在干嘛** (一条干净形状的命令照样能发数据出去)。
                #   所以审批不省: 批一次给一张一次性凭证 (见 add_grant: destructive → one_shot)。
                return "destructive", "shell: 执行外部命令, 一律要批 (沙箱管范围, 不替代审批)"
            return "carrier", f"action={action or '-'}"
        # file_ops / windows_desktop / tiger_office 等按 action 细化
        if tool == "file_ops":
            if action in READ_TOOLS["file_ops"]:
                return "read", f"action={action}"
            if any(w in action for w in DESTRUCTIVE_WORDS):
                return "destructive", f"action={action}"
            return "internal", f"action={action or '-'} (本机写入, Muse 口径不审批)"
        if tool in DESTRUCTIVE_TOOLS:
            return "destructive", "工具级破坏性"
        if tool in INTERNAL_TOOLS or action in BENIGN_WRITE_ACTIONS:
            return "internal", f"action={action or '-'}"
        return "carrier", "未登记工具 (default deny-ish: 按载体处理)"

    def _shell_dangerous(self, kwargs: dict):
        cmd = str((kwargs or {}).get("command") or (kwargs or {}).get("cmd") or "")
        return bool(self._shell_hit_stage(cmd)[1])

    def _shell_hit_stage(self, cmd: str):
        """抄 Muse 的逐段复核: -> (段列表, 第一段危险内容 or None)"""
        stages = [s.strip() for s in SHELL_SPLIT_RE.split(str(cmd or "")) if s.strip()]
        for i, st in enumerate(stages, 1):
            for pat in SHELL_DANGEROUS_RE:
                if pat.search(st):
                    return stages, f"第{i}段: {st[:60]}"
        return stages, None

    # ── 授权凭证 (grant) ───────────────────────────────────
    def _today(self):
        return time.strftime("%Y-%m-%d")

    def _grant_for(self, tool: str, klass: str, kwargs: dict):
        now = time.time()
        keep = []
        hit = None
        for g in self._grants:
            if g.get("one_shot") and g.get("used"):
                continue
            if now > float(g.get("expires") or 0):
                continue
            if g.get("tool") == tool and g.get("klass") == klass:
                hit = g
            keep.append(g)
        self._grants = keep
        return hit

    def _consume(self, grant: dict):
        if not grant:
            return
        if grant.get("one_shot"):
            grant["used"] = True
            grant["used_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write_json(self.grants_path, self._grants)

    def add_grant(self, tool: str, klass: str, note: str = "", ttl: int = None):
        if ttl is None:
            ttl = 86400 if klass in ("carrier", "narrow") else 0     # 0 = 一次性
        expires = (time.time() + ttl) if ttl else (time.time() + 86400)
        g = {"tool": tool, "klass": klass, "note": note[:120],
             "granted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "expires": expires, "one_shot": (ttl == 0), "used": False,
             "day": self._today()}
        self._grants.append(g)
        self._write_json(self.grants_path, self._grants)
        return g

    # ── 待批队列 ───────────────────────────────────────────
    def _queue(self, tool, klass, kwargs, reason):
        pid = f"{int(time.time())}-{len(self._pending)+1}"
        ent = {
            "id": pid,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool, "klass": klass, "reason": reason,
            "params": sorted((kwargs or {}).keys()),
            "summary": self._summarize(tool, kwargs),
        }
        self._pending.append(ent)
        self._pending = self._pending[-PENDING_KEEP:]
        self._write_json(self.pending_path, self._pending)
        return ent

    def _summarize(self, tool, kwargs):
        """给人看的摘要, **绝不含**被抹过的值 (走 secret_broker.redact_obj)。"""
        try:
            safe = get_secret_broker().redact_obj(kwargs or {})
        except Exception:
            safe = {"(guard)": "redact unavailable"}
        bits = []
        for k in ("to", "recipient", "subject", "path", "command", "query", "city", "text"):
            if k in (safe or {}):
                v = str(safe[k])
                bits.append(f"{k}={v[:80]}")
        return f"{tool}: " + (" · ".join(bits) if bits else " · ".join(sorted(safe.keys())[:6]))

    def list_pending(self):
        return list(self._pending)

    def approve(self, pid: str, note: str = ""):
        for ent in list(self._pending):
            if str(ent.get("id")) == str(pid):
                self._pending.remove(ent)
                self._write_json(self.pending_path, self._pending)
                g = self.add_grant(ent["tool"], ent["klass"], note or f"批准 #{pid}")
                self._audit_append("approve", ent["tool"], ent["klass"],
                                   f"#{pid} {note}".strip(), "allow")
                return g
        return None

    def deny(self, pid: str, note: str = ""):
        for ent in list(self._pending):
            if str(ent.get("id")) == str(pid):
                self._pending.remove(ent)
                self._write_json(self.pending_path, self._pending)
                self._audit_append("deny", ent["tool"], ent["klass"],
                                   f"#{pid} {note}".strip(), "deny")
                return ent
        return None

    # ── 主判定 ────────────────────────────────────────────
    def check(self, tool: str, kwargs: dict = None, scope: str = "default"):
        kwargs = kwargs or {}
        mode = str(self.policy.get("mode") or "auto_review")
        try:
            klass, detail = self.classify(tool, kwargs)
        except Exception as e:
            logger.warning("egress_guard.classify 故障 %s —— 放行并记账 (铁律 1)", e)
            self._audit_append("guard_error", tool, "?", f"classify: {e}", "allow")
            return {"decision": "allow", "klass": "?", "tainted": False,
                    "reason": f"闸门故障放行: {e}"}

        is_tainted, why = self.tainted(scope)

        if klass in ("read", "internal", "allowed"):
            return {"decision": "allow", "klass": klass, "tainted": is_tainted,
                    "reason": detail}

        if mode == "yolo":
            self._audit_append("allow", tool, klass, "yolo 模式: 只记账", "allow")
            return {"decision": "allow", "klass": klass, "tainted": is_tainted,
                    "reason": "yolo (记账不拦)"}

        if klass == "blocked":
            self._audit_append("deny", tool, klass, detail, "deny")
            return {"decision": "deny", "klass": klass, "tainted": is_tainted,
                    "reason": f"策略永久拦下: {detail}"}

        g = self._grant_for(tool, klass, kwargs)
        if g:
            self._consume(g)
            self._audit_append("allow", tool, klass,
                               f"授权凭证 ({g.get('note','')})", "allow")
            return {"decision": "allow", "klass": klass, "tainted": is_tainted,
                    "reason": f"已有授权凭证: {g.get('note','')}"}

        need = False
        reason = ""
        if klass == "destructive":
            need, reason = True, f"破坏性动作 ({detail})"
        elif klass == "carrier":
            if mode == "untrusted":
                need, reason = True, f"untrusted 模式: 非只读一律要批 ({detail})"
            elif is_tainted:
                need = True
                reason = ("带污点出口: 本会话已读过本机私有数据"
                          f" ({', '.join(why[:3]) if why else '已读'}) → 这个动作能把数据带出去")
            else:
                return {"decision": "allow", "klass": klass, "tainted": False,
                        "reason": f"干净出口放行 ({detail})"}
        elif klass == "narrow":
            if mode in ("ask", "untrusted"):
                need, reason = True, f"{mode} 模式: 窄出口也要批"
            else:
                payload = str(kwargs.get("query") or kwargs.get("text") or "")
                if is_tainted and len(payload) > BULK_PAYLOAD_CHARS:
                    need = True
                    reason = ("带污点 + 载荷过长 (看起来在成批搬内容)"
                              f" {len(payload)} 字符 > {BULK_PAYLOAD_CHARS}")
                else:
                    return {"decision": "allow", "klass": klass, "tainted": is_tainted,
                            "reason": "窄出口 (只带查询词) 放行"}

        if not need:
            return {"decision": "allow", "klass": klass, "tainted": is_tainted, "reason": "放行"}

        ent = self._queue(tool, klass, kwargs, reason)
        self._audit_append("ask", tool, klass, f"#{ent['id']} {reason}", "ask")
        return {
            "decision": "ask", "klass": klass, "tainted": is_tainted,
            "reason": reason, "pending_id": ent["id"], "summary": ent["summary"],
            "hint": (f"已入待批队列 #{ent['id']}（未执行）。批准: /egress approve {ent['id']} "
                     f"· 查看: /egress list"),
        }

    # ── 出向抹真值 + 打污点 ────────────────────────────────
    def inject(self, tool: str, kwargs: dict):
        """入向: 占位符 → 真值。缺占位符值 **fail closed** (返回错误标记, 不执行)。"""
        try:
            broker = get_secret_broker()
        except Exception as e:
            logger.warning("egress_guard: secret_broker 不可用 %s", e)
            return kwargs, [], []
        new, used, missing = broker.inject_args_ex(kwargs or {})
        if used:
            broker.audit("inject", tool, used)
        if missing:
            broker.audit("missing", tool, missing, "未知占位符 → fail closed")
        return new, used, missing

    def settle(self, tool: str, kwargs: dict, result, success: bool = True, scope: str = "default"):
        """出向: 抹真值; 读类成功则打污点。返回处理过的 result。"""
        try:
            klass, _ = self.classify(tool, kwargs or {})
        except Exception:
            klass = "?"
        try:
            out = get_secret_broker().redact_obj(result)
        except Exception as e:
            logger.warning("egress_guard: redact 故障 %s", e)
            out = result
        try:
            if klass == "read" and success and isinstance(out, dict):
                got = str(out.get("output") or out.get("result") or "")[:60]
                self.mark_read(tool, scope, why=f"{tool} 读了本机数据")
                if got:
                    out["_egress_note"] = out.get("_egress_note") or "本会话已打污点 (读过私有数据); 带内容外发需批准"
        except Exception as e:
            logger.warning("egress_guard: mark_read 故障 %s", e)
        return out

    # ── 审计 ──────────────────────────────────────────────
    def _audit_append(self, kind, tool, klass, reason, decision):
        ent = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "tool": tool,
               "klass": klass, "decision": decision, "reason": reason[:200]}
        self._audit.append(ent)
        if len(self._audit) > 500:
            self._audit = self._audit[-500:]
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ent, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("egress_guard: 审计落盘失败 %s", e)
        return ent

    def audit_tail(self, n: int = 20):
        return self._audit[-n:]

    def stats(self, scope: str = "default"):
        is_t, why = self.tainted(scope)
        return {
            "mode": self.policy.get("mode"),
            "tainted": is_t,
            "taint_reasons": why[:5],
            "pending": len(self._pending),
            "grants": len(self._grants),
            "audit_entries": len(self._audit),
        }


_GUARD = None


def scrub_text(t: str) -> str:
    """给网关异常路径用的安全抹除 (闸门自身故障时也不能把真值写进日志/错误串)。"""
    try:
        return get_secret_broker().redact_text(str(t))[0]
    except Exception:
        return str(t)


def get_secret_broker():
    from core.secret_broker import get_broker
    return get_broker()


def get_guard() -> EgressGuard:
    global _GUARD
    if _GUARD is None:
        try:
            _GUARD = EgressGuard()
        except Exception as e:
            logger.warning("egress_guard: 初始化失败 %s", e)
            raise
    return _GUARD


def reload_guard():
    global _GUARD
    _GUARD = None
    return get_guard()
