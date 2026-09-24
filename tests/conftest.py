"""
Tiger.M.M test fixtures.
Run: cd mary3 && python -m pytest tests/ -v
"""
import sys, os, tempfile, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def temp_db():
    """Temporary SQLite database path, auto-cleaned."""
    db = os.path.join(tempfile.gettempdir(), f"tmm_test_{os.getpid()}.db")
    yield db
    try:
        os.unlink(db)
    except OSError:
        pass


@pytest.fixture
def inbox(temp_db):
    """Fresh InboxStore for testing."""
    from core.inbox import InboxStore
    return InboxStore(db_path=temp_db)


@pytest.fixture
def session_mgr(temp_db):
    """Fresh SessionManager for testing (no pipeline)."""
    from core.session_manager import SessionManager
    return SessionManager(db_path=temp_db)


@pytest.fixture
def temp_dir():
    """Temporary directory for test files."""
    import tempfile, shutil
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# 用户状态守卫 (会话级)
#   实测: 有些测试会调 pipeline.process(probe=False) → 真去写 data/perception.json。
#   这里在整轮 pytest 开始前快照、结束后还原 (字节级), 保证 canonical 不留痕。
#   为什么放 conftest: 集中一处兜底, 不必给每个测试加还原代码。
# ─────────────────────────────────────────────────────────────
import pytest as _pytest  # noqa: E402
from pathlib import Path as _Path  # noqa: E402  (独立自足, 不依赖 conftest 顶部导入)


def _state_snapshot():
    import sqlite3
    root = _Path(__file__).resolve().parent.parent
    snaps = {}
    for rel in ("data/mode.json", "data/prefs.json", "data/perception.json"):
        f = root / rel
        snaps[rel] = f.read_bytes() if f.exists() else None
    db = root / "data/learned_rules.db"
    try:
        c = sqlite3.connect(str(db))
        try:
            snaps["rules"] = c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        finally:
            c.close()
    except Exception:
        snaps["rules"] = None
    # ★ 2026-09-19: chat_sessions.db 也要守 —— process() 现在统一落库, 而
    #   tests/test_nl_execution.py / test_nl_ollama.py 会调 process(probe=False)
    #   → 不守的话它们会把测试消息写进**用户的真实对话历史**。
    sess = root / "data/chat_sessions.db"
    for ext in ("", "-wal", "-shm"):
        f = _Path(str(sess) + ext)
        snaps[f"data/chat_sessions.db{ext}"] = f.read_bytes() if f.exists() else None
    try:
        c = sqlite3.connect(f"file:{sess}?mode=ro", uri=True)
        try:
            snaps["chat_max_id"] = c.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
            snaps["chat_n_sessions"] = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            c.close()
    except Exception:
        snaps["chat_max_id"] = None
        snaps["chat_n_sessions"] = None
    return snaps


def _state_restore(snaps):
    import sqlite3
    root = _Path(__file__).resolve().parent.parent
    for rel, data in snaps.items():
        if rel in ("rules", "chat_max_id", "chat_n_sessions"):
            continue
        f = root / rel
        if data is None:
            if f.exists():
                f.unlink()
        else:
            f.write_bytes(data)
    # chat_sessions.db 二次校验: 字节还原可能被"还没关掉的 sqlite 连接"回写覆盖
    # (与 perception 那次同一个坑) → 用行号兜底把超出的行删掉, 并重建 FTS。
    mx = snaps.get("chat_max_id")
    if mx is not None:
        try:
            c = sqlite3.connect(str(root / "data/chat_sessions.db"))
            try:
                cur = c.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
                n_sess_extra = 0
                if snaps.get("chat_n_sessions") is not None:
                    n_sess_extra = c.execute("SELECT COUNT(*) FROM sessions WHERE id > ?",
                                             (snaps["chat_n_sessions"],)).fetchone()[0]
                extra = c.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (mx,)).fetchone()[0]
                if extra or n_sess_extra:      # ★ 只在真被写脏时才动文件 (清洁时零字节改动)
                    c.execute("DELETE FROM messages WHERE id > ?", (mx,))
                    c.execute("UPDATE sessions SET message_count = MAX(0, message_count - ?) WHERE id = 1", (extra,))
                n_sess = snaps.get("chat_n_sessions")
                if n_sess is not None:
                    c.execute("DELETE FROM sessions WHERE id > ?", (n_sess,))
                c.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                c.commit()
            finally:
                c.close()
        except Exception:
            pass
    if snaps.get("rules") == 0:
        db = root / "data/learned_rules.db"
        try:
            c = sqlite3.connect(str(db))
            try:
                c.execute("DELETE FROM rules")
                c.commit()
            finally:
                c.close()
        except Exception:
            pass


@pytest.fixture(scope="session", autouse=True)
def _guard_user_state(tmp_path_factory):
    """整轮测试的"用户数据不落痕"守卫。

    ★ 两条措施 (实测才够):
      ① **重定向持久化目标** —— core.perception.PERCEPTION_FILE 指向临时文件。
         为什么不能只靠"事后还原字节": 应用/引擎的 atexit 会在 pytest teardown
         **之后**再写一次, 把还原覆盖掉 (实测: stats.analyzed 3508→3510 就是它写的)。
      ② 快照 + 还原其余状态文件 (mode/prefs) 与规则库行数, 兜底。
    """
    import core.perception as _perc
    tmpdir = tmp_path_factory.mktemp("_user_state")
    tmp = tmpdir / "perception.json"
    _real = _perc.PERCEPTION_FILE
    snaps = _state_snapshot()
    _perc.PERCEPTION_FILE = tmp          # ① 重定向
    # ★ ③ 同理重定向对话库: process() 现在统一落库, 而两个 NL 测试会调
    #   process(probe=False) → 不重定向的话测试消息会真的写进**用户的对话历史**
    #   (实测: 行数守卫能挡住, 但库文件字节会被改; 重定向后一个字节都不动)
    import storage.session_store as _ss
    _real_db = (_Path(__file__).resolve().parent.parent / "data" / "chat_sessions.db")
    _tmp_db = tmpdir / "chat_sessions.db"
    _orig_init = _ss.SessionStore.__init__

    def _redirect_init(self, db_path=None):
        try:
            if db_path is None or _Path(db_path).resolve() == _real_db:
                db_path = _tmp_db
        except Exception:
            pass
        return _orig_init(self, db_path)

    _ss.SessionStore.__init__ = _redirect_init
    # ★ 能力缺口台账同理: process() 现在会记账 (含用户原话), 测试跑真 pipeline
    #   不该写进用户的账。两层: 环境变量 (给 get_gap_ledger(DATA_DIR) 走的路)
    #   + 单例重置 (防它在此之前已被建好并缓存了真实路径)。
    import core.gap_ledger as _gl
    _real_gap = (_Path(__file__).resolve().parent.parent / "data" / "gap_ledger.db")
    _tmp_gap = tmpdir / "gap_ledger.db"
    _gap_env_old = os.environ.get("TMM_GAP_DB")
    _gap_sess_old = os.environ.get("TMM_SESSION_DB")
    os.environ["TMM_GAP_DB"] = str(_tmp_gap)
    os.environ["TMM_SESSION_DB"] = str(_tmp_db)
    _gl.reset_gap_ledger()
    yield
    _gl.reset_gap_ledger()
    if _gap_env_old is None:
        os.environ.pop("TMM_GAP_DB", None)
    else:
        os.environ["TMM_GAP_DB"] = _gap_env_old
    if _gap_sess_old is None:
        os.environ.pop("TMM_SESSION_DB", None)
    else:
        os.environ["TMM_SESSION_DB"] = _gap_sess_old
    _ss.SessionStore.__init__ = _orig_init
    _perc.PERCEPTION_FILE = _real
    try:
        _state_restore(snaps)            # ② 兜底还原
    except Exception:
        pass
