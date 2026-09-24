"""Tiger.M.M Bot - reads config from relay_tokens.json"""
import asyncio, json, re, sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))

# Load config from external files
with open(os.path.join(BASE, "relay_tokens.json"), "r") as f:
    _tk = json.load(f)
BOT = _tk["bot"]
USR = _tk["master"]
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

print("[bot] init...")
with open(os.path.join(BASE, "keys.json"), "r") as f:
    cfg = json.load(f)
if "ollama" in cfg and "url" not in cfg["ollama"]:
    cfg["ollama"]["url"] = "http://127.0.0.1:11434/v1"
from core.pipeline import Level4Pipeline
from core.model_client import ModelClient
pipeline = Level4Pipeline(ModelClient(cfg), cfg)
print("[bot] ready")

async def _recv(ws, t=60):
    try: return await asyncio.wait_for(ws.recv(), timeout=t)
    except asyncio.TimeoutError: return None

async def process_and_reply(ws, sender, text):
    print(f"[bot] msg: {text[:80]}")
    from datetime import datetime as _dt
    t = text.lower().strip()
    tkws = ["ji_now","ji_now2","now_time","today_date"]
    if any(k in t for k in tkws):
        now = _dt.now()
        w = ["mon","tue","wed","thu","fri","sat","sun"]
        reply = f"Today {now.month}/{now.day} {w[now.weekday()]} {now.hour}:{now.minute:02d}"
        await ws.send(json.dumps({"type":"message","to":sender,"text":reply}, ensure_ascii=False))
        return
    try:
        result = await pipeline.process(text)
        reply = result.get("response", "huge stuck")
    except Exception as e:
        reply = f"error: {e}"
    clean = reply
    em = re.search(r'"explain"\s*:\s*"([^"]+)"', clean)
    if em: clean = em.group(1)
    else:
        clean = re.sub(r'```json.*?```', '', clean, flags=re.DOTALL)
        clean = re.sub(r'\{[^{}]*"actions"[^}]*\}', '', clean, flags=re.DOTALL)
        clean = clean.strip() or reply.strip()
    if "screenshot_" in clean:
        import base64
        dtop = os.path.join(os.path.expanduser("~"),"Desktop")
        for w in clean.split():
            if w.startswith("screenshot_") and w.endswith(".jpg"):
                fp = os.path.join(dtop,w)
                if os.path.exists(fp):
                    with open(fp,"rb") as f2:
                        b64 = base64.b64encode(f2.read()).decode()
                    await ws.send(json.dumps({"type":"message","to":sender,"text":clean,"image":b64,"image_type":"jpg"}, ensure_ascii=False))
                    return
    await ws.send(json.dumps({"type":"message","to":sender,"text":clean[:2000]}, ensure_ascii=False))

async def main():
    while True:
        try:
            import websockets
            async with websockets.connect(RELAY, ping_interval=10, ping_timeout=5) as ws:
                await ws.send(json.dumps({"type":"auth","token":BOT}))
                raw = await _recv(ws, 5)
                if not raw: continue
                resp = json.loads(raw)
                if resp.get("type") != "auth_ok":
                    await asyncio.sleep(1)
                    continue
                print(f"[bot] ONLINE users={resp.get('users_online',[])}")
                while True:
                    raw = await _recv(ws, 0.5)
                    if raw is None: break
                    msg = json.loads(raw)
                    if msg.get("type") == "offline_messages":
                        for m in msg.get("messages",[]):
                            if m.get("type")=="message" and m.get("from")==USR:
                                await process_and_reply(ws, m["from"], m["text"])
                    elif msg.get("type") == "message" and msg.get("from") == USR:
                        await process_and_reply(ws, msg["from"], msg.get("text",""))
                while True:
                    raw = await _recv(ws, 30)
                    if raw is None: continue
                    msg = json.loads(raw)
                    if msg.get("type") == "offline_messages":
                        for m in msg.get("messages",[]):
                            if m.get("type")=="message" and m.get("from")==USR:
                                await process_and_reply(ws, m["from"], m["text"])
                    elif msg.get("type") == "message" and msg.get("from") == USR:
                        await process_and_reply(ws, msg["from"], msg.get("text",""))
        except Exception as e:
            print(f"[bot] DC: {e}")
            await asyncio.sleep(1)

asyncio.run(main())
