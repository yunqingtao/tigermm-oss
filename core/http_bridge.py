"""
TMM HTTP Bridge — exposes /process endpoint for VPS bridge to call.
v4.2.1: persistent bridge loop + per-request timeout wrapper.
"""
import json, threading, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import asyncio

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

logger = logging.getLogger("core.http_bridge")

_process_fn = None
_server = None
_main_loop = None
_bridge_loop = None

REQUEST_TIMEOUT = 120

def _get_bridge_loop():
    """Get or create a persistent event loop for bridge requests.
    🔴 用独立 loop（不依赖 main_loop）— main_loop 会被 CLI 的 input() 阻塞,
    导致 run_coroutine_threadsafe 提交的请求永不执行(手机端没反应)。
    Python 3.11+ asyncio.Semaphore 不绑定 loop, pipeline 可在独立 loop 运行。
    """
    global _bridge_loop
    if _bridge_loop is None or _bridge_loop.is_closed():
        _bridge_loop = asyncio.new_event_loop()
        def _run():
            asyncio.set_event_loop(_bridge_loop)
            _bridge_loop.run_forever()
        t = threading.Thread(target=_run, daemon=True, name="bridge-loop")
        t.start()
    return _bridge_loop

class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/process":
            self.send_response(404); self.end_headers(); return
        
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            # 🔴 编码容错: 调用方(relay_client/curl)可能发 GBK 中文, json.loads(bytes) 默认 UTF-8 会炸
            try:
                data = json.loads(body.decode("utf-8"))
            except UnicodeDecodeError:
                data = json.loads(body.decode("gbk", errors="replace"))
            text = data.get("text", "").strip()
            
            if not text:
                self._json(400, {"error": "missing text"})
                return
            
            loop = _get_bridge_loop()
            # Wrap with timeout to prevent single stuck request from blocking loop
            async def _timed():
                try:
                    return await asyncio.wait_for(_process_fn(text), timeout=REQUEST_TIMEOUT)
                except asyncio.TimeoutError:
                    return {"response": "虎哥正忙，稍等再试", "error": "timeout"}
            
            future = asyncio.run_coroutine_threadsafe(_timed(), loop)
            try:
                result = future.result(timeout=REQUEST_TIMEOUT + 10)
            except Exception:
                result = {"response": "虎哥正忙，稍等再试", "error": "timeout"}
            
            if isinstance(result, dict):
                resp = result.get("response", str(result))
            else:
                resp = str(result)
            
            self._json(200, {"response": resp})
        except ConnectionAbortedError:
            pass
        except Exception as e:
            # 🔴 记录真实异常 (之前静默吞掉导致手机端"处理异常"无法诊断)
            logger.exception("bridge /process error: %s", e)
            self._json(200, {"response": "处理异常，请重试"})
    
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self.send_response(404); self.end_headers()
    
    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def log_message(self, format, *args):
        pass


def start(process_fn, port=19529, main_loop=None):
    global _process_fn, _server, _main_loop
    _main_loop = main_loop or asyncio.get_event_loop()
    _process_fn = process_fn
    _server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()
    logger.info(f"HTTP bridge on port {port}")

def stop():
    global _server
    if _server:
        _server.shutdown()
        _server = None
