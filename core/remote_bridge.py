"""
RemoteBridge — 手机 ↔ TMM 双向指令通道。
以普通用户身份登录 relay，手机发消息给 TMM-PC → TMM 处理 → 结果回手机。
"""
import asyncio, json, logging, threading, time, os
from pathlib import Path
from typing import Any

logger = logging.getLogger("core.remote_bridge")
logger.setLevel(logging.INFO)

def _relay_url() -> str:
    """中继地址 —— 读取顺序: 环境变量 TMM_RELAY_URL → relay_tokens.json 的 url → 本机默认。

    ★ 2026-09-21: 原来这里写死了作者的 VPS 公网地址。分发包里不该出现个人基础设施,
      所以地址跟 token 一样放进 relay_tokens.json(不进仓库/不进分发, 走 secrets.enc 加密恢复)。
    """
    v = (os.environ.get("TMM_RELAY_URL") or "").strip()
    if v:
        return v
    for _c in (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay_tokens.json"),):
        try:
            with open(_c, encoding="utf-8") as _f:
                _u = str((json.load(_f) or {}).get("url") or "").strip()
            if _u:
                return _u
        except Exception:
            pass
    return "ws://127.0.0.1:9528"


RELAY_URL = _relay_url()
BRIDGE_NAME = "TMM-PC"
TOKEN_FILE = "bridge_token.txt"
PING_INTERVAL = 20


class RemoteBridge:
    """Background listener: login as TMM-PC user, process incoming messages."""

    def __init__(self, process_fn: Any):
        self.process_fn = process_fn
        self._ws = None
        self._running = False
        self._thread = None
        self._my_id = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("RemoteBridge started")

    def stop(self):
        self._running = False

    def _token_path(self) -> Path:
        return Path(__file__).parent.parent / "data" / TOKEN_FILE

    def _load_token(self) -> str | None:
        p = self._token_path()
        if p.exists():
            return p.read_text().strip()
        return None

    def _save_token(self, token: str):
        p = self._token_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token)

    def _run_loop(self):
        import websocket

        while self._running:
            ws = None
            try:
                ws = websocket.create_connection(RELAY_URL, timeout=10)
                logger.info("RemoteBridge connected to relay")

                token = self._load_token()
                if token:
                    ws.send(json.dumps({"type": "auth", "token": token}))
                else:
                    ws.send(json.dumps({"type": "register", "name": BRIDGE_NAME}))

                # Drain all post-auth messages
                ws.settimeout(3)
                for _ in range(10):
                    try:
                        raw = ws.recv()
                        if raw:
                            msg = json.loads(raw)
                            t = msg.get("type", "")
                            if t == "registered":
                                token = msg.get("token", "")
                                self._save_token(token)
                                logger.info("RemoteBridge registered")
                            elif t == "auth_ok":
                                logger.info("RemoteBridge authenticated, contacts=%d",
                                          len(msg.get("contacts", [])))
                            elif t in ("auth_fail", "register_fail"):
                                logger.warning("Auth failed, deleting token")
                                self._token_path().unlink(missing_ok=True)
                                break
                            else:
                                self._handle(msg, ws)
                    except websocket.WebSocketTimeoutException:
                        break

                if not self._running:
                    break

                # Main listen loop
                ws.settimeout(PING_INTERVAL)
                last_ping = time.time()

                while self._running:
                    try:
                        raw = ws.recv()
                        if not raw:
                            logger.info("RemoteBridge: connection closed")
                            break
                        msg = json.loads(raw)
                        self._handle(msg, ws)
                        last_ping = time.time()
                    except websocket.WebSocketTimeoutException:
                        # Send ping
                        try:
                            ws.send(json.dumps({"type": "ping"}))
                        except Exception:
                            logger.info("RemoteBridge: ping failed, reconnecting")
                            break
                    except Exception as e:
                        logger.warning("RemoteBridge recv error: %s", e)
                        break

            except Exception as e:
                logger.warning("RemoteBridge connect/run error: %s", e)

            finally:
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass

            if self._running:
                time.sleep(3)

        logger.info("RemoteBridge stopped")

    def _handle(self, msg: dict, ws):
        msg_type = msg.get("type", "")

        if msg_type == "offline_messages":
            for sub_msg in msg.get("messages", []):
                self._handle(sub_msg, ws)
            return

        if msg_type == "message":
            text = msg.get("text", "").strip()
            sender = msg.get("from", "")
            if not text or not sender:
                return

            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(self.process_fn(text))
                loop.close()

                if isinstance(result, dict):
                    response = result.get("response", str(result))
                else:
                    response = str(result)

                import re as _re
                response = _re.sub(r'\x1b\[[0-9;]*m', '', response)

                ws.send(json.dumps({
                    "type": "message",
                    "to": sender,
                    "text": response[:1000],
                }))
                logger.info("RemoteBridge replied to %s", sender[:20])
            except Exception as e:
                logger.warning("RemoteBridge process error: %s", e)
                try:
                    ws.send(json.dumps({
                        "type": "message",
                        "to": sender,
                        "text": f"[TMM-PC] 处理失败: {e}",
                    }))
                except Exception:
                    pass


# Singleton
_bridge: RemoteBridge | None = None


def get_bridge(process_fn=None) -> RemoteBridge:
    global _bridge
    if _bridge is None and process_fn:
        _bridge = RemoteBridge(process_fn)
    return _bridge
