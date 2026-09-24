# -*- coding: utf-8 -*-
"""4.5 综合推荐器 combiner.py
多模型加权融合. 权重默认方案(0.3/0.2/0.2/0.3), 可由 backtest 结果覆盖.
输出 Top3 组推荐号码 (红6+蓝1).

v2 (2026-08-27): 加性加入人气回避修正 — popularity_lambda 参数.
模型分决定"该买什么号", 人气分决定"中奖时和谁分钱" (Ziemba 1988: 不流行号码
期望回报超成本~65%; Wang 2016: 7/8/小号/图案组合最热). 最终分 = 模型分 - λ×人气分.
λ=0 完全关闭 (旧行为), 默认 0.3 (λ 扫描 0/0.15/0.3/0.5/0.8: 0.3 起收益递减,
人气分 +11.8→+4.2, 再大边际改善 <1 分且开始偏离模型信号).
"""
import random

try:
    from popularity import red_popularity_scores, blue_popularity_scores
    _HAS_POP = True
except Exception:
    _HAS_POP = False


def combine(hist_reds, hist_blues, weights=None, n_groups=3, seed=42,
            popularity_lambda=0.3):
    """weights: dict {freq, omission, markov, ml} 或 None(默认).
    popularity_lambda: 人气回避强度 (0=关闭).
    返回 list of (reds6, blue, scores)
    """
    from predictors import freq_model, omission_model, markov_model, ml_model

    if weights is None:
        weights = {"freq": 0.3, "omission": 0.2, "markov": 0.2, "ml": 0.3}

    # 各模型得分
    _, _, freq_rs, freq_bs = freq_model.predict(hist_reds, hist_blues)
    _, _, omi_rs, omi_bs = omission_model.predict(hist_reds, hist_blues)
    _, _, mark_rs, mark_bs = markov_model.predict(hist_reds, hist_blues)
    try:
        ml = ml_model.MLModel().fit(hist_reds)
        ml_rs = ml.predict_proba(hist_reds[-1])  # 33维概率
        ml_ok = True
    except Exception:
        ml_rs = None
        ml_ok = False

    # 加权红球得分
    red_score = {}
    for num in range(1, 34):
        s = weights["freq"] * freq_rs[num]
        s += weights["omission"] * omi_rs[num]
        s += weights["markov"] * mark_rs[num]
        if ml_ok:
            s += weights["ml"] * float(ml_rs[num - 1])
        red_score[num] = s

    # 加权蓝球得分
    blue_score = {}
    for num in range(1, 17):
        s = weights["freq"] * freq_bs[num]
        s += weights["omission"] * omi_bs[num]
        s += weights["markov"] * mark_bs[num]
        blue_score[num] = s

    # 人气回避修正: 最终分 = 模型分 - λ×人气分 (λ=0 关闭, 旧行为)
    pop_lambda = popularity_lambda if (_HAS_POP and popularity_lambda > 0) else 0.0
    if pop_lambda > 0:
        rw = red_popularity_scores()
        bw = blue_popularity_scores()
        # 人气分归一化到 ~[0,1] 再乘 λ, 避免绝对值过大压过模型分
        r_min, r_max = min(rw.values()), max(rw.values())
        b_min, b_max = min(bw.values()), max(bw.values())
        for num in range(1, 34):
            norm = (rw[num] - r_min) / (r_max - r_min) if r_max > r_min else 0.5
            red_score[num] -= pop_lambda * norm
        for num in range(1, 17):
            norm = (bw[num] - b_min) / (b_max - b_min) if b_max > b_min else 0.5
            blue_score[num] -= pop_lambda * norm

    order = sorted(red_score, key=red_score.get, reverse=True)
    blue_order = sorted(blue_score, key=blue_score.get, reverse=True)

    # 生成 n_groups 组: 轮转错开
    groups = []
    rng = random.Random(seed)
    for g in range(n_groups):
        pool = order[:]  # 全部候选
        # 每组从高分池轮转取6个, 错开避免重复
        if g == 0:
            picks = order[:6]
        elif g == 1:
            picks = order[3:9]
        elif g == 2:
            picks = order[6:12]
        else:
            picks = rng.sample(pool[:18], 6)
        groups.append((sorted(picks), blue_order[g % 3]))

    return groups, red_score, blue_score, ml_ok
