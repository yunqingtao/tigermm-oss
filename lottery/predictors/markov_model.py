# -*- coding: utf-8 -*-
"""4.3 马尔可夫链模型 markov_model.py
P(下期出X | 本期状态). 双色球无记忆性, 本模型仅作参考.
转移矩阵: 本期号码 -> 下期各号码出现频率.
"""
from collections import Counter


def build_transition(hist_reds):
    """返回 {num: Counter(下一期号码)}"""
    trans = {num: Counter() for num in range(1, 34)}
    for i in range(len(hist_reds) - 1):
        for num in hist_reds[i]:
            trans[num].update(hist_reds[i + 1])
    return trans


def score_reds(hist_reds, n=6):
    """从最后一期号码出发, 转移概率累加: 本期出现的号 -> 下期各号概率"""
    trans = build_transition(hist_reds)
    last = hist_reds[-1]
    score = {}
    for num in range(1, 34):
        s = 0.0
        for src in last:
            c = trans[src]
            total = sum(c.values())
            if total:
                s += c.get(num, 0) / total
        score[num] = s
    return score


def score_blues(hist_blues, n=3):
    """蓝球马尔可夫: 本期蓝 -> 下期蓝分布"""
    trans = {}
    for i in range(len(hist_blues) - 1):
        trans.setdefault(hist_blues[i], Counter()).update([hist_blues[i + 1]])
    last = hist_blues[-1]
    c = trans.get(last, Counter())
    total = sum(c.values())
    score = {num: (c.get(num, 0) / total if total else 0) for num in range(1, 17)}
    return score


def predict(hist_reds, hist_blues):
    rs = score_reds(hist_reds)
    bs = score_blues(hist_blues)
    red_top = sorted(rs, key=rs.get, reverse=True)[:6]
    blue_top = sorted(bs, key=bs.get, reverse=True)[:3]
    return red_top, blue_top, rs, bs
