"""
SessionTracker — snapshot core files at startup, diff on /changelog.
"""
import os, hashlib, json, time
from pathlib import Path

SNAPSHOT_FILE = "session_snapshot.json"

# Directories to track
TRACK_DIRS = ["core", "tools", "mcp", "config"]


def _md5(path: str) -> str:
    """Fast MD5 of file contents."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def take_snapshot(project_root: str) -> dict:
    """Snapshot all .py files in tracked dirs. Returns {relpath: md5}."""
    snap = {}
    root = Path(project_root)
    for d in TRACK_DIRS:
        dpath = root / d
        if not dpath.exists():
            continue
        for f in dpath.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                rel = str(f.relative_to(root))
                snap[rel] = _md5(str(f))
            except OSError:
                pass
    # Save to data dir
    # ★ 2026-09-23: 路径可被 TMM_SESSION_SNAPSHOT 覆盖 —— Level4Pipeline.__init__ 每次
    #   都会调本函数重写快照; 探针/门禁只是 new 一个 pipeline, 真实
    #   data/session_snapshot.json 就被改成"探针跑过后"的指纹 (实测抓到)。
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _snap_out = os.environ.get("TMM_SESSION_SNAPSHOT") or str(data_dir / SNAPSHOT_FILE)
    with open(_snap_out, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "files": snap}, f, indent=2)
    return snap


def get_session_changes(project_root: str) -> dict:
    """Compare current files to startup snapshot.
    Returns {added: [...], modified: [...], deleted: [...], unchanged: N}"""
    root = Path(project_root)
    data_dir = root / "data"
    # ★ 2026-09-23: 与写入端同源 (同样可被环境变量覆盖)
    snap_path = Path(os.environ.get("TMM_SESSION_SNAPSHOT") or str(data_dir / SNAPSHOT_FILE))

    # Load snapshot
    if not snap_path.exists():
        return {"error": "no snapshot — restart TMM first", "added": [], "modified": [], "deleted": []}

    try:
        with open(snap_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        snap = data.get("files", {})
        snap_ts = data.get("ts", 0)
    except (json.JSONDecodeError, KeyError):
        return {"error": "snapshot corrupted", "added": [], "modified": [], "deleted": []}

    added = []
    modified = []
    deleted = []
    unchanged = 0

    # Scan current files
    current_files = set()
    for d in TRACK_DIRS:
        dpath = root / d
        if not dpath.exists():
            continue
        for f in dpath.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            rel = str(f.relative_to(root))
            current_files.add(rel)

            if rel not in snap:
                added.append(rel)
            else:
                try:
                    if _md5(str(f)) != snap[rel]:
                        modified.append(rel)
                    else:
                        unchanged += 1
                except OSError:
                    pass

    # Check deleted
    for rel in snap:
        if rel not in current_files:
            deleted.append(rel)

    return {
        "snap_ts": snap_ts,
        "snap_age": _format_age(time.time() - snap_ts),
        "added": sorted(added),
        "modified": sorted(modified),
        "deleted": sorted(deleted),
        "unchanged": unchanged,
        "total": len(snap),
    }


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h{int((seconds % 3600) / 60)}m"


def format_session_report(changes: dict) -> str:
    """Format session changes as readable text."""
    if "error" in changes:
        return f"[changelog] {changes['error']}"

    lines = [
        f"会话变更 (快照 {changes['snap_age']}前, {changes['total']}个文件)",
        "─" * 40,
    ]

    if changes["added"]:
        lines.append(f"新增 {len(changes['added'])}:")
        for f in changes["added"][:15]:
            lines.append(f"  + {f}")
        if len(changes["added"]) > 15:
            lines.append(f"  ... 还有 {len(changes['added']) - 15} 个")

    if changes["modified"]:
        lines.append(f"修改 {len(changes['modified'])}:")
        for f in changes["modified"][:15]:
            lines.append(f"  ~ {f}")
        if len(changes["modified"]) > 15:
            lines.append(f"  ... 还有 {len(changes['modified']) - 15} 个")

    if changes["deleted"]:
        lines.append(f"删除 {len(changes['deleted'])}:")
        for f in changes["deleted"][:10]:
            lines.append(f"  - {f}")

    if not changes["added"] and not changes["modified"] and not changes["deleted"]:
        lines.append("无变更")

    lines.append(f"未变: {changes['unchanged']} | 快照: {changes['total']}")
    return "\n".join(lines)
