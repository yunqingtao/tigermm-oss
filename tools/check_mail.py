"""
Check Mail plugin — POP3 inbox reader via 163.com.
v1.2 — connection reuse, retry, read-single, error classification.
"""
import poplib, ssl, re, email, time, logging
from email.header import decode_header
from typing import Optional

logger = logging.getLogger("tools.check_mail")

PLUGIN = {
    "name": "check_mail",
    "description": "Check inbox via POP3 (163.com). Read recent emails or specific email by ID.",
    "version": "1.2",
    "trigger": ["收邮件", "查邮件", "看邮件", "check mail"],
    "permission": ["system"],
    "category": "automation",
}

import json as _json_mail
import os as _os_mail
from pathlib import Path as _Path_mail


def _mail_cfg() -> dict:
    """邮箱账号 —— 从配置读, **不硬编码** (2026-09-20 分发整改)。

    读取顺序: 环境变量 → keys.json["mail"]; 都没有则返回空 (调用方诚实报错)。
    ★ 原来 user/password 写死在源码里, 而源码被 git 跟踪 → 发布即泄露。
    """
    cfg = {"host": "pop.163.com", "port": 995, "user": "", "password": ""}
    cfg["user"] = _os_mail.environ.get("TMM_MAIL_USER", "")
    cfg["password"] = _os_mail.environ.get("TMM_MAIL_PASS", "")
    if not (cfg["user"] and cfg["password"]):
        for rel in ("keys.json", "keys_mail.json"):
            try:
                with open(_Path_mail(__file__).resolve().parent.parent / rel, encoding="utf-8") as f:
                    d = _json_mail.load(f).get("mail") or {}
                cfg["user"] = cfg["user"] or str(d.get("user", ""))
                cfg["password"] = cfg["password"] or str(d.get("password", ""))
                cfg["host"] = d.get("pop_host", cfg["host"])
                cfg["port"] = int(d.get("pop_port", cfg["port"]) or cfg["port"])
            except Exception:
                continue
    return cfg


def _mail_ready():
    c = _mail_cfg()
    if not c["user"] or not c["password"]:
        return False, ("邮箱没配置。请在 keys.json 里加一段:\n"
                       '  "mail": {"user": "你的邮箱@163.com", "password": "授权码"}\n'
                       "或设环境变量 TMM_MAIL_USER / TMM_MAIL_PASS。")
    return True, ""

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5  # seconds, exponential backoff


# ── connection pool ──

_conn: Optional[poplib.POP3_SSL] = None
_conn_at: float = 0
CONN_TTL = 60  # seconds before reconnect


def _get_conn():
    """Get or create a reusable POP3 connection."""
    global _conn, _conn_at
    now = time.time()
    if _conn is not None and (now - _conn_at) < CONN_TTL:
        try:
            # Quick health check: stat() is cheap
            _conn.stat()
            _conn_at = now
            return _conn, False  # (conn, is_new)
        except Exception:
            _close_conn()
    return _connect()


def _connect():
    """Establish a fresh POP3_SSL connection with retry."""
    global _conn, _conn_at
    _close_conn()
    for attempt in range(MAX_RETRIES):
        try:
            ctx = ssl.create_default_context()
            _mc = _mail_cfg()
            conn = poplib.POP3_SSL(_mc["host"], _mc["port"], context=ctx, timeout=15)
            conn.user(_mc["user"])
            conn.pass_(_mc["password"])
            _conn = conn
            _conn_at = time.time()
            logger.debug("POP3 connected (attempt %d)", attempt + 1)
            return conn, True
        except poplib.error_proto as e:
            err = str(e).lower()
            if "user" in err or "password" in err or "auth" in err:
                raise RuntimeError(f"AUTH_FAILURE: {e}")
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("POP3 connect attempt %d failed: %s, retrying in %.1fs",
                               attempt + 1, e, delay)
                time.sleep(delay)
            else:
                raise RuntimeError(f"CONNECT_FAILURE: {e}")
        except (OSError, ssl.SSLError, TimeoutError) as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("POP3 network error attempt %d: %s, retrying in %.1fs",
                               attempt + 1, e, delay)
                time.sleep(delay)
            else:
                raise RuntimeError(f"NETWORK_TIMEOUT: {e}")


def _close_conn():
    """Close pooled connection gracefully."""
    global _conn, _conn_at
    if _conn is not None:
        try:
            _conn.quit()
        except Exception:
            pass
        _conn = None
        _conn_at = 0


def _decode(val):
    if not val:
        return ""
    parts = decode_header(val)
    result = []
    for text, charset in parts:
        if isinstance(text, bytes):
            result.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(text))
    return "".join(result)


def _parse_email(raw_bytes: bytes) -> dict:
    """Parse a raw email into a structured dict with body + attachments."""
    msg = email.message_from_bytes(raw_bytes)

    subject_val = _decode(msg["Subject"])
    from_val = _decode(msg.get("From", "?"))
    date_val = msg.get("Date", "?")

    body = ""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))

            if "attachment" in disp:
                filename = _decode(part.get_filename() or "unnamed")
                payload = part.get_payload(decode=True)
                size = len(payload) if payload else 0
                attachments.append({
                    "filename": filename,
                    "size": size,
                    "content_type": content_type,
                })
            elif content_type == "text/plain" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset, errors="replace")
                    except Exception:
                        body = payload.decode("utf-8", errors="replace")
            elif content_type == "text/html" and not body:
                # Fallback: strip HTML tags for plain text body
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html = payload.decode(charset, errors="replace")
                        # Simple tag stripping
                        body = re.sub(r'<[^>]+>', '', html)
                        body = re.sub(r'\s+', ' ', body).strip()
                    except Exception:
                        pass
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except Exception:
                body = payload.decode("utf-8", errors="replace")

    return {
        "from": from_val,
        "subject": subject_val,
        "date": date_val,
        "body": body,
        "attachments": attachments,
    }


def _format_email(em: dict, idx: int = 0, full_body: bool = False) -> str:
    """Format one email for display."""
    lines = []
    if idx:
        lines.append(f"{idx}. {em['subject'][:50]}")
    else:
        lines.append(f"Subject: {em['subject']}")
    lines.append(f"   From: {em['from'][:50]}")
    lines.append(f"   Date: {em['date'][:30]}")
    if em.get("attachments"):
        att_names = ", ".join(a["filename"] for a in em["attachments"])
        lines.append(f"   Attachments: {att_names}")
    body = em.get("body", "") or em.get("body_preview", "")
    if body:
        if full_body:
            lines.append(f"\n{body[:3000]}")
        else:
            lines.append(f"   {body[:120].replace(chr(10), ' ').strip()}")
    return "\n".join(lines)


# ── main entry ──

async def run(action: str = "list", count: int = 10, mail_id: int = None,
              **kwargs):
    """Check inbox via POP3.

    Actions:
      list  — show recent emails (default)
      read  — read a specific email by ID (requires mail_id)
      stat  — just return inbox count, no content
    """
    conn = None
    try:
        conn, is_new = _get_conn()

        total, _ = conn.stat()

        # ── stat action ──
        if action == "stat":
            return {
                "success": True,
                "output": f"Inbox: {total} emails",
                "count": total,
                "emails": [],
            }

        resp, lines, _ = conn.list()
        ids = [int(l.split()[0]) for l in lines]

        if not ids:
            return {
                "success": True,
                "output": "Inbox empty",
                "count": 0,
                "emails": [],
            }

        # ── read action (force fresh connection) ──
        if action == "read":
            _close_conn()
            conn, is_new = _connect()
            if mail_id is None:
                return {"success": False, "error": "INVALID_PARAMS: mail_id required for action='read'", "error_type": "invalid_params"}

            if int(mail_id) not in ids:
                return {
                    "success": False,
                    "error": f"NOT_FOUND: email #{mail_id} does not exist (range: {ids[0]}-{ids[-1]})",
                    "error_type": "not_found",
                }

            resp, raw_lines, _ = conn.retr(int(mail_id))
            raw = b"\n".join(raw_lines)
            em = _parse_email(raw)
            em["id"] = mail_id

            return {
                "success": True,
                "output": _format_email(em, full_body=True),
                "email": em,
            }

        # ── list action (default) ──
        keyword = kwargs.get("keyword", "").strip()
        recent = ids[-count:]
        emails = []

        for mid in reversed(recent):
            try:
                resp, raw_lines, _ = conn.retr(mid)
                raw = b"\n".join(raw_lines)
                em = _parse_email(raw)
                em["id"] = mid
                em["body_preview"] = em["body"][:200].replace("\n", " ").strip()
                
                # Keyword filter: match subject + body
                if keyword:
                    search_text = (em.get("subject","") + " " + em.get("body","")).lower()
                    if keyword.lower() not in search_text:
                        continue  # skip non-matching
                
                emails.append(em)
            except Exception as e:
                logger.warning("Failed to fetch email #%d: %s", mid, e)
                continue  # skip corrupt emails

        if keyword:
            out_lines = [f"{_mail_cfg()['user']} 搜索「{keyword}」: {len(emails)}/{total} 封匹配\n"]
        else:
            out_lines = [f"{_mail_cfg()['user']} 收件箱: {total} 封邮件\n"]
        for i, em in enumerate(emails, 1):
            out_lines.append(_format_email(em, idx=i))
            out_lines.append("")

        return {
            "success": True,
            "output": "\n".join(out_lines),
            "count": total,
            "shown": len(emails),
            "emails": emails,
        }

    except RuntimeError as e:
        err_str = str(e)
        if err_str.startswith("AUTH_FAILURE"):
            return {"success": False, "error": err_str, "error_type": "auth"}
        elif err_str.startswith("NETWORK_TIMEOUT"):
            return {"success": False, "error": err_str, "error_type": "network"}
        elif err_str.startswith("CONNECT_FAILURE"):
            return {"success": False, "error": err_str, "error_type": "connect"}
        return {"success": False, "error": err_str, "error_type": "unknown"}

    except Exception as e:
        _close_conn()
        return {"success": False, "error": str(e), "error_type": "fatal"}
