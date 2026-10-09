"""
ErrorCN — translate common tool errors to Chinese.
"""
import re

# Pattern → Chinese translation
_ERROR_MAP = [
    (r"timed? ?out after \d+s?", "请求超时"),
    (r"Connection refused", "连接被拒绝"),
    (r"Connection reset", "连接重置"),
    (r"Network (is )?unreachable", "网络不可达"),
    (r"Name or service not known", "域名解析失败"),
    (r"authentication failed", "认证失败"),
    (r"Unable to log on", "登录失败，密码或授权码错误"),
    (r"Unauthorized", "未授权，检查API Key"),
    (r"permission denied", "权限不足"),
    (r"Access denied", "访问被拒绝"),
    (r"not found", "未找到"),
    (r"No such file", "文件不存在"),
    (r"does not exist", "不存在"),
    (r"Is a directory", "这是一个目录，请指定文件名"),
    (r"rate limit", "请求太频繁，稍后再试"),
    (r"too many requests", "请求过多"),
    (r"insufficient_balance", "余额不足"),
    (r"Payment Required", "需要付费"),
    (r"blocked", "已被安全策略拦截"),
    (r"restricted", "已被限制"),
    (r"Failed to build", "构建失败"),
    (r"ModuleNotFoundError", "缺少依赖"),
    (r"ImportError", "导入失败"),
    (r"SyntaxError", "代码语法错误"),
    (r"JSONDecodeError", "JSON解析失败"),
    (r"UnicodeDecodeError", "编码错误"),
    (r"out of memory", "内存不足"),
    (r"disk full", "磁盘已满"),
    (r"SSL.*error", "SSL证书错误"),
    (r"certificate verify failed", "证书验证失败"),
]


def translate(error_text: str) -> str:
    """Translate English error message to Chinese.
    If no match found, returns original text.
    """
    if not error_text:
        return "未知错误"
    for pattern, cn in _ERROR_MAP:
        if re.search(pattern, error_text, re.IGNORECASE):
            return cn
    return error_text


def wrap_error(result: dict) -> dict:
    """If result is a failed dict, translate its error field."""
    if not isinstance(result, dict):
        return result
    if result.get("success", True):
        return result
    err = result.get("error", "")
    if err and not _is_chinese(err):
        result = dict(result)
        result["error_cn"] = translate(err)
    return result


def _is_chinese(text: str) -> bool:
    """Check if text contains Chinese characters."""
    return bool(re.search(r'[\u4e00-\u9fff]', text))
