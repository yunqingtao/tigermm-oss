"""
Backup tool — creates and lists ZIP backups of the project.
Registered as a plugin tool so intent_router can dispatch it.
"""
import os, zipfile, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
BACKUP_DIR = PROJECT_ROOT / "backups"
MAX_BACKUPS = 10

TOOL = {
    "name": "backup",
    "description": "创建或列出项目备份",
    "keywords": ["备份", "备份列表", "备份文件", "创建备份"],
    "params": [
        {"name": "action", "type": "str", "required": False,
         "enum": ["create", "list", "prune"],
         "description": "create=创建新备份, list=列出备份, prune=清理旧备份"},
    ],
}

def execute(**kwargs):
    action = kwargs.get("action", "list")
    os.makedirs(BACKUP_DIR, exist_ok=True)

    if action == "list":
        items = []
        # Scan from all known backup locations
        search_dirs = [BACKUP_DIR]
        # Also scan the old backup system's dir (project_root.parent.parent/backups)
        legacy = PROJECT_ROOT.parent.parent / "backups"
        if legacy.exists():
            search_dirs.append(legacy)
        
        for d in search_dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*_[0-9]*.zip"), reverse=True):
                sz = f.stat().st_size
                ts = time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime))
                items.append(f"{f.name} ({sz/1024/1024:.1f}MB) {ts}")
        if not items:
            return {"success": True, "output": "暂无备份", "count": 0}
        return {"success": True, "output": "\n".join(items[:10]), "count": len(items)}

    if action == "create":
        ts = time.strftime("%Y%m%d_%H%M%S")
        name = BACKUP_DIR / f"backup_{ts}.zip"
        exclude = {"__pycache__", ".pytest_cache", "node_modules", ".git",
                   "backups", "logs", "*.pyc", "*.db", "tmm_output.log"}
        
        with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(PROJECT_ROOT):
                dirs[:] = [d for d in dirs if d not in exclude]
                for fn in files:
                    fp = os.path.join(root, fn)
                    if any(fp.endswith(ext) for ext in [".pyc", ".db"]):
                        continue
                    arc = os.path.relpath(fp, PROJECT_ROOT)
                    zf.write(fp, arc)
        
        sz = name.stat().st_size
        # Prune to max
        existing = sorted(BACKUP_DIR.glob("backup_*.zip"))
        for old in existing[:-MAX_BACKUPS]:
            old.unlink()

        return {"success": True, "output": f"备份完成: {name.name} ({sz/1024/1024:.1f}MB) | 保留最近{MAX_BACKUPS}个"}

    if action == "prune":
        existing = sorted(BACKUP_DIR.glob("backup_*.zip"))
        removed = 0
        for old in existing[:-MAX_BACKUPS]:
            old.unlink()
            removed += 1
        return {"success": True, "output": f"清理 {removed} 个旧备份，保留 {min(len(existing), MAX_BACKUPS)} 个"}

    return {"success": False, "output": f"未知操作: {action}", "error": f"Unknown action: {action}"}

async def run(**kwargs):
    return execute(**kwargs)
