"""
ToolGen — 描述需求 → 生成插件。v2 (2026-08-18)
说"加一个xxx工具" → 生成 tools/xxx.py。
v2: 有 model_client 时由 deepseek 生成可用实现(_run 函数体), 静态扫描+编译验证, 失败自动回退骨架。
"""
import os, re, logging
from pathlib import Path

logger = logging.getLogger("core.tool_gen")

TEMPLATE = '''"""
{description}
"""
import logging

logger = logging.getLogger("tools.{name}")

TOOL = {{
    "name": "{name}",
    "description": "{description}",
    "params": {{
        "action": {{"type": "string", "description": "操作: {actions}", "required": True}},
    }},
    "returns": "dict with success and result fields",
    "permission": ["normal"],
}}

# Plugin metadata
PLUGIN = {{
    "name": "{name}",
    "description": "{description}",
    "permission": ["normal"],
    "version": "0.1.0",
}}


def run(action: str, **kwargs):
    """Main entry point called by ToolGateway."""
    try:
        if action == "run":
            return _run(**kwargs)
        elif action == "status":
            return {{"success": True, "status": "ready"}}
        else:
            return {{"success": False, "error": f"Unknown action: {{action}}"}}
    except Exception as e:
        logger.error("%s failed: %s", action, e)
        return {{"success": False, "error": str(e)}}


def _run(**kwargs):
    """Core logic."""
    return {{"success": True, "result": "{name} executed"}}


# Auto-discovered by tool_schema
'''

# 骨架 _run 占位(用于智能生成时替换)
_RUN_PLACEHOLDER = '''def _run(**kwargs):
    """Core logic."""
    return {"success": True, "result": "{name} executed"}'''

# deepseek 生成 _run 函数体的 prompt
IMPL_PROMPT = """你是工具实现生成器。根据描述生成 Python 函数体。

工具名: {name}
工具描述: {description}

要求:
1. 只输出 def _run(**kwargs) 的函数体代码(含 def 行), 不要输出其他任何内容
2. 函数返回 dict: {{"success": bool, "output": "中文一句话结果", "error": "失败原因(成功时为空)"}}
3. 只用标准库 (os/re/json/urllib/urllib.request/subprocess/math/datetime), 禁止第三方依赖
4. 禁止 eval/exec/compile/__import__ 等危险调用
5. 必须 try/except 包裹, 异常时返回 {{"success": False, "error": 中文错误}}
6. 参数从 kwargs 取, 用 .get 带默认值
7. 代码要真的能跑, 逻辑完整, 不要写 TODO 或 pass

只输出代码:"""


def _generate_impl(name: str, description: str, model_client) -> str | None:
    """用模型生成 _run 实现。失败返回 None(调用方回退骨架)。"""
    try:
        prompt = IMPL_PROMPT.replace("{name}", name).replace("{description}", description[:200])
        import asyncio
        r = asyncio.run(model_client.generate(
            "deepseek", [{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=1500))
        code = r.get("text", "") or r.get("think", "") or ""
        if not code.strip():
            return None
        # 剥 markdown 代码块
        code = re.sub(r"^```\w*\n?", "", code.strip())
        code = re.sub(r"\n?```$", "", code)
        if "def _run(" not in code:
            return None
        return code
    except Exception as e:
        logger.warning("tool_gen smart impl failed, fallback skeleton: %s", e)
        return None


def _validate_impl(code: str, name: str) -> bool:
    """py_compile + 静态扫描验证生成代码。通过返回 True。"""
    import tempfile, py_compile
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write("import os, re, json\n" + code)
            tmp = f.name
        py_compile.compile(tmp, doraise=True)
        try:
            from core.static_scan import scan_code
            r = scan_code("import os, re, json\n" + code, filename=f"{name}.py")
            if not r.get("safe", True):
                logger.warning("tool_gen smart impl flagged by static scan: %s", r.get("summary"))
                return False
        except Exception:
            pass
        os.unlink(tmp)
        return True
    except Exception as e:
        logger.warning("tool_gen impl validation failed: %s", e)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def generate(name: str, description: str, tools_dir: str, model_client=None) -> str:
    """Generate a tool plugin file. Returns the file path.

    model_client 提供时尝试智能生成实现(deepseek), 静态扫描不过自动回退骨架。
    """
    # Sanitize name
    safe_name = re.sub(r'[^a-z0-9_]', '_', name.lower())[:40]

    # Detect actions from description
    actions = []
    for a in ["查询", "搜索", "发送", "获取", "列出", "创建", "删除", "更新", "导出"]:
        if a in description:
            actions.append(a)
    if not actions:
        actions = ["执行"]

    actions_str = "/".join(actions[:3])
    desc_cn = description[:100]

    content = TEMPLATE.format(
        name=safe_name,
        description=desc_cn,
        actions=actions_str,
    )

    filepath = os.path.join(tools_dir, f"{safe_name}.py")
    if os.path.exists(filepath):
        return f"EXISTS: {filepath}"

    # ── v2: 智能生成实现(有 model_client 时) ──
    if model_client is not None:
        impl = _generate_impl(safe_name, description, model_client)
        if impl and _validate_impl(impl, safe_name):
            # 用生成的 _run 替换骨架占位
            placeholder = _RUN_PLACEHOLDER.replace("{name}", safe_name)
            new_content = content.replace(placeholder, impl)
            if new_content != content:
                content = new_content
                logger.info("tool_gen: smart impl generated for %s", safe_name)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


def parse_tool_request(msg: str) -> tuple[str, str] | None:
    """Parse "加一个xxx工具 / 新建xxx工具" from user message."""
    patterns = [
        r"加一个(.+?)工具",
        r"新建(.+?)工具",
        r"添加(.+?)工具",
        r"创建一个(.+?)工具",
        r"写一个(.+?)工具",
    ]
    for pat in patterns:
        m = re.search(pat, msg)
        if m:
            name = m.group(1).strip()
            # Strip filler prefixes
            for prefix in ["一个", "个"]:
                if name.startswith(prefix):
                    name = name[len(prefix):]
            desc = f"{name}工具 — 自动生成"
            return name, desc
    return None
