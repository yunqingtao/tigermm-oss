"""
TMM Relay Client — direct WebSocket connection to relay, no tunnel needed.
Replaces VPS bridge + SSH tunnel with a single outbound WS connection.
"""
import asyncio, json, os, aiohttp, websockets, logging, traceback, sys, time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("core.relay_client")

RELAY_URL = os.environ.get("TMM_RELAY_URL", "wss://maryask.com/ws/")


def _relay_token() -> str:
    """relay 凭据 —— 从配置读, **不硬编码** (2026-09-20 分发整改)。

    读取顺序: 环境变量 TMM_RELAY_TOKEN → relay_tokens.json["pc"] → relay_tokens.json["bot"]
    ★ 原来这里写死了 relay 凭据字面量。本文件在开发仓库里**未被跟踪**, 所以"扫 git 跟踪文件"
      的门禁没扫到它 —— 但生成分发仓库时它是从工作树复制的, 于是**会跟着出门**。
      (这个漏网正是"在分发包上再跑一次门禁"抓出来的)
    """
    t = os.environ.get("TMM_RELAY_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(Path(__file__).resolve().parent.parent / "relay_tokens.json", encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("pc") or d.get("bot") or "").strip()
    except Exception:
        return ""


TOKEN = _relay_token()
TMM_URL = os.environ.get("TMM_LOCAL_URL", "http://127.0.0.1:19529/process")


def _log(msg):
    """Log to stderr so it doesn't pollute the CLI."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True, file=sys.stderr)


if not TOKEN:
    _log("relay: 未配置 token (设 TMM_RELAY_TOKEN 或往 relay_tokens.json 里加 \"pc\") —— "
         "relay 连接会失败, 这是诚实失败, 不是静默降级")

# ★ 2026-09-20: 兼容旧调用点 —— 本函数原名 log(), 整改时改名 _log() 但文件内还有
#   8 处 `log(...)` 调用 (relay_client_loop 等)。不挂别名的话那些路径一跑就 NameError。
log = _log


async def relay_client_loop():
    """Connect to relay, forward messages to local TMM, reply back."""
    reconnect_delay = 3
    _fail_log_ts = 0.0  # 上次失败日志时间戳(限频用, 2026-08-18: 防重连刷屏)
    _connected_once = False
    
    while True:
        try:
            # 🔴 2026-08-24: 首次 connecting 不打 — main.py 已有"✓ 中继客户端已启动"状态行;
            # 重连时 L93/97 会打失败/重连信息(已限频)。此行使登录界面干净。
            async with websockets.connect(RELAY_URL, ping_interval=20, ping_timeout=10) as ws:
                # Auth
                await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
                resp = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(resp)
                if data.get("type") != "auth_ok":
                    log(f"Auth failed: {data}")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 60)
                    continue
                
                # 🔴 2026-08-24: authenticated 行同样去掉 — 状态已由 main.py"✓ 中继客户端已启动"覆盖,
                # 且重连成功会反复打(断网恢复时刷屏)。仅保留失败/重连日志(L93/97 已限频)。
                # log(f"Relay: authenticated as {data.get('user_name', '?')} "
                #     f"dev:{data.get('devices',0)} contacts:{len(data.get('contacts',[]))}")
                reconnect_delay = 3
                _connected_once = True
                
                async with aiohttp.ClientSession() as http:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        
                        mtype = msg.get("type")
                        if mtype == "message":
                            sender = msg.get("from", "")
                            sender_name = msg.get("from_name", "")
                            text = msg.get("text", "")
                            
                            try:
                                async with http.post(TMM_URL, json={"text": text},
                                                    timeout=aiohttp.ClientTimeout(total=180)) as r:
                                    reply = await r.json()
                                    response = reply.get("response", "")
                                    
                                    if response:
                                        # Send reply back through relay
                                        await ws.send(json.dumps({
                                            "type": "message",
                                            "to": sender,
                                            "text": response
                                        }, ensure_ascii=False))
                            except asyncio.TimeoutError:
                                        await ws.send(json.dumps({
                                            "type": "message",
                                            "to": sender,
                                            "text": "虎哥正在处理，稍等..."
                                        }, ensure_ascii=False))
                            except asyncio.TimeoutError:
                                log(f"timeout for {sender_name}")
                                await ws.send(json.dumps({
                                    "type": "message",
                                    "to": sender,
                                    "text": "虎哥处理超时，请稍后再试"
                                }, ensure_ascii=False))
                            except Exception as e:
                                log(f"TMM error: {e}")
                        
                        elif mtype == "pong":
                            pass  # keepalive
                        elif mtype == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        
        except websockets.ConnectionClosed:
            # 限频: 距上次失败日志 < 30s 则静默, 防重连刷屏 (2026-08-18)
            if time.time() - _fail_log_ts > 30:
                log(f"Relay disconnected, reconnecting in {reconnect_delay}s...")
                _fail_log_ts = time.time()
        except Exception as e:
            if time.time() - _fail_log_ts > 30:
                log(f"Relay error: {e}, reconnecting in {reconnect_delay}s...")
                _fail_log_ts = time.time()
        
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


def start_relay_client():
    """Start relay client in background thread."""
    import threading
    
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(relay_client_loop())
        except Exception as e:
            log(f"Relay client crashed: {e}")
    
    t = threading.Thread(target=_run, name="relay-client", daemon=True)
    t.start()
    log("Relay client started in background")
    return t
