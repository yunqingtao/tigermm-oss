# gateway.plugin_mgr — re-exports from core.plugin_manager
from core.plugin_manager import PluginManager

# ★★ 2026-09-20 修 (真缺陷: 两份白名单互相不一致, 一直漂移)
#   原来本文件**自己定义**了一份 SAFE_PLUGINS (25 个), 与 config/settings.py 里被
#   PluginManager 真正使用的那份 (30 个) 不一致:
#     · 只在本表: firecrawl / libretranslate / ntfy / openmeteo / output
#     · 只在那表: chart / github_api / kb_* / sample_echo / service_check /
#                 session_logs / usage_stats
#     · 幽灵条目: output / sample_echo (没有对应的 tools/*.py)
#   危害: 新人按"新工具须登记 SAFE_PLUGINS"加代码时, **加错表就等于没加** ——
#     实际生效的是 settings 那份 (core/plugin_manager.py:12 导入它), 这份只是引用,
#     而它的 scan_plugin_safe() 又被当"白名单检查器"用, 会给出与运行时不符的结论。
#   修法: 单一来源 —— 本模块只**转发** settings 的那份, 不再自己维护一份。
from config.settings import SAFE_PLUGINS  # noqa: F401  (转发, 保持旧引用可用)


def scan_plugin_safe(tools_dir):
    """按**权威名单**(settings.SAFE_PLUGINS) 分拣工具目录。

    注意: 这里只按名字分拣, 与 core.plugin_manager.scan_plugin_safe() 的"静态扫描"
    是两件事 —— 后者还会对未登记的工具做 AST 检查 (import os 等即拒)。本函数保留
    原语义 (供 main.py 自检等处按名分拣), 但名单与运行时**同源**, 不会再给出错误结论。
    """
    from pathlib import Path
    tools_dir = Path(tools_dir)
    blocked, safe = [], []
    for pf in sorted(tools_dir.glob("*.py")):
        if pf.name.startswith("_"):
            continue
        (safe if pf.stem in SAFE_PLUGINS else blocked).append(pf.name)
    return safe, blocked
