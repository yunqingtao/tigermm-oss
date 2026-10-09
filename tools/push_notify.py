"""
Push Notification Plugin - ServerChan (Server酱) + PushPlus
============================================================
Free WeChat push notifications. No enterprise verification needed.

Setup:
  ServerChan: https://sct.ftqq.com -> WeChat scan -> copy SendKey
  PushPlus:   https://www.pushplus.plus -> WeChat scan -> copy Token
"""
import json
import os
import urllib.request

PLUGIN = {
    "name": "push_notify",
    "description": "WeChat push notifications via ServerChan/PushPlus (free, personal)",
    "version": "1.0",
    "requires": [],
    "trigger": ["push", "notify", "wechat", "alert", "notify"],
    "permission": ["system"],
    "category": "messaging",
}

# --- Configuration ---
# 凭据读取顺序 (与项目其他工具一致): 环境变量 → keys.json["notify"] → 都没有则**诚实报错**。
# ★ 2026-09-21 修: 原来这里硬编码了真 SendKey —— 分发包会把它原样带出去
#   (谁拿到包, 谁就能往你的微信推消息)。现在默认空, 由使用者自己填。
_ENV_SC = "TMM_SERVERCHAN_KEY"
_ENV_PP = "TMM_PUSHPLUS_TOKEN"


def _load_notify_cfg() -> dict:
    from pathlib import Path
    for cand in (Path(__file__).resolve().parent.parent / "keys.json", Path.cwd() / "keys.json"):
        try:
            if cand.is_file():
                cfg = json.loads(cand.read_text(encoding="utf-8")).get("notify")
                if isinstance(cfg, dict):
                    return cfg
        except Exception:
            continue
    return {}


_CFG = _load_notify_cfg()
SERVERCHAN_KEY = (os.environ.get(_ENV_SC) or _CFG.get("serverchan") or "").strip()
PUSHPLUS_TOKEN = (os.environ.get(_ENV_PP) or _CFG.get("pushplus") or "").strip()

_HOWTO = ("请在 keys.json 加一段 {\"notify\": {\"serverchan\": \"<SendKey>\"}} "
          "或设环境变量 " + _ENV_SC + " (SendKey 从 https://sct.ftqq.com 微信扫码获取)")


async def run(**kwargs) -> dict:
    action = kwargs.get("action", "serverchan")

    if action == "serverchan":
        return _send_serverchan(kwargs)
    elif action == "pushplus":
        return _send_pushplus(kwargs)
    elif action == "broadcast":
        return _send_both(kwargs)
    elif action == "status":
        return {
            "success": True,
            "output": {
                "serverchan": bool(SERVERCHAN_KEY),
                "pushplus": bool(PUSHPLUS_TOKEN),
            }
        }
    return {"success": False, "error": "Unknown action: " + action}


def _send_serverchan(kwargs) -> dict:
    if not SERVERCHAN_KEY:
        return {"success": False, "error": "ServerChan 未配置。" + _HOWTO}

    title = kwargs.get("title", kwargs.get("content", "Tiger Notification"))
    desp = kwargs.get("desp", kwargs.get("body", ""))
    openid = kwargs.get("openid", "")  # single recipient openid
    openids = kwargs.get("openids", [])  # multiple recipients

    # Collect all openids
    all_openids = []
    if openid:
        all_openids.append(openid)
    if openids:
        if isinstance(openids, str):
            all_openids.extend([o.strip() for o in openids.split(",") if o.strip()])
        else:
            all_openids.extend(openids)

    results = []

    # If no openids specified, send to self (no openid param = send to key owner)
    if not all_openids:
        url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
        data = {"title": title}
        if desp:
            data["desp"] = desp
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if resp.get("code") == 0:
                return {"success": True, "output": "ServerChan sent to self: " + title}
            return {"success": False, "error": "ServerChan error: " + resp.get("message", str(resp))}
        except Exception as e:
            return {"success": False, "error": "ServerChan failed: " + str(e)}

    # Send to each openid
    sent = []
    failed = []
    for oid in all_openids:
        url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
        data = {"title": title, "openid": oid}
        if desp:
            data["desp"] = desp
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if resp.get("code") == 0:
                sent.append(oid[:12] + "...")
            else:
                failed.append(oid[:12] + ": " + resp.get("message", "?"))
        except Exception as e:
            failed.append(oid[:12] + ": " + str(e)[:50])

    msg = "Sent to " + str(len(sent)) + "/" + str(len(all_openids))
    if failed:
        msg += " (" + str(len(failed)) + " failed)"
    return {"success": True, "output": msg, "sent": len(sent), "failed": len(failed)}


def _send_pushplus(kwargs) -> dict:
    if not PUSHPLUS_TOKEN:
        return {"success": False, "error": "PushPlus 未配置。请在 keys.json 的 notify.pushplus 填 Token "
                                           "(从 https://www.pushplus.plus 微信扫码获取), 或设环境变量 " + _ENV_PP}

    title = kwargs.get("title", kwargs.get("content", "Tiger Notification"))
    content = kwargs.get("content", kwargs.get("body", title))
    template = kwargs.get("template", "html")

    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": template,
    }

    try:
        req = urllib.request.Request(
            "https://www.pushplus.plus/send",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("code") == 200:
            return {"success": True, "output": "PushPlus push sent: " + title}
        return {"success": False, "error": "PushPlus error: " + resp.get("msg", str(resp))}
    except Exception as e:
        return {"success": False, "error": "PushPlus failed: " + str(e)}


def _send_both(kwargs) -> dict:
    """Send to both channels"""
    r1 = _send_serverchan(kwargs)
    r2 = _send_pushplus(kwargs)

    results = []
    if r1["success"]:
        results.append("ServerChan OK")
    else:
        results.append("ServerChan: " + r1.get("error", "?"))
    if r2["success"]:
        results.append("PushPlus OK")
    else:
        results.append("PushPlus: " + r2.get("error", "?"))

    return {"success": True, "output": " | ".join(results)}
