"""TMM MCP Server v1.0 -- Expose TMM tools over MCP stdio protocol.

Any MCP client can connect and use TMM tools (file_ops, windows_desktop, etc).
Usage: tmm --mcp-server
"""
import asyncio, json, logging, os, sys, traceback
from dataclasses import dataclass, field
logger = logging.getLogger("mcp_server")
@dataclass
class ToolDef:
    name: str
    description: str = ""
    params: list = field(default_factory=list)
    handler: object = None

class MCPServer:
    def __init__(self, name="tmm", version="4.2"):
        self.name = name; self.version = version; self.tools = {}; self._nid = 1
    def register(self, name, desc, handler, params=None):
        self.tools[name] = ToolDef(name, desc, params or [], handler)
    def register_plugin(self, name, info):
        m = info.get("module"); meta = getattr(m, "TOOL", {}) if m else {}
        self.tools[name] = ToolDef(name, meta.get("description","TMM: "+name), meta.get("params",[]), m)
    async def serve(self):
        logger.info("MCP Server: %d tools", len(self.tools))
        L = asyncio.get_event_loop()
        R = asyncio.StreamReader()
        await L.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(R), sys.stdin)
        wt, wp = await L.connect_write_pipe(lambda: asyncio.streams.FlowControlMixin(), sys.stdout.buffer)
        W = asyncio.StreamWriter(wt, wp, R, L)
        while True:
            try:
                ln = await R.readline()
                if not ln: break
                req = json.loads(ln.decode())
                resp = await self._handle(req)
                if resp: W.write((json.dumps(resp)+chr(10)).encode()); await W.drain()
            except json.JSONDecodeError: continue
            except Exception as e: logger.error("serve: %s", e)
    async def _handle(self, req):
        m = req.get("method",""); rid = req.get("id")
        try:
            if m == "initialize": return self._ok(rid, {"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":self.name,"version":self.version}})
            elif m == "tools/list": return self._ok(rid, {"tools":self._tool_schemas()})
            elif m == "tools/call": p=req.get("params",{}); return await self._call_tool(rid, p.get("name",""), p.get("arguments",{}))
            elif m == "notifications/initialized": return None
            else: return self._err(rid, -32601, "Unknown: "+m)
        except Exception as e: return self._err(rid, -32603, traceback.format_exc())
    async def _call_tool(self, rid, name, args):
        t = self.tools.get(name)
        if not t: return self._err(rid, -32602, "Not found: "+name)
        try:
            h = t.handler
            if hasattr(h, "execute"): r = h.execute(**args)
            elif callable(h): r = h(**args)
            else: return self._err(rid, -32603, "No handler")
            if asyncio.iscoroutine(r): r = await r
            return self._ok(rid, {"content":[{"type":"text","text":json.dumps(r,ensure_ascii=False)}]})
        except Exception as e: return self._err(rid, -32000, str(e))
    def _tool_schemas(self):
        s = []
        for t in self.tools.values():
            p = {}; r = []
            for pp in t.params:
                n = pp.get("name",""); p[n] = {"type":pp.get("type","string"),"description":pp.get("description","")}
                if pp.get("enum"): p[n]["enum"] = pp["enum"]
                if pp.get("required",False): r.append(n)
            s.append({"name":t.name,"description":t.description,"inputSchema":{"type":"object","properties":p,"required":r}})
        return s
    def _ok(self, rid, result):
        return {"jsonrpc":"2.0","id":rid,"result":result}
    def _err(self, rid, code, msg):
        return {"jsonrpc":"2.0","id":rid,"error":{"code":code,"message":msg}}

async def serve_mcp(plugin_mgr=None):
    s = MCPServer()
    if plugin_mgr:
        for name, info in plugin_mgr._plugins.items():
            try:
                if "module" not in info:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(name, info["path"])
                    if spec and spec.loader:
                        m = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(m)
                        info["module"] = m
                s.register_plugin(name, info)
            except Exception as e: logger.warning("plugin %s: %s", name, e)
    await s.serve()