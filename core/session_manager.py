"""
Tiger.M.M Session Manager - background task multiplexer
Distilled from herdr (ogulcancelik/herdr) terminal agent multiplexer
"""
import asyncio, json, os, sqlite3, time, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_KILLED = "killed"
OUTPUT_DIR = Path(__file__).parent.parent / "data" / "sessions"


@dataclass
class Session:
    id: str
    task: str
    model: str
    status: str = STATUS_IDLE
    created_at: float = 0.0
    finished_at: Optional[float] = None
    response: str = ""
    intent: str = ""
    elapsed: float = 0.0
    error: str = ""

    def to_dict(self):
        elapsed = self.elapsed
        if self.status == STATUS_RUNNING:
            elapsed = round(time.time() - self.created_at, 1)
        return {
            "id": self.id, "task": self.task[:100], "model": self.model,
            "status": self.status, "created_at": self.created_at,
            "elapsed": elapsed, "intent": self.intent,
            "error": self.error[:200] if self.error else "",
            "response_preview": self.response[:200] if self.response else "",
        }


class SessionManager:

    def __init__(self, pipeline=None, db_path=None):
        self.pipeline = pipeline
        self._sessions = {}
        self._tasks = {}
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "sessions.db"
        self.db_path = str(db_path)
        self._init_db()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            expected_cols = {'id','task','model','status','created_at',
                           'finished_at','response','intent','elapsed','error'}
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
            if cur.fetchone():
                existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
                if existing != expected_cols:
                    conn.execute("DROP TABLE sessions")
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                task TEXT,
                model TEXT DEFAULT 'auto',
                status TEXT DEFAULT 'idle',
                created_at REAL,
                finished_at REAL,
                response TEXT DEFAULT '',
                intent TEXT DEFAULT '',
                elapsed REAL DEFAULT 0,
                error TEXT DEFAULT ''
            )""")
            conn.commit()
        finally:
            conn.close()

    def _save(self, session):
        for attempt in range(3):
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO sessions
                       (id,task,model,status,created_at,finished_at,response,intent,elapsed,error)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (session.id, session.task, session.model, session.status,
                     session.created_at, session.finished_at, session.response,
                     session.intent, session.elapsed, session.error))
                conn.commit()
                return
            except Exception:
                if attempt < 2:
                    import time as _t
                    _t.sleep(0.05 * (attempt + 1))
                else:
                    raise
            finally:
                conn.close()


    def spawn(self, task, model="auto", background=False):
        """登记一个会话。★ background=False 是**纯跟踪**(process() 每次都会调它)。

        2026-09-19 深测修 (真缺陷): 原实现**无条件** `_save(session)`, 但只有
        `_run_session` (background=True 才有) 会把它 finalize —— 于是非后台会话
        永远停在 status="running"。实测 `data/sessions.db` 已被这种行堆到 **11864 条**,
        且每条用户消息 (含 probe 探针流量 —— 本该零副作用) 都在往上加。
        库里这些行还**没有任何读取方** (`_load` 不存在, `list_sessions` 读内存) → 纯垃圾。
        修法: 只有**会被收尾**的会话才落库; 纯跟踪会话只留内存 (stats/list 照常可查)。
        """
        session_id = uuid.uuid4().hex[:8]
        session = Session(id=session_id, task=task, model=model,
                         status=STATUS_RUNNING, created_at=time.time())
        self._sessions[session_id] = session
        if background:
            self._save(session)
            if self.pipeline is not None:
                t = asyncio.create_task(self._run_session(session_id))
                self._tasks[session_id] = t
        return session_id

    async def _run_session(self, session_id):
        session = self._sessions.get(session_id)
        if session is None or self.pipeline is None:
            return
        t0 = time.time()
        try:
            result = await self.pipeline.process(session.task, session.model)
            elapsed = time.time() - t0
            session.status = STATUS_DONE
            session.elapsed = elapsed
            session.finished_at = time.time()
            if isinstance(result, dict):
                session.response = result.get("response", "")
                session.intent = result.get("intent", "")
            else:
                session.response = str(result)
        except Exception as e:
            elapsed = time.time() - t0
            session.status = STATUS_ERROR
            session.elapsed = elapsed
            session.finished_at = time.time()
            session.error = str(e)[:500]
        self._save(session)
        out_path = OUTPUT_DIR / (session_id + ".txt")
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("Task: " + str(session.task) + "\n")
                fh.write("Model: " + str(session.model) + "\n")
                fh.write("Status: " + str(session.status) + "\n")
                fh.write("Elapsed: " + str(round(session.elapsed, 1)) + "s\n")
                fh.write("=" * 50 + "\n")
                if session.response:
                    fh.write(session.response)
                if session.error:
                    fh.write("\nERROR: " + str(session.error))
        except Exception:
            pass
        self._tasks.pop(session_id, None)


    def get(self, session_id):
        return self._sessions.get(session_id)

    def list_sessions(self, status=None, limit=20):
        sessions = sorted(self._sessions.values(),
                         key=lambda s: s.created_at, reverse=True)
        if status:
            sessions = [s for s in sessions if s.status == status]
        return [s.to_dict() for s in sessions[:limit]]

    def read_output(self, session_id):
        out_path = OUTPUT_DIR / (session_id + ".txt")
        if out_path.exists():
            return out_path.read_text(encoding="utf-8")
        session = self._sessions.get(session_id)
        if session and session.response:
            return session.response
        return None

    def read_output_tail(self, session_id, lines=50):
        text = self.read_output(session_id)
        if text is None:
            return None
        all_lines = text.split("\n")
        return "\n".join(all_lines[-lines:])

    def kill(self, session_id):
        session = self._sessions.get(session_id)
        if session is None or session.status != STATUS_RUNNING:
            return False
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
        session.status = STATUS_KILLED
        session.finished_at = time.time()
        self._save(session)
        return True

    def running_count(self):
        return sum(1 for s in self._sessions.values()
                   if s.status == STATUS_RUNNING)

    def stats(self):
        total = len(self._sessions)
        running = sum(1 for s in self._sessions.values()
                     if s.status == STATUS_RUNNING)
        done = sum(1 for s in self._sessions.values()
                  if s.status == STATUS_DONE)
        errors = sum(1 for s in self._sessions.values()
                    if s.status == STATUS_ERROR)
        return {"total": total, "running": running,
                "done": done, "errors": errors}


_session_manager = None

def get_session_manager(pipeline=None):
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(pipeline)
    elif pipeline is not None and _session_manager.pipeline is None:
        _session_manager.pipeline = pipeline
    return _session_manager
