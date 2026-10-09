"""
AutoBackup — TMM 自动备份
========================
ZIP 打包整个 mary3 目录，保留最近 N 个备份。

排除: __pycache__/, backup*/, data/code_pool/, .git/, *.pyc, *.db-shm, *.db-wal
保留: 10 个最新备份
触发: CLI 退出 / /backup 命令 / 每2小时自动
"""
import os, time, zipfile, logging, shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger("core.backup")

BACKUP_DIR_NAME = "backups"
KEEP_COUNT = 10
AUTO_INTERVAL = 7200  # 2 hours

# 精确匹配的目录名
EXCLUDE_DIRS_EXACT = {"__pycache__", ".git"}
# 排除的文件后缀（含 SQLite WAL 临时文件）
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".db-shm", ".db-wal"}


class AutoBackup:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.backup_dir = self.project_root.parent.parent / BACKUP_DIR_NAME
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._last_auto = 0.0

    def _dir_should_exclude(self, dirname: str) -> bool:
        """目录级排除：backup* 前缀 || 精确匹配黑名单 || data/code_pool"""
        if dirname.startswith("backup"):
            return True
        if dirname in EXCLUDE_DIRS_EXACT:
            return True
        return False

    def _should_exclude(self, path: str) -> bool:
        """文件级排除：检查后缀 和 路径中是否穿过被排除的目录"""
        fname = os.path.basename(path)
        for suffix in EXCLUDE_FILE_SUFFIXES:
            if fname.endswith(suffix):
                return True
        # 检查路径中是否包含 backup* 目录或 code_pool
        parts = Path(path).parts
        for part in parts:
            if part.startswith("backup") and part != BACKUP_DIR_NAME:
                return True
            if part == "code_pool":
                return True
        return False

    def create(self, label: str = "auto") -> Optional[dict]:
        """创建备份。返回 {path, size_mb, file_count, elapsed_s}。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"TMM_{label}_{ts}.zip"
        dest = self.backup_dir / name

        t0 = time.time()
        file_count = 0
        try:
            with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(self.project_root, onerror=lambda e: None):
                    # 目录级过滤：排除 backup* / __pycache__ / .git / code_pool
                    dirs[:] = [d for d in dirs if not self._dir_should_exclude(d)]
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        if self._should_exclude(fpath):
                            continue
                        arcname = os.path.relpath(fpath, self.project_root)
                        try:
                            zf.write(fpath, arcname)
                            file_count += 1
                        except (OSError, ValueError):
                            pass

            elapsed = time.time() - t0
            size_mb = os.path.getsize(dest) / (1024 * 1024)

            self._rotate()

            logger.info("backup: %s -> %s (%.1fMB, %d files, %.1fs)",
                       label, name, size_mb, file_count, elapsed)
            return {
                "path": str(dest), "name": name,
                "size_mb": round(size_mb, 2), "file_count": file_count,
                "elapsed_s": round(elapsed, 1),
            }
        except Exception as e:
            logger.error("backup failed: %s", e)
            if dest.exists():
                dest.unlink()
            return None

    def _rotate(self):
        zips = sorted(
            self.backup_dir.glob("TMM_*.zip"),
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        for old in zips[KEEP_COUNT:]:
            old.unlink()
            logger.debug("backup: removed old %s", old.name)

    def list_backups(self) -> list[dict]:
        zips = sorted(
            self.backup_dir.glob("TMM_*.zip"),
            key=lambda p: p.stat().st_mtime, reverse=True
        )
        out = []
        for z in zips:
            st = z.stat()
            out.append({
                "name": z.name,
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
        return out

    def auto_tick(self) -> Optional[dict]:
        """每轮调用，超过间隔自动备份。"""
        now = time.time()
        if now - self._last_auto >= AUTO_INTERVAL:
            self._last_auto = now
            return self.create("auto")
        return None

    def cleanup_all(self):
        """删除所有备份（慎用）。"""
        for z in self.backup_dir.glob("TMM_*.zip"):
            z.unlink()
        logger.info("backup: all backups removed")


_backup: Optional[AutoBackup] = None


def get_backup(project_root: Path = None) -> AutoBackup:
    global _backup
    if _backup is None and project_root is not None:
        _backup = AutoBackup(project_root)
    return _backup
