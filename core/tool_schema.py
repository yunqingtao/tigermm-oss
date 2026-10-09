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

    # ══ 2026-09-25 P1 工具可见性收口 ══════════════════════════════════════
    # ★ 这 11 条 schema 已移到 core/tool_schema_p1.py —— 不是嫌长, 是因为
    #   本文件被 tests/test_architecture_hardening.py 当"小文件"样本读 (默认
    #   一次 500 行), 加量后越线会连带七道门红。合并见文件尾的 update()。
}


# ★ P1 收口: 合并上面移出去的 11 条 schema (纯加性)。
#   双路兜底 import —— 仓库会被复制/改名/在 clone 里跑, 只写死一种导入方式会脆。
try:
    from core.tool_schema_p1 import P1_PLUGIN_SCHEMAS as _P1_SCHEMAS
except Exception:
    try:
        from tool_schema_p1 import P1_PLUGIN_SCHEMAS as _P1_SCHEMAS
    except Exception as _e:  # 兜底: 拿不到就空着, 宁可少几条也不炸导入
        import logging as _lg
        _lg.getLogger("tool_schema").warning("P1 schema 模块导入失败: %s", _e)
        _P1_SCHEMAS = {}
BUILTIN_SCHEMAS.update(_P1_SCHEMAS)

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


def _normalize_params(tool_meta):
    """把工具声明的参数**归一化成 list[{name,...}]**。

    ★ 2026-09-25 修 (真缺陷, 与 P1 工具可见性同源):
      `params` 有两种形态在仓库里真实并存 ——
        · list[{name, type, required, description}]  ← 老约定, 多数工具
        · dict{name: {type, required, description}}   ← 新约定, artifacts 在用
      原来 to_openai_schema 只按 list 遍历 dict 时拿到的是**键名字符串**,
      而 get_all_schemas 的准入判据又是 `meta.get("params")` 为真 ——
      artifacts 声明了 `parameters`(dict) 却没有 `params` ⇒ 被判成"没参数"
      ⇒ **静默不可见**。同一句缺陷也让 `kb_list` (params: []) 出局 ——
      空 list 是假值。两种形态都认, 空参数也认 (无参工具仍然是个能调的工具)。
    """
    raw = tool_meta.get("params")
    if raw is None:
        raw = tool_meta.get("parameters")
    if isinstance(raw, dict):
        out = []
        for key, val in raw.items():
            d = dict(val) if isinstance(val, dict) else {"type": "string"}
            d.setdefault("name", key)
            out.append(d)
        return out
    return list(raw or [])


def to_openai_schema(tool_meta):
    """Convert a single tool's metadata to OpenAI function calling schema."""
    params = _normalize_params(tool_meta)
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
                plug = getattr(mod, "PLUGIN", {})
                # ★ 2026-09-26 新增 (外部对比稿点出的"46 注册 / 44 可见"根因):
                #   此前"模型看不见"是**漏写 TOOL 块的副作用** —— 谁都说不清那是有意
                #   还是忘写 (本轮实测: hermes_bridge 的 trigger 是空数组所以顺带不可见;
                #   win32_input 干脆连 PLUGIN 块都没有)。现在改成**显式声明**:
                #   工具在自己的 PLUGIN/TOOL 里写 `"expose": False` + `expose_reason`,
                #   本函数照着跳过。写进日志 —— 一个能力对模型不可见, 必须留痕。
                if meta.get("expose") is False or plug.get("expose") is False:
                    _why = meta.get("expose_reason") or plug.get("expose_reason") or "(未写理由)"
                    import logging as _logging
                    _logging.getLogger("tool_schema").info(
                        "工具 %s 显式声明不对模型暴露 (expose=False): %s", plugin_name, _why)
                    continue
                # ★ 2026-09-25: 准入不再要求 `params` 非空 (空 list 是假值, 会误杀
                #   无参工具如 kb_list); 参数形态由 _normalize_params 统一。
                if meta.get("name"):
                    if meta["name"] not in added_names:
                        schemas.append(to_openai_schema(meta))
                        added_names.add(meta["name"])
        except Exception as _e:
            # ★ 2026-09-25: 原来是 `except Exception: pass` —— 某工具哪天改坏,
            #   它会**无声消失** (模型只会说"我没这能力"), 排查时毫无线索。
            #   留一行日志, 不改变控制流。
            import logging as _logging
            _logging.getLogger("tool_schema").warning(
                "工具 %s 的 TOOL 元数据加载失败, 该工具本次对模型不可见: %s: %s",
                plugin_name, type(_e).__name__, _e)

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

# ★ 2026-09-26 补全 (外部对比稿点出、当场复查属实的真缺陷):
#   这张表原来只覆盖 19 个工具, 而当时注册/内建的工具共 46 个 —— **27 个不在任何组里**。
#   而 get_schemas_for_groups() 除了 GROU 表之外**没有任何调用方** (全仓只在定义处出现),
#   所以"按意图只发相关 schema"这个省 token 的功能是**纸面功能**: 定义齐全、一次没跑过。
#   对本地模型这是最要命的一条 —— 本机 Ollama 的 ctx 只有 2000, 却每次塞 44 份 schema。
#   本次做两件 (都不改默认行为, 纯加性):
#     ① 把表补全到**全覆盖** (46 个工具一个不漏) —— 补不全会让"分组"变成"藏工具"。
#     ② 门禁 verify_tool_exposure.py 钉住"注册的工具必须都在表里", 防它再烂回去。
#   ⚠️ 仍**未**接进 pipeline (接进去会改变模型看得见什么, 属高危改动, 须单独评估+实操验证)。
#      要接的话走 get_schemas_for_message(plugin_mgr, msg) —— 它已就绪, 且带"拿不准就全发"的兜底。
TOOL_GROUPS = {
    # ── 既有六组 (原样保留) ──
    "file": ["file_ops", "shell_exec"],
    "web": ["browser", "web_search", "openmeteo", "nominatim", "libretranslate", "firecrawl", "plantuml", "ocr"],
    "system": ["system_info", "windows_desktop"],
    "comm": ["send_email", "sms_send", "ntfy", "push_notify", "im_notify"],
    "office": ["tiger_office", "office_cli", "table_ops"],
    # ── 2026-09-26 补齐的七组 ──
    "knowledge": ["kb_import", "kb_search", "kb_list", "kb_delete", "memory", "memory_claim", "session_logs"],
    "ops": ["service_check", "usage_stats", "backup", "artifacts", "task_ledger", "task_flow",
            "repo_status", "github_api", "egress", "observers"],
    "media": ["image_gen", "vision", "voice", "whisper", "chart"],
    "mail": ["check_mail"],
    "domains": ["ssq", "publish_pack"],
    # 内部工具: 分组里登记是为了**审计完整性**, 不代表模型看得见
    # (它们没有 TOOL 块 ⇒ 不进 schema; 见 verify_tool_exposure.py)
    "internal": ["hermes_bridge", "win32_input"],
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

def get_schemas_for_message(plugin_mgr, message: str, min_score: float = 1.0) -> list[dict]:
    """★ 2026-09-26 新增 (就绪, **尚未接进 pipeline**)。

    按消息挑相关组, 只发这些组的 schema —— 本机 Ollama ctx 只有 2000, 44 份 schema
    塞进去等于把上下文吃完 (实测就是"工具调用瘸"的根因之一)。

    ★ 兜底的**边界** (2026-09-26 门禁当场校准过一次, 记下来免得再想歪):
      第一版写了"分组结果不足全量三分之一就全发"。门禁一试就露馅: 单个组本来就只有
      2-10 个工具, 永远够不到 44 的三分之一 ⇒ **兜底恒成立 ⇒ 分组永不生效**, 又变成
      纸面功能。所以改成只在**真的没匹配上**时才全发:
        · 消息为空            → 全发 (没依据可挑)
        · 一个组都没命中       → 全发
        · 命中 "all"          → 全发
        · 分组结果 < 2 个工具  → 全发 (太单薄, 不像一次有把握的匹配)
        · 消息里点了名 ("全部工具"/"所有能力") → 全发
      取舍写在明处: 开着分组时, **没匹配到但本次真需要的工具会对模型不可见**。这是
      省 token 必然的代价; 所以它是个**可选开关**, 由调用方决定用不用, 不是默认行为。
      (藏工具的后果是模型"不会用"某能力, 而人会以为是模型笨 —— 这也是为什么要有
       verify_tool_exposure.py 把暴露面钉死。)
    """
    import re as _re
    full = get_all_schemas(plugin_mgr)
    msg = str(message or "").lower()
    if not msg.strip():
        return full
    if any(k in msg for k in ("全部工具", "所有工具", "所有能力", "全部能力", "all tools")):
        return full
    hit = set()
    for group, triggers in GROUP_TRIGGERS.items():
        for t in triggers:
            if str(t).lower() in msg:
                hit.add(group)
    if not hit or "all" in hit:
        return full
    grouped = get_schemas_for_groups(plugin_mgr, sorted(hit))
    if len(grouped) < 2:
        return full
    return grouped


def get_schemas_for_groups(plugin_mgr, groups: list[str]) -> list[dict]:
    """取指定组的工具 schema。

    ★ 2026-09-26 修 (三个"让工具凭空消失"的洞, 都是当场跑门禁抓出来的):
      这份实现是 get_all_schemas 的兄弟, 但 2026-09-25 那轮修 **只修了哥哥** —— 这里
      原样留着三处会**静默少发**的写法。它不是死代码时无害, 但它是"按需发 schema"
      的唯一执行路径, 谁一接线就会让模型当场丢能力且毫无提示:
        ① `if plugin_name in added_names:` 无条件**先删内置版本**, 然后只有该插件
           自己带 TOOL 块时才补回来 —— 插件没有 TOOL 块的工具 (如 shell_exec)
           于是**整条消失**。修法: 先确认插件真能提供 TOOL, 再删内置版。
        ② `meta.get("name") and meta.get("params")` —— 要求 params 非空。这是
           get_all_schemas 里 2026-09-25 已修过的同一个 bug (空 list 是假值, 会误杀
           无参工具如 kb_list), 这里漏改。修法: 只看 name。
        ③ `return schemas[:12]` —— **硬性截断到 12 条**: 组一多就静默吞掉后面的工具。
           修法: 去掉截断 (要限流应该是上层显式决定并留日志, 不能藏在取数函数里)。
      另: 尊重 `expose: False` (与 get_all_schemas 同一口径)。
    """
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

    # Plugin schemas filtered (插件自带 TOOL 时**才**覆盖内置版)
    for plugin_name in plugin_mgr._plugins:
        if plugin_name not in allowed_tools:
            continue
        try:
            plg = plugin_mgr._plugins[plugin_name]
            spec = __import__('importlib').util.spec_from_file_location(plugin_name, plg["path"])
            if not (spec and spec.loader):
                continue
            mod = __import__('importlib').util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            plug = getattr(mod, "PLUGIN", {}) or {}
            meta = getattr(mod, "TOOL", {}) or {}
            # 显式不暴露: 与 get_all_schemas 同口径 (声明了就必须生效)
            if meta.get("expose") is False or plug.get("expose") is False:
                continue
            if not meta.get("name"):
                # 没有 TOOL 块 ⇒ 不覆盖内置版 (洞 ①)。内置版留着即可。
                continue
            if plugin_name in added_names:
                schemas = [s for s in schemas if s["function"]["name"] != plugin_name]
                added_names.discard(plugin_name)
            schemas.append(to_openai_schema(meta))
            added_names.add(meta["name"])
        except Exception as e:
            import logging as _log
            _log.getLogger("tool_schema").warning("Failed to load TOOL from %s: %s", plugin_name, e)

    return schemas
