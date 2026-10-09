"""
DebugTrace — 可观测性追踪
=========================
记录每次请求的路由决策、工具调用、模型选择、耗时。
不是日志，是事后排查"为什么这次调了file_ops而不是openmeteo"的追溯路径。

用法：
  trace = get_tracer()
  trace.start("用户说了什么")
  trace.route("auto_guide", "identity match")     # 路由分支
  trace.model("ollama", "qwen3.5:9b")             # 模型选择
  trace.tool("web_search", success=True, ms=120)   # 工具调用
  trace.done()                                     # 结束

输出：
  /stats trace   → 最近20条追踪
  /stats trace N → 第N条的完整链路
"""

import json, time, logging
from pathlib import Path
from typing import Optional
from collections import deque

logger = logging.getLogger("core.debug_trace")

MAX_TRACES = 200  # ring buffer


class TraceSpan:
    """单次请求的追踪记录。"""
    __slots__ = ('started', 'message', 'route_branch', 'route_detail',
                 'model_used', 'model_detail', 'tools', 'elapsed', 'done')

    def __init__(self):
        self.started = time.time()
        self.message = ""
        self.route_branch = ""
        self.route_detail = ""
        self.model_used = ""
        self.model_detail = ""
        self.tools = []  # [(name, success, ms), ...]
        self.elapsed = 0
        self.done = False

    def to_dict(self) -> dict:
        return {
            "time": time.strftime("%H:%M:%S", time.localtime(self.started)),
            "message": self.message[:80],
            "route": f"{self.route_branch}: {self.route_detail}" if self.route_branch else "",
            "model": f"{self.model_used}({self.model_detail})" if self.model_used else "",
            "tools": [(n, "OK" if s else "FAIL", f"{ms}ms") for n, s, ms in self.tools],
            "elapsed": f"{self.elapsed:.2f}s",
        }

    def summary(self) -> str:
        """单行摘要。"""
        parts = [time.strftime("%H:%M:%S", time.localtime(self.started))]
        parts.append(self.message[:40].replace("\n", " "))
        if self.route_branch:
            parts.append(f"[{self.route_branch}]")
        if self.model_used:
            parts.append(f"@{self.model_used}")
        if self.tools:
            tool_str = ", ".join(f"{'✓' if s else '✗'}{n}" for n, s, _ in self.tools[:3])
            parts.append(f"→ {tool_str}")
        parts.append(f"({self.elapsed:.1f}s)")
        return " ".join(parts)


class DebugTracer:
    """全局追踪器 — ring buffer + 索引。"""

    def __init__(self):
        self._buffer = deque(maxlen=MAX_TRACES)
        self._current: Optional[TraceSpan] = None
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    # ── Span lifecycle ──

    def start(self, message: str) -> TraceSpan:
        """开始一次追踪。"""
        span = TraceSpan()
        span.message = message
        self._current = span
        self._buffer.append(span)
        return span

    def route(self, branch: str, detail: str = ""):
        """记录路由分支。"""
        if not self._enabled or not self._current:
            return
        self._current.route_branch = branch
        self._current.route_detail = detail

    def model(self, name: str, detail: str = ""):
        """记录模型选择。"""
        if not self._enabled or not self._current:
            return
        self._current.model_used = name
        self._current.model_detail = detail

    def tool(self, name: str, success: bool, elapsed_ms: float = 0):
        """记录工具调用。"""
        if not self._enabled or not self._current:
            return
        self._current.tools.append((name, success, round(elapsed_ms)))

    def done(self):
        """结束追踪。"""
        if not self._enabled or not self._current:
            return
        self._current.elapsed = round(time.time() - self._current.started, 2)
        self._current.done = True
        logger.info("trace: %s", self._current.summary())
        self._current = None

    # ── Query ──

    def recent(self, n: int = 20) -> list:
        """最近 N 条追踪。"""
        return [s.to_dict() for s in list(self._buffer)[-n:]]

    def summary_lines(self, n: int = 15) -> list:
        """打印友好的单行摘要。"""
        return [s.summary() for s in list(self._buffer)[-n:]]

    def get_by_index(self, idx: int) -> Optional[dict]:
        """按索引获取完整追踪。0 = 最新。"""
        buf = list(self._buffer)
        if 0 <= idx < len(buf):
            return buf[-(idx + 1)].to_dict()
        return None

    def stats(self) -> dict:
        """追踪统计。"""
        buf = list(self._buffer)
        if not buf:
            return {"total": 0}
        models = {}
        routes = {}
        tools_ok = 0
        tools_fail = 0
        for s in buf:
            if s.model_used:
                models[s.model_used] = models.get(s.model_used, 0) + 1
            if s.route_branch:
                routes[s.route_branch] = routes.get(s.route_branch, 0) + 1
            for _, ok, _ in s.tools:
                if ok:
                    tools_ok += 1
                else:
                    tools_fail += 1
        return {
            "total": len(buf),
            "models": models,
            "routes": routes,
            "tools": {"ok": tools_ok, "fail": tools_fail},
        }


# ── 全局单例 ──
_tracer: Optional[DebugTracer] = None


def get_tracer() -> DebugTracer:
    global _tracer
    if _tracer is None:
        _tracer = DebugTracer()
    return _tracer
