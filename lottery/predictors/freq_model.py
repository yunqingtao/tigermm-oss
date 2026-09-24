# -*- coding: utf-8 -*-
"""4.1 频率模型 freq_model.py
综合历史总频率 + 近期频率加权打分.
实测结论(2976期回测): 远期权重 > 近期权重, 500期窗口最强(+4.8% vs 随机).
权重 [6,5,4,3,2,1] 对应窗口 [500,200,100,50,30,10], 远期偏重.
"""
from collections import Counter

WINDOWS = [500, 200, 100, 50, 30, 10]
WEIGHTS = [6, 5, 4, 3, 2, 1]


def score_reds(hist_reds, n=6):
    """hist_reds: list[list[int]] 历史红球. 返回红球得分 dict {num: score}"""
    total_w = sum(WEIGHTS)
    score = {}
    for num in range(1, 34):
        s = 0.0
        for w, wlen in zip(WEIGHTS, WINDOWS):
            cnt = 0
            for r in hist_reds[-wlen:]:
                if num in r:
                    cnt += 1
            s += w * cnt / wlen
        score[num] = s / total_w
    return score


def score_blues(hist_blues, n=3):
    """蓝球: 近50期热度 (实测唯一可靠信号, S4). 返回得分 dict"""
    recent = hist_blues[-50:]
    cnt = Counter(recent)
    score = {num: cnt.get(num, 0) / 50 for num in range(1, 17)}
    return score


def predict(hist_reds, hist_blues):
    """返回 (reds_top6, blues_top3, red_score, blue_score)"""
    rs = score_reds(hist_reds)
    bs = score_blues(hist_blues)
    red_top = sorted(rs, key=rs.get, reverse=True)[:6]
    blue_top = sorted(bs, key=bs.get, reverse=True)[:3]
    return red_top, blue_top, rs, bs
