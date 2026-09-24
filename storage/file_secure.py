"""
Secure file path validation. All file operations must go through this module.
Includes CLI-specific hardening via cli_secure_path().
"""
from pathlib import Path
from config.settings import PROJECT_ROOT, SENSITIVE_FILES
import re

# Additional allowed roots from environment.json
def _get_env_roots():
    try:
        import json
        # Try multiple locations for environment.json
        candidates = [
            PROJECT_ROOT / "data" / "environment.json",
            PROJECT_ROOT.parent / "mary3" / "data" / "environment.json",
            Path(__file__).parent.parent / "data" / "environment.json",
        ]
        for env_f in candidates:
            if env_f.exists():
                data = json.loads(env_f.read_text(encoding="utf-8"))
                return [Path(v["value"]).resolve() for v in data.values()
                        if isinstance(v, dict) and v.get("type") == "path"]
    except Exception:
        pass
    return []

_ENV_ROOTS = _get_env_roots()

def _is_allowed(target: Path, base_root: Path) -> bool:
    # Allow any path on non-system drives (D:, E:, etc.)
    # ★ 2026-09-23 修 (真 bug, 实测): 原判据要求**目标自身已存在** (target.exists()) ——
    #   于是"在 D 盘已存在的目录里新建文件"一律被判越权:
    #     D:\tmm-考卷\秋天的绝句.txt (新文件) → PermissionError: Path outside allowed roots
    #     而同一目录里已存在的 简介.txt 却能读写 (exists=True)。
    #   用户可见表现: "写一首诗放桌面" 成功 (桌面在白名单), "存到 D:\tmm-考卷\x.txt" 报 Access denied。
    #   修法: 非 C 盘改为"目标自身**或父目录**存在"即放行 (新建文件时目标尚不存在)。
    if target.is_absolute():
        drive = target.drive.lower()
        if drive and drive != 'c:' and (target.exists() or target.parent.exists()):
            return True
    # Always-allow roots: Desktop, Documents, Downloads
    _HARD_WHITELIST = [
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home() / "Downloads",
    ]
    all_roots = [base_root.resolve()] + _ENV_ROOTS + [r.resolve() for r in _HARD_WHITELIST if r.exists()]
    for root in all_roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False

# ── 探针写盘闸 (2026-09-24 加) ──────────────────────────────────────────
# 探针猴补了 builtins.open / io.open, 拦得住 python-docx/openpyxl (走 zipfile),
# 但拦不住**子进程**写盘 —— 实测 plantuml 调 java -jar 出图, 猴补无效,
# C6 "画个架构图" 仍把 架构.png 写进用户的 D:\tmm-考卷\。
# 所以在工具调用入口按**参数**再拦一道 (网关是所有工具调用的唯一入口)。
# ★ 只在 TMM_PROBE=1 时生效 ⇒ 生产路径零开销、零行为变化。
_PROBE_WRITE_KEYS = ("path", "paths", "files", "output", "outputs", "save_path", "out_path",
                     "dst", "dest", "target", "folder", "out_dir", "filename", "output_name")


def probe_write_blocked(kwargs: dict) -> str:
    """探针态下, 参数里出现指向**用户目录**的路径 → 返回拒绝原因 (空串=放行)。"""
    import os
    if os.environ.get("TMM_PROBE") != "1":
        return ""
    import tempfile

    roots = []
    try:
        roots.append(PROJECT_ROOT.resolve())
    except Exception:
        pass
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    # ★ 与 _probe_env.install_write_guard 同一口径: 标准用户目录放行
    #   (既有门禁约定 = 探针可写桌面但必须清干净; 拦死了会把门禁弄红)。
    try:
        for _n in ("Desktop", "Documents", "Downloads"):
            _p = Path.home() / _n
            if _p.exists():
                roots.append(_p.resolve())
    except Exception:
        pass

    def _ok(v) -> bool:
        try:
            tp = Path(str(v)).resolve()
        except Exception:
            return True
        for r in roots:
            try:
                tp.relative_to(r)
                return True
            except ValueError:
                continue
        return False

    for k, v in (kwargs or {}).items():
        if k not in _PROBE_WRITE_KEYS:
            continue
        vals = v if isinstance(v, (list, tuple)) else [v]
        for item in vals:
            if not isinstance(item, str) or not item.strip():
                continue
            s = item.strip()
            # 只查"看着是绝对路径"的 (盘符:\ 或 UNC/斜杠开头); 相对路径交给工具解析
            if (len(s) > 2 and s[1] == ":") or s.startswith("\\\\") or s.startswith("//"):
                if not _ok(s):
                    return (f"[TMM_PROBE] 探针模式禁止写用户路径: {s} "
                            f"(参数 {k}; 只允许项目树 / 临时目录)")
    return ""


def safe_path(user_path: str, base_root: Path = None) -> Path:
    """Resolve and validate path stays within base_root (default: PROJECT_ROOT).
    Blocks path traversal (../) and sensitive file access."""
    if base_root is None:
        base_root = PROJECT_ROOT
    
    # Handle absolute paths (e.g. C:\Users\...\Desktop\...)
    p = Path(user_path)
    if p.is_absolute():
        target = p.resolve()
    elif user_path.lower().startswith(('desktop/', 'desktop\\', '桌面/', '桌面\\')):
        sub = user_path[user_path.find('\\')+1 if '\\' in user_path else user_path.find('/')+1:]
        target = (Path.home() / 'Desktop' / sub).resolve()
    elif user_path.lower() in ('desktop', '桌面'):
        target = (Path.home() / 'Desktop').resolve()
    elif user_path.lower().startswith(('documents/', 'documents\\', '文档/', '文档\\')):
        sub = user_path[user_path.find('\\')+1 if '\\' in user_path else user_path.find('/')+1:]
        target = (Path.home() / 'Documents' / sub).resolve()
    elif user_path.lower() in ('documents', '文档'):
        target = (Path.home() / 'Documents').resolve()
    elif user_path.lower().startswith(('downloads/', 'downloads\\', '下载/', '下载\\')):
        sub = user_path[user_path.find('\\')+1 if '\\' in user_path else user_path.find('/')+1:]
        target = (Path.home() / 'Downloads' / sub).resolve()
    elif user_path.lower() in ('downloads', '下载'):
        target = (Path.home() / 'Downloads').resolve()
    else:
        target = (base_root / user_path).resolve()
    
    # Check allowed roots
    if not _is_allowed(target, base_root):
        raise PermissionError(f"Path outside allowed roots: {target}")
    
    # Block sensitive files
    if target.name in SENSITIVE_FILES:
        raise PermissionError(f"Access denied: {target.name}")
    
    return target


def cli_secure_path(raw_path: str) -> Path:
    """CLI-specific enhanced path validation.
    Same as safe_path, but additionally blocks:
    - .crypto.key, users.enc, keys.enc (even if not in SENSITIVE_FILES)
    - Any absolute path escape attempt
    """
    # Normalize: strip leading slashes/backslashes that could be absolute
    cleaned = raw_path.lstrip("\\" + "/")
    if cleaned != raw_path:
        # User tried absolute path — treat as escape attempt
        raise PermissionError(f"CLI: absolute paths forbidden: '{raw_path}'")
    
    # Resolve relative to PROJECT_ROOT
    target = (PROJECT_ROOT / cleaned).resolve()
    
    if not _is_allowed(target, PROJECT_ROOT):
        raise PermissionError(f"CLI: path outside allowed roots: {target}")
    
    # Block sensitive files (hardcoded + from config)
    blocked_names = {".crypto.key", "users.enc", "keys.enc"} | SENSITIVE_FILES
    if target.name in blocked_names:
        raise PermissionError(f"CLI: access denied to sensitive file: {target.name}")
    
    # Block access to mary3 config internals
    if any(part.startswith(".") for part in target.parts[len(PROJECT_ROOT.parts):]):
        raise PermissionError(f"CLI: hidden files/dirs forbidden: {target.name}")
    
    return target


def safe_read(path: str, base_root: Path = None, encoding: str = "utf-8") -> str:
    """Read file through safe_path validation."""
    p = safe_path(path, base_root)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    return p.read_text(encoding=encoding)


_FORBIDDEN_WRITE_EXT = {'.py', '.pyc', '.exe', '.bat', '.cmd', '.ps1', '.vbs', '.sh', '.dll', '.so', '.msi', '.scr', '.com'}

def safe_write(path: str, content: str, base_root: Path = None, encoding: str = "utf-8"):
    """Write file through safe_path validation."""
    p = safe_path(path, base_root)
    if p.suffix.lower() in _FORBIDDEN_WRITE_EXT:
        raise PermissionError(f"安全拦截：禁止写入可执行文件类型 ({p.suffix})")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding=encoding)


def safe_list_dir(path: str = ".", base_root: Path = None) -> list:
    """List directory contents through safe_path."""
    p = safe_path(path, base_root)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    return sorted(p.iterdir())
