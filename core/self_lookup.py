"""自查与行动判定 —— 缺信息时先自己查, 而不是停在门口 (2026-09-21 立)

两个短板在这里合流
═══════════════════════════════════════════════════════════════════
短板1 (停在门口): 实测 "@数据分析 帮我看看这100个Excel怎么归类"
  → 它没动手也没自查, 直接要路径。诚实(没编数据)是对的, 但它**没试着自己找**。
短板B (只会说不会动): 「分析类」请求最后都汇到 _process_external(交模型),
  模型可以干聊一整段"你可以这样归类…"而不碰任何工具。

共同病根: 缺的是**一步只读自查**, 和一个"这句话要不要动手"的判定。

规矩 (三条, 都要能机器判)
───────────────────────────────────────────────────────────────
  ① 提到"对象"(文件/表格/文档/图/邮件…)但**没给路径** → 先在**白名单目录**里只读扫一遍,
     把候选清单摆出来 (用户挑, 或模型据此动手)。扫不到就说"没找到", 不许编。
  ② 判定"要不要动手": 有动作意图(归类/整理/做成/生成/统计/汇总…) → 明确指示模型**先用工具**,
     而不是干聊方案。
  ③ 自查全在**白名单 + 数量上限 + 只读**内 —— 借"自查"之名翻整盘是不能接受的。

白名单 (只扫这些, 别的一律不碰)
───────────────────────────────────────────────────────────────
  用户目录下的: Desktop / Downloads / Documents / 桌面 / 下载 / 文档
  加调用方显式传入的目录 (例如项目 tmp 沙箱)

自检:  python core/self_lookup.py
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

# ── 对象词: 提到这些 = 用户在说"某个东西", 但没说在哪 ──
OBJECT_WORDS = ("文件", "表格", "文档", "图片", "照片", "截图", "日志", "报告",
                "excel", "xlsx", "xls", "csv", "word", "docx", "ppt", "pptx", "pdf", "txt", "md")

# ── 动作词: 提到这些 = 这句话是要**动手**的 (B 的判据) ──
ACTION_WORDS = ("归类", "整理", "汇总", "统计", "合并", "拆分", "批量", "做成", "生成",
                "转成", "转换", "导出", "清理", "删掉", "移动", "复制", "重命名",
                "写一个", "写份", "画一个", "分析一下", "跑一下", "执行", "装一下")

# ── 明确的"别动手"信号 (问概念/问意见) ──
CHAT_ONLY = ("什么是", "是什么", "怎么理解", "为什么", "区别", "原理", "教我",
             "讲一下", "解释一下", "推荐几本", "推荐一下书", "你觉得")

# ── 扩展名映射: 对象词 → 实际后缀 ──
EXT_MAP = {
    "excel": (".xlsx", ".xls", ".csv"), "xlsx": (".xlsx", ".xls", ".csv"), "xls": (".xls", ".xlsx", ".csv"),
    "csv": (".csv", ".xlsx"), "表格": (".xlsx", ".xls", ".csv"),
    "word": (".docx", ".doc"), "docx": (".docx", ".doc"), "文档": (".docx", ".doc", ".pdf", ".md", ".txt"),
    "ppt": (".pptx", ".ppt"), "pptx": (".pptx", ".ppt"),
    "pdf": (".pdf",), "图片": (".png", ".jpg", ".jpeg", ".webp", ".bmp"),
    "照片": (".png", ".jpg", ".jpeg", ".webp"), "截图": (".png", ".jpg", ".jpeg"),
    "日志": (".log", ".txt"), "报告": (".md", ".docx", ".pdf", ".txt"),
    "txt": (".txt", ".md"), "md": (".md", ".txt"), "文件": None,
}

MAX_SCAN = 400          # 每个目录最多看这么多条 (防大目录卡住)
MAX_CAND = 30           # 最多回这么多候选
MAX_DEPTH = 2           # 只下钻两层


def _home_dirs(extra=None) -> list:
    """→ 只读自查允许扫的目录 (白名单)。

    ★ TMM_SELF_LOOKUP_DIRS (os.pathsep 分隔) 若设了 → **只用它**, 不碰家目录。
      这是给门禁/隔离跑用的收口: 验证时把它指向临时沙箱, 既不扫用户的真实桌面,
      又能把机制完整跑一遍。
    """
    env = (os.environ.get("TMM_SELF_LOOKUP_DIRS") or "").strip()
    if env:
        # ★ 独占: 设了就用它, **连 extra_dirs 也不合并** —— 否则门禁/隔离跑不 hermetic
        #   (实测踩过: 项目 tmp 里的残留候选把"空沙箱"负控搞成假绿/假红)。
        return [Path(x) for x in env.split(os.pathsep) if x.strip() and Path(x).is_dir()]
    out = []
    try:
        h = Path.home()
        for n in ("Desktop", "Downloads", "Documents", "桌面", "下载", "文档"):
            d = h / n
            if d.is_dir():
                out.append(d)
    except Exception:
        pass
    for d in (extra or []):
        p = Path(d)
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def wants_lookup(message: str) -> bool:
    """提到对象词 + 没给路径 → 值得自查。"""
    m = (message or "").lower()
    if not m.strip():
        return False
    if has_path(message):
        return False                     # 给了路径: 那是"读它", 不是"找它"
    return any(w in m for w in OBJECT_WORDS)


def has_path(message: str) -> bool:
    return bool(re.search(r"[A-Za-z]:[\\/]|\\\\|~/|/[\w\u4e00-\u9fff]+/", message or ""))


def wants_action(message: str) -> bool:
    """这句话要不要**动手** (B 的判据)。问概念/求解释 → 不动手。"""
    m = (message or "").lower()
    if not m.strip():
        return False
    if any(w in m for w in CHAT_ONLY):
        return False
    return any(w in m for w in ACTION_WORDS)


def _exts_for(message: str) -> tuple:
    m = (message or "").lower()
    got = []
    for w, exts in EXT_MAP.items():
        if w in m and exts:
            for e in exts:
                if e not in got:
                    got.append(e)
    return tuple(got)


def discover(message: str, extra_dirs=None, cap: int = MAX_CAND) -> list:
    """只读扫白名单目录, 返回匹配的候选 [{path,size,mtime}] (按 mtime 新→旧)。

    ★ 只读: 只做 scandir/stat, 绝不创建/修改任何东西。
    ★ 受控: 只在白名单目录内, 有 MAX_SCAN/MAX_CAND/MAX_DEPTH 上限。
    """
    exts = _exts_for(message)
    if not exts and "文件" not in (message or ""):
        return []
    if "文件" in (message or "") and not exts:
        exts = (".xlsx", ".docx", ".pptx", ".pdf", ".csv", ".txt", ".md", ".png", ".jpg")
    found, scanned = [], 0
    t0 = time.time()
    for root in _home_dirs(extra_dirs):
        stack = [(root, 0)]
        while stack and scanned < MAX_SCAN and time.time() - t0 < 3.0:
            d, depth = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        scanned += 1
                        if scanned >= MAX_SCAN:
                            break
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if depth < MAX_DEPTH and not e.name.startswith("."):
                                    stack.append((Path(e.path), depth + 1))
                                continue
                            if not e.is_file(follow_symlinks=False):
                                continue
                            if exts and not e.name.lower().endswith(exts):
                                continue
                            st = e.stat()
                            found.append({"path": e.path, "size": st.st_size, "mtime": st.st_mtime})
                        except Exception:
                            continue
            except Exception:
                continue
    found.sort(key=lambda x: -x["mtime"])
    return found[:cap]


def format_candidates(message: str, cands: list) -> str:
    """→ 给模型看的"只读自查结果"块 (找不到就明说, 不许编)。"""
    if not cands:
        return ("\n\n【只读自查结果】用户没给路径, 我在允许的目录里扫了一遍 —— "
                "**没找到**匹配的文件。请明确告诉用户「没找到」, 不要编造路径; "
                "可以问他文件在哪个目录。")
    lines = [f"\n\n【只读自查结果】用户没给路径, 我在允许的目录里扫到 {len(cands)} 个候选:​"]
    for c in cands[:MAX_CAND]:
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(c["mtime"]).strftime("%m-%d %H:%M")
        lines.append(f"  · {c['path']}  ({c['size'] // 1024}KB, {when})")
    lines.append("请基于这些**真实路径**回答或动手; 不要编造路径。若都不对, 请让用户指定目录。")
    return "\n".join(lines)


def format_action_directive(message: str) -> str:
    """→ 判定该动手时, 给模型的明确指示 (B)。"""
    if not wants_action(message):
        return ""
    return ("\n\n【框架判定】这句话是要**动手**的 (不是闲聊/问概念): 请**优先调用工具**去执行, "
            "做完再说结果; 不要只给方案或步骤。若确实缺少必要信息, 先用只读工具自查, "
            "仍缺就问用户 —— 但不要凭空描述「已完成」。")


if __name__ == "__main__":   # 自检
    import sys
    P, F = [], []

    def chk(n, c):
        (P if c else F).append(n)
        print(("  PASS " if c else "  FAIL ") + n)

    # 判定: 缺路径的对象 → 值得自查
    chk("'看看这100个Excel怎么归类' → 要自查", wants_lookup("帮我看看这100个Excel怎么归类"))
    chk("'表格在哪' → 要自查", wants_lookup("帮我看看表格"))
    chk("'读 D:/x/a.xlsx' → 不用自查 (有路径)", not wants_lookup("读一下 D:/x/a.xlsx"))
    chk("'什么是闭包' → 不触发", not wants_lookup("什么是闭包"))
    # 判定: 要不要动手
    chk("'把这100个Excel归类' → 要动手", wants_action("帮我看看这100个Excel怎么归类"))
    chk("'什么是闭包' → 不动手", not wants_action("什么是闭包"))
    chk("'解释一下什么是RAG' → 不动手", not wants_action("解释一下什么是RAG"))
    chk("'把表格汇总成报告' → 要动手", wants_action("把表格汇总成报告"))
    # 只读 & 受控: discover 不得创建任何东西
    before = {p.name for p in Path.home().glob("*")} if Path.home().is_dir() else set()
    # ★ 派生路径, 不写死绝对路径 (写死会把本机目录带进公开仓库 —— 洗净门禁抓到过)
    _proj_tmp = Path(__file__).resolve().parent.parent / "tmp"
    cands = discover("帮我看看Excel", extra_dirs=[str(_proj_tmp)], cap=5)
    after = {p.name for p in Path.home().glob("*")} if Path.home().is_dir() else set()
    chk("discover 只读 (home 顶层未变)", before == after)
    chk("cap 生效", len(cands) <= 5)
    chk("候选结构正确", all(set(c) == {"path", "size", "mtime"} for c in cands))
    # 找不到时如实说
    msg = format_candidates("看看Excel", [])
    chk("找不到时明说'没找到'", "没找到" in msg and "不要编造" in msg)
    # 有候选时给真实路径
    if cands:
        chk("有候选时列出真实路径", cands[0]["path"] in format_candidates("看看Excel", cands))
    chk("动作指示只在要动手时出现",
        format_action_directive("什么是闭包") == "" and "优先调用工具" in format_action_directive("把Excel归类"))
    print()
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    sys.exit(1 if F else 0)
