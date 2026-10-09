"""
ToolOrchestrator — 工具编排层
=============================
不是让模型决定"这个工具失败了怎么办"，而是编排层自己处理回退。
工具A失败 → 自动尝试工具B → 工具C，把决策权从模型手里拿回来。

三件事：
1. 工具注册：每个工具声明自己的能力+回退链+成功条件
2. 智能调用：失败自动回退，不打扰模型
3. 经验学习：记录每个工具在什么场景下成功/失败，优化回退顺序
"""

import json, time, logging
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger("core.tool_orchestrator")

# ── 工具回退链 ──
# 格式: { "primary_tool": ["backup1", "backup2", ...] }
# 当 primary 失败时，按顺序尝试 backup
TOOL_FALLBACK_CHAIN = {
    "nominatim": ["openmeteo"],           # 地理编码失败 → 直接用city名调天气
    "browser": ["web_search", "firecrawl"],  # 页面不可达 → 搜索 → 直接提取
    "web_search": ["firecrawl", "browser"],  # 搜索失败 → 提取已知URL → 浏览器
    "shell_exec": ["file_ops"],           # shell不可用 → 文件操作
    "openmeteo": ["web_search"],          # 天气API挂了 → 搜索天气
    "firecrawl": ["web_search", "browser"], # 提取失败 → 搜索 → 浏览器
}

# ── 工具分类（用于场景→工具推荐） ──
TOOL_CATEGORIES = {
    "weather": ["openmeteo", "web_search"],
    "search": ["web_search", "firecrawl", "browser"],
    "file": ["file_ops", "shell_exec"],
    "system": ["shell_exec", "system_info", "file_ops"],
    "browser": ["browser", "web_search", "firecrawl"],
    "notification": ["im_notify", "ntfy", "send_email"],
    "desktop": ["windows_desktop"],
}

# ── 场景→工具组映射 ──
SCENE_TOOLS = {
    "weather": ["openmeteo"],
    "search_web": ["web_search", "firecrawl"],
    "file_read": ["file_ops"],
    "file_write": ["file_ops", "shell_exec"],
    "file_list": ["file_ops", "shell_exec"],
    "system_info": ["system_info", "shell_exec"],
    "browse_url": ["browser", "web_search"],
    "desktop_control": ["windows_desktop"],
    "send_message": ["im_notify", "send_email"],
    "execute_command": ["shell_exec"],
}


class ToolOrchestrator:
    """工具编排器：在 gateway.call 外面包一层智能回退。"""

    def __init__(self, data_dir: Optional[Path] = None):
        self._data_dir = data_dir or Path(__file__).parent.parent / "data"
        self._stats_file = self._data_dir / "tool_stats.json"
        self._stats = self._load_stats()
        self._session_failures = {}  # 本次会话的工具失败记录

    # ── 持久化 ──

    def _load_stats(self) -> dict:
        if self._stats_file.exists():
            try:
                return json.loads(self._stats_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"tools": {}, "chains": {}, "updated_at": 0}

    def _save_stats(self):
        self._stats["updated_at"] = time.time()
        self._stats_file.write_text(
            json.dumps(self._stats, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ── 记录 ──

    def record_result(self, tool_name: str, success: bool, 
                       scene: str = "", error: str = ""):
        """记录工具调用结果，用于经验学习。"""
        ts = self._stats["tools"]
        if tool_name not in ts:
            ts[tool_name] = {"ok": 0, "fail": 0, "scenes": {}, "errors": []}
        t = ts[tool_name]
        if success:
            t["ok"] += 1
        else:
            t["fail"] += 1
            if error:
                t["errors"].append({"time": time.time(), "error": error[:200]})
                if len(t["errors"]) > 20:
                    t["errors"] = t["errors"][-20:]
        if scene:
            if scene not in t["scenes"]:
                t["scenes"][scene] = {"ok": 0, "fail": 0}
            if success:
                t["scenes"][scene]["ok"] += 1
            else:
                t["scenes"][scene]["fail"] += 1

        # Track session failures for fallback decisions
        if not success:
            self._session_failures[tool_name] = time.time()

        # Periodic save (every 10 records)
        total = t["ok"] + t["fail"]
        if total % 10 == 0:
            self._save_stats()

    # ── 成功率查询 ──

    def success_rate(self, tool_name: str) -> float:
        """返回工具的历史成功率 (0.0-1.0)。"""
        t = self._stats["tools"].get(tool_name, {})
        ok = t.get("ok", 0)
        fail = t.get("fail", 0)
        total = ok + fail
        return ok / total if total > 0 else 1.0  # 无记录默认乐观

    def get_tool_stats(self) -> dict:
        """获取所有工具的统计信息，用于注入 prompt。"""
        result = {}
        for name, stats in self._stats["tools"].items():
            ok = stats.get("ok", 0)
            fail = stats.get("fail", 0)
            total = ok + fail
            if total > 0:
                result[name] = {
                    "success_rate": round(ok / total, 2),
                    "total_calls": total,
                }
        return result

    def get_preferred_tool(self, scene: str, available_tools: list) -> str:
        """根据场景和历史成功率，推荐最佳工具。"""
        candidates = SCENE_TOOLS.get(scene, [])
        if not candidates:
            return available_tools[0] if available_tools else ""

        # Score each candidate by success rate
        best_tool = ""
        best_score = -1
        for tool in candidates:
            if tool in available_tools:
                rate = self.success_rate(tool)
                # Bonus for recently successful tools
                score = rate
                if best_score < 0 or score > best_score:
                    best_score = score
                    best_tool = tool
        return best_tool or (available_tools[0] if available_tools else "")

    # ── 回退链 ──

    def get_fallback_chain(self, tool_name: str) -> list:
        """获取工具的回退链。先看自定义链，再看通用链。"""
        return TOOL_FALLBACK_CHAIN.get(tool_name, [])

    def should_skip_tool(self, tool_name: str) -> bool:
        """判断是否应该跳过这个工具（最近刚失败过）。"""
        last_fail = self._session_failures.get(tool_name, 0)
        if last_fail:
            # 如果在最近 30 秒内失败过，建议跳过
            if time.time() - last_fail < 30:
                return True
        # 如果历史成功率低于 20% 且调用超过 5 次
        rate = self.success_rate(tool_name)
        t = self._stats["tools"].get(tool_name, {})
        total = t.get("ok", 0) + t.get("fail", 0)
        if total >= 5 and rate < 0.2:
            return True
        return False

    def get_smart_hint(self, tool_name: str) -> str:
        """根据已失败的工具，生成智能提示给模型。"""
        chain = self.get_fallback_chain(tool_name)
        if chain:
            alternatives = ", ".join(chain)
            return f"[HINT] {tool_name} may be unreliable right now. Try: {alternatives}"
        return ""

    # ── 场景推断 ──

    def infer_scene(self, message: str, tool_name: str = "") -> str:
        """根据用户消息推断场景类型。"""
        msg = message.lower()
        if any(w in msg for w in ["天气", "weather", "气温", "温度", "下雨"]):
            return "weather"
        if any(w in msg for w in ["搜索", "search", "查", "找", "搜"]):
            return "search_web"
        # Unambiguous desktop keywords (check BEFORE file ops to avoid "截图保存" -> file_write)
        if any(w in msg for w in ["截图", "截屏", "鼠标", "键盘", "点击"]):
            return "desktop_control"
        if any(w in msg for w in ["列出", "list", "ls", "文件列表", "目录"]):
            return "file_list"
        if any(w in msg for w in ["读", "read", "打开文件", "查看文件", "cat"]):
            return "file_read"
        if any(w in msg for w in ["写", "write", "保存", "创建", "新建"]):
            return "file_write"
        # "桌面" is ambiguous — only desktop_control if no file keywords
        if "桌面" in msg and not any(w in msg for w in ["列出", "文件", "读", "写", "保存", "创建", "新建"]):
            return "desktop_control"
        if any(w in msg for w in ["写", "write", "保存", "创建", "新建"]):
            return "file_write"
        if any(w in msg for w in ["列出", "list", "ls", "文件列表", "目录"]):
            return "file_list"
        if any(w in msg for w in ["系统", "system", "状态", "磁盘", "内存", "cpu"]):
            return "system_info"
        if any(w in msg for w in ["打开", "open", "浏览", "browser", "http"]):
            return "browse_url"
        if any(w in msg for w in ["发", "send", "通知", "邮件", "消息", "短信"]):
            return "send_message"
        if any(w in msg for w in ["执行", "exec", "运行", "run", "cmd", "shell"]):
            return "execute_command"
        return "general"

    # ── 核心：编排调用 ──

    async def orchestrate(self, gateway, tool_name: str, 
                           scene: str = "", **kwargs) -> dict:
        """
        编排一次工具调用：
        1. 尝试主工具
        2. 失败 → 检查回退链
        3. 自动尝试下一个工具
        4. 全部失败 → 返回带 hint 的错误

        Returns: {"success": bool, "result": Any, "tool_used": str, 
                   "attempts": int, "fallback_used": bool}
        """
        scene = scene or self.infer_scene(kwargs.get("message", ""), tool_name)
        attempts = []
        tried = set()
        current_tool = tool_name
        final_result = None
        fallback_used = False

        # Build the full attempt chain: primary + fallbacks
        chain = [tool_name] + self.get_fallback_chain(tool_name)

        for i, candidate in enumerate(chain):
            if candidate in tried:
                continue
            
            # Skip tools that are known to be failing right now
            if i > 0 and self.should_skip_tool(candidate):
                logger.info("orchestrate: skipping %s (recent failure)", candidate)
                continue

            tried.add(candidate)
            
            try:
                result = await gateway.call(candidate, **kwargs)
            except Exception as e:
                result = {"success": False, "error": str(e)}

            success = isinstance(result, dict) and result.get("success", False)
            error_msg = ""
            if isinstance(result, dict):
                error_msg = result.get("error", "") or result.get("output", "")

            # Record
            self.record_result(candidate, success, scene, error_msg[:200])

            attempts.append({
                "tool": candidate,
                "success": success,
                "error": error_msg[:200] if not success else "",
            })

            if success:
                final_result = result
                break
            else:
                # On first failure, mark fallback
                if i == 0:
                    fallback_used = True
                logger.info("orchestrate: %s failed, trying next in chain...", candidate)

        if final_result is None:
            # All failed — build rich error
            chain_desc = " → ".join(a["tool"] for a in attempts)
            error_details = "; ".join(
                f"{a['tool']}: {a['error'][:80]}" 
                for a in attempts if a["error"]
            )
            hint = self.get_smart_hint(tool_name)

            final_result = {
                "success": False,
                "error": f"All tools failed ({chain_desc}). {error_details}",
                "output": f"All tools failed ({chain_desc}). {hint}" if hint else f"All tools failed ({chain_desc})",
                "_orchestrator_chain": attempts,
            }

        # Attach metadata
        if isinstance(final_result, dict):
            final_result["_orchestrator"] = {
                "tool_chain": [a["tool"] for a in attempts],
                "fallback_used": fallback_used,
                "attempts": len(attempts),
                "scene": scene,
            }

        return final_result


# ── 全局单例 ──
_orchestrator: Optional[ToolOrchestrator] = None


def get_orchestrator(data_dir: Path = None) -> ToolOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ToolOrchestrator(data_dir)
    return _orchestrator
