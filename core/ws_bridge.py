"""
TMM WebSocket Bridge — lenient handshake WS server.
Works with websockets 9.x (VPS Python 3.6) which sends Connection: close.
"""
import asyncio, json, logging, struct, hashlib, base64, os

logger = logging.getLogger("core.ws_bridge")
_process_fn = None
_server = None
_main_loop = None


class WSBridgeProtocol(asyncio.Protocol):
    """Minimal WebSocket server — accepts any reasonable upgrade request."""
    
    def __init__(self):
        self._buffer = b""
        self._open = False
    
    def connection_made(self, transport):
        self.transport = transport
        logger.info(f"WS bridge connected: {transport.get_extra_info('peername')}")
    
    def data_received(self, data):
        if not self._open:
            self._handle_handshake(data)
        else:
            self._buffer += data
            self._process_frames()
    
    def _handle_handshake(self, data):
        """Accept WS upgrade — lenient, works with old clients."""
        request = data.decode("utf-8", errors="replace")
        if "Upgrade: websocket" not in request and "upgrade: websocket" not in request.lower():
            self.transport.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            self.transport.close()
            return
        
        # Extract Sec-WebSocket-Key
        key = None
        for line in request.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break
        
        if not key:
            self.transport.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            self.transport.close()
            return
        
        # Compute accept key
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        self.transport.write(response.encode())
        self._open = True
        logger.info("WS handshake OK")
    
    def _process_frames(self):
        """Parse WS frames from buffer."""
        while len(self._buffer) >= 2:
            opcode = self._buffer[0] & 0x0F
            masked = (self._buffer[1] & 0x80) != 0
            length = self._buffer[1] & 0x7F
            
            offset = 2
            if length == 126:
                if len(self._buffer) < 4:
                    return
                length = struct.unpack(">H", self._buffer[2:4])[0]
                offset = 4
            elif length == 127:
                if len(self._buffer) < 10:
                    return
                length = struct.unpack(">Q", self._buffer[2:10])[0]
                offset = 10
            
            mask_key = b""
            if masked:
                if len(self._buffer) < offset + 4:
                    return
                mask_key = self._buffer[offset:offset + 4]
                offset += 4
            
            if len(self._buffer) < offset + length:
                return
            
            payload = bytearray(self._buffer[offset:offset + length])
            if masked:
                for i in range(length):
                    payload[i] ^= mask_key[i % 4]
            payload = bytes(payload)
            
            self._buffer = self._buffer[offset + length:]
            
            if opcode == 0x8:  # close
                self.transport.close()
                return
            elif opcode == 0x9:  # ping → pong
                self._send_frame(0xA, payload)
            elif opcode == 0xA:  # pong
                pass
            elif opcode in (0x1, 0x2):  # text/binary
                asyncio.ensure_future(self._handle_message(payload.decode("utf-8")))
    
    def _send_frame(self, opcode, data):
        """Send a WS frame (server→client, unmasked)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        frame = bytearray([0x80 | opcode])
        length = len(data)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(data)
        self.transport.write(bytes(frame))
    
    async def _handle_message(self, text):
        """Process incoming message."""
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        
        msg_type = msg.get("type", "")
        req_id = msg.get("id", "")
        
        if msg_type == "msg":
            t = msg.get("text", "").strip()
            if not t:
                self._send_frame(0x1, json.dumps({"type":"error","id":req_id,"error":"missing text"}))
                return
            
            # Ack immediately
            self._send_frame(0x1, json.dumps({"type":"ack","id":req_id}))
            
            # Process pipeline in a separate thread with its own event loop
            import threading as _thr
            import asyncio as _asyncio
            
            def _process():
                try:
                    loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(loop)
                    result = loop.run_until_complete(_process_fn(t))
                    loop.close()
                    if isinstance(result, dict):
                        resp = result.get("response", str(result))
                    else:
                        resp = str(result)
                    self._send_frame(0x1, json.dumps(
                        {"type":"result","id":req_id,"response":resp}, ensure_ascii=False))
                except Exception as e:
                    self._send_frame(0x1, json.dumps(
                        {"type":"error","id":req_id,"error":str(e)}))
            
            _thr.Thread(target=_process, daemon=True).start()
        
        elif msg_type == "ping":
            self._send_frame(0x1, json.dumps({"type":"pong"}))
    
    def connection_lost(self, exc):
        logger.info(f"WS bridge disconnected")
        self._open = False


async def serve_ws(port=19530):
    """Start lenient WS server."""
    loop = asyncio.get_event_loop()
    server = await loop.create_server(WSBridgeProtocol, "127.0.0.1", port)
    logger.info(f"WS bridge on ws://127.0.0.1:{port}")
    return server


def start(process_fn, port=19530, main_loop=None):
    """Start WS bridge in background thread."""
    global _process_fn, _main_loop, _server
    _process_fn = process_fn
    if main_loop is None:
        main_loop = asyncio.get_event_loop()
    _main_loop = main_loop
    
    import threading
    
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _server = loop.run_until_complete(serve_ws(port))
            loop.run_forever()
        except Exception as e:
            logger.error(f"WS bridge error: {e}")
    
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info(f"WS bridge started on port {port}")


def stop():
    global _server
    if _server:
        _server.close()
        _server = None
