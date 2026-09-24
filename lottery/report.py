# -*- coding: utf-8 -*-
"""模块7: 结果展示 report.py
生成 report.html (含图表+推荐号码) 和 report.txt, 输出到 output/
"""
import os
import base64

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def _img_b64(name):
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        return ""
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode()


def write_report(stats_txt, backtest_txt, groups, out=OUT, pop_txt="", pop_html=""):
    os.makedirs(out, exist_ok=True)
    # txt
    txt_path = os.path.join(out, "report.txt")
    lines = [
        "=" * 50,
        "双色球预测报告",
        "=" * 50,
        "",
        "【统计概览】",
        stats_txt,
        "",
        "【回测评估】",
        backtest_txt,
        "",
        "【本期推荐 Top3】",
    ]
    for i, (reds, blue) in enumerate(groups, 1):
        r = " ".join(f"{x:02d}" for x in reds)
        lines.append(f"  第{i}组: {r} + {blue:02d}")
    lines.append("")
    lines.append("⚠ 彩票本质随机, 以上仅作数据挖掘/模型实践参考, 不构成投注建议.")
    if pop_txt:
        lines += ["", "【反人气分析 (学术实证)】", pop_txt]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # html
    html_path = os.path.join(out, "report.html")
    img_freq = _img_b64("red_freq.png")
    img_omit = _img_b64("red_omit.png")
    img_heat = _img_b64("red_heatmap.png")
    picks_html = ""
    for i, (reds, blue) in enumerate(groups, 1):
        r = " ".join(f"{x:02d}" for x in reds)
        picks_html += f"<div class='pick'><span class='tag'>第{i}组</span>" \
                      f"<span class='red'>{r}</span> + <span class='blue'>{blue:02d}</span></div>"
    stats_html = stats_txt.replace("\n", "<br>").replace(" ", "&nbsp;")
    bt_html = backtest_txt.replace("\n", "<br>").replace(" ", "&nbsp;")

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>双色球预测报告</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; background:#0d0d0d; color:#eee; margin:0; padding:30px; }}
.wrap {{ max-width:900px; margin:auto; }}
h1 {{ color:#ffac02; text-align:center; }}
h2 {{ color:#ffac02; border-bottom:1px solid #333; padding-bottom:6px; margin-top:36px; }}
img {{ width:100%; border-radius:8px; margin:8px 0; }}
.pick {{ background:#1a1a1a; border:1px solid #333; border-radius:10px; padding:14px 18px; margin:10px 0; font-size:20px; }}
.tag {{ background:#ffac02; color:#0d0d0d; padding:3px 10px; border-radius:5px; margin-right:12px; font-weight:bold; }}
.red {{ color:#ff5a5a; font-weight:bold; }}
.blue {{ color:#5aa2ff; font-weight:bold; }}
pre {{ background:#141414; padding:14px; border-radius:8px; font-size:13px; line-height:1.6; overflow-x:auto; }}
.warn {{ color:#ffac02; font-size:13px; }}
.pop {{ background:#141414; border-left:3px solid #ffac02; padding:10px 14px; border-radius:6px; font-size:14px; line-height:1.8; }}
</style></head><body><div class="wrap">
<h1>🎱 双色球预测报告</h1>
<h2>本期推荐 Top3</h2>
{picks_html}
<p class="warn">⚠ 彩票本质随机, 本报告仅作数据挖掘/模型实践参考, 不构成投注建议.</p>
<h2>统计分析</h2>
<pre>{stats_html}</pre>
<h2>图表</h2>
<img src="data:image/png;base64,{img_freq}" alt="red_freq">
<img src="data:image/png;base64,{img_omit}" alt="red_omit">
<img src="data:image/png;base64,{img_heat}" alt="red_heatmap">
<h2>回测评估 (最近100期滚动)</h2>
<pre>{bt_html}</pre>
{pop_html}
</div></body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return txt_path, html_path
