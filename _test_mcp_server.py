"""TMM MCP 测试服务器 — 提供 echo(回显) 和 add(加法) 和 list_dir(列目录) 三个工具."""
import sys, json

def handle(line):
    req = json.loads(line)
    rid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "test-server", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "echo", "description": "回显输入文本", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "add", "description": "两数相加", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
            {"name": "list_dir", "description": "列出目录内容", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        ]}}
    if method == "tools/call":
        name = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments", {})
        if name == "echo":
            out = f"echo: {args.get('text', '')}"
        elif name == "add":
            out = f"add: {args.get('a', 0) + args.get('b', 0)}"
        elif name == "list_dir":
            import os
            p = args.get("path", ".")
            try:
                items = os.listdir(p)
                out = "目录内容:\n" + "\n".join(f"  {x}" for x in sorted(items)[:20])
            except Exception as e:
                out = f"读取失败: {e}"
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown tool {name}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": out}]}}
    return {"jsonrpc": "2.0", "id": rid, "result": {}}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        resp = handle(line)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}}) + "\n")
        sys.stdout.flush()
