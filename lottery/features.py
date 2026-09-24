# -*- coding: utf-8 -*-
"""模块2: 特征工程 (features.py)
单号特征: 出现频率/当前遗漏/平均遗漏/最大遗漏/冷热状态
组合特征: 和值/跨度/奇偶比/大小比/连号个数/三区分布
时间序列: 滑动窗口频率/号码转移概率
"""
from collections import Counter

RED_NUM = 33
BLUE_NUM = 16


# ---------- 组合特征 ----------

def sum_value(reds):
    return sum(reds)


def span_value(reds):
    return max(reds) - min(reds)


def odd_even(reds):
    return sum(1 for x in reds if x % 2 == 1)   # 奇数个数


def big_small(reds):
    return sum(1 for x in reds if x > 16)       # 大号个数 (>16)


def consecutive_count(reds):
    s = sorted(reds)
    n = 0
    for i in range(1, 6):
        if s[i] == s[i - 1] + 1:
            n += 1
    return n


def zone_dist(reds):
    """三区分布 1-11 / 12-22 / 23-33"""
    return (
        sum(1 for x in reds if x <= 11),
        sum(1 for x in reds if 12 <= x <= 22),
        sum(1 for x in reds if x >= 23),
    )


def ac_value(reds):
    """AC值: 两两差值的不同个数 - (6-1)"""
    diffs = set()
    for i in range(6):
        for j in range(i + 1, 6):
            diffs.add(abs(reds[i] - reds[j]))
    return len(diffs) - 5


def combo_features(reds):
    """返回当期组合特征 dict"""
    return {
        "和值": sum_value(reds),
        "跨度": span_value(reds),
        "奇数": odd_even(reds),
        "大数": big_small(reds),
        "连号": consecutive_count(reds),
        "一区": zone_dist(reds)[0],
        "二区": zone_dist(reds)[1],
        "三区": zone_dist(reds)[2],
        "AC值": ac_value(reds),
    }


# ---------- 单号特征 ----------

def single_number_features(all_draws, idx, window=50):
    """基于前 idx 期(不含当期)统计每个红球的单号特征.
    all_draws: list[Draw] 或 list[tuple(reds)]
    idx: 当前期下标 (0-based), 只统计 draws[0:idx]
    """
    hist = all_draws[:idx]
    n = len(hist)
    feats = {}
    for num in range(1, RED_NUM + 1):
        # 出现期集合
        appear = [t for t, d in enumerate(hist) if num in d.reds]
        total_cnt = len(appear)
        recent_cnt = sum(1 for t in appear if t >= idx - window)
        # 遗漏: 距最后一期的间隔
        last = appear[-1] if appear else -1
        cur_gap = (idx - 1) - last if appear else n
        # 平均/最大遗漏
        gaps = []
        prev = -1
        for t in appear:
            gaps.append(t - prev - 1)
            prev = t
        gaps.append(n - prev - 1)
        avg_gap = sum(gaps) / len(gaps) if gaps else n
        max_gap = max(gaps) if gaps else n
        # 冷热状态
        rate = recent_cnt / window if window else 0
        if rate >= 0.25:
            state = "热"
        elif rate >= 0.12:
            state = "温"
        else:
            state = "冷"
        feats[num] = {
            "总频": total_cnt,
            "近频": recent_cnt,
            "当前遗漏": cur_gap,
            "平均遗漏": round(avg_gap, 1),
            "最大遗漏": max_gap,
            "状态": state,
        }
    return feats


# ---------- 时间序列 ----------

def window_freq(hist, window=500):
    """最近 window 期每个号码出现次数 (不足则全量)"""
    recent = hist[-window:]
    cnt = Counter()
    for d in recent:
        cnt.update(d)
    return cnt


def transition_prob(hist, num):
    """号码 num 出现后, 下一期各号码出现频率 (转移概率近似)"""
    nxt = Counter()
    total = 0
    for i in range(len(hist) - 1):
        if num in hist[i]:
            nxt.update(hist[i + 1])
            total += 1
    if total == 0:
        return Counter()
    return {k: v / total for k, v in nxt.items()}


# ---------- 标签 (ML 用) ----------

def make_labels(draws):
    """每期 -> 33维红球标签 + 16维蓝球标签 (one-hot)"""
    red_labels = []
    blue_labels = []
    for d in draws:
        rl = [0] * RED_NUM
        bl = [0] * BLUE_NUM
        for x in d.reds:
            rl[x - 1] = 1
        bl[d.blue - 1] = 1
        red_labels.append(rl)
        blue_labels.append(bl)
    return red_labels, blue_labels
