# -*- coding: utf-8 -*-
"""模块3: 统计分析 (stats_analysis.py)
对全量数据做统计, 输出7项报表 + 3类图表到 output/
"""
import os
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from data_loader import load_data

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def ensure_out():
    os.makedirs(OUT, exist_ok=True)


def analyze(draws):
    """返回报表 dict + 生成图表"""
    N = len(draws)
    reds_all = [d.reds for d in draws]
    blues = [d.blue for d in draws]

    red_counter = Counter()
    for r in reds_all:
        red_counter.update(r)
    blue_counter = Counter(blues)

    # 1. 红球频率榜
    red_rank = red_counter.most_common(33)
    # 2. 蓝球频率榜
    blue_rank = blue_counter.most_common(16)
    # 3. 冷热号 (近50期)
    recent = reds_all[-50:]
    recent_counter = Counter()
    for r in recent:
        recent_counter.update(r)
    hot = recent_counter.most_common(10)
    cold = [n for n in range(1, 34) if n not in recent_counter][:10]
    # 4. 遗漏分析
    last_set = set(reds_all[-1])
    omit = []
    for num in range(1, 34):
        if num in last_set:
            continue
        gap = 1
        for d in reversed(reds_all):
            if num in d:
                break
            gap += 1
        omit.append((num, gap))
    omit.sort(key=lambda x: -x[1])
    # 5. 和值分布
    sums = [sum(r) for r in reds_all]
    avg_sum = sum(sums) / N
    sum_hist = Counter(s // 10 * 10 for s in sums)
    # 6. 奇偶/大小分布
    oe = Counter((sum(1 for x in r if x % 2),) for r in reds_all)
    bs = Counter((sum(1 for x in r if x > 16),) for r in reds_all)
    # 7. 区间分布
    zone = Counter(
        (
            sum(1 for x in r if x <= 11),
            sum(1 for x in r if 12 <= x <= 22),
            sum(1 for x in r if x >= 23),
        )
        for r in reds_all
    )

    report = {
        "N": N,
        "red_rank": red_rank,
        "blue_rank": blue_rank,
        "hot": hot,
        "cold": cold,
        "omit": omit,
        "avg_sum": avg_sum,
        "sum_min": min(sums),
        "sum_max": max(sums),
        "sum_hist": sum_hist,
        "oe": oe,
        "bs": bs,
        "zone": zone,
    }

    # ---- 图表 ----
    ensure_out()
    # 柱状图: 红球频率
    nums = [n for n, _ in red_rank]
    cnts = [c for _, c in red_rank]
    plt.figure(figsize=(14, 5))
    plt.bar(nums, cnts, color="#1f77b4")
    plt.axhline(N * 6 / 33, color="red", ls="--", lw=1, label="理论均值")
    plt.title(f"红球出现频率 (共{N}期)")
    plt.xlabel("号码")
    plt.ylabel("次数")
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "red_freq.png"), dpi=100)
    plt.close()

    # 折线图: 遗漏走势 (近50期每个号码的出现间隔演化 -> 当前遗漏TOP15)
    plt.figure(figsize=(14, 5))
    top_omit = omit[:15]
    plt.bar([f"{n:02d}" for n, _ in top_omit], [g for _, g in top_omit], color="#d62728")
    plt.title("当前遗漏最大的15个红球")
    plt.xlabel("号码")
    plt.ylabel("遗漏期数")
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "red_omit.png"), dpi=100)
    plt.close()

    # 热力图: 号码 x 近100期出现热力
    import numpy as np
    mat = np.zeros((33, 100))
    for t, r in enumerate(reds_all[-100:]):
        for x in r:
            mat[x - 1, t] += 1
    plt.figure(figsize=(14, 8))
    plt.imshow(mat, aspect="auto", cmap="YlOrRd", interpolation="nearest")
    plt.colorbar(label="出现")
    plt.xlabel("期 (近100期)")
    plt.ylabel("号码 1-33")
    plt.title("红球近100期出现热力图")
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "red_heatmap.png"), dpi=100)
    plt.close()

    return report


def format_report(rep):
    N = rep["N"]
    L = []
    L.append(f"=== 双色球统计分析 (共{N}期) ===\n")
    L.append(f"[1] 红球频率 TOP15")
    for n, c in rep["red_rank"][:15]:
        L.append(f"  红{n:02d}: {c}次 ({c/N*100:.1f}%)")
    L.append(f"\n[2] 蓝球频率榜")
    for n, c in rep["blue_rank"]:
        L.append(f"  蓝{n:02d}: {c}次 ({c/N*100:.1f}%)")
    L.append(f"\n[3] 近50期热号 TOP10: {' '.join(f'{n:02d}' for n,_ in rep['hot'])}")
    L.append(f"    近50期冷号 (未出): {' '.join(f'{n:02d}' for n in rep['cold'][:10])}")
    L.append(f"\n[4] 当前遗漏最大 TOP10")
    for n, g in rep["omit"][:10]:
        L.append(f"  红{n:02d}: 遗漏{g}期")
    L.append(f"\n[5] 和值分布: 平均 {rep['avg_sum']:.1f}, 范围 {rep['sum_min']}-{rep['sum_max']}")
    for k in sorted(rep["sum_hist"]):
        L.append(f"  {k}-{k+9}: {rep['sum_hist'][k]}期")
    L.append(f"\n[6] 奇偶比分布 (奇:偶)")
    for (o,), c in sorted(rep["oe"].items()):
        L.append(f"  {o}:{6-o} -> {c}期 ({c/N*100:.1f}%)")
    L.append(f"\n[7] 三区分布 TOP8 (一区:二区:三区)")
    for (a, b, c), cnt in rep["zone"].most_common(8):
        L.append(f"  {a}:{b}:{c} -> {cnt}期 ({cnt/N*100:.1f}%)")
    return "\n".join(L)


if __name__ == "__main__":
    draws = load_data()
    rep = analyze(draws)
    print(format_report(rep))
    print(f"\n图表已输出到 {OUT}")
