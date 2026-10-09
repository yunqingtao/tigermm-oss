"""
ntfy.sh push notification plugin - JSON API, no header encoding issues
"""
import urllib.request, urllib.error, json, ssl

TOOL = {
    "name": "ntfy",
    "description": "Send push notifications via ntfy.sh",
    "keywords": ["推送", "通知", "ntfy", "push", "提醒"],
    "params": [
        {"name": "topic", "type": "string", "required": True},
        {"name": "message", "type": "string", "required": True},
        {"name": "title", "type": "string", "required": False},
        {"name": "priority", "type": "integer", "required": False},
        {"name": "tags", "type": "string", "required": False},
        {"name": "click", "type": "string", "required": False},
    ]
}

async def run(**kwargs) -> dict:
    topic = kwargs.get("topic", "").strip() or "tmm"
    message = kwargs.get("message", "") or kwargs.get("text", "") or "Tiger.M.M 推送测试"
    title = kwargs.get("title", "")
    priority = kwargs.get("priority", 3)
    tags = kwargs.get("tags", "")
    click = kwargs.get("click", "")
    server = kwargs.get("server", "https://ntfy.sh").rstrip("/")

    if not topic or not message:
        return {"success": False, "output": "通知发送失败", "error": "topic and message required"}

    payload = {"topic": topic, "message": message}
    if title:
        payload["title"] = title
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if click:
        payload["click"] = click
    try:
        p = int(priority)
        payload["priority"] = max(1, min(5, p))
    except Exception:
        payload["priority"] = 3

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(server, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, context=ctx, timeout=10)
        body = resp.read().decode()
        return {"success": True, "message_id": resp.headers.get("X-Message-ID",""),
                "response": body.strip() or "sent"}
    except urllib.error.HTTPError as e:
        return {"success": False, "output": "通知发送失败", "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"success": False, "output": "通知发送失败", "error": str(e)}
