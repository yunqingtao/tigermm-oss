"""文本匹配口径 — 单一定义, 多处共用。

为什么单独成模块 (2026-09-19):
    同一个"关键词重叠"算法原先只活在 AutoGuide._keyword_overlap 里。学习闭环
    (learn_loop) 需要用**同一个口径**判断"这条学到的规则是否命中当前消息", 否则
    两边阈值语义不同 (AutoGuide 0.4/0.3 vs 学习规则 0.5) 会产生"学了但不生效"的
    诡异现象。抽取成模块级函数, 两边共用一份实现。

历史口径 (照抄自 AutoGuide, 含注释里记的教训):
    词级重叠, 不是字符级 —— 字符级太吵: "帮我修复一个bug" vs "帮我写个病毒"
    共享字符 {帮,我,个} = 0.5 > 0.4 阈值 → 误命中。
"""
import re

_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]+")


def tokenize(s: str) -> set:
    """CJK 双字以上片段 + ASCII 词。过滤单字噪声。"""
    return set(_TOKEN.findall((s or "").lower()))


def keyword_overlap(query: str, target: str) -> float:
    """query 里有**多大比例**的 target 词被覆盖 (0.0 ~ 1.0)。"""
    t_words = tokenize(target)
    if not t_words:
        return 0.0
    q_words = tokenize(query)
    common = t_words & q_words
    if len(common) < 1 and len(t_words) >= 2:
        return 0.0
    return len(common) / len(t_words)


def _cjk_bigrams(s: str) -> set:
    """CJK 连续片段的**真双字组** + ASCII 词。

    为什么需要 (实测): 中文没有空格, `[\u4e00-\u9fff]{2,}` 会把
    "北京天气怎么样" 整句当成**一个** token —— 于是 "北京天气怎么样啊" 与
    "北京天气怎么样" 交集为空, 相似度 0, 学到的规则永远不命中。
    正确的做法是切双字组: {北京, 京天, 天气, ...}。
    """
    out = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", (s or "").lower()):
        if len(run) == 1:
            out.add(run)
        else:
            out |= {run[i:i + 2] for i in range(len(run) - 1)}
    out |= set(re.findall(r"[a-zA-Z0-9]+", (s or "").lower()))
    return out


def similarity(query: str, target: str) -> float:
    """CJK 双字组覆盖率 (target 的双字组有多少出现在 query 里)。

    专供**学习闭环**用 (学到的规则要能容忍措辞轻微变化)。
    AutoGuide 沿用老的 keyword_overlap (它的 471 条罐头答案 + 路由矩阵
    都按老口径标定, 换口径会动到既有路由行为 —— 分开是刻意的)。
    """
    t_grams = _cjk_bigrams(target)
    if not t_grams:
        return 0.0
    q_grams = _cjk_bigrams(query)
    if not q_grams:
        return 0.0
    return len(t_grams & q_grams) / len(t_grams)
