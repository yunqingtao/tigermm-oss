# -*- coding: utf-8 -*-
r"""双色球工具 —— 查开奖结果 / 算复式注数与金额。

为什么单独一个工具 (来自 2026-09-25 兜底复盘)
═══════════════════════════════════════════════════════════════
把 121 轮兜底逐条摊开后, 有 8 轮是**双色球**, 而且**两类都是确定性的**, 根本不该掉模型闲聊:

  ① "去看看双色球2026086-2026095开奖结果" × 4 轮
     ⇒ 本地就有 `lottery/双色球历史数据.txt` (期号 + 6 红 + 蓝), 查表即可, **不用联网**。
  ② "双色球7+1复式多少钱" × 4 轮 (用户还写成"复试")
     ⇒ 纯组合数: 注数 = C(红个数,6) × 蓝个数, 每注 2 元。7+1 = 7 注 = 14 元。

★ 诚实边界 (与门禁里的反向用例对应)
   - 本地没有那一期 ⇒ **明说没有**, 并说最新到哪期; **绝不**编号码;
   - 说不出"几+几" ⇒ **反问**, 不许猜一个注数出来;
   - 号码一律按**双色球**说 (红 6 个 + 蓝 1 个), 不跟大乐透混。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 本地开奖数据 (期号 -> 号码)。多写几个候选路径, 哪个在就用哪个。
_CANDIDATES = (
    ROOT / "lottery" / "双色球历史数据.txt",
    ROOT / "data" / "双色球历史数据.txt",
    ROOT / "lottery" / "ssq_history.txt",
)

TOOL = {
    "name": "ssq",
    "description": "双色球: draw=查开奖结果 (本地历史数据, 按期号或期号区间) · "
                   "price=算复式要多少钱 (注数与金额)。"
                   "★ 查不到的期号会**明说没有**, 不编号码。",
    # ★ 不放 keywords: 这里两个动作语义不同 (查 vs 算), 而 plugin.keyword 命中即走、
    #   且**不传 action** ⇒ 只有"默认动作就答对"的说法才配进 keywords。
    #   本工具没有天然默认动作, 一律交给带 action 的 IR 链 (同 tools/memory.py 的教训)。
    "keywords": [],
    "params": [
        {"name": "action", "type": "string", "required": False,
         "enum": ["draw", "price"],
         "description": "draw=查开奖结果 · price=算复式注数/金额"},
        {"name": "text", "type": "string", "required": False,
         "description": "用户原话 (工具自己抠期号 / 抠 N+M); 给了 from_to 或 red/blue 时可省"},
        {"name": "from_to", "type": "string", "required": False,
         "description": "期号或期号区间, 例: 2026095 或 2026086-2026095"},
        {"name": "red", "type": "integer", "required": False, "description": "复式红球个数"},
        {"name": "blue", "type": "integer", "required": False, "description": "复式蓝球个数"},
    ],
    "trigger": ["开奖", "开奖结果", "双色球", "复式多少钱", "复试多少钱"],
    "permission": ["read"],
    "category": "life",
}

PRICE_PER_BET = 2          # 双色球每注 2 元
RED_TOTAL, RED_PICK = 33, 6
BLUE_TOTAL, BLUE_PICK = 16, 1
_MAX_EXPAND = 120          # 一次最多展开多少期 (防"1-999999"把内存撑爆)


# ─────────────────────────── 数据 ───────────────────────────

def _data_file() -> Path | None:
    """数据文件: **环境变量优先** (门禁/探针指到临时文件, 不碰用户那份真数据)。"""
    import os
    env = os.environ.get("TMM_SSQ_DATA")
    if env:
        return Path(env)
    for p in _CANDIDATES:
        if p.is_file():
            return p
    return None


def load_draws() -> dict:
    """读本地开奖数据 (期号字符串 -> {'red': [...], 'blue': int, 'raw': 原行})。

    容错: 坏行**跳过不抛** —— 一行坏了不该让整个查询失败 (但仍会计数上报)。
    """
    p = _data_file()
    if p is None:
        return {}
    out, bad = {}, 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(20\d{5})\s+((?:\d{1,2}[,\s]+){5}\d{1,2})\s*\+\s*(\d{1,2})\s*$", s)
        if not m:
            bad += 1
            continue
        reds = [int(x) for x in re.split(r"[,\s]+", m.group(2).strip()) if x]
        out[m.group(1)] = {"red": reds, "blue": int(m.group(3)), "raw": s}
    if bad:
        out["_bad_lines"] = bad                        # 供上层提示, 不作为期号
    return out


def _latest_issue(draws: dict) -> str:
    ks = [k for k in draws if k.isdigit()]
    return max(ks) if ks else ""


# ─────────────────────────── 期号解析 ───────────────────────────

def parse_issues(text: str) -> list:
    """从原话里抠期号 / 期号区间。

    ★ 实测的原话形态 (2026-09-25): "去看看双色球2026086-2026095开奖结果。"
      ⇒ 要认出 2026086-2026095 是**区间** (短横线连接的两头), 别只取第一个数字。
    """
    t = (text or "").replace("～", "-").replace("~", "-").replace("—", "-").replace("－", "-")
    out = []
    for m in re.finditer(r"(20\d{5})\s*-\s*(20\d{5})", t):        # 区间
        a, b = m.group(1), m.group(2)
        if a > b:
            a, b = b, a
        for i in range(int(a), int(b) + 1):
            out.append(str(i))
            if len(out) > _MAX_EXPAND:
                return out
    if out:
        return out
    return re.findall(r"20\d{5}", t)                             # 单个/多个期号


# ─────────────────────────── 动作 ───────────────────────────

def _do_draw(from_to: str, text: str) -> dict:
    draws = load_draws()
    if not draws:
        return {"success": False,
                "error": "本地没有双色球开奖数据 (找过 %s)。要我去网上查就说一声 —— "
                         "但得联网, 而且我没抓到之前**不会编号码**。"
                         % " / ".join(p.name for p in _CANDIDATES)}
    latest = _latest_issue(draws)
    issues = parse_issues(from_to) or parse_issues(text)
    if not issues:
        return {"success": False,
                "error": "要告诉我查哪期 (例: 双色球2026095开奖结果 · 2026086-2026095)。"
                         "本地数据最新到 %s 期。" % latest}
    issues = sorted(set(issues))
    hit = [i for i in issues if i in draws]
    miss = [i for i in issues if i not in draws]
    ln = ["双色球开奖结果 (%d 期 · 本地数据, 最新到 %s 期)" % (len(hit), latest)]
    for i in hit:
        d = draws[i]
        ln.append("  %s  %s + %02d"
                  % (i, " ".join("%02d" % x for x in d["red"]), d["blue"]))
    if miss:
        ln.append("  ★ 本地没有这几期: %s (最新只有 %s)" % (", ".join(miss[:12]), latest))
        if len(miss) > 12:
            ln.append("     (还有 %d 期没列)" % (len(miss) - 12))
    return {"success": True, "output": "\n".join(ln), "issues": hit,
            "missing": miss, "count": len(hit)}


def parse_red_blue(text: str) -> tuple:
    """从原话抠复式规模。支持 `7+1` / `7+2` / `7 加 1` / `红7蓝1` / `7个红球1个蓝球`。

    返回 (red, blue); 抠不出来给 (None, None) —— 上层**反问**, 不猜。
    """
    t = (text or "").replace("＋", "+").replace("十", "+").strip()
    t = t.replace("个红球", "+").replace("个蓝球", "").replace("个红", "+").replace("个蓝", "")
    t = t.replace("红球", "").replace("蓝球", "").replace("红", "").replace("蓝", "")
    t = t.replace("加", "+").replace("和", "+")
    m = re.search(r"(\d{1,2})\s*\+\s*(\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})\s*\+\s*(\d{1,2})", re.sub(r"[^\d+]+", " ", text or ""))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _do_price(red, blue, text: str) -> dict:
    if red is None or blue is None:
        r2, b2 = parse_red_blue(text)
        red = red if red is not None else r2
        blue = blue if blue is not None else b2
    try:
        red = int(red) if red is not None else None
        blue = int(blue) if blue is not None else None
    except (TypeError, ValueError):
        red = blue = None
    if not red or not blue:
        return {"success": False,
                "error": "要告诉我几+几 (例: 双色球7+1复式多少钱)。"
                         "常见的: 7+1 = 14 元 · 8+1 = 56 元。"}
    if red < RED_PICK or red > RED_TOTAL:
        return {"success": False,
                "error": "红球个数要在 %d-%d 之间 (复式至少选 %d 个才有意思), 你给了 %d。"
                         % (RED_PICK, RED_TOTAL, RED_PICK + 1, red)}
    if blue < BLUE_PICK or blue > BLUE_TOTAL:
        return {"success": False,
                "error": "蓝球个数要在 1-%d 之间, 你给了 %d。" % (BLUE_TOTAL, blue)}
    n_red = math.comb(red, RED_PICK)          # 红球组合数
    bets = n_red * blue                        # × 蓝球个数
    money = bets * PRICE_PER_BET
    ln = ["双色球 %d+%d 复式:" % (red, blue),
          "  红球 %d 个选 %d = C(%d,%d) = %d 注" % (red, RED_PICK, red, RED_PICK, n_red),
          "  蓝球 %d 个 ⇒ %d × %d = **%d 注**" % (blue, n_red, blue, bets),
          "  每注 %d 元 ⇒ 一共 **%d 元**" % (PRICE_PER_BET, money)]
    if bets >= 100:
        ln.append("  ★ %d 注已经不少了 —— 中奖概率上去了, 但花的钱也是线性的。" % bets)
    return {"success": True, "output": "\n".join(ln).replace("**", ""),
            "red": red, "blue": blue, "bets": bets, "money": money}


async def run(action: str = "draw", text: str = "", from_to: str = "",
              red=None, blue=None, **kwargs) -> dict:
    """双色球查/算。见 TOOL['description']。"""
    action = (action or "").strip().lower()
    if not action:
        action = "price" if re.search(r"复式|复试", text or "") else "draw"
    try:
        if action in ("draw", "query", "查", "开奖"):
            return _do_draw(from_to, text)
        if action in ("price", "money", "多少钱", "算"):
            return _do_price(red, blue, text or from_to)
        return {"success": False, "error": "未知 action: %s (可用: draw / price)" % action}
    except Exception as e:                                   # noqa: BLE001
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e)}
