"""
ToolGateway — unified tool dispatch with permission, rate-limit, audit log.
"""
import asyncio, logging, time
from core.error_cn import wrap_error
from typing import Any

logger = logging.getLogger("core.tool_gateway")

DEFAULT_TIMEOUT = 30


MAX_CONSECUTIVE_FAILURES = 5
RATE_WINDOW = 60        # 1 minute
RATE_MAX_PER_WINDOW = 30  # max calls per tool per window


class ToolGateway:
    """Routes tool calls to plugin manager with safety guards."""

    # Tools that don't need permission checks (read-only / safe)
    _UNRESTRICTED = {
        "file_ops", "system_info", "web_search", "openmeteo",
        "check_mail", "send_email", "ocr", "firecrawl", "plantuml",
        "browser", "windows_desktop", "voice", "tiger_office",
        "office_cli", "push_notify", "ntfy", "kb_import", "kb_search", "kb_list", "kb_delete", "chart"}
    # Only shell_exec requires elevated permission (system)
    _RESTRICTED = {"shell_exec"}

    def __init__(self):
        self.plugin_mgr = None
        self._call_counts = {}
        self._fail_counts = {}
        self._total_calls = 0
        self._circuit_open = set()
        # Rate limiting: tool_name → list of timestamps
        self._rate_log = {}
        # Audit log: list of {ts, tool, params_keys, success, elapsed_ms}
        self._audit_log = []

    async def call(self, tool_name: str, timeout: int = None, **kwargs) -> dict:
        # ── MCP 外部工具路由: mcp_<server>__<tool> (不依赖 plugin_mgr) ──
        if tool_name.startswith("mcp_"):
            if self.mcp is None:
                return {"success": False, "error": "MCP client not initialized"}
            return await self.mcp.call_tool(tool_name, **kwargs)

        if not self.plugin_mgr:
            return {"success": False, "error": "No plugin manager"}

        # ── Permission gate ──
        if tool_name in self._RESTRICTED:
            self._audit(tool_name, kwargs, False, 0,
                        f"blocked: {tool_name} is restricted")
            return {
                "success": False,
                "error": f"Tool '{tool_name}' is restricted — use file_ops or system_info instead",
            }

        # ── Rate limit gate ──
        now = time.time()
        window_start = now - RATE_WINDOW
        self._rate_log.setdefault(tool_name, [])
        # Prune old entries
        self._rate_log[tool_name] = [
            t for t in self._rate_log[tool_name] if t > window_start
        ]
        if len(self._rate_log[tool_name]) >= RATE_MAX_PER_WINDOW:
            self._audit(tool_name, kwargs, False, 0,
                        f"rate-limited: {len(self._rate_log[tool_name])} calls in {RATE_WINDOW}s")
            return {
                "success": False,
                "error": f"Tool '{tool_name}' rate limited ({RATE_MAX_PER_WINDOW} calls/{RATE_WINDOW}s)",
            }

        # ── Circuit breaker ──
        if tool_name in self._circuit_open:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' temporarily disabled after {MAX_CONSECUTIVE_FAILURES} consecutive failures",
                "_circuit_open": True,
            }

        # ── Anomaly detection: 行为异常风险评估 (深夜/敏感路径/破坏性参数/突发) ──
        try:
            from core.anomaly_detector import get_detector
            _det = get_detector()
            _risk = _det.assess(tool_name, kwargs)
            _burst = _det.check_burst(tool_name, kwargs)
            if _risk["risk"] == "high" or _burst["risk"] == "high":
                _reason = _risk["reason"] or _burst["reason"]
                _det.record(tool_name, kwargs, "high", _reason)
                self._audit(tool_name, kwargs, False, 0, "anomaly: " + _reason)
            elif _risk["risk"] == "low":
                _det.record(tool_name, kwargs, "low", _risk["reason"])
        except Exception:
            pass

        self._total_calls += 1
        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1
        self._rate_log[tool_name].append(now)
        t0 = time.time()
        timeout = timeout or DEFAULT_TIMEOUT

        try:
            result = await asyncio.wait_for(
                self.plugin_mgr.call(tool_name, **kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            elapsed = round((time.time() - t0) * 1000)
            self._fail_counts[tool_name] = self._fail_counts.get(tool_name, 0) + 1
            self._check_circuit(tool_name)
            logger.warning("ToolGateway: %s timeout after %.1fs", tool_name, timeout)
            self._audit(tool_name, kwargs, False, elapsed, f"timeout after {timeout}s")
            return {"success": False, "error": f"Tool '{tool_name}' timed out after {timeout}s",
                    "_elapsed_ms": elapsed}
        except Exception as e:
            elapsed = round((time.time() - t0) * 1000)
            self._fail_counts[tool_name] = self._fail_counts.get(tool_name, 0) + 1
            self._check_circuit(tool_name)
            logger.error("ToolGateway: %s error: %s", tool_name, e)
            self._audit(tool_name, kwargs, False, elapsed, str(e)[:100])
            return {"success": False, "error": str(e), "_elapsed_ms": elapsed}

        elapsed_ms = round((time.time() - t0) * 1000)
        is_success = isinstance(result, dict) and result.get("success", False)
        if is_success:
            self._fail_counts[tool_name] = 0
        else:
            self._fail_counts[tool_name] = self._fail_counts.get(tool_name, 0) + 1
            self._check_circuit(tool_name)

        if isinstance(result, dict):
            result["_elapsed_ms"] = elapsed_ms
            result["_call_count"] = self._call_counts[tool_name]

        self._audit(tool_name, kwargs, is_success, elapsed_ms)
        return wrap_error(result)

    def _audit(self, tool: str, params: dict, success: bool, elapsed_ms: int,
               note: str = ""):
        """Record structured audit entry. Keep last 500 entries."""
        self._audit_log.append({
            "ts": time.strftime("%H:%M:%S"),
            "tool": tool,
            "params": list(params.keys())[:5],
            "success": success,
            "elapsed_ms": elapsed_ms,
            "note": note,
        })
        if len(self._audit_log) > 500:
            self._audit_log = self._audit_log[-500:]

    def _check_circuit(self, tool_name: str):
        if self._fail_counts.get(tool_name, 0) >= MAX_CONSECUTIVE_FAILURES:
            self._circuit_open.add(tool_name)
            logger.warning(
                "ToolGateway: CIRCUIT OPEN for %s (%d consecutive failures)",
                tool_name, self._fail_counts[tool_name],
            )
            return
        # 成功率 <30% 自动禁用 (总调用 >= 10 才评估, 避免小样本误判)
        total = self._call_counts.get(tool_name, 0)
        fails = self._fail_counts.get(tool_name, 0)
        if total >= 10 and fails / total > 0.7:
            self._circuit_open.add(tool_name)
            logger.warning(
                "ToolGateway: CIRCUIT OPEN for %s (success rate %.0f%% < 30%%, %d calls)",
                tool_name, (1 - fails / total) * 100, total,
            )

    def reset_circuit(self, tool_name: str = None):
        if tool_name:
            self._circuit_open.discard(tool_name)
            self._fail_counts[tool_name] = 0
        else:
            self._circuit_open.clear()
            self._fail_counts.clear()

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "by_tool": dict(self._call_counts),
            "circuit_open": list(self._circuit_open),
        }

    def audit_tail(self, n: int = 20) -> list:
        """Return last N audit log entries."""
        return self._audit_log[-n:]
