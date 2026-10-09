"""
Verify → Fix loop: classify failures, recover or escalate.
Wired into _process_external tool execution.
"""
import logging

logger = logging.getLogger("core.verify_fix")

# ── Error classification ──
def classify_error(result: dict) -> str:
    """Classify tool failure into recovery category.
    Returns: timeout|network|perm|not_found|auth|rate|unknown"""
    if not isinstance(result, dict) or result.get("success", False):
        return "ok"
    err = str(result.get("error", "")).lower()
    if not err:
        return "ok"

    if "timeout" in err or "timed out" in err:
        return "timeout"
    if "connect" in err or "refused" in err or "network" in err or "unreachable" in err:
        return "network"
    if "permission" in err or "restricted" in err or "blocked" in err or "denied" in err:
        return "perm"
    if "not found" in err or "no such file" in err or "does not exist" in err:
        return "not_found"
    if "auth" in err or "login" in err or "401" in err or "unauthorized" in err or "unable to log" in err:
        return "auth"
    if "rate" in err or "limit" in err or "too many" in err:
        return "rate"
    return "unknown"


# ── Recovery strategies ──
RECOVERY = {
    "timeout": {
        "retry": True, "max_retries": 2, "backoff": 2.0,
        "hint": "超时了。试试减少数据量，或用更小的limit/offset分批读。"
    },
    "network": {
        "retry": True, "max_retries": 2, "backoff": 3.0,
        "hint": "网络不通。检查代理或换用离线工具。"
    },
    "perm": {
        "retry": False,
        "hint": "权限不足。换种方式："
    },
    "not_found": {
        "retry": False,
        "hint": "没找到。试试扩大范围：去掉keyword过滤、换成list看全貌、或用search遍历。"
    },
    "auth": {
        "retry": False,
        "hint": "认证失败。"
    },
    "rate": {
        "retry": True, "max_retries": 1, "backoff": 5.0,
        "hint": "请求太频繁。等几秒再试。"
    },
    "unknown": {
        "retry": True, "max_retries": 1, "backoff": 1.0,
        "hint": "失败了。换种方式重试。"
    },
}

# ── Alternative tool suggestions for blocked tools ──
TOOL_ALTERNATIVES = {
    "shell_exec": ["file_ops", "system_info"],
    "browser": ["web_search", "firecrawl"],
    "nominatim": ["web_search"],
    "libretranslate": ["web_search"],
}


def get_fix_hint(tool_name: str, error_type: str) -> str:
    """Generate a targeted recovery hint for the model."""
    base = RECOVERY.get(error_type, RECOVERY["unknown"])["hint"]

    if error_type == "perm" and tool_name in TOOL_ALTERNATIVES:
        alts = "、".join(TOOL_ALTERNATIVES[tool_name])
        return f"{base} 用 {alts} 代替 {tool_name}。"

    if error_type == "auth":
        if "mail" in tool_name or "email" in tool_name:
            return f"{base} 邮箱密码或授权码可能过期。告诉用户去网页版重新生成授权码。"
        return f"{base} 告诉用户检查API key或账号密码。"

    if error_type == "not_found" and tool_name == "check_mail":
        return f"{base} 试试 action=list 不加 keyword，或者 action=stat 看收件箱状态。"

    return base


def should_retry(error_type: str, attempt: int) -> bool:
    """Check if this error type should be retried at this attempt."""
    strategy = RECOVERY.get(error_type, RECOVERY["unknown"])
    if not strategy["retry"]:
        return False
    return attempt < strategy["max_retries"]


def get_backoff(error_type: str) -> float:
    """Get backoff delay in seconds for this error type."""
    return RECOVERY.get(error_type, RECOVERY["unknown"]).get("backoff", 1.0)


def format_escalation(tool_name: str, error_type: str, attempts: int,
                      original_error: str) -> str:
    """Format an escalation message for the user when recovery fails."""
    type_names = {
        "timeout": "超时", "network": "网络故障", "perm": "权限不足",
        "not_found": "未找到", "auth": "认证失败", "rate": "频率限制",
        "unknown": "未知错误",
    }
    cn = type_names.get(error_type, error_type)
    return (
        f"⛔ {tool_name} {cn}，{attempts}次重试均失败。\n"
        f"原因: {original_error[:200]}\n"
        f"请换一种方式，或告诉我如何处理。"
    )
