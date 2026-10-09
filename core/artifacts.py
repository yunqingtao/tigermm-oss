"""Artifacts — 产物/交付物登记与盘点 (2026-09-20)

为什么有这个东西
═══════════════════════════════════════════════════════════════════
引擎每天会产出成片/报告/图/表格/音频, 但它们散落在桌面与项目目录里,
"今天产出了什么、在哪、多大" 只能靠人肉翻目录或反复问。

用户习惯 (来自长期反馈):
  · 问进度要**查盘直答**, 不要反问
  · 目录只留成品, 母带/中间件/冗余副本要清
  · 交付物不能混调试痕迹
→ 所以需要一个**登记 + 盘点**层: 谁产出的、什么时候、在哪个路径、多大、属于哪类。

设计原则
───────────────────────────────────────────────────────────────
· **加性**: 不改任何现有工具的行为; 其它工具愿意就调 `add()`, 不调也不影响
· **只读 + 追加**: 登记表是 append-only 的 JSONL; 不删用户文件, 也不移动任何东西
· **不重复**: 同一路径重复登记只保留最新一条 (查询时按 path 去重)
· **可隔离**: 登记表路径可由环境变量 `TMM_ARTIFACTS_FILE` 覆盖 —— 门禁/探针才不会
  污染真实数据 (这是本项目反复踩过的坑, 所以从第一版就留好口子)

产物的"类"(kind): video / audio / image / doc / sheet / slide / code / report / other
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "data" / "artifacts.jsonl"

# 常见产物目录 (盘点用)。均由用户主目录**派生**, 不写死个人路径。
KIND_BY_EXT = {
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".avi": "video", ".webm": "video",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio", ".ogg": "audio",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image", ".bmp": "image",
    ".docx": "doc", ".doc": "doc", ".pdf": "doc", ".txt": "doc", ".md": "doc",
    ".xlsx": "sheet", ".xls": "sheet", ".csv": "sheet",
    ".pptx": "slide", ".ppt": "slide",
    ".py": "code", ".js": "code", ".html": "code", ".json": "code", ".bat": "code",
    ".enc": "other", ".key": "other",
}

# 盘点时**跳过**的目录名 (缓存/仓库内部/系统)
SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".pytest_cache", "venv", ".venv",
                  "backups", "AppData", "$RECYCLE.BIN", "System Volume Information"}


def store_path() -> Path:
    """登记表路径 (可由 TMM_ARTIFACTS_FILE 覆盖, 便于隔离)。"""
    p = os.environ.get("TMM_ARTIFACTS_FILE")
    return Path(p) if p else DEFAULT_FILE


def _ensure(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def kind_of(p) -> str:
    e = Path(p).suffix.lower()
    return KIND_BY_EXT.get(e, "other")


def human_size(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if x < 1024 or u == "GB":
            return f"{x:.1f}{u}" if u != "B" else f"{int(x)}B"
        x /= 1024
    return f"{x:.1f}GB"


def add(path, title: str = "", tags=None, source: str = "manual", kind: str = "") -> dict:
    """登记一个产物。返回登记的那条记录 (不移动/不修改文件本身)。

    文件不存在也允许登记? **不允许** —— 登记一个不存在的路径只会制造假信息。
    (踩过的同类教训: "谎报成功"比报错更贵。)
    """
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"路径不存在: {p}"}
    try:
        st = p.stat()
    except Exception as e:
        return {"ok": False, "error": f"取不到文件信息: {type(e).__name__}: {e}"}
    rec = {
        "path": str(p),
        "name": p.name,
        "kind": kind or kind_of(p),
        "title": title or p.stem,
        "size": st.st_size,
        "size_h": human_size(st.st_size),
        "mtime": int(st.st_mtime),
        "at": int(time.time()),
        "source": source,
        "tags": list(tags or []),
    }
    f = store_path()
    _ensure(f)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "record": rec}


def _read_all() -> list:
    """读全部记录。带上**行序**作为并列时的次序判据。

    为什么需要行序 (2026-09-20 自测抓到): `at` 只到**秒**。同一秒内登记两条同路径记录时
    排序并列, "保留最新"就变成看运气 (实测: 留住了旧的那条 title)。
    JSONL 是 append-only → **后出现的行一定更新**, 所以用 (at, 行序) 排序就是确定的。
    行序只在内存里用 (`_seq`), 不写进文件, 不污染数据格式。
    """
    f = store_path()
    if not f.is_file():
        return []
    out = []
    for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue          # 坏行跳过, 不影响其余 (登记表不该因为一行坏了全废)
        rec["_seq"] = i
        out.append(rec)
    return out


def list_recent(limit: int = 20, kind: str = "", source: str = "", alive_only: bool = True) -> list:
    """最近产物 (按登记时间倒序; 同路径只留最新一条)。

    alive_only=True 时**过滤掉已不在磁盘上的** —— 避免报出"早被清理的产物"(诚实优先)。
    """
    recs = _read_all()
    if kind:
        recs = [r for r in recs if r.get("kind") == kind]
    if source:
        recs = [r for r in recs if r.get("source") == source]
    seen, uniq = set(), []
    # ★ (at, 行序) 双键排序 —— at 只到秒, 同秒登记会并列 (自测抓到 "保留最新" 变看运气)
    for r in sorted(recs, key=lambda x: (x.get("at", 0), x.get("_seq", 0)), reverse=True):
        p = r.get("path")
        if not p or p in seen:
            continue
        seen.add(p)
        uniq.append(r)
    if alive_only:
        uniq = [r for r in uniq if Path(r["path"]).exists()]
    return uniq[:limit] if limit else uniq


def stats() -> dict:
    """按类统计 (只算还在盘上的)。"""
    recs = [r for r in list_recent(limit=0, alive_only=True)]
    by = {}
    total = 0
    for r in recs:
        by[r.get("kind", "other")] = by.get(r.get("kind", "other"), 0) + 1
        total += int(r.get("size") or 0)
    return {"count": len(recs), "by_kind": by, "total_size": total,
            "total_size_h": human_size(total), "store": str(store_path())}


def public_rec(rec: dict) -> dict:
    """去掉内部字段 (`_seq`) 的记录 —— 对外输出要干净, 不能混实现细节。"""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def scan_dirs(dirs=None, days: int = 1, limit: int = 60) -> list:
    """盘点目录里的近期产物 (不登记, 只返回发现)。

    默认扫: 用户桌面 + 项目根的常见产物目录。目录由主目录派生, 不写死个人路径。
    days=1 指最近 1 天内有改动的文件。
    """
    if dirs is None:
        home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(ROOT))
        dirs = [home / "Desktop", ROOT / "reports", ROOT / "tmp"]
    cutoff = time.time() - max(0, days) * 86400
    found = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in SKIP_DIR_NAMES for part in p.parts):
                    continue
                if p.suffix.lower() not in KIND_BY_EXT:
                    continue
                try:
                    st = p.stat()
                except Exception:
                    continue
                if st.st_mtime < cutoff:
                    continue
                found.append({"path": str(p), "name": p.name, "kind": kind_of(p),
                              "size": st.st_size, "size_h": human_size(st.st_size),
                              "mtime": int(st.st_mtime)})
        except Exception:
            continue
    found.sort(key=lambda r: r["mtime"], reverse=True)
    return found[:limit]


def render_list(recs, title: str = "最近产物") -> str:
    """把记录渲染成给人看的几行 (给 CLI / 技能用)。"""
    if not recs:
        return f"{title}: (无)"
    lines = [f"{title} ({len(recs)} 个):"]
    for r in recs:
        t = time.strftime("%m-%d %H:%M", time.localtime(r.get("mtime") or r.get("at") or 0))
        lines.append(f"  {t}  [{r.get('kind','?'):<6}] {r.get('size_h','?'):>8}  {r.get('path')}")
    return "\n".join(lines)
