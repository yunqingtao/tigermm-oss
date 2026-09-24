"""
SelfEvolve — 「从用户纠正中学习」的历史入口。

★ 2026-09-19 重写 (审计发现这条链是**死的**):
    旧实现有三个真问题, 全是"看起来在工作其实没工作":
      ① main.py 调用时传 `original_msg=""` → `_extract_intent("")` 返回空 →
         `record_correction` **立即 return None**, 从来没记下过任何东西
         (所以 data/corrections.json 一直不存在)。
      ② `apply_rules_to_autoguide` 找 `"]  # end of rules"` 标记来插规则,
         而 auto_guide.py 里**根本没有这个标记** → replace 什么也没改,
         `added` 却照样 +1 → **谎报"已写入 N 条规则"**。
      ③ 设计是往 **auto_guide.py 源码里写规则** —— 自我修改生产源码, 无门禁、无复核,
         且规则散在代码里, 没法测量/衰减/撤销。

    现在: 一切学习委托给 **learn_loop** (数据存储, 带置信/测量/衰减/人工门),
    **绝不改源码**。本模块保留全部旧函数签名 (旧端兼容), 内部改为适配器。
"""
import json
import time
from pathlib import Path

CORRECTIONS_FILE = "corrections.json"


def _learn_loop(project_root: str = None):
    from core.learn_loop import get_learn_loop
    return get_learn_loop()


def record_correction(project_root: str, original_msg: str, correction_msg: str):
    """记录一次纠正。返回学到的规则 (dict) 或 None。

    旧契约: "纠正同一意图 3 次 → 生成规则"。
    新契约: **立即生效** (低置信), 被再次纠正会自动降级/撤销 —— 见 learn_loop。
    原文为空时返回 None (调用方必须传真实的"被纠正的那句话")。
    """
    if not (original_msg or "").strip() or not (correction_msg or "").strip():
        return None
    ll = _learn_loop(project_root)
    r = ll.record_answer(original_msg.strip(), correction_msg.strip(), source="self_evolve")
    return r.get("rule")


def _load(project_root: str) -> dict:
    """旧数据结构 (corrections.json)。保留读取能力, 供迁移用。"""
    path = Path(project_root) / "data" / CORRECTIONS_FILE
    if not path.exists():
        return {"intents": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return {"intents": {}}


def get_rules(project_root: str) -> list:
    """旧接口: 返回"待生效的规则"。

    现在规则统一存在 learn_loop 里, 所以这里返回:
      · learn_loop 里活跃的 answer 规则 (转成旧格式)
      · 加上 corrections.json 里历史遗留的 _generate_rule 结果 (如果有)
    """
    out = []
    ll = _learn_loop(project_root)
    for r in ll.list_rules(status="active", kind="answer", limit=200):
        out.append({"intent": "answer", "match": r["key"], "action": r["value"],
                    "source": "learn_loop", "created": r.get("updated_at")})
    for intent, v in (_load(project_root).get("intents") or {}).items():
        rule = (v or {}).get("rule")
        if rule:
            out.append(rule)
    return out


def apply_rules_to_autoguide(project_root: str) -> int:
    """**不再往源码写规则** (旧实现就是这里在谎报 + 自改源码)。

    现在: 把 corrections.json 里**历史遗留**的规则迁移进 learn_loop (数据), 返回迁移条数。
    没有可迁移的 → 返回 0 (诚实的 0, 不是"假成功")。
    """
    ll = _learn_loop(project_root)
    migrated = 0
    for intent, v in (_load(project_root).get("intents") or {}).items():
        rule = (v or {}).get("rule") or {}
        match, action = (rule.get("match") or "").strip(), (rule.get("action") or "").strip()
        if match and action:
            res = ll.record_answer(match, action, source="migrated:self_evolve")
            if res.get("action") in ("created", "reinforced", "superseded"):
                migrated += 1
    return migrated


# ── 以下为旧内部函数, 保留以免外部引用报错 (不再被主流程使用) ──

def _extract_intent(msg: str) -> str:
    """[遗留] 从消息里抽意图键。新流程用 learn_loop 的触发问题原文, 不再抽意图。"""
    msg = (msg or "").lower().strip()
    for word in ["帮我", "请", "能不能", "可以", "一下", "吗", "吧", "吧？"]:
        msg = msg.replace(word, "")
    actions = ["查", "发", "搜", "写", "读", "删", "备份", "截图", "翻译", "天气", "邮件"]
    for a in actions:
        if a in msg:
            idx = msg.find(a)
            return msg[idx:idx + 10].strip()
    return msg[:12].strip()


def _generate_rule(intent: str, examples: list) -> dict:
    """[遗留] 由示例生成规则 dict。新流程不再需要 (规则=触发问题→答案)。"""
    from datetime import datetime
    corrections = [(e or {}).get("correction", "") for e in (examples or [])][-3:]
    originals = list({(e or {}).get("original", "")[:40] for e in (examples or [])})
    return {
        "intent": intent,
        "match": originals[0] if originals else intent,
        "action": (corrections[0][:60] if corrections else "handle correctly"),
        "source": "self-evolved(legacy)",
        "created": datetime.now().isoformat(),
    }
