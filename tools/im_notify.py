"""
IM Messaging Plugin - DingTalk/Feishu webhook bot
==================================================
Zero deps (stdlib only). Just paste webhook URL.

Setup:
  DingTalk: Group Settings -> Smart Assistant -> Add Bot -> Custom -> Copy webhook
  Feishu: Group Settings -> Bots -> Add Bot -> Custom -> Copy webhook
"""
import json
import time
import hmac
import hashlib
import base64
import urllib.request
import urllib.parse

PLUGIN = {
    "name": "im_notify",
    "description": "DingTalk/Feishu group bot messaging via webhook",
    "version": "1.0",
    "requires": [],
    "trigger": ["dingtalk", "feishu", "notify", "notification", "bot message"],
    "permission": ["system"],
    "category": "messaging",
}

# --- Configuration (paste your webhook URLs here) ---
DINGTALK_WEBHOOK = ""       # e.g. "https://oapi.dingtalk.com/robot/send?access_token=xxx"
DINGTALK_SECRET = ""        # optional signing secret

FEISHU_WEBHOOK = ""         # e.g. "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"


async def run(**kwargs) -> dict:
    action = kwargs.get("action", "dingtalk")

    if action == "dingtalk":
        return _send_dingtalk(kwargs)
    elif action == "feishu":
        return _send_feishu(kwargs)
    elif action == "status":
        return {
            "success": True,
            "output": {
                "dingtalk": bool(DINGTALK_WEBHOOK),
                "feishu": bool(FEISHU_WEBHOOK),
            }
        }
    return {"success": False, "error": f"Unknown action: {action}"}


def _send_dingtalk(kwargs) -> dict:
    if not DINGTALK_WEBHOOK:
        return {"success": False, "error": "DingTalk webhook not configured"}

    content = kwargs.get("content", kwargs.get("text", ""))
    if not content:
        return {"success": False, "error": "Content required"}

    msg_type = kwargs.get("msg_type", "text")
    title = kwargs.get("title", "Tiger Notification")
    url = DINGTALK_WEBHOOK

    # Signing (if secret configured)
    if DINGTALK_SECRET:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = timestamp + chr(10) + DINGTALK_SECRET
        hmac_code = hmac.new(
            DINGTALK_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = DINGTALK_WEBHOOK + "&timestamp=" + timestamp + "&sign=" + sign

    if msg_type == "markdown":
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": content}
        }
    else:
        at_mobiles = kwargs.get("at_mobiles", [])
        at_all = kwargs.get("at_all", False)
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {
                "atMobiles": at_mobiles if isinstance(at_mobiles, list) else [],
                "isAtAll": bool(at_all)
            }
        }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("errcode") == 0:
            return {"success": True, "output": "DingTalk message sent"}
        return {"success": False, "error": "DingTalk error: " + resp.get("errmsg", str(resp))}
    except Exception as e:
        return {"success": False, "error": "Send failed: " + str(e)}


def _send_feishu(kwargs) -> dict:
    if not FEISHU_WEBHOOK:
        return {"success": False, "error": "Feishu webhook not configured"}

    content = kwargs.get("content", kwargs.get("text", ""))
    if not content:
        return {"success": False, "error": "Content required"}

    msg_type = kwargs.get("msg_type", "text")
    title = kwargs.get("title", "Tiger Notification")

    if msg_type == "interactive":
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": kwargs.get("color", "orange")
                },
                "elements": [{"tag": "markdown", "content": content}]
            }
        }
    else:
        payload = {
            "msg_type": "text",
            "content": {"text": content}
        }

    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("code") == 0 or resp.get("StatusCode") == 0:
            return {"success": True, "output": "Feishu message sent"}
        return {"success": False, "error": "Feishu error: " + resp.get("msg", str(resp))}
    except Exception as e:
        return {"success": False, "error": "Send failed: " + str(e)}
