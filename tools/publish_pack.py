# -*- coding: utf-8 -*-
r"""发布包 (publish pack) —— 「发送前一步」的半自动交付。

设计来源: docs/策划_能力提升_v1.md 方向 3 / P2。
    TMM 做到「发送前一步」, 人是最后一下:
        F:\发布包\<平台>\<日期>\
            封面.jpg / 视频.mp4 / 配图1.png …
            发布清单.md      ← 标题 · 正文 · 话题标签 · 建议时间 · 对应文件名
            发布清单.json    ← 同上的机器可读版 (P3 半自动填表直接读它)

★ 红线 (策划第四节第 1 条): **不做全自动发布**。
  国内外平台 ToS 都禁止自动化发布, 代价是账号。本工具只组织文件 + 写清单,
  永远不点发送。清单里也写明这一点, 免得以后有人"顺手"接上自动化。

★ 诚实原则 (09-24 复盘: 回执"已生成"没和磁盘对账):
  每个文件复制完都 `exists()` + 比字节数, 任何一个没落地就如实报缺,
  不许在回执里写"已生成"。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

TOOL = {
    "name": "publish_pack",
    "description": "组装「发布包」—— 把做好的图/视频按平台规范归位, 并生成发布清单"
                   "(标题/正文/话题/建议时间/文件名对照)。用户说\"做一条小红书笔记\""
                   "\"发抖音\"\"弄个发布包\"时用。★ 只到发送前一步, 不自动发布。",
    "keywords": ["发布包", "发布清单", "做一条小红书", "发小红书", "发抖音", "小红书笔记",
                 "抖音视频", "投稿包", "待发布", "publish pack"],
    "params": [
        {"name": "action", "type": "string", "required": True,
         "enum": ["build", "list", "show"],
         "description": "build=组装发布包 · list=列已有包 · show=看某个包的清单"},
        {"name": "platform", "type": "string", "required": False,
         "description": "平台: 小红书 / 抖音 / 视频号 / 快手 / B站 / YouTube / TikTok / 推特"},
        {"name": "title", "type": "string", "required": False,
         "description": "标题 (build)"},
        {"name": "body", "type": "string", "required": False,
         "description": "正文/文案 (build)"},
        {"name": "tags", "type": "string", "required": False,
         "description": "话题标签, 逗号分隔, 可带可不带 # (build)"},
        {"name": "media", "type": "string", "required": False,
         "description": "素材文件路径, 逗号分隔 (图或视频) (build)"},
        {"name": "cover", "type": "string", "required": False,
         "description": "封面文件路径 (build, 可选; 不给则用第一张图)"},
        {"name": "publish_at", "type": "string", "required": False,
         "description": "建议发布时间, 如 '19:30' 或 '2026-09-25 19:30' (build)"},
        {"name": "pack_root", "type": "string", "required": False,
         "description": "输出根目录, 默认 F:\\发布包"},
        {"name": "date", "type": "string", "required": False,
         "description": "日期目录名 YYYY-MM-DD, 默认今天"},
    ],
}

#: 兼容只读 PLUGIN 的旧代码路径 (tool_schema.py 两处都认)
PLUGIN = {
    "name": "publish_pack",
    "description": "发布包组装 (半自动交付, 不自动发布)",
    "version": "1.0",
    "requires": [],
    "trigger": TOOL["keywords"],
    "permission": ["file_read", "file_write"],
    "category": "content",
}

DEFAULT_ROOT = r"F:\发布包"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

#: 平台规范 (尺寸/标题上限/标签前缀/建议时间/首选形态)
PLATFORMS = {
    "小红书": {"size": "封面 3:4 (1080x1440), 视频 9:16", "title_max": 20,
               "tag": "#", "time": "12:00-13:00 或 19:00-22:00",
               "form": "图文优先; 视频也能发", "note": "标题别写太长, 前 20 字是全部"},
    "抖音":   {"size": "9:16 (1080x1920)", "title_max": 55,
               "tag": "#", "time": "12:00 或 18:00-21:00",
               "form": "视频优先; 图文(图文模式)次之",
               "note": "前 3 秒决定完播, 封面单独做"},
    "视频号": {"size": "9:16 (1080x1920)", "title_max": 22,
               "tag": "#", "time": "20:00-22:00",
               "form": "视频优先", "note": "标题偏短, 靠描述区带话"},
    "快手":   {"size": "9:16 (1080x1920)", "title_max": 30,
               "tag": "#", "time": "18:00-21:00", "form": "视频优先", "note": ""},
    "B站":    {"size": "16:9 (1920x1080)", "title_max": 80,
               "tag": "#", "time": "18:00-20:00", "form": "视频优先",
               "note": "横屏; 标题可长, 用「关键词｜钩子」结构"},
    "YouTube": {"size": "16:9 (1920x1080)", "title_max": 100,
                "tag": "#", "time": "按目标时区 15:00-18:00", "form": "视频优先",
                "note": "标题前 60 字可见; 描述区前 2 行最关键"},
    "TikTok": {"size": "9:16 (1080x1920)", "title_max": 100,
               "tag": "#", "time": "按目标时区 18:00-22:00", "form": "视频优先", "note": ""},
    "推特":   {"size": "16:9 或 1:1", "title_max": 0,
               "tag": "#", "time": "按目标时区 08:00-10:00", "form": "图文/视频皆可",
               "note": "没有独立标题字段 —— 首句就是标题"},
    # ── P4 (2026-09-25): 微信两个交互点 ────────────────────────────────────
    #   用户拍板"交互对接先做微信"(你日常在哪就在哪)。微信**没有**官方发布接口,
    #   所以走 P3 的桌面自动化通道: 把内容填进界面, **停在发送前**, 由人点最后一下。
    #   `channel` 字段就是这条通道的说明, 由 _manifest_md 渲染进发布清单。
    "微信公众号": {"size": "封面 2.35:1 (900x383); 正文图宽 1080", "title_max": 64,
                   "tag": "#", "time": "07:00-09:00 或 20:00-22:00",
                   "form": "图文(可多图) / 视频",
                   "note": "标题 ≤64 字; 摘要 ≤120 字; 首图自动当封面",
                   "channel": "无官方接口 ⇒ 桌面自动化填进编辑器后**停手**, 由人点【群发】"},
    "微信朋友圈": {"size": "9:16 (1080x1920) 或 1:1", "title_max": 0,
                   "tag": "#", "time": "11:30-13:00 或 20:00-22:00",
                   "form": "图文 / 视频",
                   "note": "没有独立标题字段 —— 首句就是标题; 纯文字 ≤1500 字",
                   "channel": "无官方接口 ⇒ 桌面自动化填好后**停手**, 由人点【发表】"},
}

_BAD_DIR = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str, fallback: str = "未命名") -> str:
    s = _BAD_DIR.sub("_", (name or "").strip()).strip(" .")
    return s or fallback


def _split(val) -> list:
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        return [str(v).strip() for v in val if str(v).strip()]
    return [p.strip() for p in re.split(r"[,，;；\n]", str(val)) if p.strip()]


def _tag_list(tags) -> list:
    out = []
    for t in _split(tags):
        t = t.strip()
        if not t:
            continue
        if not t.startswith("#"):
            t = "#" + t
        out.append(t)
    return out


def _manifest_md(platform: str, date: str, title: str, body: str, tags: list,
                 media_map: list, publish_at: str, pack: Path) -> str:
    rule = PLATFORMS.get(platform, {})
    lines = [
        "# %s 发布包 · %s" % (platform, date),
        "",
        "> 本包只到**发送前一步** —— 最后一下请人工点发送。",
        "> (国内外平台的 ToS 都禁止自动化发布, 代价是账号; 这条是项目红线。)",
        "",
        "## 标题",
        "",
        (title or "(未填)"),
        "",
    ]
    if rule.get("title_max"):
        lines += ["> 该平台标题建议 ≤ %d 字, 当前 %d 字" % (rule["title_max"], len(title or "")), ""]
    lines += ["## 正文 / 文案", "", (body or "(未填)"), ""]
    lines += ["## 话题标签", ""]
    lines += [" ".join(tags) if tags else "(未填)", ""]
    lines += [
        "## 文件对照",
        "",
        "| 用途 | 文件名 | 大小 |",
        "|---|---|---|",
    ]
    for role, name, size in media_map:
        lines.append("| %s | %s | %s |" % (role, name, _human(size)))
    lines += [
        "",
        "## 发布参数",
        "",
        "- 建议发布时间: %s" % (publish_at or rule.get("time") or "(未指定)"),
        "- 画面尺寸: %s" % (rule.get("size") or "(按平台默认)"),
        "- 首选形态: %s" % (rule.get("form") or "(不限)"),
    ]
    if rule.get("note"):
        lines.append("- 平台提醒: %s" % rule["note"])
    if rule.get("channel"):
        # P4 (2026-09-25): 微信这类**没有发布接口**的平台, 把"怎么送出去"写清楚,
        # 明说停手位置 —— 免得下次有人以为清单出来就等于发出去了。
        lines.append("- 发送通道: %s" % rule["channel"])
    lines += ["", "## 路径", "", "`%s`" % str(pack), ""]
    return "\n".join(lines)


def _human(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "?"


def _unique(dst: Path) -> Path:
    """目标已存在就不覆盖 —— 加 _2 / _3 后缀 (发布包是素材, 误覆盖不可挽回)。"""
    if not dst.exists():
        return dst
    stem, suf, i = dst.stem, dst.suffix, 2
    while True:
        cand = dst.with_name("%s_%d%s" % (stem, i, suf))
        if not cand.exists():
            return cand
        i += 1


def _do_build(kw: dict) -> dict:
    platform = _sanitize(kw.get("platform") or "", "")
    if not platform:
        return {"success": False, "output": "",
                "error": "没给平台。可用: %s" % " / ".join(PLATFORMS)}
    date = (kw.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"success": False, "output": "",
                "error": "日期格式要 YYYY-MM-DD, 收到 %r" % date}

    root = Path(kw.get("pack_root") or DEFAULT_ROOT).expanduser()
    pack = root / platform / date

    media = _split(kw.get("media"))
    cover = str(kw.get("cover") or "").strip()

    # ★ 先验素材, **再**建目录 —— 反过来的话, 素材不存在也会留下一个空的
    #   `F:\发布包\<平台>\<日期>\`, 用户看到目录以为成了, 进去是空的 (半个包)。
    missing = [p for p in media + ([cover] if cover else []) if not Path(p).is_file()]
    if missing:
        return {"success": False, "output": "",
                "error": "这些素材文件不存在, 发布包没组装(也没建目录): %s" % " / ".join(missing)}
    if not media and not cover:
        return {"success": False, "output": "",
                "error": "没给素材。用 media=/cover= 指图或视频的路径"}

    try:
        pack.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"success": False, "output": "",
                "error": "建不了发布包目录 %s: %s: %s" % (pack, type(e).__name__, e)}

    # 角色分配: cover → 封面; 视频 → 视频; 其余图 → 配图N
    plan: list[tuple[str, Path]] = []
    if cover:
        c = Path(cover)
        plan.append(("封面", c))
    vid_idx = 0
    img_idx = 0
    for p in (Path(x) for x in media):
        ext = p.suffix.lower()
        if ext in VIDEO_EXT:
            vid_idx += 1
            role = "视频" if vid_idx == 1 else "视频%d" % vid_idx
            plan.append((role, p))
        elif ext in IMAGE_EXT:
            if not cover and img_idx == 0:
                img_idx += 1
                plan.append(("封面", p))
            else:
                img_idx += 1
                plan.append(("配图%d" % img_idx, p))
        else:
            plan.append(("其它", p))

    written, media_map, failed = [], [], []
    for role, src in plan:
        dst = _unique(pack / ("%s%s" % (_sanitize(role), src.suffix.lower())))
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            failed.append("%s -> %s: %s" % (src, dst.name, e))
            continue
        # ★ 落盘对账 (09-24 复盘的要求): 复制完必须真的在, 且字节对得上
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            written.append(dst)
            media_map.append((role, dst.name, dst.stat().st_size))
        else:
            failed.append("%s 复制后对不上账 (源 %s 字节 / 目标 %s)"
                          % (dst.name, src.stat().st_size,
                             dst.stat().st_size if dst.is_file() else "不存在"))

    title = str(kw.get("title") or "").strip()
    body = str(kw.get("body") or "").strip()
    tags = _tag_list(kw.get("tags"))
    publish_at = str(kw.get("publish_at") or "").strip()

    md_path = pack / "发布清单.md"
    json_path = pack / "发布清单.json"
    md_path.write_text(_manifest_md(platform, date, title, body, tags, media_map,
                                    publish_at, pack), encoding="utf-8")
    json_path.write_text(json.dumps({
        "platform": platform, "date": date, "pack_dir": str(pack),
        "title": title, "body": body, "tags": tags,
        "publish_at": publish_at or (PLATFORMS.get(platform, {}) or {}).get("time", ""),
        "platform_rules": PLATFORMS.get(platform, {}),
        "files": [{"role": r, "name": n, "bytes": b} for r, n, b in media_map],
        "auto_publish": False,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 清单本身也要对账
    for f in (md_path, json_path):
        if not f.is_file():
            failed.append("清单没落盘: %s" % f)

    summary = ["发布包已就位: %s" % pack,
               "  素材 %d 个: %s" % (len(written), " / ".join(n for _r, n, _b in media_map)),
               "  清单: 发布清单.md + 发布清单.json"]
    if failed:
        summary.append("  ⚠ %d 项没成功: %s" % (len(failed), "; ".join(failed)))
    summary.append("  ★ 只到发送前一步 —— 请人工点发送 (不自动发布)")
    return {"success": not failed, "output": "\n".join(summary),
            "pack_dir": str(pack),
            "files": [str(p) for p in written],
            "manifest_md": str(md_path), "manifest_json": str(json_path),
            "failed": failed,
            "error": "" if not failed else "; ".join(failed)}


def _do_list(kw: dict) -> dict:
    root = Path(kw.get("pack_root") or DEFAULT_ROOT).expanduser()
    if not root.is_dir():
        return {"success": True, "output": "还没有发布包 (%s 不存在)" % root, "packs": []}
    packs = []
    for plat in sorted(p for p in root.iterdir() if p.is_dir()):
        for day in sorted((d for d in plat.iterdir() if d.is_dir()), reverse=True):
            packs.append({"platform": plat.name, "date": day.name, "dir": str(day)})
    if not packs:
        return {"success": True, "output": "还没有发布包 (%s 是空的)" % root, "packs": []}
    lines = ["已有发布包 %d 个 (新→旧):" % len(packs)]
    for p in packs[:30]:
        lines.append("  %s / %s   %s" % (p["platform"], p["date"], p["dir"]))
    return {"success": True, "output": "\n".join(lines), "packs": packs}


def _do_show(kw: dict) -> dict:
    platform = _sanitize(kw.get("platform") or "", "")
    date = (kw.get("date") or "").strip()
    root = Path(kw.get("pack_root") or DEFAULT_ROOT).expanduser()
    if not (platform and date):
        return {"success": False, "output": "",
                "error": "看某个包要同时给 platform 和 date (如 platform=小红书 date=2026-09-25)"}
    md = root / platform / date / "发布清单.md"
    if not md.is_file():
        return {"success": False, "output": "", "error": "没有这个包: %s" % md}
    return {"success": True, "output": md.read_text(encoding="utf-8"), "manifest_md": str(md)}


async def run(action: str = "build", platform: str = "", title: str = "", body: str = "",
              tags: str = "", media: str = "", cover: str = "", publish_at: str = "",
              pack_root: str = "", date: str = "", **kwargs) -> dict:
    """发布包。action: build(组装) / list(列已有) / show(看清单)。"""
    kw = {"platform": platform, "title": title, "body": body, "tags": tags,
          "media": media, "cover": cover, "publish_at": publish_at,
          "pack_root": pack_root, "date": date}
    act = (action or "build").strip().lower()
    if act in ("build", "make", "新建", "组装"):
        return _do_build(kw)
    if act in ("list", "ls", "列表"):
        return _do_list(kw)
    if act in ("show", "read", "cat", "查看"):
        return _do_show(kw)
    return {"success": False, "output": "",
            "error": "Unknown action: %s; 可用: build / list / show" % (action or "(空)")}
