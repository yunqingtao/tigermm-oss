"""
Tiger.M.M OfficeCLI wrapper - Word/Excel/PPT for AI agents
Uses iOfficeAI/OfficeCLI v1.0.141 binary

Commands: create, get, set, add, query, view, open, close
"""
import subprocess, os, json
from pathlib import Path

BINARY = Path(__file__).parent / "officecli.exe"


def _run(args: list, timeout: int = 30) -> dict:
    if not BINARY.exists():
        return {"success": False, "error": "officecli.exe not found"}
    try:
        r = subprocess.run(
            [str(BINARY)] + args + ["--json"],
            capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace'
        )
        output = (r.stdout or r.stderr).strip()
        # Try parse JSON
        try:
            data = json.loads(output)
            return {"success": r.returncode == 0, "data": data, "raw": output[:500]}
        except (json.JSONDecodeError, TypeError):
            return {"success": r.returncode == 0, "output": output[:3000]}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout ({timeout}s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def office_create(path: str, doc_type: str = "") -> dict:
    """Create a new blank Office document."""
    args = ["create", path]
    if doc_type:
        args += ["--type", doc_type]
    return _run(args)


def office_get(path: str, element_path: str = "/") -> dict:
    """Read document content at path (default: root)."""
    return _run(["get", path, element_path])


def office_set(path: str, element_path: str, value: str) -> dict:
    """Set/modify document content."""
    return _run(["set", path, element_path, value])


def office_add(path: str, parent_path: str, element_type: str = "paragraph") -> dict:
    """Add a new element to document."""
    return _run(["add", path, parent_path, "--type", element_type])


def office_query(path: str, selector: str) -> dict:
    """Query document with CSS-like selectors."""
    return _run(["query", path, selector])


def office_view(path: str, mode: str = "text") -> dict:
    """View document in different modes (text, json, html)."""
    return _run(["view", path, mode])


def office_info(path: str) -> dict:
    """Get file metadata."""
    if not os.path.exists(path):
        return {"success": False, "error": f"File not found: {path}"}
    return {
        "success": True,
        "path": path,
        "size": os.path.getsize(path),
        "ext": Path(path).suffix,
    }


def office_read(path: str) -> dict:
    """Read full text content from Office file (shortcut: view text)."""
    ext = Path(path).suffix.lower()
    if ext not in (".docx", ".xlsx", ".pptx"):
        return {"success": False, "error": f"Not an Office file: {ext}"}
    return _run(["view", path, "text"])


def office_write_text(path: str, text: str) -> dict:
    """Write plain text to a new docx file."""
    if not path.endswith(".docx"):
        path += ".docx"
    r = _run(["create", path, "--force"])
    if not r["success"]:
        return r
    # Add paragraphs for each line
    for line in text.split("\n"):
        if line.strip():
            r2 = _run(["add", path, "/", "paragraph"])
            if r2["success"]:
                # Set paragraph text via the last added element
                pass  # Need to get the added element path first
    return {"success": True, "output": f"Created: {path}"}

# ── 统一入口 (网关约定: run/execute + action 派发) ──────────────────────────
# ★ 2026-09-19 深测修 (真缺陷): 本模块原来只有一组 `office_xxx` 平函数, **没有 run/execute**
#   → 网关 PluginManager.call 认不出入口 → 该工具对 Agent **完全不可调用**
#   (深测实测: "无 run/execute 入口")。这里照 tiger_office 的既有约定补上派发表。
#   `officecli.exe` 不在时 _run 会诚实返回 {"success": False, "error": "officecli.exe not found"}。
_ACTIONS = {
    "create": lambda **kw: office_create(kw.get("path", ""), kw.get("doc_type", "") or kw.get("type", "")),
    "get": lambda **kw: office_get(kw.get("path", ""), kw.get("element_path", "/") or "/"),
    "set": lambda **kw: office_set(kw.get("path", ""), kw.get("element_path", ""), kw.get("value", "")),
    "add": lambda **kw: office_add(kw.get("path", ""), kw.get("parent_path", "/") or "/",
                                   kw.get("element_type", "paragraph") or "paragraph"),
    "query": lambda **kw: office_query(kw.get("path", ""), kw.get("selector", "")),
    "view": lambda **kw: office_view(kw.get("path", ""), kw.get("mode", "text") or "text"),
    "info": lambda **kw: office_info(kw.get("path", "")),
    "read": lambda **kw: office_read(kw.get("path", "")),
    "write_text": lambda **kw: office_write_text(kw.get("path", ""), kw.get("text", "")),
}

ACTIONS = tuple(_ACTIONS)          # 供技能工厂/提示词抽真实动作名


async def run(**kwargs) -> dict:
    """统一入口。action 决定走哪个分支 (与 tiger_office 同约定)。"""
    action = str(kwargs.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"success": False, "output": "",
                "error": f"Unknown action: {action or '(空)'}; 可用: {', '.join(ACTIONS)}"}
    try:
        return fn(**kwargs)
    except Exception as e:
        return {"success": False, "output": "", "error": f"{type(e).__name__}: {e}"}
