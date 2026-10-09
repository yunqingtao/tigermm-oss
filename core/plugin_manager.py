"""
PluginManager — loads and executes tool plugins from tools/ directory.
"""
import asyncio, logging
from pathlib import Path

logger = logging.getLogger("core.plugin_manager")



import ast
from config.settings import TOOLS_DIR, SAFE_PLUGINS, PLUGIN_FORBIDDEN_IMPORTS


def scan_plugin_safe(file_path):
    """AST-based static scan of plugin source before loading.
    Detects dangerous imports (os, subprocess, socket, ctypes, requests).
    SAFE_PLUGINS bypass the scan (trusted system tools).
    Returns False if dangerous imports found in non-whitelisted plugin."""
    if file_path.stem in SAFE_PLUGINS:
        return True
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_module = alias.name.split(".")[0]
                    if root_module in PLUGIN_FORBIDDEN_IMPORTS:
                        logger.warning("Plugin %s blocked: imports %s", file_path.name, root_module)
                        return False
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    root_module = node.module.split(".")[0]
                    if root_module in PLUGIN_FORBIDDEN_IMPORTS:
                        logger.warning("Plugin %s blocked: from %s import", file_path.name, root_module)
                        return False
        return True
    except SyntaxError as e:
        logger.warning("Plugin %s blocked: syntax error %s", file_path.name, e)
        return False
    except Exception as e:
        logger.error("Plugin %s scan error: %s", file_path.name, e)
        return False

def _norm(result):
    """统一工具返回契约: 结果文本必须能在 `output` 里读到。

    加性: 仅当 `output` 为空且兄弟键(result/data/text)有内容时补上, 不覆盖既有字段,
    不改失败语义。与 core/pipeline.py 的 `_norm_tool_result` 同一口径
    (33 个工具里 output/result/data 三种键混用, 出口必须归一)。
    """
    if not isinstance(result, dict):
        return result
    try:
        if str(result.get("output") or "").strip():
            return result
        for k in ("result", "data", "text"):
            v = result.get(k)
            if v is None or v == "":
                continue
            if not isinstance(v, str):
                import json as _j
                v = _j.dumps(v, ensure_ascii=False)
            out = dict(result)
            out["output"] = v
            out.setdefault("output_from", k)
            return out
    except Exception:
        return result
    return result


class PluginManager:
    def __init__(self, data_dir):
        self._plugins = {}
        self.data_dir = data_dir
        self._load_plugins()
    
    def _load_plugins(self):
        self._plugins = {}
        tools_dir = Path(__file__).parent.parent / "tools"
        if tools_dir.exists():
            for f in tools_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                if not scan_plugin_safe(f):
                    logger.warning("Plugin %s REJECTED by security scan", f.name)
                    continue
                self._plugins[f.stem] = {"name": f.stem, "tool": f.stem, "path": str(f)}
        logger.info("Plugins: %d loaded", len(self._plugins))

    def reload(self):
        old_count = len(self._plugins)
        for plg in self._plugins.values():
            plg.pop("module", None)
        self._load_plugins()
        new_count = len(self._plugins)
        logger.info("Plugins: reloaded %d -> %d (cache cleared)", old_count, new_count)
        return new_count
    
    async def call(self, tool_name, **kwargs):
        if tool_name in self._plugins:
            plugin = self._plugins[tool_name]
            if "module" not in plugin:
                import importlib.util
                spec = importlib.util.spec_from_file_location(tool_name, plugin["path"])
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    plugin["module"] = mod
            mod = plugin.get("module")
            if mod:
                if hasattr(mod, 'execute'):
                    fn = mod.execute
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(**kwargs)
                    else:
                        result = await asyncio.to_thread(fn, **kwargs)
                    return _norm(result)
                elif hasattr(mod, 'run'):
                    fn = mod.run
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(**kwargs)
                    else:
                        result = await asyncio.to_thread(fn, **kwargs)
                    return _norm(result)
        # 🔴 2026-09-19 深测修 (真缺陷): 原来这里返回**成功** [{tool}] called ——
        #   与 core/pipeline.py 里那份 PluginManager 是同一个谎报 bug, 但**这份漏改了**
        #   (它经 gateway/__init__、gateway/plugin_mgr、mcp/tool_adapter 仍是活跃路径,
        #    并接进两处留档验证器)。后果同样是: 没有 run/execute 入口的模块、或导入失败、
        #   或干脆不存在的工具名, 调用方都拿到"成功" → 用户看到"✓ 已完成"而什么都没发生。
        #   诚实报错, 不谎报。
        if tool_name in getattr(self, "_plugins", {}):
            return {"success": False, "output": "",
                    "error": f"工具 '{tool_name}' 没有可调用入口 (需要 run()/execute()), 未执行"}
        return {"success": False, "output": "", "error": f"未知工具: {tool_name}"}
    
    def match_keywords(self, message: str):
        matches = []
        for name, plg in self._plugins.items():
            try:
                import importlib.util as _iu
                spec = _iu.spec_from_file_location(name, plg["path"])
                if spec and spec.loader:
                    mod = _iu.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    tool_meta = getattr(mod, "TOOL", {})
                    kws = tool_meta.get("keywords", [])
                    for kw in kws:
                        if kw in message:
                            matches.append((name, kw, plg))
            except Exception:
                pass
        return matches
