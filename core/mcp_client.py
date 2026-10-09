"""
MCP Client — connect to Model Context Protocol servers.
Supports stdio and SSE transports. Discovers and calls MCP tools.
"""
import json, asyncio, subprocess, logging, urllib.request, urllib.error
from pathlib import Path
from typing import Any

logger = logging.getLogger("core.mcp_client")

MCP_VERSION = "2024-11-05"


class MCPTool:
    """Represents a tool exposed by an MCP server."""
    def __init__(self, server_name: str, name: str, description: str, input_schema: dict):
        self.server_name = server_name
        self.name = name
        self.full_name = f"mcp_{server_name}__{name}"  # namespaced
        self.description = description
        self.input_schema = input_schema

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function calling format."""
        props = {}
        required = []
        if self.input_schema and "properties" in self.input_schema:
            for pname, pdef in self.input_schema["properties"].items():
                props[pname] = {
                    "type": pdef.get("type", "string"),
                    "description": pdef.get("description", ""),
                }
            required = self.input_schema.get("required", [])
        return {
            "type": "function",
            "function": {
                "name": self.full_name,
                "description": f"[MCP:{self.server_name}] {self.description}",
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                } if props else {"type": "object", "properties": {}},
            }
        }


class MCPClient:
    """Manages multiple MCP server connections."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.servers: dict[str, dict] = {}  # server_name -> config
        self.tools: list[MCPTool] = []
        self._processes: dict[str, subprocess.Popen] = {}

    def load_config(self, servers: list[dict]):
        """Load MCP server configs.
        Each: {"name": "xxx", "transport": "stdio", "command": "python", "args": ["server.py"]}
        Or:   {"name": "xxx", "transport": "sse", "url": "http://localhost:8080/sse"}
        """
        for s in servers:
            name = s.get("name", "")
            if name and name not in self.servers:
                self.servers[name] = s

    async def discover_tools(self, server_name: str) -> list[MCPTool]:
        """Connect to MCP server and discover its tools."""
        cfg = self.servers.get(server_name)
        if not cfg:
            return []

        transport = cfg.get("transport", "stdio")

        if transport == "stdio":
            return await self._discover_stdio(server_name, cfg)
        elif transport == "sse":
            return await self._discover_sse(server_name, cfg)
        return []

    async def _discover_stdio(self, server_name: str, cfg: dict) -> list[MCPTool]:
        """Discover tools from a stdio MCP server."""
        import subprocess
        cmd = [cfg["command"]] + cfg.get("args", [])
        env = cfg.get("env", {})

        try:
            # ★ 2026-09-19 修 (用户报"自动拉起的那个黑框"): 引擎自己是用
            #   DETACHED_PROCESS|CREATE_NO_WINDOW 起的 (无控制台)。此时再起一个**控制台**
            #   子进程而不带创建标志, Windows 会给它**新分配一个控制台** —— Win11 默认终端是
            #   Windows Terminal, 于是屏幕上就多一个黑框 (实测: 窗口标题 = python.exe 路径,
            #   出现时刻 = MCP 后台刷新, 进程树里 MCP 子进程带自己的 conhost)。
            #   修法: 显式 CREATE_NO_WINDOW (仅 Windows)。
            _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if __import__("os").name == "nt" else 0
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**__import__('os').environ, **env} if env else None,
                creationflags=_flags,
            )
        except Exception as e:
            logger.warning("MCP stdio start failed for %s: %s", server_name, e)
            return []

        # Initialize
        init_req = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_VERSION, "capabilities": {}, "clientInfo": {"name": "tmm", "version": "4.2"}}
        }) + "\n"

        try:
            proc.stdin.write(init_req.encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            init_resp = json.loads(line.decode())
        except Exception as e:
            logger.warning("MCP init failed for %s: %s", server_name, e)
            proc.kill()
            return []

        # List tools
        list_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        try:
            proc.stdin.write(list_req.encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            list_resp = json.loads(line.decode())
        except Exception as e:
            logger.warning("MCP tools/list failed for %s: %s", server_name, e)
            proc.kill()
            return []

        # Keep process alive for future calls.
        # ★ 先 reap 旧进程 —— 否则 refresh 每次重新 discover 都会覆盖 `_processes[name]`,
        #   把上一个进程丢成孤儿 (实测: 引擎后台刷新会持续漏 MCP 子进程)。
        await self._reap(server_name)
        self._processes[server_name] = proc

        tools = []
        for t in list_resp.get("result", {}).get("tools", []):
            tool = MCPTool(
                server_name=server_name,
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            tools.append(tool)
        logger.info("MCP %s: discovered %d tools", server_name, len(tools))
        return tools

    async def _discover_sse(self, server_name: str, cfg: dict) -> list[MCPTool]:
        """Discover tools from an SSE MCP server via HTTP."""
        url = cfg.get("url", "")
        if not url:
            return []

        # For SSE, we use a simplified JSON-RPC over HTTP approach
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
        }).encode()

        try:
            req = urllib.request.Request(url, data=body,
                                        headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
        except Exception as e:
            logger.warning("MCP SSE %s: %s", server_name, e)
            return []

        tools = []
        for t in data.get("result", {}).get("tools", []):
            tools.append(MCPTool(
                server_name=server_name,
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            ))
        return tools

    def get_all_tools(self) -> list[MCPTool]:
        return self.tools

    def get_schemas(self) -> list[dict]:
        """Get all MCP tool schemas in OpenAI format."""
        return [t.to_openai_schema() for t in self.tools]

    async def refresh_all(self):
        """Refresh tool lists from all configured servers."""
        self.tools = []
        for name in list(self.servers.keys()):
            try:
                tools = await self.discover_tools(name)
                self.tools.extend(tools)
            except Exception as e:
                logger.warning("MCP refresh %s failed: %s", name, e)

    async def call_tool(self, full_name: str, **kwargs) -> dict:
        """Call an MCP tool by its full name (mcp_server__tool)."""
        for tool in self.tools:
            if tool.full_name == full_name:
                server_name = tool.server_name
                cfg = self.servers.get(server_name)
                if not cfg:
                    return {"success": False, "error": f"MCP server {server_name} not found"}

                if cfg.get("transport") == "stdio":
                    return await self._call_stdio(server_name, tool.name, kwargs)
                elif cfg.get("transport") == "sse":
                    return await self._call_sse(server_name, tool.name, kwargs, cfg)
        return {"success": False, "error": f"MCP tool {full_name} not found"}

    async def _call_stdio(self, server_name: str, tool_name: str, args: dict) -> dict:
        proc = self._processes.get(server_name)
        if not proc:
            return {"success": False, "error": "MCP server not connected"}

        req = json.dumps({
            "jsonrpc": "2.0", "id": 100, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args}
        }) + "\n"

        try:
            proc.stdin.write(req.encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            resp = json.loads(line.decode())
        except Exception as e:
            return {"success": False, "error": str(e)}

        result = resp.get("result", {})
        content = result.get("content", [])
        text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return {"success": True, "output": "\n".join(text_parts) or str(result)}

    async def _call_sse(self, server_name: str, tool_name: str, args: dict, cfg: dict) -> dict:
        url = cfg.get("url", "")
        body = json.dumps({
            "jsonrpc": "2.0", "id": 100, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args}
        }).encode()
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            result = data.get("result", {})
            return {"success": True, "output": str(result.get("content", result))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _reap(self, server_name: str):
        """kill 并 await 某个 server 的旧进程, 从表里摘掉。

        两处需要: ①refresh 重新 discover 前 (否则 `_processes[name] = proc` 覆盖旧进程 → 每次刷新漏一个)
                  ②aclose 收尾。
        必须 `await proc.wait()` —— 光 kill 的话 asyncio 传输留到 GC 才收, 那时 loop 已关
        (实测 `RuntimeError: Event loop is closed` + 子进程变孤儿)。
        """
        old = self._processes.pop(server_name, None)
        if old is None:
            return
        try:
            old.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(old.wait(), timeout=5)
        except Exception as e:
            logger.debug("MCP %s reap: %s", server_name, e)

    async def aclose(self):
        """关闭所有 stdio 子进程 (逐个 reap, 含 await wait)。"""
        for name in list(self._processes.keys()):
            await self._reap(name)

    def shutdown(self):
        """同步尽力而为地关闭 (保留兼容)。异步上下文请用 `aclose()`。"""
        for name, proc in list(self._processes.items()):
            try:
                proc.kill()
            except Exception:
                pass
        self._processes.clear()


# ═══ Singleton ═══
_mcp: MCPClient | None = None


def get_mcp_client(data_dir: Path = None) -> MCPClient:
    global _mcp
    if _mcp is None and data_dir is not None:
        _mcp = MCPClient(data_dir)
    return _mcp
