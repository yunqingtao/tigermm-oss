"""TMM Push — send notification to phone via relay server"""
import asyncio, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load config
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(BASE, "relay_tokens.json"), "r") as f:
    _tk = json.load(f)

def _relay_url() -> str:
    """中继地址 —— 读取顺序: 环境变量 TMM_RELAY_URL → relay_tokens.json 的 url → 本机默认。

    ★ 2026-09-21: 原来这里写死了作者的 VPS 公网地址。分发包里不该出现个人基础设施,
      所以地址跟 token 一样放进 relay_tokens.json(不进仓库/不进分发, 走 secrets.enc 加密恢复)。
    """
    v = (os.environ.get("TMM_RELAY_URL") or "").strip()
    if v:
        return v
    for _c in (os.path.join(BASE, "relay_tokens.json"),):
        try:
            with open(_c, encoding="utf-8") as _f:
                _u = str((json.load(_f) or {}).get("url") or "").strip()
            if _u:
                return _u
        except Exception:
            pass
    return "ws://127.0.0.1:9528"


RELAY = _relay_url()
BOT_TOKEN = _tk["bot"]
MASTER_TOKEN = _tk["master"]

async def push(message):
    """Send a message from TMM to the phone app"""
    try:
        import websockets
        async with websockets.connect(RELAY) as ws:
            # Auth as bot
            await ws.send(json.dumps({"type": "auth", "token": BOT_TOKEN}))
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            
            # Send message to master
            payload = json.dumps({
                "type": "message",
                "to": MASTER_TOKEN,
                "text": message
            }, ensure_ascii=False)
            await ws.send(payload)
            print(f"PUSHED: {message[:100]}")
            return True
    except Exception as e:
        print(f"PUSH FAIL: {e}")
        return False

if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "TMM test push"
    asyncio.run(push(msg))
