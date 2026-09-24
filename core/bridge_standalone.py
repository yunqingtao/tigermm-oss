"""
TMM-PC Bridge — standalone process.
Relay ↔ file-based inbox/outbox ↔ TMM CLI.
"""
import websocket, json, time, sys, os, re

def _relay_url() -> str:
    """中继地址 —— 读取顺序: 环境变量 TMM_RELAY_URL → relay_tokens.json 的 url → 本机默认。

    ★ 2026-09-21: 原来这里写死了作者的 VPS 公网地址。分发包里不该出现个人基础设施,
      所以地址跟 token 一样放进 relay_tokens.json(不进仓库/不进分发, 走 secrets.enc 加密恢复)。
    """
    v = (os.environ.get("TMM_RELAY_URL") or "").strip()
    if v:
        return v
    for _c in (os.path.join(PROJECT_ROOT, "relay_tokens.json"),):
        try:
            with open(_c, encoding="utf-8") as _f:
                _u = str((json.load(_f) or {}).get("url") or "").strip()
            if _u:
                return _u
        except Exception:
            pass
    return "ws://127.0.0.1:9528"


RELAY = _relay_url()
NAME = "TMM-PC"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(PROJECT_ROOT, "data", "bridge_token.txt")
INBOX_FILE = os.path.join(PROJECT_ROOT, "data", "bridge_inbox.json")
OUTBOX_FILE = os.path.join(PROJECT_ROOT, "data", "bridge_outbox.json")
_msg_counter = 0

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return None

def save_token(t):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(t)

def write_inbox(sender, text):
    """Write message to TMM inbox. Returns msg_id."""
    global _msg_counter
    _msg_counter += 1
    msg_id = str(_msg_counter)
    os.makedirs(os.path.dirname(INBOX_FILE), exist_ok=True)
    try:
        existing = []
        try:
            with open(INBOX_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        existing.append({"sender": sender, "text": text, "msg_id": msg_id})
        with open(INBOX_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
    except Exception as e:
        print(f"[bridge] inbox write error: {e}", flush=True)
    return msg_id

def poll_outbox(msg_id, timeout=30):
    """Wait for TMM to write response. Returns text or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(OUTBOX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                if item.get("msg_id") == msg_id:
                    # Remove this item
                    data.remove(item)
                    with open(OUTBOX_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    return item.get("text", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    return None

def run():
    token = load_token()
    
    while True:
        ws = None
        try:
            print(f"[bridge] connecting...", flush=True)
            ws = websocket.create_connection(RELAY, timeout=10)
            
            if token:
                ws.send(json.dumps({"type": "auth", "token": token}))
            else:
                ws.send(json.dumps({"type": "register", "name": NAME}))
            
            ws.settimeout(3)
            authed = False
            while True:
                try:
                    raw = ws.recv()
                    if not raw:
                        break
                    msg = json.loads(raw)
                    t = msg.get("type", "")
                    if t == "registered":
                        token = msg.get("token", "")
                        save_token(token)
                        print(f"[bridge] registered", flush=True)
                    elif t == "auth_ok":
                        authed = True
                        print(f"[bridge] authenticated", flush=True)
                    elif t in ("auth_fail", "register_fail"):
                        print(f"[bridge] auth failed", flush=True)
                        if os.path.exists(TOKEN_FILE):
                            os.remove(TOKEN_FILE)
                        token = None
                        break
                    elif t in ("message", "offline_messages"):
                        messages = [msg] if t == "message" else msg.get("messages", [])
                        for m in messages:
                            text = m.get("text", "").strip()
                            sender = m.get("from", "")
                            if text and sender:
                                print(f"[bridge] ← {sender[:12]}: {text[:40]}", flush=True)
                                mid = write_inbox(sender, text)
                                reply = poll_outbox(mid, timeout=30)
                                if reply:
                                    reply = re.sub(r'\x1b\[[0-9;]*m', '', reply)
                                    ws.send(json.dumps({"type": "message", "to": sender, "text": reply[:1000]}))
                                    print(f"[bridge] → replied", flush=True)
                                else:
                                    ws.send(json.dumps({"type": "message", "to": sender, "text": "[TMM-PC] 超时，PC可能未登录TMM"}))
                                    print(f"[bridge] → timeout", flush=True)
                except websocket.WebSocketTimeoutException:
                    break
            
            if not authed:
                ws.close()
                time.sleep(5)
                continue
            
            ws.settimeout(20)
            print("[bridge] listening", flush=True)
            
            while True:
                try:
                    raw = ws.recv()
                    if not raw:
                        print("[bridge] closed", flush=True)
                        break
                    msg = json.loads(raw)
                    t = msg.get("type", "")
                    if t in ("message", "offline_messages"):
                        messages = [msg] if t == "message" else msg.get("messages", [])
                        for m in messages:
                            text = m.get("text", "").strip()
                            sender = m.get("from", "")
                            if text and sender:
                                print(f"[bridge] ← {sender[:12]}: {text[:40]}", flush=True)
                                mid = write_inbox(sender, text)
                                reply = poll_outbox(mid, timeout=30)
                                if reply:
                                    reply = re.sub(r'\x1b\[[0-9;]*m', '', reply)
                                    ws.send(json.dumps({"type": "message", "to": sender, "text": reply[:1000]}))
                                    print(f"[bridge] → replied", flush=True)
                                else:
                                    ws.send(json.dumps({"type": "message", "to": sender, "text": "[TMM-PC] PC离线或TMM未登录"}))
                except websocket.WebSocketTimeoutException:
                    try:
                        ws.send(json.dumps({"type": "ping"}))
                    except Exception:
                        break
                except Exception as e:
                    print(f"[bridge] error: {e}", flush=True)
                    break
                    
        except Exception as e:
            print(f"[bridge] connect error: {e}", flush=True)
        
        try:
            ws.close()
        except Exception:
            pass
        
        time.sleep(3)


if __name__ == "__main__":
    run()
