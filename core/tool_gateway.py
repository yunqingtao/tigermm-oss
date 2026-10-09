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
        "office_cli", "push_notify", "ntfy", "kb_import", "kb_search", "kb_list", "kb_delete", "chart",
        "task_ledger", "repo_status", "task_flow", "ssq"}
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
        # ★ 2026-09-26 (抄 Codex CLI 的"两个独立旋钮"): 原来 shell_exec **整条封死** ——
        #   能力没了, 但不是安全的胜利 (要跑命令的活只能人自己来)。现在分成两头:
        #     · 默认: 仍旧硬拦 (加性, 旧行为不变)
        #     · 只有 data/egress_policy.json 里写 {"allow_shell": true} 才放行;
        #       放行后**仍然**要过出口闸门 (shell 一律要批, 一次性凭证) + 命令沙箱。
        #   两个旋钮各自独立: 关掉审批还有沙箱, 关掉沙箱 (allow_shell=false) 就整条拦。
        if tool_name in self._RESTRICTED:
            _allowed_by_policy = False
            try:
                from core.egress_guard import get_guard
                _allowed_by_policy = bool(get_guard().policy.get("allow_shell"))
            except Exception:
                _allowed_by_policy = False
            if not _allowed_by_policy:
                self._audit(tool_name, kwargs, False, 0,
                            f"blocked: {tool_name} is restricted (policy.allow_shell 未开)")
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' is restricted — use file_ops or system_info instead",
                    "output": (f"该工具默认封禁。要启用: 在 data/egress_policy.json 里写 "
                               f'{{"allow_shell": true}} —— 开启后每条命令仍要你逐条批准, '
                               f"并在命令沙箱内执行。")
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

        # ── 出口闸门 + 凭据代管 (2026-09-26, 抄 Meta Muse Sentinel) ──
        #   单一出口原则: 决策只在这一个地方做。klass/污点/授权凭证见 core/egress_guard.py
        _eg = None
        try:
            from core.egress_guard import get_guard, scrub_text as _scrub
            _eg = get_guard()
            _dec = _eg.check(tool_name, kwargs)
            if _dec.get("decision") != "allow":
                self._audit(tool_name, kwargs, False, 0, "egress:" + str(_dec.get("reason"))[:80])
                return {
                    "success": False,
                    "error": f"出口闸门拦下 [{_dec.get('klass')}]: {_dec.get('reason')}",
                    "output": str(_dec.get("hint") or "该动作未执行 —— 需批准后方可执行"),
                    "_egress_decision": _dec.get("decision"),
                    "_egress_pending": _dec.get("pending_id"),
                }
            kwargs, _eg_used, _eg_missing = _eg.inject(tool_name, kwargs)
            if _eg_missing:
                return {
                    "success": False,
                    "error": f"未知凭据占位符 {_eg_missing} —— fail closed, 未执行",
                    "output": "凭据名未登记, 已拦下 (不在册的占位符不许原样传给工具)",
                }
        except Exception as _e:
            logger.warning("gateway: 出口闸门故障 %s —— 放行并记账 (见 egress_guard 铁律 1)", _e)
            try:
                _eg._audit_append("guard_error", tool_name, "?", f"gateway: {_e}", "allow")
            except Exception:
                pass
        try:
            _scrub  # 闸门可用时已在上面 import 绑定
        except NameError:  # 只有闸门 import 失败才退回恒等函数 (兜底, 不覆盖真实现)
            def _scrub(t):
                return t

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
            logger.error("ToolGateway: %s error: %s", tool_name, _scrub(str(e)))
            self._audit(tool_name, kwargs, False, elapsed, _scrub(str(e))[:100])
            return {"success": False, "error": _scrub(str(e)), "_elapsed_ms": elapsed}

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
        # ── 出口闸门出向: 抹真值 (凭据不许顺流回上下文) + 读类打污点 ──
        try:
            from core.egress_guard import get_guard as _get_eg2
            result = _get_eg2().settle(tool_name, kwargs, result, is_success)
        except Exception as _e2:
            logger.warning("gateway: egress settle 故障 %s", _e2)
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
