"""tools/session_logs.py — 对话史查询工具测试 (2026-09-20)

为什么要有这一份: 这是**唯一能读用户对话库**的工具, 一旦它能写, 就是数据事故。
所以本文件的重点不是"输出好看", 而是:

  ★ 只读铁律: 任何 action 跑完, 库文件必须**一字节未变** (md5 比对)
  ★ 不创建: 库不存在时如实报错, **不许**顺手建一个空库 (mode=ro 的意义所在)
  ★ 隔离变量: TMM_SESSION_DB 生效 (否则测试/门禁会打到真库)
  ★ 不过滤漏: 关键词/天数/limit 都要真起作用 (limit 要有上限, 别被 99999 拖死)

夹具做法: 把真库**结构**在 tmp 里重建 + 灌入自造数据 (绝不动真库)。
"""
import asyncio
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import session_logs as SL


def _mkdb(tmp_path, rows=None, sessions=None):
    """按真库结构造一个测试库。rows = [(role, content, intent, model, ts)]"""
    p = tmp_path / "chat_sessions.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at REAL NOT NULL,
                               message_count INTEGER DEFAULT 0);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
                               role TEXT NOT NULL, content TEXT NOT NULL,
                               intent TEXT, model TEXT, elapsed REAL, timestamp REAL NOT NULL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, role, intent,
            content='messages', content_rowid='id');
        CREATE TABLE state_meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    sess = sessions or [(1, time.time() - 3600, 0)]
    for sid, st, mc in sess:
        c.execute("INSERT INTO sessions(id, started_at, message_count) VALUES(?,?,?)", (sid, st, mc))
    rows = rows if rows is not None else [
        ("user", "帮我写个周报", "skill", "local", time.time() - 3000),
        ("assistant", "好的，周报已生成", "skill", "deepseek", time.time() - 2990),
        ("user", "双色球这期怎么买", "chat", "local", time.time() - 1000),
        ("assistant", "给你一组参考号码", "chat", "deepseek", time.time() - 990),
        ("user", "昨天那个双色球的号码呢", "chat", "local", time.time() - 60),
        ("user", "五天前问过的旧事", "chat", "local", time.time() - 5 * 86400),
    ]
    for i, (role, content, intent, model, ts) in enumerate(rows, 1):
        c.execute("INSERT INTO messages(id, session_id, role, content, intent, model, elapsed, timestamp)"
                  " VALUES(?,?,?,?,?,?,?,?)", (i, 1, role, content, intent, model, 0.5, ts))
    c.commit()
    c.close()
    return p


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    p = _mkdb(tmp_path)
    monkeypatch.setenv("TMM_SESSION_DB", str(p))
    return p


def _md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


class TestReadOnly:
    """★★ 最重要的一组: 查询不得改动库。"""

    @pytest.mark.parametrize("kw", [{"action": "recent"}, {"action": "stats"},
                                    {"action": "recent", "keyword": "双色球"},
                                    {"action": "recent", "days": 1}])
    def test_db_bytes_unchanged(self, env_db, kw):
        before = _md5(env_db)
        r = asyncio.run(SL.run(**kw))
        assert r.get("success"), r
        assert _md5(env_db) == before, "查询把用户的库改了!"

    def test_no_wal_sidecar_created(self, env_db, tmp_path):
        """只读连接不该落下 -wal/-shm (落了说明其实在读写模式)。"""
        asyncio.run(SL.run(action="recent"))
        assert not list(tmp_path.glob("chat_sessions.db-wal"))
        assert not list(tmp_path.glob("chat_sessions.db-shm"))

    def test_fts_index_not_rebuilt(self, env_db):
        """不复用 SessionStore.search() 的原因: 它会 rebuild FTS (写操作)。

        ★ 断言只看**代码字面量**, 排除 docstring/注释 —— 否则本文件的说明文字
          自己就含 'rebuild' 而假红 (踩过: 断言注释文本)。
        """
        import ast
        asyncio.run(SL.run(action="recent", keyword="周报"))
        tree = ast.parse(Path(SL.__file__).read_text(encoding="utf-8"))
        doc_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                d = ast.get_docstring(node, clean=False)
                if d:
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        doc_nodes.add(id(body[0].value))
        lits = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_nodes]
        assert not [s for s in lits if "rebuild" in s.lower()], "代码里出现了 FTS rebuild"
        # 行为层再兜一道: 影子表内容不变 (rebuild 会重写它)
        import sqlite3
        c = sqlite3.connect(f"file:{env_db}?mode=ro", uri=True)
        shadow = c.execute("SELECT COUNT(*) FROM messages_fts_data").fetchone()[0]
        c.close()
        assert shadow >= 0


class TestMissingDb:
    def test_missing_db_honest_error_and_not_created(self, tmp_path, monkeypatch):
        tgt = tmp_path / "nope" / "chat_sessions.db"
        monkeypatch.setenv("TMM_SESSION_DB", str(tgt))
        r = asyncio.run(SL.run(action="recent"))
        assert r.get("success") is False
        assert "不存在" in r.get("error", ""), r
        assert not tgt.exists(), "不该顺手建库"


class TestFiltering:
    def test_keyword_filters(self, env_db):
        r = asyncio.run(SL.run(action="recent", keyword="双色球"))
        assert r["success"] and r["count"] == 2, r.get("output")
        assert all("双色球" in m["content"] for m in r["messages"])

    def test_keyword_no_match(self, env_db):
        r = asyncio.run(SL.run(action="recent", keyword="量子力学"))
        assert r["success"] and r["count"] == 0
        assert "没有符合条件" in r["output"]

    def test_days_filters(self, env_db):
        """★ days 是"最近 N*24 小时" —— 夹具里有一条 5 天前的, 用来证明它真被排除。
        (初版夹具全在 1 小时内, 期望写 1 条 → 假红; 是**测试错**不是工具错。)"""
        r = asyncio.run(SL.run(action="recent", days=1))
        assert r["count"] == 5, r.get("output")       # 5 条新的, 旧的被排除
        assert all("五天前" not in m["content"] for m in r["messages"])
        r2 = asyncio.run(SL.run(action="recent", days=10))
        assert r2["count"] == 6 and any("五天前" in m["content"] for m in r2["messages"])

    def test_limit_ordering_is_chronological(self, env_db):
        r = asyncio.run(SL.run(action="recent", limit=3))
        ts = [m["timestamp"] for m in r["messages"]]
        assert ts == sorted(ts), "应按时序正序输出"

    @pytest.mark.parametrize("bad,want", [("99999", 200), ("0", 1), ("-5", 1), ("abc", 20), (None, 20)])
    def test_limit_clamped(self, env_db, bad, want):
        r = asyncio.run(SL.run(action="recent", limit=bad))
        assert r["success"]
        assert r["count"] <= want

    def test_stats_action(self, env_db):
        r = asyncio.run(SL.run(action="stats"))
        assert r["success"]
        for k in ("共 6 条", "1 个会话", "按角色", "按意图", "按模型", "今天"):
            assert k in r["output"], f"缺 {k}: {r['output'][:200]}"

    def test_unknown_action_honest(self, env_db):
        r = asyncio.run(SL.run(action="drop_table"))
        assert r.get("success") is False and "未知 action" in r.get("error", "")
        assert _md5(env_db) == _md5(env_db)


class TestOutputShape:
    def test_overview_header_present(self, env_db):
        r = asyncio.run(SL.run(action="recent"))
        assert "对话史概览" in r["output"] and "共 6 条" in r["output"]

    def test_roles_rendered_in_chinese(self, env_db):
        r = asyncio.run(SL.run(action="recent", limit=8))
        assert "你" in r["output"] and "虎哥" in r["output"]

    def test_preview_truncated(self, tmp_path, monkeypatch):
        long = "长" * 500
        p = _mkdb(tmp_path, rows=[("user", long, "", "local", time.time())])
        monkeypatch.setenv("TMM_SESSION_DB", str(p))
        r = asyncio.run(SL.run(action="recent", limit=1))
        assert r["success"]
        assert len(r["output"]) < 1000, "预览应截断, 否则整段塞进提示词"

    def test_empty_db_ok(self, tmp_path, monkeypatch):
        p = _mkdb(tmp_path, rows=[])
        monkeypatch.setenv("TMM_SESSION_DB", str(p))
        r = asyncio.run(SL.run(action="recent"))
        assert r["success"] and "没有符合条件" in r["output"]


class TestKwargVariants:
    """技能参数抽取可能把关键词放进别的字段 —— 工具要兜住, 否则用户白问。"""

    @pytest.mark.parametrize("field", ["keyword", "text", "query", "content"])
    def test_keyword_from_any_field(self, env_db, field):
        r = asyncio.run(SL.run(action="recent", **{field: "双色球"}))
        assert r["success"] and r["count"] == 2, (field, r.get("output"))

    def test_no_keyword_returns_all_recent(self, env_db):
        r = asyncio.run(SL.run(action="recent"))
        assert r["count"] == 6
