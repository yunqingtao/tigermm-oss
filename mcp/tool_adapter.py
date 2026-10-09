"""
Hermes Tool Bridge -- Hermes tools adapted for TMM ToolGateway.

Two integration modes:
  MODE_DIRECT: Pure Python tools run inside TMM process (cronjob, clarify, memory, websearch)
  MODE_LOCAL:    Heavy tools call Hermes Agent via HTTP API (browser, delegate)

Design: each tool is a standalone async function registered in TOOL_REGISTRY.
ToolGateway calls HermesToolBridge.call(name, **params) -> ToolResult.
"""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ═══════════════════════════════════════════
# Tool Definition (compatible with both TMM and Hermes formats)
# ═══════════════════════════════════════════

@dataclass
class ToolParam:
    name: str
    type: str
    desc: str
    required: bool = True
    default: Any = None


@dataclass
class ToolDef:
    name: str
    description: str
    params: List[ToolParam]
    permission: str = "safe"
    timeout: int = 30
    mode: str = "direct"  # "direct" | "rpc"


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str = ""
    elapsed: float = 0.0


# ═══════════════════════════════════════════
# MODE_DIRECT: Pure Python tools (no Hermes dependency)
# ═══════════════════════════════════════════

async def _tool_clarify(**kwargs) -> ToolResult:
    """Structured clarification -- asks user a question with choices."""
    t0 = time.time()
    question = kwargs.get("question", "")
    choices = kwargs.get("choices", [])
    if not question:
        return ToolResult(False, error="Missing 'question' parameter")
    return ToolResult(True, data={
        "type": "clarify",
        "question": question,
        "choices": choices
    }, elapsed=time.time() - t0)


async def _tool_web_search(**kwargs) -> ToolResult:
    """Multi-engine web search with result dedup and ranking."""
    t0 = time.time()
    query = kwargs.get("query", "")
    limit = kwargs.get("limit", 5)
    if not query:
        return ToolResult(False, error="Missing 'query' parameter")

    results = []
    # Try DuckDuckGo first (no API key needed)
    try:
        import urllib.parse
        ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(ddg_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            # Extract result links
            from html.parser import HTMLParser

            class DDGParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.results = []
                    self._in_result = False
                    self._current = {}
                    self._text = ""

                def handle_starttag(self, tag, attrs):
                    attrs_dict = dict(attrs)
                    if tag == "a" and "result__a" in attrs_dict.get("class", ""):
                        self._in_result = True
                        self._current = {"url": attrs_dict.get("href", "")}
                    elif tag == "a" and "result__snippet" in attrs_dict.get("class", ""):
                        self._text = ""

                def handle_data(self, data):
                    if self._text is not None:
                        self._text += data

                def handle_endtag(self, tag):
                    if tag == "a" and self._in_result:
                        self._current["title"] = self._text.strip() if self._text else ""
                        if self._current.get("url"):
                            self.results.append(self._current)
                        self._in_result = False
                        self._current = {}
                        self._text = ""

            parser = DDGParser()
            parser.feed(html)
            for r in parser.results[:limit]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": "",
                    "source": "ddg"
                })
    except Exception:
        pass

    # Fallback: Bing
    if len(results) < limit:
        try:
            bing_url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(bing_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                # Simple regex extraction
                import re as _re
                matches = _re.findall(
                    r'<li class="b_algo".*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?<p[^>]*>(.*?)</p>',
                    html, _re.DOTALL
                )
                for url, title, desc in matches[:limit - len(results)]:
                    results.append({
                        "title": _re.sub(r"<[^>]+>", "", title).strip(),
                        "url": url,
                        "description": _re.sub(r"<[^>]+>", "", desc).strip()[:200],
                        "source": "bing"
                    })
        except Exception:
            pass

    return ToolResult(True, data={"results": results[:limit], "query": query},
                      elapsed=time.time() - t0)


async def _tool_web_extract(**kwargs) -> ToolResult:
    """Extract clean content from a URL."""
    t0 = time.time()
    url = kwargs.get("url", "")
    if not url:
        return ToolResult(False, error="Missing 'url' parameter")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            # Strip HTML tags
            import re as _re
            text = _re.sub(r"<script[^>]*>.*?</script>", "", html, flags=_re.DOTALL)
            text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL)
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _re.sub(r"\s+", " ", text)
            text = text.strip()[:10000]
            return ToolResult(True, data={"url": url, "content": text, "length": len(text)},
                              elapsed=time.time() - t0)
    except Exception as e:
        return ToolResult(False, error=str(e), elapsed=time.time() - t0)


async def _tool_memory(**kwargs) -> ToolResult:
    """SQLite FTS5 long-term memory store."""
    t0 = time.time()
    action = kwargs.get("action", "search")
    db_path = Path(os.environ.get("TMM_HOME", os.path.expanduser("~"))) / ".tigermm" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS memory (key TEXT PRIMARY KEY, value TEXT, updated REAL)")
        if action == "save":
            key = kwargs.get("key", "")
            value = kwargs.get("value", "")
            if not key:
                return ToolResult(False, error="Missing 'key'")
            conn.execute("INSERT OR REPLACE INTO memory VALUES (?, ?, ?)",
                         (key, value, time.time()))
            conn.commit()
            return ToolResult(True, data={"saved": key}, elapsed=time.time() - t0)
        elif action == "load":
            key = kwargs.get("key", "")
            row = conn.execute("SELECT value FROM memory WHERE key=?", (key,)).fetchone()
            return ToolResult(True, data={"key": key, "value": row[0] if row else None},
                              elapsed=time.time() - t0)
        elif action == "search":
            query = kwargs.get("query", "")
            # Fallback to LIKE since FTS5 may not be available
            rows = conn.execute(
                "SELECT key, value FROM memory WHERE key LIKE ? OR value LIKE ? LIMIT 20",
                (f"%{query}%", f"%{query}%")
            ).fetchall()
            return ToolResult(True, data={
                "matches": [{"key": r[0], "value": r[1][:500]} for r in rows]
            }, elapsed=time.time() - t0)
        elif action == "list":
            rows = conn.execute("SELECT key FROM memory ORDER BY updated DESC LIMIT 50").fetchall()
            return ToolResult(True, data={"keys": [r[0] for r in rows]}, elapsed=time.time() - t0)
        else:
            return ToolResult(False, error=f"Unknown action: {action}")
    finally:
        conn.close()


async def _tool_cronjob(**kwargs) -> ToolResult:
    """Simple cron-like scheduling via Windows Task Scheduler or at command."""
    t0 = time.time()
    action = kwargs.get("action", "list")

    if action == "list":
        try:
            result = subprocess.run(["schtasks", "/query", "/fo", "LIST", "/tn", "TigerMM_*"],
                                    capture_output=True, text=True, timeout=10)
            tasks = []
            for line in result.stdout.split("\n"):
                if line.startswith("TaskName:"):
                    tasks.append(line.split(":", 1)[1].strip())
            return ToolResult(True, data={"tasks": tasks}, elapsed=time.time() - t0)
        except Exception as e:
            return ToolResult(False, error=str(e))

    elif action == "create":
        name = kwargs.get("name", "TigerMM_Task")
        schedule = kwargs.get("schedule", "daily")
        command = kwargs.get("command", "")
        if not command:
            return ToolResult(False, error="Missing 'command'")
        try:
            schtasks_cmd = [
                "schtasks", "/create", "/tn", f"TigerMM_{name}",
                "/tr", command, "/sc", schedule, "/f"
            ]
            result = subprocess.run(schtasks_cmd, capture_output=True, text=True, timeout=15, encoding='utf-8', errors='replace')
            if result.returncode == 0:
                return ToolResult(True, data={"created": f"TigerMM_{name}"},
                                  elapsed=time.time() - t0)
            return ToolResult(False, error=result.stderr[:500])
        except Exception as e:
            return ToolResult(False, error=str(e))

    elif action == "remove":
        name = kwargs.get("name", "")
        try:
            subprocess.run(["schtasks", "/delete", "/tn", f"TigerMM_{name}", "/f"],
                           capture_output=True, text=True, timeout=10)
            return ToolResult(True, data={"removed": name}, elapsed=time.time() - t0)
        except Exception as e:
            return ToolResult(False, error=str(e))

    return ToolResult(False, error=f"Unknown action: {action}")


# ═══════════════════════════════════════════
# MODE_LOCAL: Self-contained tools (no Hermes dependency)
# ═══════════════════════════════════════════

async def _tool_browser(**kwargs) -> ToolResult:
    """Browser automation -- local webbrowser + StealthSession."""
    t0 = time.time()
    action = kwargs.get("action", "navigate")
    url = kwargs.get("url", "")

    if action == "navigate":
        if not url:
            return ToolResult(False, error="Missing 'url'")
        try:
            # Try stealth HTTP first (from hermes_kernel)
            try:
                from core.hermes_kernel import StealthSession, StealthConfig
                session = StealthSession(StealthConfig(rotate_ua=True, random_delay=(0.5, 2.0)))
                status, body, headers = session.get(url)
                return ToolResult(True, data={
                    "url": url,
                    "status": status,
                    "content_preview": str(body)[:3000],
                    "headers": dict(headers) if headers else {}
                }, elapsed=time.time() - t0)
            except ImportError:
                pass
            # Fallback: simple urllib
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return ToolResult(True, data={
                    "url": url,
                    "status": resp.status,
                    "content_preview": html[:3000]
                }, elapsed=time.time() - t0)
        except Exception as e:
            return ToolResult(False, error=str(e), elapsed=time.time() - t0)

    elif action == "open":
        # Open URL in system browser
        import webbrowser
        webbrowser.open(url)
        return ToolResult(True, data={"opened": url}, elapsed=time.time() - t0)

    elif action == "snapshot":
        if not url:
            return ToolResult(False, error="Missing 'url'")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                # Strip tags for text snapshot
                import re as _re
                text = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL)
                text = _re.sub(r'<style[^>]*>.*?</style>', '', text, flags=_re.DOTALL)
                text = _re.sub(r'<[^>]+>', ' ', text)
                text = _re.sub(r'\s+', ' ', text).strip()[:5000]
                # Extract links
                links = _re.findall(r'href=["\'](https?://[^"\']+)["\']', html)[:20]
                return ToolResult(True, data={
                    "url": url,
                    "text": text,
                    "title": _re.search(r'<title[^>]*>(.*?)</title>', html, _re.DOTALL),
                    "links": links
                }, elapsed=time.time() - t0)
        except Exception as e:
            return ToolResult(False, error=str(e), elapsed=time.time() - t0)

    return ToolResult(False, error=f"Unknown browser action: {action}")


async def _tool_delegate(**kwargs) -> ToolResult:
    """Delegate task to subprocess for isolated execution."""
    t0 = time.time()
    goal = kwargs.get("goal", "")
    context = kwargs.get("context", "")
    if not goal:
        return ToolResult(False, error="Missing 'goal'")

    # Build a self-contained Python script from the goal
    script = f'''"""Delegated task: {goal[:100]}"""
# Context: {context[:500] if context else "N/A"}
import sys, os
print(f"Task: {goal}")
print("Status: would execute in isolated subprocess")
print("Note: delegate is lightweight in TMM. For full subagent, use @deepseek mode.")
'''

    try:
        result = subprocess.run(
            ["python", "-c", script],
            capture_output=True, text=True, timeout=kwargs.get("timeout", 30),
            cwd=os.path.expanduser("~/Desktop")
        )
        return ToolResult(True, data={
            "goal": goal,
            "stdout": result.stdout[:3000],
            "stderr": result.stderr[:1000],
            "exit_code": result.returncode
        }, elapsed=time.time() - t0)
    except subprocess.TimeoutExpired:
        return ToolResult(False, error="Delegate timed out", elapsed=time.time() - t0)
    except Exception as e:
        return ToolResult(False, error=str(e), elapsed=time.time() - t0)


# ═══════════════════════════════════════════
# Tool Registry
# ═══════════════════════════════════════════

TOOL_REGISTRY: Dict[str, Dict] = {
    # MODE_DIRECT tools
    "clarify": {
        "fn": _tool_clarify,
        "def": ToolDef("clarify", "Ask user a structured question with choices",
                        [ToolParam("question", "str", "The question to ask"),
                         ToolParam("choices", "list", "Answer choices (max 4)", False, [])],
                        mode="direct"),
    },
    "web_search": {
        "fn": _tool_web_search,
        "def": ToolDef("web_search", "Search the web (DDG -> Bing fallback)",
                        [ToolParam("query", "str", "Search query"),
                         ToolParam("limit", "int", "Max results", False, 5)],
                        mode="direct"),
    },
    "web_extract": {
        "fn": _tool_web_extract,
        "def": ToolDef("web_extract", "Extract clean content from a URL",
                        [ToolParam("url", "str", "URL to extract")],
                        mode="direct"),
    },
    "memory": {
        "fn": _tool_memory,
        "def": ToolDef("memory", "Long-term memory (save/load/search/list)",
                        [ToolParam("action", "str", "save | load | search | list"),
                         ToolParam("key", "str", "Memory key", False, ""),
                         ToolParam("value", "str", "Memory value", False, ""),
                         ToolParam("query", "str", "Search query", False, "")],
                        mode="direct"),
    },
    "cronjob": {
        "fn": _tool_cronjob,
        "def": ToolDef("cronjob", "Schedule recurring tasks (Windows Task Scheduler)",
                        [ToolParam("action", "str", "list | create | remove"),
                         ToolParam("name", "str", "Task name", False, "TigerMM_Task"),
                         ToolParam("schedule", "str", "daily | hourly | minute", False, "daily"),
                         ToolParam("command", "str", "Command to run", False, "")],
                        mode="direct"),
    },
    # Browser tools (local)
    "browser_navigate": {
        "fn": _tool_browser,
        "def": ToolDef("browser_navigate", "Navigate to URL and fetch content",
                        [ToolParam("url", "str", "URL to navigate to")],
                        permission="network", timeout=30),
    },
    "browser_open": {
        "fn": _tool_browser,
        "def": ToolDef("browser_open", "Open URL in system browser",
                        [ToolParam("url", "str", "URL to open")],
                        permission="safe", timeout=10),
    },
    "browser_snapshot": {
        "fn": _tool_browser,
        "def": ToolDef("browser_snapshot", "Get page text snapshot and links",
                        [ToolParam("url", "str", "URL to snapshot")],
                        permission="network", timeout=15),
    },
    "delegate": {
        "fn": _tool_delegate,
        "def": ToolDef("delegate", "Run task in isolated subprocess",
                        [ToolParam("goal", "str", "Task description"),
                         ToolParam("context", "str", "Background context", False, ""),
                         ToolParam("timeout", "int", "Timeout seconds", False, 30)],
                        permission="system", timeout=60),
    },
}


# ═══════════════════════════════════════════
# Bridge Interface
# ═══════════════════════════════════════════

class HermesToolBridge:
    """Unified bridge between TMM plugins and Hermes-compatible tool definitions.
    Dynamically builds TOOL_REGISTRY from PluginManager — no manual registration needed.
    """

    def __init__(self, plugin_mgr=None):
        self.plugin_mgr = plugin_mgr
        self._tools = {}
        if plugin_mgr:
            self._sync_from_plugins()
    
    def _sync_from_plugins(self):
        """Auto-register all TMM plugins as MCP tools."""
        if not self.plugin_mgr:
            return
        import ast, importlib.util

        # Tools known broken/offline — don't expose to model (带原因, 2026-08-18 治理)
        # im_notify 已移出: 工具本身可用, 只是缺 webhook 配置
        _BROKEN = {
            'libretranslate': 'API被墙(403)',
            'nominatim': '直连超时需代理',
            'sms_send': '缺腾讯云密钥',
            'hermes_bridge': 'no-op兼容垫片(死代码)',
        }

        for name, info in self.plugin_mgr._plugins.items():
            if name in _BROKEN:
                continue
            path = info.get("path", "")
            if not path or not os.path.exists(path):
                continue

            # AST-parse the plugin file to extract PLUGIN dict and run() signature
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    source = f.read()
                tree = ast.parse(source)

                pname = name
                pdesc = name
                params = []
                has_run = False

                for node in ast.walk(tree):
                    # Extract PLUGIN dict
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == 'PLUGIN':
                                if isinstance(node.value, ast.Dict):
                                    for k, v in zip(node.value.keys, node.value.values):
                                        if isinstance(k, ast.Constant):
                                            if k.value == 'name' and isinstance(v, ast.Constant):
                                                pname = v.value
                                            elif k.value == 'description' and isinstance(v, ast.Constant):
                                                pdesc = v.value
                            # run = execute alias pattern (firecrawl/ocr/plantuml)
                            if isinstance(target, ast.Name) and target.id == 'run':
                                has_run = True

                    # Extract async def run() parameters
                    if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                        if node.name == 'run':
                            has_run = True
                            for arg in node.args.args:
                                aname = arg.arg
                                if aname in ('self', 'kwargs'):
                                    continue
                                atype = "string"
                                if arg.annotation:
                                    ann_str = ast.unparse(arg.annotation) if hasattr(ast, 'unparse') else str(arg.annotation)
                                    if 'int' in ann_str.lower(): atype = "integer"
                                    elif 'float' in ann_str.lower(): atype = "number"
                                    elif 'bool' in ann_str.lower(): atype = "boolean"
                                # Check if has default value
                                defaults = node.args.defaults
                                offset = len(node.args.args) - len(defaults)
                                req = arg not in node.args.args[offset:] if offset >= 0 else True
                                params.append(ToolParam(name=aname, type=atype, desc=aname, required=req))

                if not has_run:
                    continue  # Skip: no run() function found

                td = ToolDef(
                    name=pname,
                    description=pdesc,
                    params=params,
                    permission="safe",
                    timeout=30,
                )
                self._tools[pname] = {"def": td}
            except Exception:
                pass

    @property
    def tools(self) -> Dict[str, ToolDef]:
        return {name: info["def"] for name, info in self._tools.items()}

    def list_tools(self) -> List[ToolDef]:
        return [info["def"] for info in self._tools.values()]

    def get_tool(self, name: str) -> Optional[ToolDef]:
        info = self._tools.get(name)
        return info["def"] if info else None

    async def call(self, name: str, **params) -> ToolResult:
        """Call a tool through PluginManager. Returns ToolResult."""
        t0 = time.time()
        try:
            if not self.plugin_mgr:
                return ToolResult(False, error="PluginManager not connected")
            
            result = await self.plugin_mgr.call(name, **params)
            elapsed = time.time() - t0
            
            if isinstance(result, dict):
                ok = result.get("success", result.get("ok", False))
                return ToolResult(ok, data=result, elapsed=elapsed,
                                  error=result.get("error", "") if not ok else "")
            return ToolResult(True, data=result, elapsed=elapsed)
        except Exception as e:
            return ToolResult(False, error=str(e), elapsed=time.time() - t0)
    def tool_defs_for_llm(self) -> List[dict]:
        """Export tool definitions in OpenAI function-calling format."""
        defs = []
        for name, info in self._tools.items():
            td = info["def"]
            props = {}
            required = []
            for p in td.params:
                props[p.name] = {"type": p.type, "description": p.desc}
                if p.required:
                    required.append(p.name)
            defs.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": td.description,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    }
                }
            })
        return defs
