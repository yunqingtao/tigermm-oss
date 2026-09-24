"""
AnomalyDetector -- Tiger.M.M 行为异常检测 (主动免疫层)
======================================================
在静态黑名单(guardrails)之上, 建立用户行为"基准线"式检测:

  1. 深夜时段敏感操作 -- 23:00-06:00 执行删除/写敏感路径类工具 → 风险标记
  2. 敏感路径触碰 -- system32 / Program Files / boot 等系统区
  3. 高频重复危险操作 -- 短窗口内同一敏感动作爆发式调用
  4. 破坏性参数 -- 格式化/清空/递归删除/覆盖系统文件类参数

检测结果不自动阻断(本地单用户Agent), 而是:
  - 返回结果附带 _risk 字段, 供上层提示
  - 写入审计日志 anomaly_log, 供事后追溯
"""
import time, re, logging
from datetime import datetime

logger = logging.getLogger("core.anomaly_detector")

# 敏感工具 → 危险动作
SENSITIVE_ACTIONS = {
    "file_ops": {"delete", "remove", "write", "move", "rename", "clear"},
    "kb_delete": {"delete", "clear", "all"},
    "shell_exec": {"*"},
    "system_info": {"*"},
}

# 敏感系统路径片段 (不区分大小写)
SENSITIVE_PATHS = [
    "system32", "windows\\", "program files", "\\boot", "pagefile.sys",
    "ntuser.dat", "\\recovery", "\\$recycle", "programdata",
]

# 破坏性参数模式
DESTRUCTIVE_PATTERNS = [
    (r'(\b|/)rm\s+(-[a-z]*r[a-z]*\s+)?\S', "递归删除命令"),
    (r'format\s+[a-zA-Z]:', "格式化盘符"),
    (r'del\s+/[a-z]*[fq][a-z]*', "强制删除"),
    (r'DROP\s+TABLE', "删表"),
    (r'TRUNCATE\s+TABLE', "清表"),
    (r'^\s*(清空|删除|清除)\s*(所有|全部|整个)', "批量清空"),
]

NIGHT_START_HOUR = 23   # 深夜开始
NIGHT_END_HOUR = 6      # 深夜结束

RISK_LOG_MAX = 200


class AnomalyDetector:
    """行为异常检测器(进程内单例)。"""

    def __init__(self):
        self._history: list[dict] = []   # {ts, tool, kwargs_snapshot, risk, reason}
        self._burst: dict[str, list[float]] = {}  # tool:action -> [timestamps]

    # ── 单次风险评估 ──

    def assess(self, tool_name: str, kwargs: dict, now: float = None) -> dict:
        """评估一次工具调用风险。返回 {risk: 'none'|'low'|'high', reason: str}。"""
        now = now if now is not None else time.time()
        risk = "none"
        reasons = []

        # 1. 敏感动作 + 深夜
        action = str(kwargs.get("action", "")).lower()
        is_night = self._is_night(now)
        danger_actions = SENSITIVE_ACTIONS.get(tool_name, set())
        if danger_actions and (action in danger_actions or "*" in danger_actions):
            if is_night:
                risk = "high"
                reasons.append(f"深夜({self._hour_str(now)})执行危险动作 {tool_name}.{action}")
            else:
                risk = "low"
                reasons.append(f"危险动作 {tool_name}.{action}")

        # 2. 敏感路径 (强信号: 与时段无关, 触碰系统区即 high)
        param_str = " ".join(str(v) for v in kwargs.values()).lower()
        for sp in SENSITIVE_PATHS:
            if sp in param_str:
                risk = "high"
                reasons.append(f"触碰系统路径 {sp.strip()}")
                break

        # 3. 破坏性参数
        for pat, desc in DESTRUCTIVE_PATTERNS:
            if re.search(pat, param_str, re.IGNORECASE):
                risk = "high"
                reasons.append(desc)
                break

        return {"risk": risk, "reason": "；".join(reasons) if reasons else ""}

    # ── 突发检测: 短窗口内同类危险动作爆发 ──

    def check_burst(self, tool_name: str, kwargs: dict,
                    window: float = 60.0, max_hits: int = 5,
                    now: float = None) -> dict:
        """60秒内同一敏感动作 >= 5 次 → high。返回 {risk, reason}。"""
        now = now if now is not None else time.time()
        action = str(kwargs.get("action", "")).lower()
        key = tool_name + "." + action
        self._burst.setdefault(key, []).append(now)
        self._burst[key] = [t for t in self._burst[key] if now - t <= window]
        if len(self._burst[key]) >= max_hits:
            return {"risk": "high",
                    "reason": f"{window:.0f}秒内 {key} 调用 {len(self._burst[key])} 次 — 疑似失控循环"}
        return {"risk": "none", "reason": ""}

    # ── 记录 + 审计日志 ──

    def record(self, tool_name: str, kwargs: dict, risk: str, reason: str,
               now: float = None) -> None:
        now = now if now is not None else time.time()
        self._history.append({
            "ts": now, "time_str": self._hour_str(now),
            "tool": tool_name, "args": str(kwargs)[:200],
            "risk": risk, "reason": reason,
        })
        if len(self._history) > RISK_LOG_MAX:
            self._history = self._history[-RISK_LOG_MAX:]
        if risk == "high":
            logger.warning("⚠ anomaly[high] %s %s — %s", self._hour_str(now),
                           tool_name, reason)

    def tail(self, n: int = 10) -> list[dict]:
        return list(self._history[-n:])

    def stats(self) -> dict:
        high = sum(1 for h in self._history if h["risk"] == "high")
        low = sum(1 for h in self._history if h["risk"] == "low")
        return {"total": len(self._history), "high": high, "low": low}

    # ── helpers ──

    def _is_night(self, ts: float) -> bool:
        h = datetime.fromtimestamp(ts).hour
        return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR

    @staticmethod
    def _hour_str(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M")


# ═══ Singleton ═══
_detector: AnomalyDetector | None = None


def get_detector() -> AnomalyDetector:
    global _detector
    if _detector is None:
        _detector = AnomalyDetector()
    return _detector
