"""
Tool schema generator — converts Tiger.M.M plugins to OpenAI function calling format.
"""
import importlib.util
from pathlib import Path


# Built-in schemas for system tools without TOOL metadata
BUILTIN_SCHEMAS = {
    "shell_exec": {
        "name": "shell_exec",
        "description": "Execute a Windows command (cmd.exe). "
                       "Use for: dir, mkdir, running scripts, pip/npm. "
                       "Do NOT use for disk/disk space checks — system_info handles that.",
        "params": [
            {"name": "command", "type": "string", "required": True,
             "description": "The command to execute (e.g., 'dir', 'python script.py')"}
        ]
    },
    "system_info": {
        "name": "system_info",
        "description": "Get system info. action='time': current date/time (几点/日期/星期). "
                       "action='disk': drive usage C/D/E (total/used/free GB). "
                       "action='sysinfo': OS/host/Python version. action='ps': running processes. "
                       "Use this for time/disk space queries instead of shell_exec.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["time", "sysinfo", "disk", "ps", "network"],
             "description": "What to check: 'time' (current date/time), 'disk' (drive space), 'sysinfo' (OS info), 'ps' (processes)"}
        ]
    },

    "file_ops": {
        "name": "file_ops",
        "description": "File operations - PREFERRED for all file work. Supports: read files with optional chunking "
                       "(action='read', use offset/limit for large files), write text files (action='write'), "
                       "list directory (action='list'), delete files (action='delete'). "
                       "For large files, use offset (line number) and limit (line count) to read in chunks. "
                       "Always check file size first - files >500KB should be read in chunks.",
        "params": [
            {"name": "action", "type": "string", "required": True,
             "description": "Operation: 'read', 'write', 'list', 'delete', 'copy', 'move'"},
            {"name": "path", "type": "string", "required": True,
             "description": "File or directory path (absolute, e.g., D:olderile.txt)"},
            {"name": "content", "type": "string", "required": False,
             "description": "Content to write (only for 'write' action)"},
            {"name": "dest", "type": "string", "required": False,
             "description": "Destination path (only for 'copy' and 'move')"},
            {"name": "offset", "type": "integer", "required": False,
             "description": "Line number to start reading from (1-indexed). For chunked reads of large files."},
            {"name": "limit", "type": "integer", "required": False,
             "description": "Maximum lines to read. Default 500. Use with offset for chunked reading."}
        ]
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email via SMTP. Use when user asks to send email, mail someone, etc.",
        "params": [
            {"name": "to", "type": "string", "required": True,
             "description": "Recipient email address, e.g. user@example.com"},
            {"name": "subject", "type": "string", "required": True,
             "description": "Email subject line"},
            {"name": "body", "type": "string", "required": True,
             "description": "Email body content"},
            {"name": "cc", "type": "string", "required": False,
             "description": "CC recipients (comma separated)"},
            {"name": "is_html", "type": "boolean", "required": False,
             "description": "Whether body is HTML format"}
        ]
    },
    "check_mail": {
        "name": "check_mail",
        "description": "Check email inbox via POP3. action='list': list recent emails (returns emails array with id/subject/from/date). action='read': read specific email by ID. action='stat': inbox statistics. Use when user asks to check/read/see emails or inbox.",
        "params": [
            {"name": "action", "type": "string", "required": True,
             "enum": ["list", "read", "stat"],
             "description": "'list' (recent emails), 'read' (specific email), 'stat' (inbox stats)"},
            {"name": "count", "type": "integer", "required": False,
             "description": "Number of emails to list (for 'list', default 10)"},
            {"name": "mail_id", "type": "integer", "required": False,
             "description": "Email ID to read (for 'read' action)"},
            {"name": "keyword", "type": "string", "required": False,
             "description": "Filter emails by keyword in subject (for 'list')"}
        ]
    },

    "windows_desktop": {
        "name": "windows_desktop",
        "description": "Windows桌面操控: list_windows(列出所有窗口), capture(截图+元素树), click(点击元素), type_text(输入文字), scroll(滚动), press_key(按键). 先capture再click/type_text.",
        "params": [
            {"name": "action", "type": "string", "required": True,
             "enum": ["capture", "screenshot", "click", "move", "type", "type_text", "list_windows", "find_window", "scroll", "press_key", "key", "hotkey", "screenshot"],
             "description": "Action: click/move/type/hotkey/screenshot"},
            {"name": "x", "type": "integer", "required": False,
             "description": "X coordinate for click/move"},
            {"name": "y", "type": "integer", "required": False,
             "description": "Y coordinate for click/move"},
            {"name": "text", "type": "string", "required": False,
             "description": "Text to type (for 'type' action)"},
            {"name": "keys", "type": "string", "required": False,
             "description": "Key combo, e.g. 'ctrl+c' (for 'hotkey' action)"}
        ]
    },
}

def load_tool_metadata(tools_dir):
    """Load TOOL metadata from all plugin .py files. Returns {tool_name: {meta}}."""
    tools = {}
    for f in sorted(tools_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f.stem, str(f))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # ★ 2026-09-19 修 (真缺陷): 只认 `TOOL` 约定 → 漏掉 13 个用老约定
                #   `PLUGIN = {...}` 声明的工具 (tiger_office / send_email / check_mail /
                #   whisper / voice / shell_exec …) —— 工厂编译技能时看不到它们,
                #   而 12 个正式技能里 9 个靠 tiger_office。两种约定都要认。
                #   关键词字段也对齐: PLUGIN 用 "trigger", TOOL 用 "keywords"。
                meta = getattr(mod, "TOOL", None) or getattr(mod, "PLUGIN", None) or {}
                if meta.get("name"):
                    tools[meta["name"]] = {
                        "name": meta["name"],
                        "description": meta.get("description", ""),
                        "keywords": list(meta.get("keywords") or meta.get("trigger") or []),
                        "params": meta.get("params", []),
                        "path": str(f),
                        "declared_as": "TOOL" if getattr(mod, "TOOL", None) else "PLUGIN",
                    }
        except Exception:
            pass
    return tools


def to_openai_schema(tool_meta):
    """Convert a single tool's metadata to OpenAI function calling schema."""
    params = tool_meta.get("params", [])
    properties = {}
    required = []

    type_map = {"str": "string", "int": "integer", "float": "number",
                "bool": "boolean", "list": "array", "dict": "object"}

    for p in params:
        json_type = type_map.get(p.get("type", "string"), p.get("type", "string"))
        prop = {"type": json_type, "description": p.get("description", "")}
        if "enum" in p:
            prop["enum"] = p["enum"]
        properties[p["name"]] = prop
        if p.get("required", False):
            required.append(p["name"])

    return {
        "type": "function",
        "function": {
            "name": tool_meta["name"],
            "description": tool_meta.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            } if properties else {"type": "object", "properties": {}},
        }
    }


def get_all_schemas(plugin_mgr, mcp_client=None):
    """Get OpenAI tool schemas from plugin manager. Merges built-in + TOOL metadata + MCP external tools."""
    schemas = []
    added_names = set()

    # Add built-in system tool schemas
    for name, meta in BUILTIN_SCHEMAS.items():
        schemas.append(to_openai_schema(meta))
        added_names.add(name)

    # Add TOOL-metadata schemas from plugins (skip if already in built-in)
    for plugin_name in plugin_mgr._plugins:
        try:
            plg = plugin_mgr._plugins[plugin_name]
            spec = importlib.util.spec_from_file_location(plugin_name, plg["path"])
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                meta = getattr(mod, "TOOL", {})
                if meta.get("name") and meta.get("params"):
                    if meta["name"] not in added_names:
                        schemas.append(to_openai_schema(meta))
                        added_names.add(meta["name"])
        except Exception:
            pass

    # Add MCP external tools (mcp_<server>__<tool>) — 一次接线吃整个生态
    if mcp_client is not None:
        try:
            for s in mcp_client.get_schemas():
                fname = s.get("function", {}).get("name", "")
                if fname and fname not in added_names:
                    schemas.append(s)
                    added_names.add(fname)
        except Exception:
            pass

    return schemas



# ═══ Dynamic Tool Grouping ═══
# Instead of sending all schemas, send only relevant groups based on message intent.

TOOL_GROUPS = {
    "file": ["file_ops", "shell_exec"],
    "web": ["browser", "web_search", "openmeteo", "nominatim", "libretranslate", "firecrawl", "plantuml", "ocr"],
    "system": ["system_info", "windows_desktop"],
    "comm": ["send_email", "sms_send", "ntfy", "push_notify", "im_notify"],
    "office": ["tiger_office", "office_cli"],
    "all": [],  # special: include everything
}

GROUP_TRIGGERS = {
    "file": ["file_ops", "shell_exec", "文件", "读取", "写入", "代码", "命令", "运行",
             "dir", "read", "write", "core/", "tools/", ".py", "源码"],
    "web": ["搜索", "打开", "https://", "http://", "网页", "浏览器", "天气", "翻译",
            "browser", "web_search", "openmeteo", "查询", "抓取", "架构图", "流程图", "时序图", "类图", "plantuml", "ocr", "识别", "firecrawl"],
    "system": ["截图", "桌面", "系统", "进程", "磁盘", "窗口", "点击", "screenshot", "打开"],
    "comm": ["发送", "邮件", "短信", "通知", "推送", "send_email", "sms_send"],
    "office": ["excel", "word", "表格", "文档", "xlsx", "docx"],
}


def get_relevant_groups(message: str) -> list[str]:
    """Determine which tool groups are relevant based on message content."""
    scores = {}
    for group, triggers in GROUP_TRIGGERS.items():
        score = sum(1 for t in triggers if t.lower() in message.lower())
        if score > 0:
            scores[group] = score

    if not scores:
        return ["file", "web", "system"]  # default: common groups

    # Always include "file" + top 2 matching groups
    selected = ["file"]
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    for group, _ in ranked[:2]:
        if group not in selected:
            selected.append(group)

    return selected



def score_tool_relevance(tool_name: str, tool_desc: str, message: str) -> float:
    """Score tool relevance to the message. Higher = more relevant."""
    msg_lower = message.lower()
    desc_lower = tool_desc.lower()
    name_lower = tool_name.lower()
    score = 0.0
    
    # Direct name match in message
    if name_lower in msg_lower:
        score += 5.0
    
    # Keyword groups: each group maps to a set of tool names
    group_tools = {
        "weather": ["openmeteo"],
        "file": ["file_ops"],
        "web_search": ["web_search", "browser", "firecrawl"],
        "system": ["system_info", "windows_desktop"],
        "geocode": ["nominatim"],
        "translate": ["libretranslate"],
        "diagram": ["plantuml"],
        "command": ["shell_exec"],
        "ocr": ["ocr"],
    }
    
    group_keywords = {
        "weather": ["天气", "气温", "温度", "下雨", "weather", "气象", "几度", "冷", "热"],
        "file": ["文件", "读取", "写入", "保存", "read", "write", "file", "代码", "源码", "code"],
        "web_search": ["搜索", "网页", "search", "查询", "查", "找", "https", "http", "打开", "浏览"],
        "system": ["桌面", "截图", "窗口", "进程", "磁盘", "screenshot", "capture"],
        "geocode": ["坐标", "经纬度", "位置", "在哪", "geocode"],
        "translate": ["翻译", "translate", "译"],
        "diagram": ["架构图", "流程图", "plantuml", "uml", "时序图", "类图"],
        "command": ["运行", "执行", "命令", "cmd", "shell", "pip", "npm"],
        "ocr": ["ocr", "识别", "图片文字"],
    }
    
    for group, keywords in group_keywords.items():
        for kw in keywords:
            if kw in msg_lower:
                # Message has this keyword — give points to tools in this group
                for t in group_tools.get(group, []):
                    if t == tool_name:
                        score += 2.0
    
    # Bonus: keyword appears in tool description
    for kw in msg_lower.split():
        if len(kw) >= 2 and kw in desc_lower:
            score += 1.0
    
    return score

def get_schemas_for_groups(plugin_mgr, groups: list[str]) -> list[dict]:
    """Get tool schemas for the selected groups."""
    allowed_tools = set()
    for g in groups:
        allowed_tools.update(TOOL_GROUPS.get(g, []))

    schemas = []
    added_names = set()
    # Built-in schemas filtered
    for name, meta in BUILTIN_SCHEMAS.items():
        if name in allowed_tools:
            schemas.append(to_openai_schema(meta))
            added_names.add(name)

    # Plugin schemas filtered (plugin TOOL overrides builtin if both exist)
    for plugin_name in plugin_mgr._plugins:
        if plugin_name in allowed_tools:
            # Remove builtin version if plugin has its own TOOL metadata
            if plugin_name in added_names:
                schemas = [s for s in schemas if s["function"]["name"] != plugin_name]
                added_names.discard(plugin_name)
            try:
                plg = plugin_mgr._plugins[plugin_name]
                spec = __import__('importlib').util.spec_from_file_location(plugin_name, plg["path"])
                if spec and spec.loader:
                    mod = __import__('importlib').util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    meta = getattr(mod, "TOOL", {})
                    if meta.get("name") and meta.get("params"):
                        schemas.append(to_openai_schema(meta))
                        added_names.add(meta["name"])
            except Exception as e:
                import logging as _log
                _log.getLogger("tool_schema").warning("Failed to load TOOL from %s: %s", plugin_name, e)

    return schemas[:12]
