"""
AutoMail — 定时查收件箱 + 分类 + 通知。
接入 CLI 主循环，每 10 轮检查一次。
"""
import logging, re, time
from datetime import datetime
from typing import Any

logger = logging.getLogger("core.auto_mail")

# ── Classifiers ──
SPAM_SENDERS = ["noreply@", "no-reply@", "newsletter@", "marketing@", "promo@"]
SPAM_SUBJECTS = ["优惠", "促销", "广告", "活动", "subscribe", "weekly", "digest"]
SECURITY_SENDERS = ["security", "安全", "account", "账号", "登录", "login", "verify"]
SECURITY_SUBJECTS = ["安全提醒", "security alert", "account", "账号", "密码", "password", "login"]
VIP_SENDERS = ["taoge", "涛哥", "黑山", "老板", "boss", "partner"]


def classify(msg: dict) -> str:
    """Classify email into: spam/security/vip/normal"""
    sender = (msg.get("from", "") or "").lower()
    subject = (msg.get("subject", "") or "").lower()

    # Spam
    for kw in SPAM_SENDERS:
        if kw in sender:
            return "spam"
    for kw in SPAM_SUBJECTS:
        if kw in subject:
            return "spam"

    # Security
    for kw in SECURITY_SENDERS:
        if kw in sender:
            return "security"
    for kw in SECURITY_SUBJECTS:
        if kw in subject:
            return "security"

    # VIP
    for kw in VIP_SENDERS:
        if kw in sender:
            return "vip"

    return "normal"


def check_and_notify(mail_tool: Any, notify_fn: Any) -> str | None:
    """Check inbox, classify, notify important ones.
    Returns summary string or None if nothing important.
    """
    try:
        result = mail_tool(action="list", limit=10)
    except Exception as e:
        logger.warning("auto_mail check failed: %s", e)
        return None

    if not result or not result.get("success"):
        return None

    mails = result.get("mails", [])
    if not mails:
        return None

    important = []
    spam_count = 0
    security_count = 0

    for m in mails:
        cat = classify(m)
        if cat == "vip":
            important.append(m)
        elif cat == "security":
            security_count += 1
        elif cat == "spam":
            spam_count += 1
        elif cat == "normal" and not m.get("seen"):
            important.append(m)

    # Build summary
    parts = []
    if important:
        parts.append(f"📬 {len(important)}封重要邮件:")
        for m in important[:3]:
            sender = m.get("from", "")[:20]
            subj = m.get("subject", "")[:40]
            parts.append(f"  • {sender}: {subj}")
        if len(important) > 3:
            parts.append(f"  ...还有{len(important)-3}封")

    if security_count > 0:
        parts.append(f"🔒 {security_count}封安全提醒")

    if spam_count > 3:
        parts.append(f"🗑 {spam_count}封垃圾邮件")

    if not parts:
        return None

    summary = "\n".join(parts)
    try:
        notify_fn("TMM 邮件", summary)
    except Exception:
        pass

    return summary


def auto_reply(mail_tool: Any, send_tool: Any, rules: list) -> int:
    """Auto-reply to matching emails. Returns count of replies sent.
    rules: list of {"from_keyword": "..", "subject_keyword": "..", "reply": ".."}
    """
    try:
        result = mail_tool(action="list", limit=10)
    except Exception:
        return 0

    if not result or not result.get("success"):
        return 0

    mails = result.get("mails", [])
    count = 0

    for m in mails:
        sender = (m.get("from", "") or "").lower()
        subject = (m.get("subject", "") or "").lower()

        for rule in rules:
            fk = (rule.get("from_keyword", "") or "").lower()
            sk = (rule.get("subject_keyword", "") or "").lower()
            if (not fk or fk in sender) and (not sk or sk in subject):
                try:
                    send_tool(
                        to=m.get("from", ""),
                        subject=f"Re: {m.get('subject', '')}",
                        body=rule["reply"],
                    )
                    count += 1
                    logger.info("auto-replied to %s", m.get("from", ""))
                except Exception as e:
                    logger.warning("auto-reply failed: %s", e)
                break  # one reply per mail

    return count
