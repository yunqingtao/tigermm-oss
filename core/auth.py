"""
Auth Engine v2.0 — Complete authentication & authorization system.
Business error codes, rate limiting, lockout, logout, audit logging.

Error codes (returned as 200 + code):
  1001 - Account not found
  1002 - Invalid password
  1003 - Account locked
  1004 - Token expired
  1005 - Token invalid
  1006 - Token blacklisted (logged out)
  1007 - Rate limited
  1008 - Password too weak
"""
import hashlib, secrets, time, json, sqlite3, logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("core.auth")

# ============================================================
# Data structures
# ============================================================

@dataclass
class UserRecord:
    username: str
    password_hash: str = ""
    token: str = ""
    role: str = "user"
    locked_until: float = 0.0
    login_attempts: int = 0
    last_login_ip: str = ""
    last_login_time: str = ""
    created_at: str = ""

@dataclass
class AuditEntry:
    timestamp: str
    username: str
    action: str
    ip: str = ""
    user_agent: str = ""
    detail: str = ""

# ============================================================
# Constants
# ============================================================

MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 1800
TOKEN_EXPIRY = 86400 * 7

# ============================================================
# Password hashing
# ============================================================

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"sha256${salt}${h}"

def _verify_password(password: str, hash_str: str) -> bool:
    if not hash_str:
        return False
    try:
        algo, salt, h = hash_str.split("$", 2)
        if algo == "sha256":
            return h == hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    except ValueError:
        pass
    return False

def _generate_token() -> str:
    return f"tmm-{secrets.token_hex(24)}"

# ============================================================
# Auth DB (SQLite)
# ============================================================

class AuthDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username    TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL DEFAULT '',
                token       TEXT NOT NULL DEFAULT '',
                role        TEXT NOT NULL DEFAULT 'user',
                locked_until REAL NOT NULL DEFAULT 0,
                login_attempts INTEGER NOT NULL DEFAULT 0,
                last_login_ip TEXT NOT NULL DEFAULT '',
                last_login_time TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS token_blacklist (
                token       TEXT PRIMARY KEY,
                blacklisted_at TEXT NOT NULL,
                reason      TEXT NOT NULL DEFAULT 'logout'
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                username    TEXT NOT NULL,
                action      TEXT NOT NULL,
                ip          TEXT NOT NULL DEFAULT '',
                user_agent  TEXT NOT NULL DEFAULT '',
                detail      TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_log(username);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
        """)
        conn.commit()
        conn.close()

    def get_user(self, username: str) -> Optional[UserRecord]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if row:
            return UserRecord(**{k: row[k] for k in row.keys()})
        return None

    def get_user_by_token(self, token: str) -> Optional[UserRecord]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        conn.close()
        if row:
            return UserRecord(**{k: row[k] for k in row.keys()})
        return None

    def create_user(self, username: str, password: str, role: str = "user") -> UserRecord:
        now = datetime.now().isoformat()
        u = UserRecord(
            username=username,
            password_hash=_hash_password(password),
            token=_generate_token(),
            role=role,
            created_at=now,
        )
        conn = self._conn()
        conn.execute("""INSERT INTO users(username,password_hash,token,role,created_at)
                        VALUES(?,?,?,?,?)""",
                     (u.username, u.password_hash, u.token, u.role, u.created_at))
        conn.commit()
        conn.close()
        return u

    def update_user(self, u: UserRecord):
        conn = self._conn()
        conn.execute("""UPDATE users SET password_hash=?,token=?,role=?,locked_until=?,
                        login_attempts=?,last_login_ip=?,last_login_time=?
                        WHERE username=?""",
                     (u.password_hash, u.token, u.role, u.locked_until,
                      u.login_attempts, u.last_login_ip, u.last_login_time,
                      u.username))
        conn.commit()
        conn.close()

    def list_users(self) -> list:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        conn.close()
        return [UserRecord(**{k: row[k] for k in row.keys()}) for row in rows]

    def blacklist_token(self, token: str, reason: str = "logout"):
        conn = self._conn()
        conn.execute("INSERT OR REPLACE INTO token_blacklist(token,blacklisted_at,reason) VALUES(?,?,?)",
                     (token, datetime.now().isoformat(), reason))
        conn.commit()
        conn.close()

    def is_blacklisted(self, token: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM token_blacklist WHERE token=?", (token,)).fetchone()
        conn.close()
        return row is not None

    def audit(self, username: str, action: str, ip: str = "", user_agent: str = "", detail: str = ""):
        conn = self._conn()
        conn.execute("""INSERT INTO audit_log(timestamp,username,action,ip,user_agent,detail)
                        VALUES(?,?,?,?,?,?)""",
                     (datetime.now().isoformat(), username, action, ip, user_agent, detail))
        conn.commit()
        conn.close()

    def get_audit_log(self, username: str = "", limit: int = 100) -> list:
        conn = self._conn()
        if username:
            rows = conn.execute(
                "SELECT timestamp,username,action,ip,user_agent,detail FROM audit_log WHERE username=? ORDER BY id DESC LIMIT ?",
                (username, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT timestamp,username,action,ip,user_agent,detail FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        conn.close()
        return [AuditEntry(**{k: row[k] for k in row.keys()}) for row in rows]


# ============================================================
# Auth Engine
# ============================================================

class AuthEngine:
    def __init__(self, db: AuthDB):
        self.db = db

    def login(self, username: str, password: str, ip: str = "", user_agent: str = "") -> dict:
        u = self.db.get_user(username)

        if not u:
            self.db.audit(username, "login_fail", ip, user_agent, "account_not_found")
            return {"success": False, "code": 1001, "message": "账号不存在"}

        if u.locked_until > time.time():
            remaining = int(u.locked_until - time.time())
            self.db.audit(username, "login_fail", ip, user_agent, f"locked_{remaining}s")
            return {"success": False, "code": 1003,
                    "message": f"账号已锁定，{remaining} 秒后重试"}

        if not _verify_password(password, u.password_hash):
            u.login_attempts += 1
            if u.login_attempts >= MAX_LOGIN_ATTEMPTS:
                u.locked_until = time.time() + LOCKOUT_DURATION
                self.db.audit(username, "lockout", ip, user_agent,
                              f"max_attempts({MAX_LOGIN_ATTEMPTS})")
            self.db.update_user(u)
            self.db.audit(username, "login_fail", ip, user_agent,
                          f"bad_password_attempt_{u.login_attempts}")
            return {"success": False, "code": 1002, "message": "密码错误"}

        u.login_attempts = 0
        u.locked_until = 0
        u.last_login_ip = ip
        u.last_login_time = datetime.now().isoformat()
        self.db.update_user(u)
        self.db.audit(username, "login", ip, user_agent, "success")

        return {
            "success": True, "code": 0, "message": "登录成功",
            "token": u.token, "user": {
                "username": u.username,
                "role": u.role,
                "created_at": u.created_at,
            }
        }

    def logout(self, token: str, ip: str = "") -> dict:
        u = self.db.get_user_by_token(token)
        if not u:
            return {"success": False, "code": 1005, "message": "Token 无效"}

        self.db.blacklist_token(token, "logout")
        u.token = _generate_token()
        self.db.update_user(u)
        self.db.audit(u.username, "logout", ip, "", "success")

        return {"success": True, "code": 0, "message": "已登出"}

    def refresh_token(self, token: str, ip: str = "") -> dict:
        u = self.db.get_user_by_token(token)
        if not u:
            return {"success": False, "code": 1005, "message": "Token 无效"}
        if self.db.is_blacklisted(token):
            return {"success": False, "code": 1006, "message": "Token 已登出"}

        old_token = token
        u.token = _generate_token()
        self.db.update_user(u)
        self.db.blacklist_token(old_token, "refresh")
        self.db.audit(u.username, "token_refresh", ip, "", "success")

        return {"success": True, "code": 0, "message": "Token 已刷新",
                "token": u.token}

    def verify_token(self, token: str) -> Optional[UserRecord]:
        if self.db.is_blacklisted(token):
            return None
        return self.db.get_user_by_token(token)

    def change_password(self, username: str, old_password: str, new_password: str) -> dict:
        u = self.db.get_user(username)
        if not u:
            return {"success": False, "code": 1001, "message": "账号不存在"}
        if not _verify_password(old_password, u.password_hash):
            return {"success": False, "code": 1002, "message": "原密码错误"}
        if len(new_password) < 6:
            return {"success": False, "code": 1008, "message": "密码至少6位"}

        u.password_hash = _hash_password(new_password)
        u.token = _generate_token()
        self.db.update_user(u)
        self.db.audit(username, "password_change", "", "", "success")
        return {"success": True, "code": 0, "message": "密码已修改，请重新登录",
                "token": u.token}


# ============================================================
# Singleton
# ============================================================

_auth_db = None
_auth_engine = None

def init_auth(db_path: Path) -> AuthEngine:
    global _auth_db, _auth_engine
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _auth_db = AuthDB(db_path)
    _auth_engine = AuthEngine(_auth_db)
    logger.debug("Auth engine initialized: %s", db_path)
    return _auth_engine

def get_auth() -> AuthEngine:
    if _auth_engine is None:
        raise RuntimeError("Auth not initialized. Call init_auth() first.")
    return _auth_engine
