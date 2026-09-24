"""
MARY III — centralized configuration.
No magic numbers, no hardcoded paths.
"""
from pathlib import Path
import os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.env'))


# === Project root ===
PROJECT_ROOT = Path(os.environ.get("MARY3_ROOT") or Path(__file__).resolve().parent.parent).resolve()

# === Paths (all relative to PROJECT_ROOT) ===

# 加密密钥路径
CRYPTO_KEY_PATH = PROJECT_ROOT / ".crypto.key"
DATA_DIR = PROJECT_ROOT / "data"
MEMORY_DB_PATH = DATA_DIR / "memory.db"
CHAT_HISTORY_ENC_PATH = DATA_DIR / "chat_history.enc"
MEMORY_DIR = DATA_DIR / "memory"
CODE_POOL_DIR = DATA_DIR / "code_pool"
TOOLS_DIR = PROJECT_ROOT / "tools"
PLUGINS_DATA_DIR = DATA_DIR / "plugins_data"
PLUGIN_ALLOW_DIR = DATA_DIR / "plugins_data"

# === Files ===
USERS_FILE = DATA_DIR / "users.enc"
KEYS_FILE = PROJECT_ROOT / "keys.enc"
RULES_FILE = DATA_DIR / "rules.enc"
LEARNED_RULES_FILE = DATA_DIR / "learned_rules.enc"
PREFS_FILE = DATA_DIR / "prefs.enc"
CHAT_HISTORY_FILE = DATA_DIR / "chat_history.enc"
MEMORY_DB = MEMORY_DIR / "memory.db"
CRYPTO_KEY = PROJECT_ROOT / ".crypto.key"
AGENT_LOG = DATA_DIR / "agent.log"

# === Server ===
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8800
API_RATE_LIMIT = "30/minute"
CORS_ORIGINS = ["http://127.0.0.1:8800", "http://localhost:8800"]

# === Timeouts (seconds) ===
SANDBOX_TIMEOUT = 30
SHELL_TIMEOUT = 30
MODEL_TIMEOUT = 120
PLUGIN_TIMEOUT = 60
HEARTBEAT_INTERVAL = 5
WORKER_STALL_TIMEOUT = 60

# === Sandbox ===
MAX_CODE_LENGTH = 50000
MAX_CODE_OUTPUT = 50000
MAX_CODE_ROUNDS = 3

# === AutoGuide ===
MAX_LEARNED_RULES = 1000
RULE_TTL_DAYS = 30
LCS_CACHE_SIZE = 2000
LCS_THRESHOLD_HIGH = 0.9
LCS_THRESHOLD_LOW = 0.35

# === Model defaults ===
DEFAULT_MODEL = "deepseek"
OLLAMA_PORT = 11434
MODEL_FAIL_THRESHOLD = 3
MODEL_COOLDOWN = 180

# === Async ===
ASYNC_POOL_SIZE = 8
DB_POOL_SIZE = 5

# === Conversation ===
MAX_HISTORY_ROUNDS = 10
SUMMARY_MAX_CHARS = 150
CHAT_RETENTION_DAYS = 30

# === Security ===
SENSITIVE_FILES = {".crypto.key", "users.enc", "keys.enc"}
SHELL_BLACKLIST = {"rm -rf", "sudo", "dd if=", "mkfs", "shutdown", "reboot",
                    "format c:", "del /f /s", "rd /s /q"}
FORBIDDEN_MODULES = {"os", "subprocess", "shutil", "socket", "urllib", "requests",
                      "sqlite3", "ctypes", "multiprocessing", "http", "ftplib",
                      "telnetlib", "smtplib", "poplib", "imaplib", "pickle", "marshal", "sys"}
NETWORK_WHITELIST = {"127.0.0.1", "localhost", "pypi.org", "files.pythonhosted.org",
                      "api.deepseek.com", "api.openai.com"}
PLUGIN_FORBIDDEN_IMPORTS = {"os", "subprocess", "socket", "ctypes", "requests"}

# === Plugins ===
# ★ 2026-09-20: session_logs 加入受信表 —— 它 import os (取 TMM_SESSION_DB / 拼路径),
#   而 os 在 PLUGIN_FORBIDDEN_IMPORTS 里 (那条是给**外部**插件的供应链防线,
#   见 /depot 的组装流程)。第一方工具一律走本表, 与其余工具同一规矩。
#   注意: 本表只免**静态扫描**, 不代表免除其他约束 —— 本工具仍强制只读打开库。
# ★★ 2026-09-20 再修 (真缺陷, 三个功能静默失效): vision / image_gen / backup 三个
#   第一方工具**从未登记本表** → PluginManager._load_plugins() 里 scan_plugin_safe()
#   因它们 import os 而返回 False → 被 "REJECTED by security scan" **不加载** →
#   调用一律得 {"success": False, "error": "未知工具: vision"}。
#   后果 (实测):
#     · core/pipeline.py:4035 `await self.gateway.call("vision", …)` 永远失败,
#       而且失败提示是"请检查视觉凭证(keys.json 的 qwen.key)" —— **把病因说成凭证问题**
#     · intent_router 把"备份"映射到 backup → 备份功能在 PluginManager 路径上不可用
#   溯源: 这三个文件都是未跟踪的新文件 (settings.py 最后改于 08-04), 即"新工具须登记
#   SAFE_PLUGINS"这条规矩漏了三处。已加常驻门禁 verify_tool_registry.py 防再犯。
SAFE_PLUGINS = {"file_ops", "system_info", "shell_exec", "send_email", "web_search", "windows_desktop", "hermes_bridge", "tiger_office", "im_notify", "sms_send", "push_notify", "browser", "nominatim", "ocr", "office_cli", "plantuml", "win32_input", "voice", "whisper",
    "check_mail", "kb_import", "kb_search", "kb_list", "kb_delete", "chart",
    "session_logs", "service_check", "usage_stats", "github_api",
    "vision", "image_gen", "backup",
    "artifacts"}

# === Environment switches (defaults) ===
NO_PLUGINS = int(os.environ.get("NO_PLUGINS", "0"))
NO_IDENTITY = int(os.environ.get("NO_IDENTITY", "0"))
NO_COLOR = int(os.environ.get("NO_COLOR", "0"))
MINIMAL_MODE = int(os.environ.get("MINIMAL", "0"))

# ── Run mode: lightweight / full ──
# lightweight: short context, no long-memory, fewer tools, 2B-7B friendly
# full:        complete context, long-memory, all tools, large-window models
# Set via env TMM_MODE or /mode command (persisted to prefs.json)
def _load_run_mode():
    # 1. Env override
    env_mode = os.environ.get("TMM_MODE", "").lower()
    if env_mode in ("lightweight", "full"):
        return env_mode
    # 2. prefs.json
    try:
        import json
        prefs_path = PROJECT_ROOT / "data" / "prefs.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            mode = prefs.get("run_mode", "").lower()
            if mode in ("lightweight", "full"):
                return mode
    except Exception:
        pass
    # 3. Default
    return "full"

RUN_MODE = _load_run_mode()
VALID_MODES = ("lightweight", "full")
TIGERMM_ISOLATE = int(os.environ.get("TIGERMM_ISOLATE", "1"))
