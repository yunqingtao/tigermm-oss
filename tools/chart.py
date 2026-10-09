"""
chart 工具 — 数据图表生成 (2026-08-19)
根据用户提供的数据/占比生成比例图(PNG)保存到指定路径。
支持: 饼图(pie) / 柱状图(bar) / 折线图(line)
依赖: matplotlib (Python311 已装 3.10.8)
"""
TOOL = {
    "name": "chart",
    "description": "数据图表生成: 根据数据生成饼图/柱状图/折线图, 保存PNG到路径",
    "keywords": ["图表", "比例图", "画图", "饼图", "柱状图", "折线图", "生成图", "统计图"],
    "params": [
        {"name": "action", "type": "str", "required": True,
         "enum": ["pie", "bar", "line"],
         "description": "图表类型: pie=饼图 bar=柱状图 line=折线图"},
        {"name": "data", "type": "str", "required": True,
         "description": "数据, 格式: '类别1:数值1, 类别2:数值2' 或 'x1:y1,x2:y2'"},
        {"name": "path", "type": "str", "required": False,
         "description": "保存路径, 默认桌面 chart_时间戳.png"},
        {"name": "title", "type": "str", "required": False,
         "description": "图表标题, 默认'数据分布图'"},
    ],
}
PLUGIN = {
    "name": "chart",
    "description": "数据图表生成: 饼图/柱状图/折线图 → PNG",
    "version": "1.0",
    "requires": ["matplotlib"],
    "trigger": ["图表", "比例图", "饼图", "柱状图", "折线图", "画图", "统计图"],
    "permission": ["file_write"],
    "category": "data",
}

import os as _os
import re as _re
import time as _time
import json as _json
from pathlib import Path


def _parse_data(data_str: str):
    """解析 '类别1:数值1, 类别2:数值2' → (labels, values)"""
    items = _re.split(r'[,，;；]', data_str)
    labels, values = [], []
    for it in items:
        it = it.strip()
        if not it:
            continue
        m = _re.match(r'(.+?)[:：]\s*([\d.]+)', it)
        if m:
            labels.append(m.group(1).strip())
            values.append(float(m.group(2)))
    if not labels:
        # fallback: 空格/竖线分隔
        m = _re.match(r'(.+?)[\s|｜]\s*([\d.]+)', data_str.strip())
        if m:
            labels.append(m.group(1).strip())
            values.append(float(m.group(2)))
    return labels, values


def _make_chart(action: str, labels, values, title: str, out_path: Path) -> dict:
    import matplotlib
    matplotlib.use("Agg")  # 无显示环境
    import matplotlib.pyplot as plt

    # ── 中文字体配置: 自动探测系统中文字体, 避免标签乱码方框 ──
    try:
        from matplotlib import font_manager
        _zh_fonts = ["Microsoft YaHei", "SimHei", "SimSun", "PingFang SC",
                     "Noto Sans CJK SC", "WenQuanYi Zen Hei", "Source Han Sans SC"]
        _installed = {f.name for f in font_manager.fontManager.ttflist}
        _chosen = next((f for f in _zh_fonts if f in _installed), None)
        if _chosen:
            plt.rcParams["font.sans-serif"] = [_chosen, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
        else:
            import subprocess
            _fc = subprocess.run(["fc-list", ":lang=zh", "family"],
                                 capture_output=True, text=True, timeout=5)
            _families = set()
            for _line in (_fc.stdout or "").splitlines():
                for _fam in _line.split(","):
                    _families.add(_fam.strip())
            _chosen2 = next((f for f in _zh_fonts if f in _families), None)
            if _chosen2:
                plt.rcParams["font.sans-serif"] = [_chosen2, "DejaVu Sans"]
                plt.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass  # 字体配置失败则用默认(可能乱码, 但图表仍生成)

    fig, ax = plt.subplots(figsize=(8, 6))
    if action == "pie":
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
        ax.set_title(title)
    elif action == "bar":
        ax.bar(labels, values, color="#4C72B0")
        ax.set_title(title)
        ax.set_ylabel("数值")
        plt.xticks(rotation=30, ha="right")
    elif action == "line":
        ax.plot(labels, values, marker="o", color="#4C72B0")
        ax.set_title(title)
        ax.set_ylabel("数值")
        plt.xticks(rotation=30, ha="right")
    else:
        plt.close(fig)
        return {"success": False, "error": f"未知图表类型: {action}"}
    plt.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return {"success": True, "output": f"图表已保存: {out_path}", "path": str(out_path)}


async def run(**kwargs) -> dict:
    action = kwargs.get("action", "pie")
    data_str = kwargs.get("data", kwargs.get("content", ""))
    path = kwargs.get("path", kwargs.get("save_path", ""))
    title = kwargs.get("title", "数据分布图")

    if not data_str:
        return {"success": False, "error": "缺少数据: 请提供 '类别:数值, 类别:数值' 格式的数据"}

    labels, values = _parse_data(data_str)
    if len(labels) < 2:
        return {"success": False,
                "error": f"数据解析失败(得到{len(labels)}项): '{data_str[:80]}' 应为 '类别1:数值1, 类别2:数值2'"}

    # 默认保存到桌面
    if not path:
        path = str(Path(_os.path.expanduser(r"~\Desktop")) / f"chart_{int(_time.time())}.png")
    else:
        # 用户给了目录 → 生成默认文件名; 给了 .png → 直接用
        if not path.lower().endswith(".png"):
            path = str(Path(path) / f"chart_{int(_time.time())}.png")

    out_path = Path(path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return _make_chart(action, labels, values, title, out_path)
    except Exception as e:
        return {"success": False, "error": f"图表生成失败: {e}"}
