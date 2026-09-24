# -*- coding: utf-8 -*-
"""模块6: SQLite 存储 (database.py)
- 同步 F:/data.txt 到 ssq.db (幂等: 按行号对齐, 只追加新增)
- 提供统一的读库接口
"""
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssq.db")
SRC = r"F:\data.txt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS draws(
    id INTEGER PRIMARY KEY,
    r1 INTEGER, r2 INTEGER, r3 INTEGER, r4 INTEGER, r5 INTEGER, r6 INTEGER,
    blue INTEGER
)
"""


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def sync_from_file(path=SRC, db=DB):
    """幂等同步: 读取文件, 与库内比对, 只插入新增行.
    文件期数 >= 库内期数时正常追加; 文件更短时不动库(保护数据).
    返回 (库内总行数, 本次新增行数)
    """
    from data_loader import parse_line

    with open(path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(SCHEMA)
    cur.execute("SELECT COUNT(*) FROM draws")
    have = cur.fetchone()[0]

    # 已有行数 >= 文件行数 => 无需更新 (或文件被截断, 不破坏库)
    if have >= len(lines):
        conn.close()
        return have, 0

    added = 0
    for i in range(have, len(lines)):
        res = parse_line(lines[i])
        if res is None:
            continue
        red, blue = res
        cur.execute(
            "INSERT INTO draws(r1,r2,r3,r4,r5,r6,blue) VALUES(?,?,?,?,?,?,?)",
            (*red, blue),
        )
        added += 1
    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    conn.close()
    return total, added


def load_all(db=DB):
    """读全部开奖, 返回 list[tuple(r1..r6, blue)] 按期序"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT r1,r2,r3,r4,r5,r6,blue FROM draws ORDER BY id"
    ).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


if __name__ == "__main__":
    total, added = sync_from_file()
    print(f"库内总期数: {total}, 本次新增: {added}")
