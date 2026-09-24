# -*- coding: utf-8 -*-
"""4.2 遗漏回补模型 omission_model.py
⚠ 重要: 冷号回补已被3477期全量验证为赌徒谬误(漏越久出率越低, >50期→1.5% vs 随机6.25%).
本模型仅作展示性参考, 不用于最终推荐. 打分方向: 遗漏适中(接近平均遗漏)反而优先.
"""
from collections import Counter

OMIT_WINDOW = 50


def _omit_info(hist_reds, num):
    """num 在当前时刻的遗漏期数 (从尾部往前数)"""
    gap = 0
    for r in reversed(hist_reds):
        if num in r:
            break
        gap += 1
    return gap


def score_reds(hist_reds, n=6):
    """得分 = 越接近平均遗漏越优先 (不追极端冷, 不追热)"""
    # 平均遗漏 ≈ 33/6 = 5.5 期 (理论), 用实际近500期平均
    avg_gap = 33 / 6
    score = {}
    for num in range(1, 34):
        gap = _omit_info(hist_reds, num)
        # 距平均遗漏越近得分越高: 1 - |gap - avg| / max(gap, avg)
        score[num] = max(0.0, 1 - abs(gap - avg_gap) / max(gap, avg_gap, 1))
    return score


def score_blues(hist_blues, n=3):
    cnt = Counter(hist_blues[-OMIT_WINDOW:])
    score = {num: cnt.get(num, 0) / OMIT_WINDOW for num in range(1, 17)}
    return score


def predict(hist_reds, hist_blues):
    rs = score_reds(hist_reds)
    bs = score_blues(hist_blues)
    red_top = sorted(rs, key=rs.get, reverse=True)[:6]
    blue_top = sorted(bs, key=bs.get, reverse=True)[:3]
    return red_top, blue_top, rs, bs
