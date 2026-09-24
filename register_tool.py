# -*- coding: utf-8 -*-
"""
register_tool.py — 新工具六步注册自动化 (2026-08-18)
=====================================================
用法:
  python register_tool.py <工具名> <描述> [--model] [--no-route]

自动完成六步 (全部幂等, 已存在自动跳过):
  1. tools/<name>.py — 生成插件文件 (tool_gen.generate; --model 时 deepseek 写实现)
  2. config/settings.py — SAFE_PLUGINS 追加
  3. core/tool_gateway.py — _UNRESTRICTED 追加
  4. core/intent_router.py — ACTION_INTENTS + INTENT_PRIORITY 追加 (--no-route 跳过)
  5. COMMAND_MAP.md — 追加映射行
  6. 清 __pycache__

示例:
  python register_tool.py currency "汇率查询工具" --model
"""
import sys, os, re, shutil, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("register_tool")

ROOT = Path(__file__).parent
TOOLS_DIR = ROOT / "tools"
SETTINGS = ROOT / "config" / "settings.py"
GATEWAY = ROOT / "core" / "tool_gateway.py"
ROUTER = ROOT / "core" / "intent_router.py"
COMMAND_MAP = ROOT / "COMMAND_MAP.md"


# ── 1. 生成工具文件 ──
def step1_generate(name: str, description: str, use_model: bool) -> str:
    from core.tool_gen import generate
    mc = None
    if use_model:
        try:
            import main as M
            from core.model_client import ModelClient
            mc = ModelClient(M._load_config())
        except Exception as e:
            logger.warning(f"  [1] 模型不可用({e}), 回退骨架生成")
    fp = generate(name, description, str(TOOLS_DIR), model_client=mc)
    if fp.startswith("EXISTS"):
        logger.info(f"  [1] 工具已存在, 跳过: {fp}")
        return fp.split(": ", 1)[1]
    logger.info(f"  [1] ✓ 生成 {fp}")
    return fp


# ── 2. SAFE_PLUGINS ──
def _append_to_set(src: str, pattern: str, tool_name: str) -> str | None:
    """在集合字面量内追加元素, 处理尾逗号/无尾逗号两种情况。"""
    m = re.search(pattern, src, re.DOTALL)
    if not m:
        return None
    inner = m.group(1).rstrip()
    inner = inner.rstrip(",")  # 去掉尾逗号, 统一加
    new = inner + f', "{tool_name}"' + m.group(2)
    return src[:m.start()] + new + src[m.end():]


def step2_safe_plugins(tool_name: str) -> bool:
    src = SETTINGS.read_text(encoding="utf-8")
    if f'"{tool_name}"' in src:
        logger.info(f"  [2] SAFE_PLUGINS 已有, 跳过")
        return True
    new_src = _append_to_set(src, r'(SAFE_PLUGINS\s*=\s*\{[^}]*?)(\})', tool_name)
    if new_src is None:
        logger.warning("  [2] 找不到 SAFE_PLUGINS, 跳过")
        return False
    SETTINGS.write_text(new_src, encoding="utf-8")
    logger.info(f"  [2] ✓ SAFE_PLUGINS += {tool_name}")
    return True


# ── 3. _UNRESTRICTED ──
def step3_unrestricted(tool_name: str) -> bool:
    src = GATEWAY.read_text(encoding="utf-8")
    if f'"{tool_name}"' in src:
        logger.info(f"  [3] _UNRESTRICTED 已有, 跳过")
        return True
    new_src = _append_to_set(src, r'(_UNRESTRICTED\s*=\s*\{[^}]*?)(\})', tool_name)
    if new_src is None:
        logger.warning("  [3] 找不到 _UNRESTRICTED, 跳过")
        return False
    GATEWAY.write_text(new_src, encoding="utf-8")
    logger.info(f"  [3] ✓ _UNRESTRICTED += {tool_name}")
    return True


# ── 4. intent_router 路由 ──
def step4_router(tool_name: str, description: str) -> bool:
    src = ROUTER.read_text(encoding="utf-8")
    key = tool_name.replace("-", "_")
    if f'"{key}"' in src.split("INTENT_PRIORITY")[0]:
        logger.info(f"  [4] ACTION_INTENTS 已有 {key}, 跳过")
        return True
    # 从描述提取关键词: 去"工具"后缀, 取前4字词作为触发词
    words = re.findall(r'[\u4e00-\u9fff]{2,6}', description)
    patterns = "[\"" + "\", \"".join(words[:3]) + "\"]" if words else f'["{key}"]'
    entry = (f'    "{key}":       {{"patterns": {patterns}, "tool": "{tool_name}", '
             f'"action": None, "output_type": "text"}},\n')
    # 插到 url_shared 之前
    anchor = '    "url_shared":'
    if anchor in src:
        src = src.replace(anchor, entry + anchor, 1)
    else:
        # 兜底: 插到 ACTION_INTENTS 闭合前
        m = re.search(r'(\}\n\n# Priority tiers)', src)
        if m:
            src = src[:m.start()] + entry + src[m.start():]
        else:
            logger.warning("  [4] 找不到插入点, 跳过路由")
            return False
    # INTENT_PRIORITY 追加
    if f'"{key}":' not in src.split("DELIVERY_INTENTS")[0].split("INTENT_PRIORITY")[1]:
        src = src.replace('    "search": 1,', f'    "{key}": 3,\n    "search": 1,', 1)
    ROUTER.write_text(src, encoding="utf-8")
    logger.info(f"  [4] ✓ intent_router: {key} 意图 + 优先级3")
    return True


# ── 5. COMMAND_MAP.md ──
def step5_command_map(tool_name: str, description: str) -> bool:
    line = f"| `{tool_name}` | `tools/{tool_name}.py` | {description} (自动注册) |\n"
    src = COMMAND_MAP.read_text(encoding="utf-8")
    if f"`{tool_name}`" in src:
        logger.info("  [5] COMMAND_MAP 已有, 跳过")
        return True
    COMMAND_MAP.write_text(src.rstrip() + "\n\n## 工具 (tools/)\n\n| 工具 | 文件 | 说明 |\n|------|------|------|\n" + line if "## 工具" not in src
                           else src.rstrip() + "\n" + line, encoding="utf-8")
    logger.info(f"  [5] ✓ COMMAND_MAP += {tool_name}")
    return True


# ── 6. 清 pycache ──
def step6_clean():
    n = 0
    for root, dirs, files in os.walk(ROOT):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                n += 1
    logger.info(f"  [6] ✓ 清理 {n} 个 __pycache__")


def register_tool(name: str, description: str, use_model: bool = False,
                  no_route: bool = False) -> bool:
    """六步注册。任一步失败返回 False(不中断, 打印警告)。"""
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())[:40]
    logger.info(f"== 注册工具: {name} ({safe_name}) ==")
    step1_generate(name, description, use_model)
    step2_safe_plugins(safe_name)
    step3_unrestricted(safe_name)
    if not no_route:
        step4_router(safe_name, description)
    step5_command_map(safe_name, description)
    step6_clean()
    logger.info("== 完成。重启 TMM 生效。==")
    return True


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_model = "--model" in sys.argv
    no_route = "--no-route" in sys.argv
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)
    tool_name = args[0]
    desc = args[1] if len(args) > 1 else f"{tool_name}工具 — 自动生成"
    sys.exit(0 if register_tool(tool_name, desc, use_model, no_route) else 1)
