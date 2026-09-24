"""
Code sandbox — isolated Python execution with security restrictions.
No tool() proxy. No __import__ escape. No open(). No eval/exec.
Thread-based timeout. Default process isolation. No env var dependency.
"""
import io
import json
import re
import sys
import time
import threading
from pathlib import Path
from config.settings import SANDBOX_TIMEOUT, MAX_CODE_LENGTH, MAX_CODE_OUTPUT, FORBIDDEN_MODULES, PROJECT_ROOT

class CodeSandbox:
    MAX_EXEC_TIME = SANDBOX_TIMEOUT
    MAX_OUTPUT = MAX_CODE_OUTPUT
    _exec_count = 0

    # Minimal safe builtins — NO open, NO __import__, NO Path, NO file IO
    SAFE_BUILTINS = {
        "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
        "chr": chr, "dict": dict, "divmod": divmod, "enumerate": enumerate,
        "filter": filter, "float": float, "format": format, "frozenset": frozenset,
        "hex": hex, "int": int, "isinstance": isinstance, "issubclass": issubclass,
        "iter": iter, "len": len, "list": list, "map": map, "max": max,
        "min": min, "next": next, "oct": oct, "ord": ord, "pow": pow,
        "print": print, "range": range, "repr": repr, "reversed": reversed,
        "round": round, "set": set, "slice": slice, "sorted": sorted,
        "str": str, "sum": sum, "tuple": tuple, "type": type, "zip": zip,
        "json": json, "re": re,
        "math": __import__("math"),
        "datetime": __import__("datetime"),
        "collections": __import__("collections"),
        "itertools": __import__("itertools"),
        "random": __import__("random"),
        "True": True, "False": False, "None": None,
        "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "IndexError": IndexError,
    }
    
    FORBIDDEN_MODULES = {"os", "subprocess", "shutil", "socket", "urllib", "requests",
                          "sqlite3", "ctypes", "multiprocessing", "http", "ftplib",
                          "telnetlib", "smtplib", "poplib", "imaplib", "pickle", "marshal", "sys"}

    # Dynamic execution patterns to block
    DYNAMIC_EXEC_PATTERNS = ["__import__(", "eval(", "exec(", "compile("]

    @staticmethod
    def extract_code(response):
        blocks = []
        pattern = r"```(?:python|py)?\s*\n(.*?)```"
        for match in re.finditer(pattern, response, re.DOTALL):
            blocks.append(match.group(1).strip())
        return blocks

    @staticmethod
    def _pre_scan(code: str) -> str | None:
        """Pre-scan code for dangerous patterns. Returns error string or None."""
        code_lower = code.lower()
        
        # Block dynamic execution
        for pat in CodeSandbox.DYNAMIC_EXEC_PATTERNS:
            if pat.lower() in code_lower:
                return f"安全拦截：禁止动态执行 '{pat}'"
        
        # Block dangerous shell patterns
        dangerous = ["rm -rf", "format c:", "del /f", "shutdown", "reboot",
                     ":(){ :|:& };:", "chmod 777"]
        for pat in dangerous:
            if pat in code_lower:
                return f"安全拦截：危险操作 '{pat}'"
        
        # Block forbidden module imports
        for mod in CodeSandbox.FORBIDDEN_MODULES:
            if f"import {mod}" in code_lower or f"from {mod}" in code_lower:
                return f"安全拦截：禁止导入高危模块 {mod}"
        
        # Block open() calls
        if "open(" in code_lower:
            return "安全拦截：沙箱禁止文件IO操作 open()"
        
        return None

    @staticmethod
    def execute(code):
        """Execute code. Default: process isolation (isolate_proc)."""
        # Process isolation dispatch — default ON (no env var dependency)
        import os as _os_iso
        from config.settings import TIGERMM_ISOLATE
        if TIGERMM_ISOLATE:
            try:
                from proc_isolate import ProcessIsolator as _PI
                iso = _PI.get()
                r = iso.submit_task("code_run", code=code, timeout=CodeSandbox.MAX_EXEC_TIME)
                if r.get("success") and r.get("data"):
                    return r["data"]
                return {"success": False, "error": r.get("err", "isolate error"),
                        "stdout": "", "stderr": r.get("err", "")}
            except ImportError:
                pass  # Fall through to in-process sandbox
        
        CodeSandbox._exec_count += 1
        
        if len(code) > MAX_CODE_LENGTH:
            return {"stdout": "", "stderr": "", "error": f"Code too long (max {MAX_CODE_LENGTH} chars)", "success": False}
        
        # Pre-scan
        scan_error = CodeSandbox._pre_scan(code)
        if scan_error:
            return {"stdout": "", "stderr": "", "error": scan_error, "success": False}
        
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        result = {"stdout": "", "stderr": "", "error": "", "success": False}
        restricted_globals = {"__builtins__": CodeSandbox.SAFE_BUILTINS}

        exec_exc = [None]

        def _run():
            try:
                old_stdout = sys.stdout
                old_stderr = sys.stderr
                sys.stdout = stdout_buf
                sys.stderr = stderr_buf
                try:
                    exec(code, restricted_globals)
                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
            except Exception as e:
                exec_exc[0] = e

        _t_exec_start = time.time()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=CodeSandbox.MAX_EXEC_TIME)

        if t.is_alive():
            result["error"] = f"Execution timed out after {CodeSandbox.MAX_EXEC_TIME}s"
        elif exec_exc[0]:
            result["error"] = f"{type(exec_exc[0]).__name__}: {exec_exc[0]}"
        else:
            result["success"] = True
            output = stdout_buf.getvalue()
            if len(output) > CodeSandbox.MAX_OUTPUT:
                output = output[:CodeSandbox.MAX_OUTPUT] + "\n[...truncated]"
            result["stdout"] = output
            result["stderr"] = stderr_buf.getvalue()[:CodeSandbox.MAX_OUTPUT]
            result["exec_count"] = CodeSandbox._exec_count
            result["exec_time_ms"] = round((time.time() - _t_exec_start) * 1000)

        return result
