# -*- coding: utf-8 -*-
"""模块5: 回测评估 backtest.py (重要)
滑动窗口滚动回测最近 BACK_N 期:
- 每个模型用前 N 期训练/统计, 预测第 N+1 期
- 统计红球命中 0-6 个分布 / 平均命中 / 蓝球命中率
- 与随机基线对比 (红球理论 6/33=0.1818, 平均1.091/期; 蓝球 1/16=6.25%)
结论判据: 命中率显著优于随机 => 模型有参考价值; 否则仅作参考.
"""
from collections import Counter
from random import Random

BACK_N = 100
RANDOM_RED_MEAN = 6 / 33 * 6  # = 1.0909
RANDOM_BLUE = 1 / 16

from predictors import freq_model, omission_model, markov_model, ml_model


def _hits(pred_reds, actual_reds):
    return len(set(pred_reds) & set(actual_reds))


def run_backtest(hist_reds, hist_blues, back_n=BACK_N, with_ml=True, seed=42):
    """返回 {model: {red_hits_dist, avg_red, blue_rate, n}} + random 基线"""
    n_total = len(hist_reds)
    start = n_total - back_n
    results = {
        "freq": {"dist": Counter(), "blue": 0, "n": 0},
        "omission": {"dist": Counter(), "blue": 0, "n": 0},
        "markov": {"dist": Counter(), "blue": 0, "n": 0},
    }
    if with_ml:
        results["ml"] = {"dist": Counter(), "blue": 0, "n": 0}
    results["random"] = {"dist": Counter(), "blue": 0, "n": 0}

    # ML 一次性拟合 (用 back_n 之前的全部数据), 滚动预测
    ml_model_fit = None
    if with_ml:
        try:
            ml_model_fit = ml_model.MLModel().fit(hist_reds[:start])
        except Exception:
            ml_model_fit = None

    rng = Random(seed)
    for i in range(start, n_total):
        h_reds = hist_reds[:i]
        h_blues = hist_blues[:i]
        actual_reds = hist_reds[i]
        actual_blue = hist_blues[i]

        # freq
        pr, pb, _, _ = freq_model.predict(h_reds, h_blues)
        results["freq"]["dist"][_hits(pr, actual_reds)] += 1
        results["freq"]["blue"] += 1 if actual_blue in pb else 0
        results["freq"]["n"] += 1

        # omission
        pr, pb, _, _ = omission_model.predict(h_reds, h_blues)
        results["omission"]["dist"][_hits(pr, actual_reds)] += 1
        results["omission"]["blue"] += 1 if actual_blue in pb else 0
        results["omission"]["n"] += 1

        # markov
        pr, pb, _, _ = markov_model.predict(h_reds, h_blues)
        results["markov"]["dist"][_hits(pr, actual_reds)] += 1
        results["markov"]["blue"] += 1 if actual_blue in pb else 0
        results["markov"]["n"] += 1

        # ml (只测红球; 蓝球复用 freq 的 top3)
        if ml_model_fit is not None:
            pr = ml_model_fit.predict(h_reds)[0]
            _, pb2, _, _ = freq_model.predict(h_reds, h_blues)
            results["ml"]["dist"][_hits(pr, actual_reds)] += 1
            results["ml"]["blue"] += 1 if actual_blue in pb2 else 0
            results["ml"]["n"] += 1

        # random
        pr = rng.sample(range(1, 34), 6)
        pb = rng.sample(range(1, 17), 3)
        results["random"]["dist"][_hits(pr, actual_reds)] += 1
        results["random"]["blue"] += 1 if actual_blue in pb else 0
        results["random"]["n"] += 1

    # 汇总统计
    for k, v in results.items():
        n = v["n"]
        total_hits = sum(h * c for h, c in v["dist"].items())
        v["avg_red"] = total_hits / n if n else 0
        v["blue_rate"] = v["blue"] / n if n else 0
    return results


def format_backtest(results):
    L = [f"=== 回测评估 (最近 {results['random']['n']} 期滚动) ==="]
    L.append(f"随机基线: 红球平均 {RANDOM_RED_MEAN:.4f}/期, 蓝球命中率 {RANDOM_BLUE*100:.1f}%")
    L.append(f"{'模型':<10}{'平均红球':>10}{'vs随机':>10}{'蓝球率':>10}{'0红':>5}{'1红':>5}{'2红':>5}{'3+红':>6}")
    for k in ["freq", "omission", "markov", "ml", "random"]:
        v = results.get(k)
        if v is None:
            continue
        diff = v["avg_red"] - RANDOM_RED_MEAN
        d = v["dist"]
        p3 = sum(c for h, c in d.items() if h >= 3)
        L.append(
            f"{k:<10}{v['avg_red']:>10.4f}{diff:>+10.4f}{v['blue_rate']*100:>9.1f}%"
            f"{d.get(0,0):>5}{d.get(1,0):>5}{d.get(2,0):>5}{p3:>6}"
        )
    return "\n".join(L)


# ---------- 多 seed 稳定性测试 ----------

def run_stability(hist_reds, hist_blues, back_n=BACK_N, seeds=(1, 2, 3, 4, 5),
                  with_ml=False):
    """多 seed 滚动回测, 统计各模型平均红球命中率的分布.
    用途: 验证 freq 优势是否稳定落在随机噪声带之外.
    返回 {model: {means: [...], best: float, worst: float, avg: float, spread: float}}
    """
    from statistics import mean
    agg = {}
    for seed in seeds:
        res = run_backtest(hist_reds, hist_blues, back_n=back_n,
                           with_ml=with_ml, seed=seed)
        for k, v in res.items():
            agg.setdefault(k, []).append(v["avg_red"])
    out = {}
    for k, vals in agg.items():
        out[k] = {
            "means": vals,
            "avg": mean(vals),
            "best": max(vals),
            "worst": min(vals),
            "spread": max(vals) - min(vals),
        }
    return out


def format_stability(st, random_ref="random"):
    """st: run_stability 的返回. 输出各模型跨 seed 的均值/最优/最差/极差."""
    L = ["=== 多 seed 稳定性测试 ==="]
    L.append(f"{'模型':<10}{'平均':>10}{'最优':>10}{'最差':>10}{'极差':>10}")
    for k in ["freq", "omission", "markov", "ml", "random"]:
        if k not in st:
            continue
        v = st[k]
        L.append(f"{k:<10}{v['avg']:>10.4f}{v['best']:>10.4f}{v['worst']:>10.4f}{v['spread']:>10.4f}")
    # 对比: 主力模型最差值 vs 随机最差值
    if random_ref in st and "freq" in st:
        f = st["freq"]
        r = st[random_ref]
        L.append("")
        L.append(f"freq 最差 {f['worst']:.4f} vs 随机最差 {r['worst']:.4f} "
                 f"=> {'稳定跑赢随机' if f['worst'] > r['worst'] else '存在被随机反超的seed'}")
    return "\n".join(L)


if __name__ == "__main__":
    from data_loader import load_data
    draws = load_data()
    hist_reds = [d.reds for d in draws]
    hist_blues = [d.blue for d in draws]
    res = run_backtest(hist_reds, hist_blues)
    print(format_backtest(res))
