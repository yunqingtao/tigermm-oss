"""
Shell execution plugin — safe command execution with security whitelist.
"""
import subprocess, os, shlex, re

PLUGIN = {
    "name": "shell_exec",
    "description": "Shell command execution with 30s timeout (safe: no shell=True)",
    "version": "1.3",
    "requires": [],
    "trigger": ["运行", "执行", "cmd", "shell", "bash", "run", "命令", "终端"],
    "permission": ["system"],
    "category": "automation",
}

# ═══ Security: blocked patterns ═══
DANGEROUS_COMMANDS = [
    # Destructive operations
    # ★ 2026-09-20 修 (实跑发现): 原来是裸 `\bformat\b` —— 本意拦"格式化磁盘",
    #   结果把 ffmpeg/ffprobe 的**合法用法**全拦了:
    #   实测 `ffprobe -show_entries format=duration` → "Security blocked: dangerous pattern '\bformat\b'"
    #   (任何带 format 字样的媒体命令都中招, 而我们正要加"视频抽帧"技能)。
    #   真正的危险形态是"格式化某个盘": format 后跟盘符, 或单独一个 format。
    r'\bformat\s+[a-z]:', r'^\s*format\s*$', r'\bdel\s+/[fsq]', r'\brd\s+/[sq]', r'\berase\b',
    # System control
    r'\bshutdown\b', r'\brestart\b', r'\blogoff\b', r'\block\b',
    # Registry / disk  
    r'\breg\s+(add|delete|import|export)', r'\bdiskpart\b', r'\bchkdsk\b',
    # User/security
    r'\bnet\s+(user|localgroup|share|session)', r'\bicacls\b', r'\bcacls\b',
    r'\btaskkill\b', r'\bwmic\s+process', r'\bstop-process\b',
    # PowerShell dangerous
    r'Remove-Item\s+.*-Recurse.*-Force', r'Stop-Computer', r'Restart-Computer',
    # Network dangerous
    r'\bnetsh\b', r'\bipconfig\s+/release', r'\broute\s+(delete|add)',
    # Boot/config
    r'\bbcdedit\b', r'\bbootrec\b', r'\bsfc\b',
]

PROTECTED_PATHS = [
    r'C:\\Windows', r'C:\\Program\s+Files', r'C:\\ProgramData',
    r'%SystemRoot%', r'%ProgramFiles%', r'%AppData%',
    r'/etc/', r'/bin/', r'/boot/', r'/sys/', r'/proc/',
]

SAFE_COMMANDS = [
    'dir', 'type', 'echo', 'cd', 'mkdir', 'md', 'copy', 'move', 'ren', 'rename',
    'python', 'python3', 'pip', 'node', 'npm', 'npx',
    'where', 'whoami', 'hostname', 'ver', 'date', 'time', 'set',
    'tasklist', 'findstr', 'find', 'sort', 'more', 'tree',
    'git', 'curl', 'wget', 'tar', 'unzip', '7z',
    'wmic', 'fsutil',
]


def _check_security(cmd: str) -> tuple[bool, str]:
    """Check if command is safe. Returns (allowed, reason)."""
    cmd_lower = cmd.lower().strip()
    
    # 1. Check for dangerous patterns
    for pattern in DANGEROUS_COMMANDS:
        if re.search(pattern, cmd_lower):
            return False, f"Blocked: dangerous pattern '{pattern}'"
    
    # 2. Check for protected paths in destructive context
    for pp in PROTECTED_PATHS:
        if re.search(pp, cmd, re.IGNORECASE):
            # Only block if there's also a write/delete action
            # ★ 2026-09-20 修 (与 DANGEROUS_COMMANDS 同一类误判): 这里的破坏词表也有
            #   裸 `format` → 带 "format" 的媒体命令只要路径含 `/bin/`
            #   (如 c:/ffmpeg/bin/ffmpeg.exe) 就被判"改保护路径"。
            #   实测: 抽帧技能被 "Blocked: cannot modify protected path matching '/bin/'" 拦死。
            if re.search(r'\b(del|rd|rm|format\s+[a-z]:|move|ren|copy)\b', cmd_lower):
                return False, f"Blocked: cannot modify protected path matching '{pp}'"
    
    # 3. Extract base command and check against safe list
    # Handle complex syntax: strip redirects and pipes for base command check
    base = re.split(r'[|&><]', cmd_lower)[0].strip()
    base_cmd = base.split()[0] if base.split() else ""
    
    # Allow commands with full paths (e.g., D:\tools\something.exe)
    if '\\' in base_cmd or '/' in base_cmd:
        return True, ""
    
    # Allow known safe commands
    if base_cmd in SAFE_COMMANDS:
        return True, ""
    
    # For unknown commands, check if they look like file paths or common tools
    if base_cmd.endswith(('.exe', '.bat', '.cmd', '.py', '.js')):
        return True, ""
    
    # If we can't identify it but it doesn't match dangerous patterns, allow with warning
    return True, f"Note: unrecognized command '{base_cmd}' — allowed but monitor"


async def run(**kwargs) -> dict:
    cmd = kwargs.get("command", "")
    cwd = kwargs.get("cwd", os.getcwd())
    if not cmd:
        return {"success": False, "error": "No command provided"}
    
    # ═══ Security check ═══
    allowed, reason = _check_security(cmd)
    if not allowed:
        return {"success": False, "error": f"Security blocked: {reason}"}
    
    # cmd.exe built-in commands (not executables, need cmd /c)
    CMD_BUILTINS = {'dir', 'cd', 'type', 'echo', 'copy', 'del', 'mkdir', 'md',
                    'rmdir', 'rd', 'ren', 'rename', 'set', 'cls', 'date', 'time',
                    'ver', 'vol', 'title', 'color', 'prompt', 'assoc', 'ftype'}
    base_cmd_name = cmd.strip().split()[0].lower() if cmd.strip().split() else ""
    is_builtin = base_cmd_name in CMD_BUILTINS
    
    # Detect complex cmd.exe syntax
    # ★ 2026-09-23 修 (真 bug, 实测): 原写法扫**整串**, 引号里的特殊符也算数 ——
    #   实测 `python -c "import openpyxl; ... ' | '.join(...)"` 里那个 `|` 是**代码内容**,
    #   却被判成管道 → 走 `cmd /c` → cmd 的引号规则把嵌套引号切碎
    #   → python 收到 `"import ...` → SyntaxError: unterminated string literal。
    #   修法: 先剥掉引号包裹的片段, 只在**引号之外**判定特殊符。
    _unquoted = re.sub(r'"[^"]*"|\'[^\']*\'', '', cmd)
    has_special = any(ch in _unquoted for ch in ['&&', '||', '|', '>', '<', '2>', '1>'])
    try:
        if has_special or is_builtin:
            # ★ 2026-09-23 修 (真 bug, 老代码即有): 原写法 ["cmd","/c",cmd] + shell=False ——
            #   Python 的 list2cmdline 会给**含空格的整个命令串**再包一层引号,
            #   cmd 于是把 `"dir /b "` 当成命令名 → "文件名、目录名或卷标语法不正确"。
            #   实测 (同机同串):
            #     ["cmd","/c",'dir /b "D:\\...\\docs" | findstr TMM']        → rc=1 失败
            #     直接 shell=True 同一条                                        → rc=0 正常
            #   崩的是**带内层引号**的命令 (引号包路径/引号包代码); 裸 `dir /b`
            #   两种写法都正常 (cmd 会剥掉最外层引号) —— 所以旧代码"有时能用"。
            #   本分支本意就是交给 cmd 解析,
            #   故改用 shell=True (安全闸 _check_security 已在上游拦过危险命令)。
            r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=30, cwd=cwd, shell=True)
        else:
            parts = shlex.split(cmd)
            r = subprocess.run(parts, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=30, cwd=cwd, shell=False)
        output = (r.stdout or r.stderr or "(no output)")[:5000]
        result = {"success": r.returncode == 0, "output": output,
                "exit_code": r.returncode}
        if reason:
            result["warning"] = reason
        return result
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timed out (30s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}
