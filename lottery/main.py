# -*- coding: utf-8 -*-
"""一键跑通全流程: 数据 -> 统计 -> 建模 -> 回测 -> 推荐 -> 报告"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from data_loader import load_data
from database import sync_from_file
from stats_analysis import analyze, format_report
from backtest import run_backtest, format_backtest
from predictors.combiner import combine
from report import write_report


def main():
    print("[1/5] 同步数据库 ...")
    total, added = sync_from_file()
    print(f"  库内 {total} 期, 本次新增 {added}")

    print("[2/5] 加载数据 ...")
    draws = load_data()
    hist_reds = [d.reds for d in draws]
    hist_blues = [d.blue for d in draws]
    print(f"  {len(draws)} 期")

    print("[3/5] 统计分析 + 图表 ...")
    rep = analyze(draws)
    stats_txt = format_report(rep)

    print("[4/5] 回测评估 (最近100期) ...")
    bt = run_backtest(hist_reds, hist_blues, with_ml=True)
    bt_txt = format_backtest(bt)
    print(bt_txt)

    # 由回测结果动态定权: 有效模型加权, 无效降权
    base = bt["random"]["avg_red"]
    w = {}
    for k in ["freq", "omission", "markov", "ml"]:
        if k in bt and bt[k]["n"]:
            diff = bt[k]["avg_red"] - base
            w[k] = max(0.0, diff) + 0.05   # 有效加权, 保底0.05
        else:
            w[k] = 0.05
    tot = sum(w.values())
    weights = {k: v / tot for k, v in w.items()}
    print(f"  动态权重: { {k: round(v,3) for k,v in weights.items()} }")

    print("[5/5] 综合推荐 + 反人气分析 + 报告 ...")
    groups, rs, bs, ml_ok = combine(hist_reds, hist_blues, weights=weights)
    for i, (reds, blue) in enumerate(groups, 1):
        r = " ".join(f"{x:02d}" for x in reds)
        print(f"  第{i}组: {r} + {blue:02d}")

    # 反人气分析 (学术实证: Wang 2016 / Ding 2011 / Ziemba 1988)
    try:
        from popularity import (red_popularity_scores, blue_popularity_scores,
                                combo_popularity, gen_unpopular_combos,
                                gen_fushi, popularity_table_html)
        rw = red_popularity_scores()
        bw = blue_popularity_scores()
        pop_lines = ["依据: Wang et al. 2016 (5百万选择样本) — 7/8最热, 小号/生日段热, ",
                     "  大号冷, 图案组合超热; Ding 2011 (中国实证) — 吉利8热, 避讳14冷;",
                     "  Ziemba 1988 — 不流行号码期望回报超成本~65%, 机选/热门无优势.",
                     "  原理: 开奖概率不变, 优化中奖时分摊人数 (浮动奖). 非预测工具.",
                     "",
                     "号码人气 (正=热门该回避, 负=冷门可倾向):"]
        hot_r = sorted(rw, key=rw.get, reverse=True)[:6]
        cold_r = sorted(rw, key=rw.get)[:6]
        hot_b = sorted(bw, key=bw.get, reverse=True)[:3]
        cold_b = sorted(bw, key=bw.get)[:3]
        pop_lines.append(f"  红球最热(避): {' '.join(f'{n:02d}' for n in hot_r)} | "
                         f"最冷(可倾向): {' '.join(f'{n:02d}' for n in cold_r)}")
        pop_lines.append(f"  蓝球最热(避): {' '.join(f'{n:02d}' for n in hot_b)} | "
                         f"最冷(可倾向): {' '.join(f'{n:02d}' for n in cold_b)}")
        pop_lines.append("")
        pop_lines.append("推荐组合人气分 (越低越冷门, 中奖时少分摊):")
        for i, (reds, blue) in enumerate(groups, 1):
            p = combo_popularity(reds, blue, rw, bw)
            r = " ".join(f"{x:02d}" for x in reds)
            pop_lines.append(f"  第{i}组: {r} + {blue:02d}  人气分 {p:+.1f}")
        pop_lines.append("")
        pop_lines.append("纯反人气组合 (完全不依赖模型, 只求最低分摊):")
        for i, (c, b, p) in enumerate(gen_unpopular_combos(3, seed=42, red_w=rw, blue_w=bw), 1):
            r = " ".join(f"{x:02d}" for x in c)
            pop_lines.append(f"  第{i}组: {r} + {b:02d}  人气分 {p:+.1f}")
        pop_lines.append("")
        pop_lines.append("反人气复式方案 (红球池覆盖 C(n,6) 注, 蓝球池 ×m):")
        for n in [8, 10]:
            r, b, t, c, ap, s = gen_fushi(n, 2, red_w=rw, blue_w=bw)
            rs = " ".join(f"{x:02d}" for x in r)
            bs = " ".join(f"{x:02d}" for x in b)
            pop_lines.append(f"  {n}红+{len(b)}蓝: 红 {' '.join(f'{x:02d}' for x in r)} | "
                             f"蓝 {bs} | {t}注 {c}元 | {s}")
        pop_txt = "\n".join(pop_lines)
        pop_html = (f"<h2>反人气分析 (学术实证)</h2><div class='pop'>"
                    + pop_txt.replace("\n", "<br>").replace(" ", "&nbsp;")
                    + "</div>")
        print("\n" + pop_txt)
    except Exception as e:
        pop_txt, pop_html = "", ""
        print(f"  ⚠ 反人气分析跳过: {e}")

    txt_path, html_path = write_report(stats_txt, bt_txt, groups, pop_txt=pop_txt, pop_html=pop_html)
    print(f"\n报告: {txt_path}")
    print(f"HTML: {html_path}")


if __name__ == "__main__":
    main()
