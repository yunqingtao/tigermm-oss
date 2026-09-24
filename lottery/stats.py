# -*- coding: utf-8 -*-
"""SSQ 3494期完整统计分析"""
import sqlite3
from collections import Counter

DB = r'str(Path(__file__).resolve().parents[1] / "lottery/ssq.db")'
c = sqlite3.connect(DB)

rows = c.execute("SELECT r1,r2,r3,r4,r5,r6,blue FROM draws ORDER BY id").fetchall()
N = len(rows)
print(f"总期数: {N}\n")

# 红球频率
red_counter = Counter()
blue_counter = Counter()
for r in rows:
    for v in r[:6]:
        red_counter[v] += 1
    blue_counter[r[6]] += 1

print("=== 红球频率 TOP15 ===")
for num, cnt in red_counter.most_common(15):
    print(f"红{num:02d}: {cnt}次 ({cnt/N*100:.1f}%)")

print("\n=== 红球冷号(最低频率) ===")
for num, cnt in sorted(red_counter.items(), key=lambda x: x[1])[:10]:
    print(f"红{num:02d}: {cnt}次")

print("\n=== 蓝球频率 ===")
for num in range(1,17):
    cnt = blue_counter.get(num,0)
    print(f"蓝{num:02d}: {cnt}次 ({cnt/N*100:.1f}%)")

# 和值
sums = [sum(r[:6]) for r in rows]
avg_sum = sum(sums)/N
print(f"\n=== 和值 ===")
print(f"平均和值: {avg_sum:.1f}")
print(f"和值范围: {min(sums)} - {max(sums)}")

# 区间分布 1-11 12-22 23-33
def zone(reds):
    z1=sum(1 for x in reds if x<=11)
    z2=sum(1 for x in reds if 12<=x<=22)
    z3=sum(1 for x in reds if x>=23)
    return z1,z2,z3
zone_counter = Counter(zone(r[:6]) for r in rows)
print("\n=== 三区间分布(前10种组合) ===")
for z,cnt in zone_counter.most_common(10):
    print(f"一区{z[0]} 二区{z[1]} 三区{z[2]}: {cnt}次 ({cnt/N*100:.1f}%)")

# 连号检测
def consecutive(reds):
    s=sorted(reds); n=0
    for i in range(1,6):
        if s[i]==s[i-1]+1: n+=1
    return n
consec_counter = Counter(consecutive(r[:6]) for r in rows)
print("\n=== 连号情况 ===")
for k in sorted(consec_counter):
    print(f"{k}组连号: {consec_counter[k]}次 ({consec_counter[k]/N*100:.1f}%)")

# 遗漏(最近50期冷号)
recent = rows[-50:]
recent_reds = set()
for r in recent:
    recent_reds.update(r[:6])
missed = [n for n in range(1,34) if n not in recent_reds]
print("\n=== 近50期遗漏红球(冷号) ===")
print(" ".join(f"{n:02d}" for n in missed) if missed else "无遗漏")

# 连出(近50期热号)
recent_red_counter = Counter()
for r in recent:
    recent_red_counter.update(r[:6])
print("\n=== 近50期红球热号 TOP10 ===")
for num,cnt in recent_red_counter.most_common(10):
    print(f"红{num:02d}: {cnt}次")

# 蓝球遗漏
recent_blue = {r[6] for r in recent}
missed_blue = [n for n in range(1,17) if n not in recent_blue]
print("\n=== 近50期蓝球遗漏 ===")
print(" ".join(f"{n:02d}" for n in missed_blue) if missed_blue else "无遗漏")

c.close()
