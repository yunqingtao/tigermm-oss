# -*- coding: utf-8 -*-
"""模块8: 号码人气模型 popularity.py (2026-08-27 学术实证升级)

来源 (跨文化一致的实证研究):
- Wang, Potter van Loon, van den Assem, van Dolder (2016) "Number preferences in
  lotteries", Judgment and Decision Making 11(3): 243-259 — 5百万+选择样本:
  7 全球最热(4.19%), 8 第二(4.05%); 小号1-12热(生日/月份); 1-31生日段热;
  最大号最冷; 奇数>偶数; 图案组合(连号/等差/直线)超热; 表单中心热边缘冷
- Ding (2011) "What Numbers to Choose for My Lottery Ticket? Behavior Anomalies
  in the Chinese Online Lottery Market", SSRN 1926526 — 中国彩民实证:
  偏爱吉利 8 (上期刚中也照选); 回避不吉利 14; 派利制下扎堆热门(引导效应)

用途: 反人气期望值优化 (Ziemba 1988: 不流行号码期望回报超成本~65%, 滚存时
$2.25/$1; 机选/热门号无优势). 这不是预测 — 开奖概率不变, 优化的是"中奖时
分摊人数". 浮动奖(双色球一二三四等)受分摊影响, 固定奖(五六等)不受.
"""
from collections import Counter

# ---------- 号码级人气权重 (红球 1-33; 正=热门该回避, 负=冷门可倾向) ----------
# 依据: 7/8 全球最热; 1-12 生日月热; 13-31 生日日热; 32-33 生日段外冷;
#       Ding 中国实证: 8 吉利热, 14 避讳冷; 车牌/房价实证: 4 避讳冷
def red_popularity_scores():
    w = {}
    for n in range(1, 34):
        s = 0.0
        if n in (7, 8):
            s += 3.0          # 全球最热
        elif n in (6, 9):
            s += 1.5          # 顺/久 (中国吉利, 弱于8)
        if n <= 12:
            s += 2.0          # 生日月段
        elif n <= 31:
            s += 1.0          # 生日日段
        else:
            s -= 1.5          # 32/33 生日段外 → 冷门
        if n == 14:
            s -= 2.0          # 中国避讳 "要死" (实证)
        elif n == 4:
            s -= 1.0          # 避讳 (车牌/房价实证)
        if n in (10, 20, 30):
            s -= 0.5          # 整十数略冷 (round numbers 实证中不受偏好)
        w[n] = s
    return w

def blue_popularity_scores():
    """蓝球 1-16"""
    w = {}
    for n in range(1, 17):
        s = 0.0
        if n == 8:
            s += 3.0
        elif n == 7:
            s += 2.5
        elif n in (6, 9):
            s += 1.5
        if n <= 12:
            s += 1.0          # 生日月
        else:
            s -= 0.5          # 13-16 生日段外
        if n == 14:
            s -= 2.0
        elif n == 4:
            s -= 1.0
        w[n] = s
    return w

# ---------- 组合级图案惩罚 (实证: 图案组合超热, 随机期望仅0.1%却出现上千次) ----------
def combo_popularity(reds6, blue, red_w=None, blue_w=None):
    """组合人气分 = 号码权重和 + 图案惩罚. 越低越冷门."""
    if red_w is None:
        red_w = red_popularity_scores()
    if blue_w is None:
        blue_w = blue_popularity_scores()
    s = sum(red_w[n] for n in reds6) + blue_w.get(blue, 0)
    seq = sorted(reds6)
    # 三连号+
    for i in range(4):
        if seq[i + 2] - seq[i] == 2:
            s += 2.0
    # 等差序列
    if len({seq[i + 1] - seq[i] for i in range(5)}) == 1:
        s += 3.0
    # 全奇/全偶
    odds = sum(1 for n in reds6 if n % 2)
    if odds in (0, 6):
        s += 2.0
    # 和值热门带 95-115 (实证: 玩家倾向"正常"和值)
    sm = sum(reds6)
    if 95 <= sm <= 115:
        s += 2.0
    # 均匀分布倾向 (玩家偏好均匀铺开)
    gaps = [seq[i + 1] - seq[i] for i in range(5)]
    if max(gaps) - min(gaps) <= 2:
        s += 1.5
    return s


# ---------- 反人气组合生成 (实证权重, 多样性约束) ----------
def gen_unpopular_combos(n_combos=3, seed=42, red_w=None, blue_w=None):
    """从全部 C(33,6)*16 组合中采样, 取人气分最低且结构多样的 n 组.
    输出 (reds6, blue, popularity_score)."""
    import random
    from itertools import combinations
    if red_w is None:
        red_w = red_popularity_scores()
    if blue_w is None:
        blue_w = blue_popularity_scores()
    rng = random.Random(seed)
    all_red = list(combinations(range(1, 34), 6))
    # 预筛: 先按号码权重和排序取最低 5%, 再细算图案惩罚
    num_w = {c: sum(red_w[n] for n in c) for c in all_red}
    ranked = sorted(num_w.items(), key=lambda kv: kv[1])[:max(300, len(all_red) // 50)]
    cold_blues = sorted(blue_w, key=blue_w.get)[:3]   # 最冷门 3 个蓝球轮转
    scored = []
    for c, nw in ranked:
        # 排序分只算红球权重+图案 (蓝球权重会扭曲排序, 让最低蓝球垄断)
        p = nw
        seq = sorted(c)
        for i in range(4):
            if seq[i + 2] - seq[i] == 2:
                p += 2.0
        if len({seq[i + 1] - seq[i] for i in range(5)}) == 1:
            p += 3.0
        odds = sum(1 for n in c if n % 2)
        if odds in (0, 6):
            p += 2.0
        sm = sum(c)
        if 95 <= sm <= 115:
            p += 2.0
        scored.append((p, c))
    scored.sort(key=lambda t: t[0])
    # 多样性: 和值/奇偶/三区错开 + 号码重合度 ≤2
    out = []
    for p, c in scored:
        sm = sum(c)
        odd = sum(1 for n in c if n % 2)
        z1 = sum(1 for n in c if n <= 11)
        z3 = sum(1 for n in c if n >= 23)
        if all(abs(sm - sum(o)) >= 8 or odd != sum(1 for n in o if n % 2)
               or z1 != sum(1 for n in o if n <= 11)
               or z3 != sum(1 for n in o if n >= 23)
               for o, _ in out):
            # 与已有组合重合 ≤2 个号
            if all(len(set(c) & set(o)) <= 2 for o, _ in out):
                out.append((list(c), p))
                if len(out) >= n_combos:
                    break
    # 蓝球独立轮转分配 (最冷门3个), 不参与红球排序
    blues = [cold_blues[i % len(cold_blues)] for i in range(len(out))]
    total = []
    for (c, p), b in zip(out, blues):
        total.append((c, b, p + blue_w[b]))
    return total


def popularity_table_html(red_w=None, blue_w=None):
    """生成人气权重表 HTML (红球1-33 + 蓝球1-16)"""
    if red_w is None:
        red_w = red_popularity_scores()
    if blue_w is None:
        blue_w = blue_popularity_scores()
    rows = []
    hot_red = sorted(red_w, key=red_w.get, reverse=True)[:6]
    cold_red = sorted(red_w, key=red_w.get)[:6]
    hot_blue = sorted(blue_w, key=blue_w.get, reverse=True)[:4]
    cold_blue = sorted(blue_w, key=blue_w.get)[:4]
    rows.append(f"<p><b>最热门(该回避):</b> 红球 {' '.join(f'{n:02d}' for n in hot_red)} · "
                f"蓝球 {' '.join(f'{n:02d}' for n in hot_blue)}</p>")
    rows.append(f"<p><b>最冷门(可倾向):</b> 红球 {' '.join(f'{n:02d}' for n in cold_red)} · "
                f"蓝球 {' '.join(f'{n:02d}' for n in cold_blue)}</p>")
    return "\n".join(rows)


def gen_fushi(n_reds=8, n_blues=1, seed=42, red_w=None, blue_w=None):
    """复式方案: 选 n_reds 个低人气红球 (区间/奇偶均衡) + n_blues 个低人气蓝球.
    注数 = C(n_reds, 6) * n_blues, 成本 = 注数 * 2 元.
    返回 (reds_pool, blues_pool, n_tickets, cost, avg_pop, zone_odd_summary).
    低人气池内做贪心均衡: 优先补三区缺口和奇偶缺口, 避免全挤大号区.
    """
    from math import comb
    if red_w is None:
        red_w = red_popularity_scores()
    if blue_w is None:
        blue_w = blue_popularity_scores()

    # 低人气候选: 按三区配额, 每区取人气分最低的一批 (一区生日段全正, 但复式必须覆盖)
    ranked = sorted(range(1, 34), key=lambda n: red_w[n])
    per_zone = max(2, n_reds // 3 + 1)   # 每区至少取这么多候选
    cand = []
    for lo, hi in [(1, 12), (12, 23), (23, 34)]:
        znum = [n for n in ranked if lo <= n < hi]
        cand += znum[:per_zone]

    # 贪心均衡选 n_reds 个: 每轮选当前最冷门且不破坏均衡的号
    picked = []
    zones = {1: 0, 2: 0, 3: 0}
    odd_cnt = 0
    target_odd = n_reds // 2   # 奇偶尽量 1:1
    for _ in range(n_reds):
        best, best_score = None, None
        for n in cand:
            if n in picked:
                continue
            z = 1 if n <= 11 else (2 if n <= 22 else 3)
            # 均衡分: 偏好补最缺的区/奇偶, 再按人气分
            zone_need = max(zones.values()) - zones[z]
            # 奇数过剩就偏好偶数, 反之亦然 (双向往 target_odd 靠)
            odd_need = (odd_cnt < target_odd) - (odd_cnt > target_odd)
            need = (1 if n % 2 == 1 else -1)
            odd_bal = odd_need * need   # >0 表示补对了方向
            bal = zone_need * 3 + (0 if odd_bal > 0 else 1)
            score = bal * 100 - red_w[n]   # 均衡优先, 人气次之
            if best_score is None or score < best_score:
                best, best_score = n, score
        picked.append(best)
        z = 1 if best <= 11 else (2 if best <= 22 else 3)
        zones[z] += 1
        odd_cnt += best % 2

    picked.sort()
    blues = sorted(range(1, 17), key=lambda n: blue_w[n])[:n_blues]
    n_tickets = comb(n_reds, 6) * n_blues
    cost = n_tickets * 2
    avg_pop = sum(red_w[n] for n in picked) / n_reds
    summary = (f"三区 {zones[1]}/{zones[2]}/{zones[3]}, "
               f"奇偶 {odd_cnt}:{n_reds - odd_cnt}, 平均人气 {avg_pop:+.1f}")
    return picked, blues, n_tickets, cost, avg_pop, summary


if __name__ == "__main__":
    rw = red_popularity_scores()
    bw = blue_popularity_scores()
    print("=== 红球人气权重 TOP6 热 / 6 冷 ===")
    for n in sorted(rw, key=rw.get, reverse=True)[:6]:
        print(f"  {n:02d}: {rw[n]:+.1f}")
    print("  ...")
    for n in sorted(rw, key=rw.get)[:6]:
        print(f"  {n:02d}: {rw[n]:+.1f}")
    print("=== 反人气组合 (实证权重) ===")
    for i, (c, b, p) in enumerate(gen_unpopular_combos(5, seed=42), 1):
        print(f"  第{i}组: {' '.join(f'{x:02d}' for x in c)} + {b:02d}  (人气分{p:.1f})")
