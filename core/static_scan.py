"""
StaticScan -- Tiger.M.M 静态安全扫描 (主动免疫层, 补 code_engine 的语法检查缺口)
==============================================================================
用 Python AST 扫描 CodeEngine 生成/修改的代码, 检测危险调用与硬编码密钥,
而不只是 py_compile 语法检查。

规则分级:
  high   -- eval/exec/动态导入/进程执行/网络套接字/系统调用
  medium -- 硬编码密钥/危险模块导入/写系统路径
  low    -- 可疑但常见模式 (如 requests.post)

纯 stdlib (ast), 零第三方依赖。扫描发现不阻断执行, 以警告形式注入验证结果。
"""
import ast, re, logging
from pathlib import Path

logger = logging.getLogger("core.static_scan")

# 规则: (节点类型, 匹配器, 严重级, 描述)
HIGH_CALLS = {
    "eval": "eval() 动态执行 — 代码注入风险",
    "exec": "exec() 动态执行 — 代码注入风险",
    "compile": "compile() 动态编译",
    "__import__": "__import__ 动态导入",
    "system": "os.system 系统调用",
    "popen": "os.popen 系统调用",
    "remove": "os.remove 删除文件",
    "rmtree": "shutil.rmtree 递归删除",
    "unlink": "os.unlink 删除文件",
    "getattr": "getattr 动态属性访问(可能绕过限制)",
}

MEDIUM_CALLS = {
    "subprocess": "subprocess 子进程执行",
    "socket": "socket 网络套接字",
    "ctypes": "ctypes 原生库调用",
    "winreg": "注册表操作",
    "load": "pickle.load 反序列化(不可信数据危险)",
    "requests": "requests 网络请求",
    "urlopen": "urllib 网络请求",
    "write": "文件写入",
}

# 硬编码密钥模式: 赋值语句右侧为字符串字面量
SECRET_KEYS = re.compile(r'(password|passwd|api[_-]?key|secret|token|auth[_-]?code)', re.IGNORECASE)
SECRET_VALUE = re.compile(r'[A-Za-z0-9_\-\.]{10,}')


class _Visitor(ast.NodeVisitor):
    """AST 访问器: 收集风险发现。"""

    def __init__(self, source: str):
        self.source = source
        self.findings: list[dict] = []
        self._lines = source.splitlines()

    def _line_text(self, lineno: int) -> str:
        try:
            return (self._lines[lineno - 1] or "").strip()[:100]
        except Exception:
            return ""

    def _add(self, lineno: int, severity: str, rule: str, desc: str):
        self.findings.append({
            "line": lineno, "severity": severity, "rule": rule,
            "desc": desc, "snippet": self._line_text(lineno),
        })

    def visit_Call(self, node: ast.Call):
        try:
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in HIGH_CALLS:
                self._add(node.lineno, "high", name, HIGH_CALLS[name])
            elif name in MEDIUM_CALLS:
                self._add(node.lineno, "medium", name, MEDIUM_CALLS[name])
        except Exception:
            pass
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self._check_import(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            self._check_import(node.module, node.lineno)

    def _check_import(self, module: str, lineno: int):
        top = module.split(".")[0]
        if top in ("subprocess", "socket", "ctypes", "winreg", "pickle",
                   "paramiko", "ftplib", "telnetlib", "win32com", "wmi"):
            self._add(lineno, "high", "import_" + top,
                      f"导入 {module} — 进程/网络/系统级操作")
        elif top in ("requests", "urllib", "http", "asyncio"):
            self._add(lineno, "low", "import_" + top, f"导入 {module} — 网络操作")

    def visit_Assign(self, node: ast.Assign):
        try:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                val = node.value.value
                if len(val) >= 10 and SECRET_VALUE.search(val):
                    for t in node.targets:
                        tname = ""
                        if isinstance(t, ast.Name):
                            tname = t.id
                        elif isinstance(t, ast.Attribute):
                            tname = t.attr
                        if SECRET_KEYS.search(tname):
                            self._add(node.lineno, "medium", "hardcoded_secret",
                                      f"疑似硬编码密钥: {tname}")
                            break
        except Exception:
            pass
        self.generic_visit(node)


def scan_code(code: str, filename: str = "generated.py") -> dict:
    """扫描一段 Python 代码。返回 {safe, findings, summary}。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {
            "safe": False,
            "findings": [{"line": getattr(e, "lineno", 0), "severity": "high",
                          "rule": "syntax", "desc": f"语法错误: {e}",
                          "snippet": ""}],
            "summary": "语法错误",
        }
    v = _Visitor(code)
    v.visit(tree)
    findings = v.findings
    high = [f for f in findings if f["severity"] == "high"]
    summary = "干净" if not findings else \
        (f"{len(high)} 高危 + {len(findings) - len(high)} 关注" if high
         else f"{len(findings)} 项关注")
    return {
        "safe": not high,
        "findings": findings,
        "summary": summary,
        "filename": filename,
    }


def scan_file(path) -> dict:
    """扫描一个 .py 文件。"""
    p = Path(path)
    try:
        code = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"safe": False, "findings": [], "summary": f"读取失败: {e}",
                "filename": str(p)}
    return scan_code(code, filename=str(p))


def format_report(result: dict, limit: int = 8) -> str:
    """扫描结果 → 中文报告(注入验证输出)。"""
    findings = result.get("findings", [])
    if not findings:
        return ""
    lines = [f"⚠ [静态扫描] {result.get('filename', '?')}: {result.get('summary', '')}"]
    for f in findings[:limit]:
        sev_cn = {"high": "高危", "medium": "关注", "low": "提示"}.get(f.get("severity"), "?")
        snip = f.get("snippet", "")
        lines.append(f"  L{f.get('line', '?')} [{sev_cn}] {f.get('desc', '')}"
                     + (f" | {snip}" if snip else ""))
    if len(findings) > limit:
        lines.append(f"  ... 共 {len(findings)} 项")
    return "\n".join(lines)
