"""Experts — 角色专家层 (2026-09-20)

为什么有这个东西 (借鉴通用 Agent 平台架构图的 "Experts 面")
═══════════════════════════════════════════════════════════════════
TMM 原来只有两面: 技能(怎么做事) + 工具(用什么做)。缺"以什么**身份/口径**做事"。
同一句话, "数据分析师" 和 "技术写手" 该给的结构/语气/默认工具集完全不同。
本层把角色做成**一份 md 一个专家**, 命中就把人设与输出规范注入系统提示。

与技能/工具的分工 (三层各管一段)
───────────────────────────────────────────────────────────────
    技能 (tmm_skills/)  = 怎么做 (DAG 步骤)
    工具 (tools/)       = 用什么做 (可执行动作)
    专家 (experts/)     = 以什么身份做 (人设 + 输出规范 + 优先技能/工具)

安全与诚实约定 (踩过的教训直接写进设计)
───────────────────────────────────────────────────────────────
① **加性**: 没命中任何专家 → 调用方拿到的系统提示**逐字节不变** (不改变既有行为)
② **不静默顶替**: `@某专家` 若不是已知专家 → **不假装**成别的身份。
   同名精确命中 → 注入人设; 近似但不存在 → 只加一句"可能是笔误 + 可用清单"的提示;
   完全不像 → 什么都不做 (因为 `@` 在正常文本里也常用, 例如邮件/提及, 不能误伤)
③ 角色文件是**纯文本配置**, 不含任何凭据; 专家本身不持权限 —— 权限仍由工具层管
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
EXPERT_DIR = ROOT / "experts"


def _parse(path: Path) -> dict:
    """解析一份专家 md (frontmatter + 正文)。解析失败返回 {} (调用方当不存在处理)。"""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return {}
    if not raw.startswith("---"):
        return {}
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        import yaml
        meta = yaml.safe_load(parts[1]) or {}
    except Exception as e:
        logger.warning("expert %s frontmatter 解析失败: %s", path.name, e)
        return {}
    if not isinstance(meta, dict) or not meta.get("name"):
        return {}
    meta["_file"] = str(path)
    meta["_body"] = parts[2].strip()
    meta.setdefault("aliases", [])
    meta.setdefault("keywords", [])
    meta.setdefault("skills", [])
    meta.setdefault("tools", [])
    if not isinstance(meta["aliases"], list):
        meta["aliases"] = [str(meta["aliases"])]
    return meta


def load_all() -> dict:
    """载入全部专家: {名字: 记录}。目录不存在 → 空 dict (不是错误)。"""
    out = {}
    if not EXPERT_DIR.is_dir():
        return out
    for p in sorted(EXPERT_DIR.glob("*.md")):
        if p.name.startswith("_"):
            continue
        rec = _parse(p)
        if rec:
            out[str(rec["name"])] = rec
    return out


def _names(rec: dict) -> list:
    return [str(rec.get("name", ""))] + [str(a) for a in (rec.get("aliases") or [])]


def _common_prefix(a: str, b: str) -> int:
    """共同前缀长度。"""
    n = 0
    for x, y in zip(str(a or ""), str(b or "")):
        if x != y:
            break
        n += 1
    return n


def _looks_typo_of(handle: str, candidate: str) -> bool:
    """`handle` 像不像 `candidate` 的笔误?

    ★ 判据用**共同前缀 >= 2**, 不用编辑距离 (2026-09-20 自测抓到编辑距离不可靠):
        "@数据控" vs "数据分析" → 共同前缀 "数据" = 2  → 像 (该提示)
        "@老板"   vs "老师"     → 共同前缀 0           → 不像 (该放过)
      而编辑距离会把 老板/老师 判成 1 (共享"老"), 于是"发给 @老板 一封邮件"被误伤。
      共同前缀能同时满足"能认笔误"和"不误伤普通 @提及"。
    """
    if not handle or not candidate:
        return False
    if len(str(handle)) < 2 or len(str(candidate)) < 2:
        return False
    return _common_prefix(handle, candidate) >= 2


def match(message: str) -> tuple:
    """按消息找专家。返回 (专家记录 | None, 说明)。

    说明取值:
      "explicit"       — 显式 @名字 命中
      "keyword"        — 关键词命中 (取最长关键词那个, 避免歧义)
      "unknown:<名>"   — 显式 @了一个**不存在**的专家 (调用方应诚实告知, 不顶替)
      "no-match"       — 没有专家语义 (加性: 调用方什么都不做)
    """
    experts = load_all()
    if not experts:
        return None, "no-match"
    msg = message or ""

    # ① 显式 @名字 (最高优先)
    handles = re.findall(r"@([^\s@,，。；;:：]{1,16})", msg)
    if handles:
        for h in handles:
            for rec in experts.values():
                if h == str(rec["name"]) or h in _names(rec):
                    return rec, "explicit"
        # @了但我们没有 → 看是不是"像"(笔误), 像就诚实提示; 不像就当普通 @ (不误伤)
        # 判据见 _looks_typo_of (共同前缀 >= 2): 认得出 @数据控, 但放过 @老板/@张经理。
        for h in handles:
            near = [str(r["name"]) for r in experts.values()
                    if any(_looks_typo_of(h, n) for n in _names(r))]
            if near:
                return None, f"unknown:{h}"
        return None, "no-match"

    # ② 关键词命中 (无 @ 时)。取**最长**命中关键词, 并列则视为歧义 → 不命中
    best, best_kw, tie = None, "", False
    for rec in experts.values():
        for kw in (rec.get("keywords") or []):
            kw = str(kw)
            if kw and kw in msg:
                if len(kw) > len(best_kw):
                    best, best_kw, tie = rec, kw, False
                elif len(kw) == len(best_kw) and rec is not best:
                    tie = True
    if best and not tie:
        return best, "keyword"
    return None, "no-match"


def system_block(rec: dict) -> str:
    """把专家渲染成注入系统提示的一段 (纯追加)。"""
    name = rec.get("name")
    lines = [f"# 现在的角色: {name}"]
    if rec.get("persona"):
        lines.append(str(rec["persona"]).strip())
    if rec.get("output"):
        lines.append(f"输出规范: {str(rec['output']).strip()}")
    if rec.get("skills"):
        lines.append("优先考虑的技能: " + ", ".join(str(x) for x in rec["skills"]))
    if rec.get("tools"):
        lines.append("常用工具: " + ", ".join(str(x) for x in rec["tools"]))
    if rec.get("_body"):
        lines.append(rec["_body"])
    lines.append("以上角色只改变**表达与组织方式**, 不改变事实与安全规则: "
                 "该调工具还是要调工具, 数字/路径一律来自工具真实返回。")
    return "\n".join(lines)


def hint_block(handle: str) -> str:
    """`@了不存在的专家` 的诚实提示 (给模型的指令: 先说清楚, 别装成那个身份)。"""
    names = sorted(load_str_names())
    return (f"# 注意: 用户提到了「@{handle}」, 但本机**没有**这个专家 (可能是笔误)。\n"
            f"请在回答开头用一句话说明这一点, 不要假装自己是那个身份。\n"
            f"现有专家: {', '.join(names) if names else '(无)'}")


def load_str_names() -> list:
    return [str(r["name"]) for r in load_all().values()]


def render_list() -> str:
    experts = load_all()
    if not experts:
        return "本机还没有专家 (experts/*.md 为空)。"
    out = [f"专家 {len(experts)} 位 (用 @名字 指定, 例如 @{sorted(experts)[0]} 帮我看看):"]
    for name in sorted(experts):
        rec = experts[name]
        al = [a for a in (rec.get("aliases") or [])]
        out.append(f"  {name}" + (f" (别名: {', '.join(al)})" if al else "")
                   + (f" —— {str(rec.get('persona', '')).strip()[:60]}" if rec.get("persona") else ""))
    return "\n".join(out)
