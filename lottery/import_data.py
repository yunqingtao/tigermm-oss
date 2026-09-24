# -*- coding: utf-8 -*-
"""双色球历史数据解析入库"""
import sqlite3, os, re

SRC = r"F:\data.txt"
DB  = os.path.join(os.path.dirname(__file__), "ssq.db")

def parse_line(line):
    """解析一行: '10 11 12 13 26 28 + 11' -> (red_list, blue)"""
    line = line.strip()
    if not line:
        return None
    parts = re.split(r'\s*\+\s*', line)
    if len(parts) != 2:
        return None
    red = [int(x) for x in parts[0].split()]
    blue = int(parts[1].strip())
    if len(red) != 6 or blue < 1 or blue > 16:
        return None
    if any(r < 1 or r > 33 for r in red):
        return None
    if len(set(red)) != 6:  # 红球不重复
        return None
    return sorted(red), blue

def main():
    with open(SRC, encoding="utf-8") as f:
        lines = f.readlines()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS draws(
        id INTEGER PRIMARY KEY,
        r1 INTEGER, r2 INTEGER, r3 INTEGER, r4 INTEGER, r5 INTEGER, r6 INTEGER,
        blue INTEGER
    )""")
    cur.execute("DELETE FROM draws")

    n = 0
    bad = 0
    for i, line in enumerate(lines):
        res = parse_line(line)
        if res is None:
            bad += 1
            continue
        red, blue = res
        cur.execute("INSERT INTO draws(r1,r2,r3,r4,r5,r6,blue) VALUES(?,?,?,?,?,?,?)",
                    (*red, blue))
        n += 1

    conn.commit()
    cur.execute("SELECT COUNT(*) FROM draws")
    print(f"总行数: {len(lines)}")
    print(f"成功入库: {n}")
    print(f"解析失败: {bad}")
    print(f"库内记录: {cur.fetchone()[0]}")
    cur.execute("SELECT * FROM draws LIMIT 3")
    for row in cur.fetchall():
        print(row)
    conn.close()

if __name__ == "__main__":
    main()
