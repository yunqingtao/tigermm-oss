"""对话落库 (chat_sessions.db) 的测试 —— 2026-09-19 实测确认的真缺陷。

背景 (四段实测证据):
  ① Web/桌面端发消息 → chat_sessions.db **Δ0** 行, 但 /history(内存 deque) Δ2 条
  ② 重启引擎 → /history 从 2 条变 0 条 (历史丢了)
  ③ 同一套 SessionStore 按 run_cli 的调用方式写 → 落库 ✓ (说明不是库的问题)
  ④ 全库统计: sessions.add_message 只在 CLI 两处被调用, process() 一处都没有

根因: process()(Web/桌面/relay/scheduler 全部入口) 只写内存 TigerMemory,
      写库只发生在 CLI 的循环里 → 非 CLI 入口的对话从不落库。

修法: process() 收尾统一落库 (_persist_turn); 删掉 CLI 的重复写库(否则双写)。
本测试守的就是这条不变量: **任何入口跑一轮 process = 恰好 2 行 (user+assistant)**。
"""
import asyncio
import sqlite3
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ───────────────────────── fixture ─────────────────────────

@pytest.fixture(scope="module")
def PL(tmp_path_factory):
    """造一个真实 pipeline, 但把落库目标换成**临时库** (零污染用户历史)。

    同时隔离 MCP 后台线程 (真子进程会留下 asyncio 传输警告, 弄红 verify_mcp_reap)。
    """
    tmp = tmp_path_factory.mktemp("sess_persist")
    import core.perception as _p
    _p.PERCEPTION_FILE = tmp / "perc.json"

    import core.mcp_client as _mc
    stub = types.SimpleNamespace(load_config=lambda *a, **k: None,
                                 list_tools=lambda *a, **k: [], tools=[],
                                 get_tool=lambda *a, **k: None)
    orig = _mc.get_mcp_client
    _mc.get_mcp_client = lambda *a, **k: stub
    try:
        from main import _load_config
        from core.knowledge import KnowledgeEngine
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from config.settings import DATA_DIR
        from storage.session_store import SessionStore
        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl._ke = KnowledgeEngine(DATA_DIR)
        pl.sessions = SessionStore(tmp / "chat_sessions.db")   # ★ 落库目标 = 临时库
        yield pl
    finally:
        _mc.get_mcp_client = orig


def run(PL, msg, **kw):
    return asyncio.run(PL.process(msg, **kw))


def rows(PL):
    c = PL.sessions.conn
    return c.execute("SELECT role, content, intent, model FROM messages ORDER BY id").fetchall()


def n_rows(PL):
    return PL.sessions.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


# ───────────────────── A. 核心: 落库真的发生了 ─────────────────────

class TestPersistHappens:
    """★ 修前: 跑 process 后库里 0 行 (这个测试会红)。修后: 2 行。"""

    def test_local_command_gets_persisted(self, PL):
        before = n_rows(PL)
        r = run(PL, "/stats", mode_override="craft")
        assert isinstance(r, dict) and r.get("response"), r
        got = rows(PL)[before:]
        assert len(got) == 2, f"一轮对话应落 2 行 (user+assistant), 实际 {got}"
        assert got[0][0] == "user" and got[0][1] == "/stats"
        assert got[1][0] == "assistant" and got[1][1] == r["response"]

    def test_intent_and_model_recorded(self, PL):
        """result 里的 intent/model 要写进去 (与 CLI 老口径一致)。"""
        before = n_rows(PL)
        r = run(PL, "/stats", mode_override="craft")
        got = rows(PL)[before:]
        assert got[0][2] == r.get("intent", "general")
        assert got[0][3] == r.get("model", "local")

    def test_two_rounds_two_pairs(self, PL):
        """连续两轮 → 恰好 4 行 (不多不少)。防"双写"回归。"""
        before = n_rows(PL)
        run(PL, "/stats", mode_override="craft")
        run(PL, "/mode", mode_override="craft")
        assert n_rows(PL) - before == 4, rows(PL)[before:]

    def test_history_survives_new_process_object(self, PL, tmp_path):
        """★ 用户最在意的: 重启(新进程/新实例)后历史还在。

        同一 DB 路径重新开一个 SessionStore → 应读得到刚才那几轮。
        """
        run(PL, "/stats", mode_override="craft")
        from storage.session_store import SessionStore
        fresh = SessionStore(Path(PL.sessions.conn.execute("PRAGMA database_list").fetchone()[2]))
        hist = fresh.recent(20)
        assert any(x.get("content") == "/stats" for x in hist), hist


# ───────────────────── B. probe 必须零写入 ─────────────────────

class TestProbeGuard:
    """probe=True 是"验证/探针流量"的守卫 —— 绝不能写用户历史。"""

    def test_probe_writes_nothing(self, PL):
        before = n_rows(PL)
        run(PL, "/stats", probe=True, mode_override="craft")
        assert n_rows(PL) == before, rows(PL)[before:]

    def test_probe_false_writes(self, PL):
        before = n_rows(PL)
        run(PL, "/stats", probe=False, mode_override="craft")
        assert n_rows(PL) - before == 2


# ───────────────────── C. 唯一入口: 内部自调用不写库 ─────────────────────

class TestSingleEntryPoint:
    """★ plan→craft 会**再次**调用处理逻辑 (同一条消息跑两遍)。
    如果内部自调用走的是包装后的 process(), 同一条消息就会入库两次。
    所以内部自调用必须走 _process_impl()。
    """

    def test_impl_alone_persists_nothing(self, PL):
        before = n_rows(PL)
        r = asyncio.run(PL._process_impl("/stats", mode_override="craft"))
        assert isinstance(r, dict) and r.get("response")
        assert n_rows(PL) == before, "内部实现不该碰库 (否则 plan 场景双写)"

    def test_run_plan_internal_call_does_not_persist(self, PL):
        """★ 行为级: plan 执行方案时会把"最初那条原话"用实干路径**再跑一遍**
        (core/pipeline.py 的 _run_plan → 自调用)。这次内部调用绝不能直接落库,
        否则同一句用户消息会入库两次。
        实测: 把内部调用从 _process_impl 改成 process → 这条测试变红。
        """
        import time as _t
        before = n_rows(PL)
        r = asyncio.run(PL._run_plan({"steps": [], "message": "/stats"}, None, _t.time(),
                                     probe=False))
        assert isinstance(r, dict) and r.get("response"), r
        assert n_rows(PL) == before, ("plan 的内部自调用落库了 → 真实流程会双写: "
                                      + str(rows(PL)[before:]))

    def test_persist_turn_called_exactly_once_per_process(self, PL):
        """spy: 跑一轮 process → _persist_turn 恰好 1 次 (双写回归守卫)。"""
        calls = []
        real = PL._persist_turn
        PL._persist_turn = lambda m, r: (calls.append(m), real(m, r))[0]
        try:
            run(PL, "/stats", mode_override="craft")
        finally:
            PL._persist_turn = real
        assert len(calls) == 1, f"_persist_turn 被调 {len(calls)} 次 (应为 1): {calls}"

    def test_only_persist_turn_touches_db(self):
        """源码级: 全项目只有 _persist_turn 调 sessions.add_message。

        (行为级已由上面几条覆盖; 这条是防止有人又往别的入口插一处写库)
        """
        root = Path(__file__).resolve().parent.parent
        hits = []
        for f in list(root.glob("*.py")) + list(root.glob("core/*.py")):
            t = f.read_text(encoding="utf-8", errors="replace")
            for i, ln in enumerate(t.split("\n"), 1):
                if ".add_message(" in ln:
                    hits.append(f"{f.name}:{i}")
        assert len(hits) == 2, f"add_message 只该在 _persist_turn 里出现 2 次, 实际 {hits}"


# ───────────────────── D. 写库失败不能影响对话 ─────────────────────

class TestFailureIsolated:
    def test_broken_sessions_does_not_break_chat(self, PL):
        """落库炸了, 对话本身必须照样返回 (CLI 原来就是 try/except 口径)。"""
        class Boom:
            def add_message(self, *a, **k):
                raise RuntimeError("db is on fire")
        real = PL.sessions
        PL.sessions = Boom()
        try:
            r = run(PL, "/stats", mode_override="craft")
            assert isinstance(r, dict) and r.get("response"), r
        finally:
            PL.sessions = real

    def test_none_result_tolerated(self, PL):
        """防御: result 不是 dict 时 _persist_turn 不能崩。"""
        PL._persist_turn("/stats", None)          # 不抛即通过
        PL._persist_turn("/stats", "raw string")

# ───────── E. /history 的读路径: 必须跨会话、从库里读 ─────────

class TestHistoryReadsDb:
    """★ 第二轮实测挖出的"修一半": 落库修好了, 但界面 /history 仍读内存
    (pipeline.memory.recent) 且 SessionStore.recent() 只认 current_session_id
    → 重启后界面照样空。本组守"读"这条路。
    """

    def test_recent_all_spans_sessions(self, tmp_path):
        """跨会话: 两个不同 session 的消息都要读得到 (重启后新会话也能看到旧的)。"""
        from storage.session_store import SessionStore
        s = SessionStore(tmp_path / "x.db")
        s.add_message("user", "第一会话的问题")
        s.add_message("assistant", "第一会话的回答")
        s.conn.execute("INSERT INTO sessions (started_at) VALUES (1.0)")   # 模拟"重启"
        s.conn.commit()
        s.start_session()                                                   # 新会话
        s.add_message("user", "第二会话的问题")
        got = s.recent_all(10)
        texts = [x["content"] for x in got]
        assert "第一会话的问题" in texts and "第二会话的问题" in texts, texts

    def test_recent_all_returns_role_and_content(self, tmp_path):
        from storage.session_store import SessionStore
        s = SessionStore(tmp_path / "x.db")
        s.add_message("user", "问", "general", "local", 1.0)
        s.add_message("assistant", "答", "general", "local", 1.0)
        got = s.recent_all(10)
        assert [x["role"] for x in got] == ["user", "assistant"]
        assert got[0]["content"] == "问" and got[1]["content"] == "答"

    def test_recent_all_limit_and_order(self, tmp_path):
        from storage.session_store import SessionStore
        s = SessionStore(tmp_path / "x.db")
        for i in range(6):
            s.add_message("user", f"m{i}")
        got = s.recent_all(3)
        assert [x["content"] for x in got] == ["m3", "m4", "m5"], got   # 最近 3 条, 按时间正序

    def test_recent_all_empty_db(self, tmp_path):
        from storage.session_store import SessionStore
        s = SessionStore(tmp_path / "x.db")
        assert s.recent_all(10) == []

    def test_history_endpoint_maps_content_to_text(self):
        """前端要 {role, text}; 库里是 content → /history 必须做映射。

        (源码级 + 语义级: 直接断言 endpoint 的映射表达式存在, 并模拟一次映射)
        """
        src = (Path(__file__).resolve().parent.parent / "web_server.py").read_text(
            encoding="utf-8", errors="replace")
        assert "recent_all(50)" in src, "web /history 必须读库 (recent_all)"
        assert '{"role": t.get("role", ""), "text": t.get("content", "")}' in src or \
               '"text": t.get("content"' in src, "缺少 content→text 映射"
        # 语义: 按同样规则映射一份库记录 → 前端拿到的 key 正确
        db_row = {"role": "user", "content": "库里的话"}
        mapped = {"role": db_row.get("role", ""), "text": db_row.get("content", "")}
        assert mapped == {"role": "user", "text": "库里的话"}

# ───────── F. 跨线程 (Web 服务的真实处境) ─────────

class TestCrossThread:
    """★ 2026-09-19 实测抓到的真 bug: sqlite3 默认 check_same_thread=True, 而 uvicorn 把
    同步 endpoint 丢进线程池执行 → SessionStore 连接跨线程被拒:
        "SQLite objects created in a thread can only be used in that same thread"
    后果是 /history 读库静默退回内存、_persist_turn 静默不落库 (err.log 里能看到)。
    修法: check_same_thread=False + RLock 串行化。本组守住它 (去掉修复必红)。
    """

    def test_writes_from_many_threads_all_land(self, tmp_path):
        import threading
        from storage.session_store import SessionStore
        s = SessionStore(tmp_path / "ct.db")
        errs = []

        def work(i):
            try:
                s.add_message("user", f"线程{i}")
                s.add_message("assistant", f"回复{i}")
            except Exception as e:               # 跨线程被拒就在这里炸
                errs.append(repr(e))

        ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs, f"跨线程写入抛异常: {errs}"
        assert s.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 12

    def test_read_from_another_thread(self, tmp_path):
        """读也要能跨线程 (web_server.history 在线程池里跑)。"""
        import threading
        from storage.session_store import SessionStore
        s = SessionStore(tmp_path / "rt.db")
        s.add_message("user", "主线程写的")
        got, errs = [], []

        def reader():
            try:
                got.extend(x["content"] for x in s.recent_all(10))
            except Exception as e:
                errs.append(repr(e))

        t = threading.Thread(target=reader); t.start(); t.join()
        assert not errs, f"跨线程读抛异常: {errs}"
        assert "主线程写的" in got

# ───────── G. 门禁隔离: TMM_SESSION_DB 重定向语义 ─────────

class TestSessionDbRedirect:
    """★ 门禁跑"走真实路径"的验证器时, 靠 TMM_SESSION_DB 把对话库重定向到临时文件,
    否则测试消息会写进用户的真实历史 (实测两轮门禁 +32 / +16 行)。

    语义必须精确 (两轮实测才定下来):
      ① 不传 db_path (默认取值)          → 重定向
      ② 传的**就是**真实对话库 (pipeline 的写法 SessionStore(DATA_DIR/"chat_sessions.db"))
                                          → 重定向   ← 漏了这条就漏 16 行
      ③ 传别的路径 (测试自己的临时库)      → **不动** (否则套件互相串数据)
    """

    REAL = Path(__file__).resolve().parent.parent / "data" / "chat_sessions.db"

    def _mk(self, db_path, tmp_path, monkeypatch):
        # ★ 直测纯函数 resolve_db_path —— 不走 SessionStore.__init__, 因为 tests/conftest.py
        #   的会话级守卫也在 __init__ 上做了重定向 (两层隔离会互相干扰, 实测踩到)。
        from storage.session_store import resolve_db_path
        monkeypatch.setenv("TMM_SESSION_DB", str(tmp_path / "gate.db"))
        return Path(resolve_db_path(db_path)).resolve()

    def test_default_path_is_redirected(self, tmp_path, monkeypatch):
        assert self._mk(None, tmp_path, monkeypatch) == (tmp_path / "gate.db").resolve()

    def test_explicit_real_path_is_redirected(self, tmp_path, monkeypatch):
        assert self._mk(self.REAL, tmp_path, monkeypatch) == (tmp_path / "gate.db").resolve()

    def test_explicit_other_path_is_untouched(self, tmp_path, monkeypatch):
        own = (tmp_path / "own.db").resolve()
        assert self._mk(own, tmp_path, monkeypatch) == own

    def test_no_env_keeps_real_path(self, tmp_path, monkeypatch):
        from storage.session_store import resolve_db_path
        monkeypatch.delenv("TMM_SESSION_DB", raising=False)
        assert Path(resolve_db_path(self.REAL)).resolve() == self.REAL.resolve()
        assert Path(resolve_db_path()).resolve() == self.REAL.resolve()
