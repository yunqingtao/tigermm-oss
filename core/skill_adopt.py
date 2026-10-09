"""SkillAdopt — 把外面的技能**取形式、自己重编**成我们的技能 (第 4 步)。

═══════════════════════════════════════════════════════════════════════
铁律 (用户定 + 项目一贯风格)
═══════════════════════════════════════════════════════════════════════
    **外部素材只取形式, 按自有风格重制**
→ 所以本模块**绝不直接搬别人的技能文件**。它只从外部素材里提取两样东西:

    ① capabilities (能力形式): "这个技能能做什么" —— 一段抽象描述
    ② steps_hint  (步骤形式): "它是怎么一步步做的" —— **抽象动作**列表
                              (允许的动词只有 map / search / fetch / download / generate /
                               write / parse / convert / list / llm / knowledge ...)
      ★ 外部原文一律**不进**我们的编译提示词 —— 免得把别人的工具名/URL/实现细节
        抄进来 (那既不安全也不是"我们自己的")。只喂抽象形式。

产物 = 技能工厂的候选 (skill_candidates/<name>/), 再走既有四道门:
    编译 → 质检 → 自测(链路可达) → **实跑门** → 人工确认 (/skill approve)
→ 也就是说: "做成自己的" = 我们工厂编出来的 + 过了我们四道门的 + 你点头的。

═══════════════════════════════════════════════════════════════════════
安全闸 (先测安全再操作; 与 depot 同源)
═══════════════════════════════════════════════════════════════════════
    ① 不下载、不执行外部技能文件 (只读文本/元数据)
    ② 许可证: copyleft / 无许可证 → **不采用** (只当灵感看一眼也不入库)
    ③ 抽象化: 外部文本里的工具名/路径/URL 会被剥掉, 只留动作形式
    ④ 产出走我们自己的质检线 —— 与其信任外来的, 不如让我们的门来判断
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("core.skill_adopt")

# ═══════════════════════════════════════════════════════════════════
# 抽象动作词表 —— 只保留"形式", 其余(工具名/URL/路径)全剥掉
# ═══════════════════════════════════════════════════════════════════
ACTION_WORDS = {
    "map":       ("map", "mapping", "映射", "整理", "归类", "聚合"),
    "search":    ("search", "query", "检索", "搜索", "查找", "查询"),
    "fetch":     ("fetch", "request", "http", "抓取", "请求", "获取", "拉取"),
    "download":  ("download", "下载", "保存到本地"),
    "generate":  ("generate", "compose", "创作", "生成", "编写", "撰写"),
    "write":     ("write", "save", "export", "输出", "写入", "导出", "保存"),
    "parse":     ("parse", "extract", "解析", "提取", "抽取"),
    "convert":   ("convert", "transform", "转换", "转成", "转成"),
    "list":      ("list", "enumerate", "列出", "列举", "扫描"),
    "summarize": ("summarize", "summary", "摘要", "总结", "概括"),
    "translate": ("translate", "翻译",),
    "llm":       ("llm", "model", "prompt", "模型", "提示词", "让模型"),
    "record":    ("record", "录制", "录屏", "截图", "capture"),
    "process":   ("process", "处理", "post-process", "postprocess", "加工"),
    "knowledge": ("knowledge", "entity", "知识库", "实体表", "查实体"),
}

# 抽象化时**必须剥掉**的东西: 具体工具名 / URL / 路径 / 代码块 / 具体包名
_STRIP = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[A-Za-z]:[\\/][^\s，,。；;、'\"]*"), "<path>"),
    (re.compile(r"(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]*"), "<path>"),
    (re.compile(r"```[\s\S]*?```"), "<code>"),                    # 代码块整段去掉
    (re.compile(r"`[^`\n]{1,80}`"), "<code>"),                    # 行内代码
    (re.compile(r"\b(?:pip|npm|apt|winget|choco)\s+install\s+\S+"), "<install>"),
    (re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.(py|js|ts|sh|json|ya?ml|md|txt)\b"), "<file>"),
]


@dataclass
class AdoptPlan:
    """把外部素材抽象成的"形式" —— 喂给我们自己的工厂。"""
    name_hint: str = ""
    capability: str = ""                  # 一句话能力描述 (抽象后)
    steps_hint: list = field(default_factory=list)   # 抽象动作序列
    license: str = ""
    license_class: str = ""
    source: str = ""                      # 来源标识 (只记出处, 不存内容)
    blocked: bool = False                 # 许可证不合格 → 不采用
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _strip_externals(text: str) -> str:
    """剥掉外部实现细节 (URL/路径/代码/安装命令/文件名) —— 只留形式。"""
    s = str(text or "")
    for pat, rep in _STRIP:
        s = pat.sub(rep, s)
    return s.strip()


def abstract_actions(text: str, limit: int = 8) -> tuple[list[str], str]:
    """从外部技能文本里抽出**抽象动作序列** (纯函数)。

    返回 (actions, capability_hint):
      actions         —— 动作词 (去重保序), 如 ['search', 'generate', 'write']
      capability_hint —— 抽取到的能力线索 (抽象后的第一段有效描述)
    ★ 只认 ACTION_WORDS 里的动作 —— 外部的工具名/实现细节不会被带进来。
    """
    s = _strip_externals(text).lower()
    found, seen = [], set()
    for word, keys in ACTION_WORDS.items():
        if any(k in s for k in keys):
            if word not in seen:
                seen.add(word)
                found.append(word)
    # 步骤顺序: 按动作词**在原文里首次出现的位置**排 (保留它的编排思路 = "形式")
    def first_pos(w):
        return min((s.find(k) for k in ACTION_WORDS[w] if k in s), default=10 ** 9)
    found.sort(key=first_pos)
    # 能力线索: 抽第一段**像人话**的行。
    # ★ 实测踩到: README 第一行常是徽章 `[![xxx](<url>)` 或图片/链接, 直接取会得出
    #   "[![<file>](<url>" 这种垃圾。所以显式跳过: 图片/徽章/纯链接/表格线/表头行。
    # 两趟找人话:
    #   第一趟 找**像句子**的 (说明"能做什么": 够长 / 带标点 / 带动词线索)
    #   第二趟 退而求其次 (任何干净的一行)
    #   ★ 为什么要两趟: 实测 README 结构常是 `# 项目名` + 徽章 + 一句话说明;
    #     只取"第一行干净文本"会拿到标题 ("Awesome Tools") —— 那是"叫什么", 不是"能做什么"。
    def _ok_lines():
        for raw_line in _strip_externals(text).split("\n"):
            t = raw_line.strip(" #*-—·\t|")
            if not (8 <= len(t) <= 120):
                continue
            if t.startswith(("!", "[![", "<url>", "<path>", "<code>", "<file>", "<install>",
                             "|", ">", "==")):
                continue
            if t.startswith("[") and "](" in t:        # 纯链接行
                continue
            if re.fullmatch(r"[\W_]+", t):            # 纯符号
                continue
            if t.count("<") >= 2:                      # 剥完还剩一堆占位符 = 不是人话
                continue
            yield t

    cap = ""
    for t in _ok_lines():                              # 第一趟: 像句子的
        if len(t) >= 25 or re.search(r"[.。！？:：]\s*$", t) or re.search(
                r"\b(is|are|can|helps?|tool|toolkit|utility|wrapper|用于|用来|可以|帮助)\b", t, re.I):
            cap = t
            break
    if not cap:
        cap = next(_ok_lines(), "")                    # 第二趟: 兜底
    return found[:limit], cap


def parse_skill_md(text: str) -> dict:
    """素材是**真 SKILL.md** 时, 直接解析它的 frontmatter —— 形式最准的一档。

    返回 {is_skill_md, description, steps, tools, tags}
      steps  = 每步的**抽象动作** (由该步的工具名映射而来; 映射不到就用 'llm')
      tools  = 外部工具名 (只用于判断"它需要几件事", **不会**进我们的编译提示词)
    ★ 这是"取形式"的最佳来源: 它本来就是一份声明式技能描述, 比从 README 抽词准得多。
    """
    out = {"is_skill_md": False, "description": "", "steps": [], "tools": [], "tags": []}
    s = str(text or "")
    m = re.search(r"^\s*---\s*\n([\s\S]*?)\n\s*---", s)
    if not m:
        return out
    fm = m.group(1)
    out["is_skill_md"] = True
    # description
    # ★★ 2026-09-25 修 (门禁 verify_skill_adopt 抓到的真 bug):
    #   YAML 里 description 常用**块标量**写法:
    #       description: >-
    #         第一行
    #         第二行
    #   旧代码的 `(.+)$` 只拿到 `>-` 这个**符号本身**, 于是"能力形式"变成 ">-"
    #   (实测: 抓 daymade/claude-code-skills 时, 能力形式 = ">-")。
    #   现在遇到块标量指示符 → 把它下面**再缩进的行**拼起来。
    d = re.search(r"^description:\s*(.+)$", fm, re.M)
    if d:
        _val = d.group(1).strip()
        if re.fullmatch(r"[>|][-+]?\d*|[-+]?\d*[>|]", _val):
            # 块标量: 取后续缩进块 (到下一行顶格 key 为止)
            _rest = fm[d.end():].split("\n")
            _blk = []
            for _ln in _rest:
                if not _ln.strip():
                    _blk.append("")
                    continue
                if _ln[:1] not in (" ", "\t"):
                    break                      # 顶格 = 下一个 key, 块结束
                _blk.append(_ln.strip())
            _joined = " ".join(x for x in _blk if x).strip()
            if _joined:
                _val = _joined
        desc = _strip_externals(_val.strip().strip('"').strip("'"))
        # ★ 外部技能描述常是一大段多句 (含 Use when… 触发说明) —— 取**第一句**才是"能力",
        #   后面的 "Use this skill whenever…" 是给人看的触发说明, 不是能力本体。
        first = re.split(r"(?<=[.。!！?？])\s+|\s+Use (?:this|when)\b", desc, maxsplit=1)[0]
        out["description"] = (first or desc).strip()[:200]
    # tags
    tg = re.search(r"^tags:\s*\[(.*?)\]", fm, re.M)
    if tg:
        out["tags"] = [t.strip().strip('"').strip("'") for t in tg.group(1).split(",") if t.strip()][:12]
    # steps: 找每个 "- id:" 块里的 "tool:"
    tools = re.findall(r"^\s*tool:\s*([\w.\-]+)", fm, re.M)
    if not tools:
        tools = re.findall(r"^\s*requires_tools:\s*\[([^\]]*)\]", fm, re.M)
        tools = [t.strip() for t in (tools[0].split(",") if tools else []) if t.strip()]
    # 外部工具名 → 抽象动作 (映射不到就记 llm: 它至少是"让模型做点什么")
    acts = []
    for t in tools:
        tl = t.lower()
        hit = None
        for word, keys in ACTION_WORDS.items():
            if any(k in tl for k in keys):
                hit = word
                break
        acts.append(hit or "llm")
    # ★ frontmatter 里没有 steps/tool 时 (Claude 风格技能: 只有 name+description,
    #   步骤写在正文), 从**正文**抽动作 —— 那才是它"怎么做事"的形式。
    if not acts:
        body = s[m.end():][:2500]
        body_acts, _ = abstract_actions(body)
        acts = body_acts
    out["tools"] = tools[:12]
    out["steps"] = acts[:12]
    return out


def describe_form(capability: str, steps: list, triggers_enabled: bool = True) -> str:
    """把"形式"渲染成**给技能工厂的需求描述** (纯函数 → 可测)。

    ★ 关键: 描述里只出现抽象动作和抽象能力, 不出现外部工具名/URL/路径。
      工厂拿到它, 会从**我们自己的工具目录**里挑工具来实现 —— 这才是"做成自己的"。
    """
    parts = []
    if capability:
        parts.append(f"能力: {capability}")
    if steps:
        parts.append("参考步骤形式(抽象动作, 具体用什么工具由你从可用工具表里选): "
                     + " → ".join(steps))
    parts.append("请把它实现成我们自己风格的 DAG 技能: 触发词要具体、参数尽量非必填、"
                 "路径用 $params.path 或绝对路径。")
    return "；".join(parts)


def assess_external(meta: dict) -> AdoptPlan:
    """安全闸: 外部技能/仓库的元数据 → 采用计划 (纯函数)。

    meta: {name, license, text, source, stars, archived, pushed_at}
    许可证不合格 (copyleft/无) → blocked=True, **不采用**。
    """
    from core.depot import license_class, typosquat_suspects
    name = str(meta.get("name") or meta.get("repo") or "").strip()
    lic = str(meta.get("license") or "").strip()
    lc = license_class(lic)
    p = AdoptPlan(name_hint=name, license=lic, license_class=lc,
                  source=str(meta.get("source") or ""))
    if meta.get("license_error") and not lic:
        # ★ 读不到 → 说"读不到", 不说"没有" (实测: GitHub 限流会让每个仓库都变成"无许可证")
        p.blocked = True
        p.reasons.append(f"许可证**读不到** ({str(meta['license_error'])[:60]}) —— "
                         f"无法判定, 不采用; 等能读到再评")
    elif lc == "bad":
        p.blocked = True
        p.reasons.append(f"许可证不明/没有 ({lic or '空'}) —— 不采用 (法律上不能用)")
    elif lc == "careful":
        p.blocked = True
        p.reasons.append(f"许可证 {lic} 属 copyleft —— 不把它的实现带进来 (只取形式也不做)")
    else:
        p.reasons.append(f"许可证 {lic} (宽松) ✓")
    if meta.get("archived"):
        p.warnings.append("仓库已 archived (可能停维护) —— 形式可参考, 但别指望它更新")
    if name and typosquat_suspects(name):
        p.warnings.append(f"名称与知名项目近似 ({typosquat_suspects(name)}) —— 重点核对来源")
    # ★ 形式只从**概述段**取, 不从整份 README 取 (实测: 拿 20k 全文抽动作, 后面
    #   安装/贡献/列表章节全在贡献动作词 → 得到 search→parse→write→knowledge→fetch→…
    #   这种一锅炖, 没法当"步骤形式"用)。概述段才承载"这个技能怎么做事"。
    raw_txt = str(meta.get("text") or "")
    # ★ 优先: 真 SKILL.md 直接解析 (形式最准) → 退化: 概述段抽词
    parsed = parse_skill_md(raw_txt)
    if parsed["is_skill_md"]:
        acts, cap = parsed["steps"], parsed["description"]
    else:
        txt = raw_txt[:3000] if raw_txt else ""
        acts, cap = abstract_actions(txt)
    p.steps_hint = acts
    # 能力描述的优先级 (实测定的):
    #   ① 真 SKILL.md 自己的 description —— 技能本体的话, 最准
    #   ② 仓库简介 (API description) —— 比 README 首行可靠
    #   ③ README 概述段抽到的线索
    summary = str(meta.get("summary") or "").strip()
    if parsed["is_skill_md"] and cap:
        p.capability = cap
        if summary and summary[:20] not in cap:
            p.warnings.append(f"(仓库简介: {summary[:60]})")
    else:
        p.capability = summary or cap or ""
        if summary and cap and summary[:20] not in cap:
            p.warnings.append(f"(README 抽到的线索: {cap[:60]})")
    # 动作词也可能只写在简介里 (README 是空壳时) → 补一次
    if not acts and summary:
        acts2, _ = abstract_actions(summary)
        p.steps_hint = acts2
    # ★ 素材来源说明: 有真 SKILL.md 时形式最准 (那本来就是技能本体)
    pts = meta.get("paths_found") or []
    if parsed["is_skill_md"]:
        p.warnings.append(f"素材是真 SKILL.md —— 直接解析 frontmatter (最准的一档); "
                          f"它需要的动作: {' → '.join(parsed['steps']) or '(无)'}")
    elif any(str(x).lower().endswith("skill.md") for x in pts):
        p.warnings.append("素材含 SKILL.md 但没解析出 frontmatter —— 可能格式不标准")
    elif pts:
        p.warnings.append(f"素材只有 {', '.join(map(str, pts))} (README 级) —— "
                          f"形式按概述段抽, 精度有限, 重编后请过目")
    if not acts:
        p.warnings.append("没从素材里识别出可抽象的动作 —— 可能内容太薄或格式不认")
    return p


def render_plan(p: AdoptPlan) -> str:
    """采用计划 → 给人看的文本。"""
    head = "✗ 不采用" if p.blocked else "✓ 可重编"
    lines = [f"{head}  {p.name_hint or '(未命名)'}  —— 只取形式, 自己重编"]
    for r in p.reasons:
        lines.append(f"  · {r}")
    if p.capability:
        lines.append(f"  能力形式: {p.capability[:100]}")
    if p.steps_hint:
        lines.append(f"  步骤形式: {' → '.join(p.steps_hint)}")
    for w in p.warnings:
        lines.append(f"  ⚠ {w}")
    if not p.blocked:
        lines.append("  → 下一步: /depot skill-adopt <来源> 让我们工厂按这个形式编一版"
                     " (产物是候选, 过质检+实跑门 + 你确认才生效)")
    return "\n".join(lines)
