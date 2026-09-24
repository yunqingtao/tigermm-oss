# -*- coding: utf-8 -*-
"""模块1: 数据加载与解析
读取 F:/data.txt, 解析为结构化数据 Draw(reds, blue)
"""
import os
import re

SRC = r"F:\data.txt"


class Draw:
    __slots__ = ("period", "reds", "blue")

    def __init__(self, period, reds, blue):
        self.period = period      # 期号（文件无期号，用行号代替）
        self.reds = reds          # 6个红球，升序
        self.blue = blue          # 1个蓝球

    def __repr__(self):
        r = " ".join(f"{x:02d}" for x in self.reds)
        return f"{self.period}: {r} + {self.blue:02d}"


def parse_line(line):
    """解析一行: '10 11 12 13 26 28 + 11' -> (red_list, blue) or None"""
    line = line.strip()
    if not line:
        return None
    parts = re.split(r"\s*\+\s*", line)
    if len(parts) != 2:
        return None
    red = [int(x) for x in parts[0].split()]
    blue = int(parts[1].strip())
    if len(red) != 6 or blue < 1 or blue > 16:
        return None
    if any(r < 1 or r > 33 for r in red):
        return None
    if len(set(red)) != 6:
        return None
    return sorted(red), blue


def load_data(path=SRC):
    """读取文件, 返回 list[Draw], 共 3495 条"""
    draws = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            res = parse_line(line)
            if res is None:
                continue
            reds, blue = res
            draws.append(Draw(i, reds, blue))
    return draws


if __name__ == "__main__":
    ds = load_data()
    print(f"总期数: {len(ds)}")
    print("首期:", ds[0])
    print("末期:", ds[-1])
