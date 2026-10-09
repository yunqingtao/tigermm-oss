"""
Send Email plugin — SMTP email via 163.com.
账号从 keys.json['mail'] 或环境变量 TMM_MAIL_USER / TMM_MAIL_PASS 读取 (不硬编码)
"""
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

PLUGIN = {
    "name": "send_email",
    "description": "Send email via SMTP (163.com). Supports attachments (comma-separated paths).",
    "version": "1.1",
    "trigger": ["发邮件", "发送邮件", "send email", "mail"],
    "permission": ["system"],
    "category": "automation",
}

import time as _time_smtp
import json as _json_smtp
import os as _os_smtp
from pathlib import Path as _Path_smtp

_last_send = 0
_MIN_GAP = 5  # seconds between sends to avoid 163 anti-spam


def _mail_cfg() -> dict:
    """邮箱账号 —— 从配置读, **不硬编码** (2026-09-20 分发整改)。

    ★ 原来 user/password 是写死在源码里的 (而源码是被 git 跟踪的 → 一旦发布就出门)。
      现在读取顺序: 环境变量 → keys.json["mail"] → .env
      都拿不到就**诚实报错**, 不再假装能发 (也不许退回某个写死的账号)。
    """
    cfg = {"host": "smtp.163.com", "port": 465, "from_name": "虎哥 Tiger.M.M",
           "user": "", "password": ""}
    cfg["user"] = _os_smtp.environ.get("TMM_MAIL_USER", "") or cfg["user"]
    cfg["password"] = _os_smtp.environ.get("TMM_MAIL_PASS", "") or cfg["password"]
    if not (cfg["user"] and cfg["password"]):
        for rel in ("keys.json", "keys_mail.json"):
            p = _Path_smtp(__file__).resolve().parent.parent / rel
            try:
                with open(p, encoding="utf-8") as f:
                    d = _json_smtp.load(f).get("mail") or {}
                cfg["user"] = cfg["user"] or str(d.get("user", ""))
                cfg["password"] = cfg["password"] or str(d.get("password", ""))
                cfg["host"] = d.get("smtp_host", cfg["host"])
                cfg["port"] = int(d.get("smtp_port", cfg["port"]) or cfg["port"])
            except Exception:
                continue
    return cfg


def _mail_ready() -> tuple[bool, str]:
    c = _mail_cfg()
    if not c["user"] or not c["password"]:
        return False, ("邮箱没配置。请在 keys.json 里加一段:\n"
                       '  "mail": {"user": "你的邮箱@163.com", "password": "SMTP授权码"}\n'
                       "或设环境变量 TMM_MAIL_USER / TMM_MAIL_PASS。")
    return True, ""


_RE_EMAIL_OK = None


def _email_ok(addr: str) -> bool:
    """收件人必须是显式 ASCII 邮箱 —— 含中文 = 上游解析错, 不是地址。"""
    global _RE_EMAIL_OK
    if _RE_EMAIL_OK is None:
        import re as _re
        _RE_EMAIL_OK = _re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')
    return bool(_RE_EMAIL_OK.fullmatch((addr or "").strip()))


async def run(to: str, subject: str, body: str, cc: str = "", is_html: bool = False,
               attachments: str = "", action=None, **kwargs):
    # ★ 2026-09-20: 配置门禁 —— 没配邮箱时**诚实报错**, 不假装能发
    _ok, _why = _mail_ready()
    if not _ok:
        return {"success": False, "output": _why, "error": "mail not configured"}
    # ★ 2026-09-23 加 (实测事故): 收件人为空/非法时**当场说清** ——
    #   原来直接拿去发 → smtplib 抛 SMTPRecipientsRefused({}) → 文案变成
    #   "SMTP 错误: {}" (一个空字典, 真因被吃干净)。
    _to_str = (to or "").strip()
    if not _to_str:
        return {"success": False, "output": "收件人地址为空 —— 没解析出发给谁, 邮件没发出。",
                "error": "收件人为空"}
    if not _email_ok(_to_str):
        return {"success": False,
                "output": f"收件人地址不合法: {_to_str!r} (含中文或非邮箱格式, 邮件没发出)",
                "error": f"收件人非法: {_to_str}"}
    # ★ 2026-09-19 修 (真 bug): 本工具原为**严格签名** —— 而 IR chain 会把意图里的
    #   action(="send") 注入调用参数 → "run() got an unexpected keyword argument 'action'"。
    #   其他工具(file_ops/tiger_office/system_info…)都有 **kwargs 兜着, 只有这里会崩。
    #   实测: "发邮件给 x@y.com 主题:嗨 内容:你好" → ✗ 报错, 邮件根本没发出去。
    #   action 对本工具无意义(只有一个动作), 收下即忽略; **kwargs 兼容未来/多余参数。
    import os as _os
    global _last_send
    elapsed = _time_smtp.time() - _last_send
    if elapsed < _MIN_GAP:
        wait = _MIN_GAP - elapsed
        _time_smtp.sleep(wait)
    try:
        msg = MIMEMultipart()
        _mc = _mail_cfg()
        msg["From"] = f"{_mc['from_name']} <{_mc['user']}>"
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc

        content_type = "html" if is_html else "plain"
        msg.attach(MIMEText(body, content_type, "utf-8"))

        # Attach files (comma-separated paths)
        attached = []
        if attachments:
            from email.mime.base import MIMEBase
            from email import encoders
            for fpath in attachments.split(","):
                fpath = fpath.strip()
                if fpath and _os.path.isfile(fpath):
                    try:
                        with open(fpath, "rb") as af:
                            part = MIMEBase("application", "octet-stream")
                            part.set_payload(af.read())
                        encoders.encode_base64(part)
                        part.add_header("Content-Disposition", f"attachment; filename={_os.path.basename(fpath)}")
                        msg.attach(part)
                        attached.append(_os.path.basename(fpath))
                    except Exception:
                        pass

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(_mc["host"], _mc["port"], context=context) as server:
            server.login(_mc["user"], _mc["password"])
            server.send_message(msg)

        recipients = [to]
        if cc:
            recipients.append(cc)
        att_msg = f" (含{len(attached)}个附件)" if attached else ""
        _last_send = _time_smtp.time()
        return {
            "success": True,
            "output": f"邮件已发送 -> {', '.join(recipients)}{att_msg}",
            "to": to,
            "subject": subject,
            "attachments": attached,
        }
    except smtplib.SMTPAuthenticationError:
        return {"success": False, "error": "SMTP 认证失败，检查授权码是否正确"}
    except smtplib.SMTPRecipientsRefused as e:
        # ★ 2026-09-23: 空字典异常单独兜 —— SMTPRecipientsRefused({}) 的 str() 就是 "{}"
        _rej = getattr(e, "recipients", None) or {}
        return {"success": False,
                "error": f"收件人被服务器拒收: {_rej if _rej else '收件人地址被拒绝(空或非法)'}"}
    except smtplib.SMTPException as e:
        return {"success": False, "error": f"SMTP 错误: {type(e).__name__}: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
