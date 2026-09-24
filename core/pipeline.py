
"""MARY III v4.2 — Level4Pipeline + CLI + API Server.
Think->Execute->Verify->Fix. All in one file.
"""
import sys, os, json, re, time, asyncio, logging, traceback, webbrowser, py_compile, threading
from pathlib import Path

logging.getLogger('comtypes').setLevel(logging.WARNING)
# [PIPELINE] v4.2-20260803 loaded — 10 components, 25 plugins, IR chain
logger = logging.getLogger("core.pipeline")

# Optional deps
try:
    from fastapi import FastAPI, HTTPException, Request as FR
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from config.settings import DATA_DIR, CORS_ORIGINS, PROJECT_ROOT
from core.auto_guide import AutoGuide
from core.context_builder import ContextBuilder
from storage.session_store import SessionStore
from core.guardrails import run_input_guards, run_output_guards
from core.session_manager import get_session_manager
from core.memory import TigerMemory
from core.retry import retry, async_retry, safe_call
from core.risk import classify_command, RiskClass
from core.hive import get_hive
from core.theme import rule as _rule
from core.self_regulation import SelfRegulationEngine, MODULE_BOUNDARY_PROMPT
from core.brain import TigerBrain
from core.code_engine import CodeEngine, CodeResult
from core.perception import PerceptionEngine, get_perception
from core.intent_router import get_router as get_intent_router
from core.model_router import get_model_router
from core.routes import (RouteRegistry, Route, P_COMMAND, P_REGULATOR, P_GATE, P_EXPLICIT_MODEL, P_SKILL,
                         P_PLANNER, P_ANALYSIS, P_GEOCODE, P_GUARD, P_PLUGIN, P_OPEN, P_SEARCH,
                         P_BRAIN, P_SMART, P_IR_CHAIN,
                         P_L4, P_SKILL_CATCH, P_LEARNED, P_LEARN_TEACH, P_AUTOGUIDE, P_FIXGUARD, P_L1, P_L2, P_L3,
                         P_EMAIL, P_URLOPEN, P_FTS, P_LOCAL_FALLBACK, P_MODEL,
                         P_LOOKUP, P_SAVE_GEN, P_UNDERSTAND)
from core.tool_orchestrator import get_orchestrator
from core.skill_registry import get_skill_registry
from core.skill_executor import get_executor
from core.task_planner import TaskPlanner, PlanExecutor
from core.task_watcher import get_watcher
from core.scheduler import get_scheduler
from core.retry import CircuitBreaker
from core.memory_long import get_long_memory
from core.learner import get_learner
from core.mcp_client import MCPClient
from core.hive_mind import get_hive_mind
from core.command_handler import CommandHandler
from core.modes import ModeManager
from core.session_tracker import take_snapshot
from core.backup import get_backup
from core.output_validator import OutputValidator, check_evidence

CHAT_HISTORY_FILE = DATA_DIR / "chat_history.enc"

# ═══ Colors ═══
GOLD = '\033[38;5;214m'
DARK = '\033[48;5;232m'
DIM = '\033[2m'
BOLD = '\033[1m'
RST = '\033[0m'
CYAN = '\033[36m'
GREEN = '\033[32m'
RED = '\033[31m'
CLEAR = '\033[2J\033[H'

# ═══ Splash ═══
FRAMEWORK_TEXT = rf"""
{CLEAR}
{GOLD}{BOLD}
    ████████╗ ██╗  ██████╗  ███████╗ ██████╗      ███╗   ███╗   ███╗   ███╗
    ╚══██╔══╝ ██║ ██╔════╝  ██╔════╝ ██╔══██╗     ████╗ ████║   ████╗ ████║
       ██║    ██║ ██║  ███╗ █████╗   ██████╔╝     ██╔████╔██║   ██╔████╔██║
       ██║    ██║ ██║   ██║ ██╔══╝   ██╔══██╗     ██║╚██╔╝██║   ██║╚██╔╝██║
       ██║    ██║ ╚██████╔╝ ███████╗ ██║  ██║     ██║ ╚═╝ ██║   ██║ ╚═╝ ██║
       ╚═╝    ╚═╝  ╚═════╝  ╚══════╝ ╚═╝  ╚═╝     ╚═╝     ╚═╝   ╚═╝     ╚═╝
{RST}
{GOLD}  ======================================================================
  {BOLD}MARY III v4.2  —  虎哥·凌霄  Local AI Agent{RST}{GOLD}
  Think → Execute → Verify → Fix
  ===================================================================
{RST}"""

# ═══════════════════════════════════════════════
# Level4Pipeline
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
# ToolGateway — routes tool calls to plugins
# ═══════════════════════════════════════════════

class ToolGateway:
    def __init__(self):
        self.plugin_mgr = None
        self._progress_cb = None
        # ★ 产物登记去重: 同一路径本进程只登记一次 (避免重复写台账)
        # ★ 2026-09-23: 本轮用过的工具 (给「日常干活账」的能力维度用)。
        #   由最外层回合 (process 包装层) reset/读走 —— 与 _pipeline_depth 同一套口径。
        self.turn_tools: list = []
        self._art_registered = set()

    def set_progress_cb(self, cb):
        """进度回调: cb({type:'tool_start'|'tool_end', tool, args, success, result})。用于 web 流式实时推送。"""
        self._progress_cb = cb

    # ── 产物自动登记 (归档台账) ──
    _ART_FRESH_SEC = 300        # 只登记 5 分钟内变动过的文件 (= 本次刚产出)
    #: 临时/沙箱路径不进台账 —— 否则门禁与探针产生的临时文件会污染用户的真台账
    #  (实测风险: 门禁真调 file_ops 写沙箱 → 不拦的话真台账会被灌进一堆死路径)
    _ART_SKIP_PARTS = ("tmp", "temp", "Temp", "TEMP", "_probe", "_vfy", "__pycache__",
                       ".pytest_cache", "AppData")

    def _register_artifacts(self, tool_name: str, params: dict, result) -> int:
        """从工具入参/返回值里认出**刚产出的文件**并登记。返回登记条数 (出错不影响工具结果)。

        ★ 2026-09-21 立: 原来 core/artifacts.py 的 add() 有实现、有工具、有 web 端点, 但
        **没有任何生产路径调它** → 台账永远是空的 ("今天产出了什么"只能诚实答"登记表空")。
        这里挂在工具调用的唯一出口, 一个点覆盖全部写入型工具。
        ★ 只记"刚产出的"(mtime 近 5 分钟) → 不把"刚读过的旧文件"误登记。
        ★ 探针安全: 落盘路径受 TMM_ARTIFACTS_FILE 控制, 已被 _probe_env 隔离。
        """
        # ★★ 失败即闭 (2026-09-21 两次污染后改成这样): 只有**真人会话**才登记。
        #   TMM_LIVE=1 由真实入口设置; 没有它 = 测试/门禁/探针 → 一律不写。
        #   原来只拦 TMM_PROBE, 结果"门禁直接调 process() 但不传 probe"照样写进真台账。
        if os.environ.get("TMM_PROBE") in ("1", "true", "yes"):
            return 0
        if os.environ.get("TMM_LIVE") not in ("1", "true", "yes"):
            return 0                              # 非真人会话不登记
        try:
            from core import artifacts as _A
        except Exception:
            return 0
        cands, seen = [], set()
        for src in (params, result if isinstance(result, dict) else {}):
            for v in (src or {}).values():
                if isinstance(v, str) and v:
                    cands.append(v)
                elif isinstance(v, (list, tuple)):
                    cands.extend([x for x in v if isinstance(x, str)])
        now = time.time()
        n = 0
        for c in cands:
            s = c.strip().strip('"\'')
            if len(s) < 4 or len(s) > 500:
                continue
            p = Path(s)
            if not p.is_absolute() or not p.is_file():
                continue
            # 临时/沙箱路径不进台账 —— 但**隔离跑时放行** (此时台账本身就落在沙箱里,
            # 正是要验证登记链路; 生产模式没有 TMM_ARTIFACTS_FILE, 才启用这条拦截)。
            if not os.environ.get("TMM_ARTIFACTS_FILE") and any(
                    part in self._ART_SKIP_PARTS or part.startswith(("_probe", "_vfy"))
                    for part in p.parts):
                continue
            key = str(p)
            if key in seen or key in self._art_registered:
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            if now - st.st_mtime > self._ART_FRESH_SEC:
                continue                       # 旧文件 → 是"读到的", 不是"产出的"
            if _A.kind_of(p) == "other":
                continue                       # 不认识的扩展名 → 不进台账 (避免噪声)
            seen.add(key)
            res = _A.add(key, source=f"tool:{tool_name}", tags=[tool_name])
            if isinstance(res, dict) and res.get("ok"):
                self._art_registered.add(key)
                n += 1
        return n

    async def call(self, tool_name, **kwargs):
        # ── 探针写盘闸 (2026-09-24 加; 只在 TMM_PROBE=1 时生效) ──
        # 为什么在这: 探针猴补拦不住**子进程**写盘 (plantuml→java 实测漏了),
        # 而网关是所有工具调用的唯一入口, 在这按参数拦最可靠。
        # ★ 教训: 本项目有**两个** ToolGateway —— core/tool_gateway.py 的是死代码,
        #   实际生效的是本类 (core/pipeline.py)。改网关必须改这个, 否则白改。
        try:
            from storage.file_secure import probe_write_blocked as _pwb
            _blk = _pwb(kwargs)
            if _blk:
                logger.warning("probe write blocked: %s", _blk)
                return {"success": False, "error": _blk}
        except ImportError:
            pass
        # ★ 2026-09-23: 记下本轮用过哪些工具 (能力维度; 只记名字不记参数)
        try:
            if tool_name and tool_name not in self.turn_tools:
                self.turn_tools.append(tool_name)
        except Exception:
            pass
        if self.plugin_mgr:
            if self._progress_cb:
                try:
                    self._progress_cb({"type": "tool_start", "tool": tool_name, "args": str(kwargs)[:300]})
                except Exception:
                    pass
            try:
                result = await asyncio.wait_for(
                    self.plugin_mgr.call(tool_name, **kwargs),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                result = {"success": False, "error": f"Tool '{tool_name}' timed out after 30s"}
            except Exception as e:
                result = {"success": False, "error": str(e)}
            if self._progress_cb:
                try:
                    self._progress_cb({"type": "tool_end", "tool": tool_name,
                                       "success": bool(isinstance(result, dict) and result.get("success")),
                                       "result": str(result)[:300]})
                except Exception:
                    pass
            # ── ★ 产物登记: 成功调用的输出若落了新文件 → 进台账 (失败不影响工具结果) ──
            if isinstance(result, dict) and result.get("success"):
                try:
                    self._register_artifacts(tool_name, kwargs, result)
                except Exception as _ae:
                    from core.result import degrade as _deg
                    _deg("产物登记失败", f"{type(_ae).__name__}: {str(_ae)[:60]}", "gateway")
            return result
        return {"success": False, "error": "No plugin manager"}



# ═══════════════════════════════════════════════
# PluginManager — loads and executes tool plugins
# ═══════════════════════════════════════════════

def _norm_tool_result(result):
    """统一工具返回契约: 保证结果文本在 `output` 里。

    ★ 2026-09-19 深测修: 33 个工具里 30 个把结果放 `output`, 13 个用 `result`,
      4 个用 `data`, 网关**不做归一化** → 任何按 `output` 读结果的调用方 (技能 DAG、
      `_run_search`、IR chain) 对 web_search/nominatim/libretranslate 这些工具**看不到结果**
      (实测: 深测台第一版就因此误判 3 个工具失败; `_run_search` 也只能回 "[搜索] {...}" 的裸字典)。

    加性: 只在 `output` **为空**且兄弟键有内容时补上, 不覆盖既有字段, 不改变失败语义。
    """
    if not isinstance(result, dict):
        return result
    try:
        if str(result.get("output") or "").strip():
            return result
        for k in ("result", "data", "text"):
            v = result.get(k)
            if v is None or v == "":
                continue
            if not isinstance(v, str):
                import json as _j
                v = _j.dumps(v, ensure_ascii=False)
            out = dict(result)
            out["output"] = v
            out.setdefault("output_from", k)     # 留痕: 这个 output 是从哪个键归一的
            return out
    except Exception:
        return result
    return result


class PluginManager:
    def __init__(self, data_dir):
        self._plugins = {}
        self.data_dir = data_dir
        self._load_plugins()
    
    def _load_plugins(self):
        tools_dir = Path(__file__).parent.parent / "tools"
        if tools_dir.exists():
            for f in tools_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                self._plugins[f.stem] = {"name": f.stem, "tool": f.stem, "path": str(f)}
        logger.info("Plugins: %d loaded", len(self._plugins))
    
    async def call(self, tool_name, **kwargs):
        if tool_name in self._plugins:
            plugin = self._plugins[tool_name]
            import importlib.util
            spec = importlib.util.spec_from_file_location(tool_name, plugin["path"])
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, 'execute'):
                    result = mod.execute(**kwargs)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return _norm_tool_result(result)
                elif hasattr(mod, 'run'):
                    result = mod.run(**kwargs)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return _norm_tool_result(result)
        # 🔴 2026-09-19 修 (真缺陷): 原来这里返回**成功** ["{tool}] called" ——
        #   后果: 没有 run()/execute() 入口的模块 (office_cli / win32_input) 或
        #   导入失败的模块 (windows_desktop 缺 comtypes) 被调用时**谎报成功**,
        #   调用方(技能 DAG / IR chain)以为干完了 → 用户拿到"✓ 已完成"但什么都没发生。
        #   技能工厂实测踩到: 编译出的技能用 office_cli 存 Word, 自测过、真跑也"成功",
        #   但**文件根本不存在**。诚实报错, 不谎报。
        if tool_name in getattr(self, "_plugins", {}):
            return {"success": False, "output": "",
                    "error": f"工具 '{tool_name}' 没有可调用入口 (需要 run()/execute()), 未执行"}
        return {"success": False, "output": "", "error": f"未知工具: {tool_name}"}
    
    def match_keywords(self, message: str):
        """Check all plugins TOOL.keywords against message."""
        matches = []
        for name, plg in self._plugins.items():
            try:
                import importlib.util as _iu
                spec = _iu.spec_from_file_location(name, plg["path"])
                if spec and spec.loader:
                    mod = _iu.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    tool_meta = getattr(mod, "TOOL", {})
                    kws = tool_meta.get("keywords", [])
                    for kw in kws:
                        if kw in message:
                            matches.append((name, kw, plg))
            except Exception:
                pass
        return matches


# ── Shell command safety classifier ──
def _check_shell_safety(cmd: str) -> str:
    """Classify a shell command as 'safe', 'warn', or 'block'.
    - safe: read-only / info commands — allow
    - block: destructive / dangerous — deny
    - warn: everything else — allow with log warning
    """
    cmd_lower = cmd.strip().lower()
    
    # DANGEROUS: destructive commands — block
    _block_patterns = [
        "format ", "del /", "rd /s", "rmdir /s", "diskpart",
        "shutdown", "reg delete", "reg add", "cacls", "takeown",
        "icacls /deny", "icacls /grant", "wmic process delete",
        "wmic process call terminate", "taskkill /f", "taskkill /im",
        "net stop", "net user", "sc delete", "sc stop",
        "bcdedit", "fsutil", "subst", "label",
        "move nul", "copy nul", "type nul >", "> nul",
        "erase ", "deltree",
    ]
    for p in _block_patterns:
        if p in cmd_lower:
            return "block"
    
    # Check for pipe to dangerous command
    if "|" in cmd:
        after_pipe = cmd_lower.split("|")[-1].strip()
        for p in _block_patterns:
            if p in after_pipe:
                return "block"
    
    # SAFE: read-only / info commands
    _safe_commands = [
        "dir", "echo", "type", "date", "time", "whoami", "hostname", "ver",
        "cd", "chdir", "set", "path", "where", "findstr", "sort", "find",
        "more", "tree", "help", "assoc", "ftype",
        "ipconfig", "ping", "tracert", "nslookup", "netstat", "arp",
        "tasklist", "systeminfo", "driverquery", "schtasks /query",
        "wmic os get", "wmic cpu get", "wmic memorychip get",
        "wmic diskdrive get", "wmic logicaldisk get", "wmic nic get",
        "wmic product get", "wmic service get", "wmic process get",
        "python --version", "python -c", "python -m pip list",
        "pip list", "pip show", "pip freeze",
        "git status", "git log", "git diff", "git branch", "git remote",
        "git config --list", "git stash list",
    ]
    
    # Exact command name match (first word before space or /)
    first_token = cmd_lower.split()[0] if cmd_lower.split() else ""
    
    for s in _safe_commands:
        if cmd_lower.startswith(s) or first_token == s.split()[0]:
            # Extra check: dir with redirect to file is not just read-only
            if first_token == "dir" and (">" in cmd or ">>" in cmd):
                return "warn"
            return "safe"
    
    # GRAY: everything else — allow but warn
    return "warn"


# ── 系统类问题检测 (2026-08-20 硬防线): 时间/日期/文件统计/磁盘 → 必须工具调用 ──
_SYS_Q_PATTERNS = [
    # 时间/日期
    r'(几点|几点了|现在几点|什么时间|日期|星期[一二三四五六日天几]|周几|几号|今天是|今天几|当前时间|当前日期)',
    # 文件/目录统计 (数字 + 文件类词)
    r'(多少|几个|多少个|数量|统计|总共有|一共).{0,8}(文件|目录|文件夹|个文件|txt|文档)',
    # 磁盘/资源
    r'(磁盘|硬盘|空间|内存|进程|CPU|cpu|占用|剩余).{0,6}(多少|满|不够|够|使用|情况|多大|几个|剩余|占用|空间)',
    # 纯资源查询
    r'^(查|查一下|看看|看一下|看下)?(磁盘|硬盘|内存|进程|cpu|CPU)(占用|使用|情况|空间)?$',
]


def _sys_question(msg: str) -> bool:
    """消息是否属于必须工具调用才能回答的系统类问题(时间/文件统计/磁盘)。"""
    if not msg:
        return False
    return any(re.search(p, msg) for p in _SYS_Q_PATTERNS)


def _trace_has_sys_tool(trace, message) -> bool:
    """系统类问题是否已被 trace 中的工具满足 (时间→system_info time / shell date;
    磁盘→system_info disk; 文件统计→file_ops list/walk/search)。"""
    if not trace:
        return False
    _time_q = any(w in message for w in ["时间", "几点", "日期", "星期", "几号", "什么时候"])
    _disk_q = any(w in message for w in ["磁盘", "硬盘", "空间", "内存", "进程", "CPU", "cpu"])
    _file_q = any(w in message for w in ["文件", "目录", "文件夹", "统计", "数量"])
    for t in trace:
        tool = t.get("tool", "")
        args = str(t.get("args", ""))
        if _time_q and (tool == "system_info" and "time" in args):
            return True
        if _disk_q and (tool == "system_info" and any(a in args for a in ("disk", "ps", "sysinfo"))):
            return True
        if _file_q and (tool == "file_ops" and any(a in args for a in ("list", "walk", "search"))):
            return True
    return False


def _claims_action(text: str) -> bool:
    """响应文本是否声称完成了某操作(动作词+成功词) → 需要工具轨迹支撑, 否则为幻觉。"""
    if not text or not text.strip():
        return False
    try:
        from core.audit_agent import CLAIM_TOOLS, SUCCESS_WORDS
    except Exception:
        return False
    # 补充词表: audit_agent 未覆盖的口语化声称 (2026-08-20)
    _claim_extra = ['保存', '下载', '打开', '安装', '启动', '运行', '执行']
    _suc_extra = ['创建了', '已写入', '已保存', '保存了', '已创建', '写好了', '发出去了', '下载完成']
    has_claim = any(kw in text for kw in CLAIM_TOOLS) or any(kw in text for kw in _claim_extra)
    has_success = any(w in text for w in SUCCESS_WORDS) or any(w in text for w in _suc_extra)
    return has_claim and has_success


# ── 本地模型工具调用上限: gemma4-12B 每轮最多 2 个并行工具, 长链天然不稳 (2026-08-20) ──
_LOCAL_MODEL_MAX_TOOLS_PER_ROUND = 2


#: 会话历史读取失败只告警一次 (防刷屏); 库故障要**看得见**, 不能静默
_HIST_WARNED: set = set()


def _warn_hist_read_failed(where: str, exc: BaseException) -> None:
    """读会话历史失败 → **响亮告警**(一次)。

    ★ 2026-09-24 加 (用户实测: "他上下文联系有问题, 我问他刚才是不是写了一篇短文,
      他无法正面回答"): 两处历史注入原为 `except Exception: pass` ⇒ 库坏掉时
      **静默无历史** ⇒ 模型只能答"我没有记录" ⇒ 用户以为是它笨/健忘,
      完全不知道是记忆库故障 (今天库坏了 4 次, 全部静默)。
      现在: 记 WARNING + 一次性, 故障可见。
    """
    key = f"hist:{where}"
    if key in _HIST_WARNED:
        return
    _HIST_WARNED.add(key)
    logger.warning(
        "★ 会话历史读取失败 (%s) —— 本轮回答将【没有上下文】: %s: %s. "
        "库可能损坏 (见技能 tmm-dev-pitfalls-v2 条目六十八/七十三)", 
        where, type(exc).__name__, exc)


class Level4Pipeline:
    MAX_CODE_ROUNDS = 3

    def __init__(self, model_client, config):
        global pipeline; pipeline = self
        self.model_client = model_client
        self.config = config
        self.conversations = {}
        self.stats = {"total": 0, "errors": 0}
        self._prefs = {}
        self._last_preread = {}  # 🔴 会话记忆: 最近预读的文件 {path, cols} — 无路径追问("数量多少")复用
        self._semaphore = asyncio.Semaphore(5)
        self._startup = time.time()
        self.auto_guide = AutoGuide()
        self.ctx = ContextBuilder(DATA_DIR)
        self.sessions = SessionStore(DATA_DIR / "chat_sessions.db")
        self.gateway = ToolGateway()
        self.plugins = PluginManager(DATA_DIR)
        self.gateway.plugin_mgr = self.plugins
        # ── MCP Client 接线: 加载 config/mcp_servers.json, 后台刷新外部工具 ──
        try:
            from core.mcp_client import get_mcp_client
            self.gateway.mcp = get_mcp_client(DATA_DIR)
            _mcp_cfg = PROJECT_ROOT / "config" / "mcp_servers.json"
            if _mcp_cfg.exists():
                import json as _mcpj
                _mcp_cfg_data = _mcpj.loads(_mcp_cfg.read_text(encoding="utf-8"))
                _mcp_servers = _mcp_cfg_data.get("servers", []) or []
                if _mcp_servers:
                    self.gateway.mcp.load_config(_mcp_servers)
                    _t = threading.Thread(target=self._mcp_refresh_bg, daemon=True)
                    _t.start()
                    logger.info("MCP: %d server(s) configured, background refresh started", len(_mcp_servers))
        except Exception as _mcp_e:
            logger.warning("MCP wiring failed: %s", _mcp_e)
        self._ke = None
        self._reasoner = None
        self._we = None
        self._load_prefs()
        self._regex_passthrough = self._prefs.get("regex_passthrough", True)
        self.sm = get_session_manager(pipeline=self)
        self.memory = TigerMemory()
        self.hive = get_hive(pipeline=self)
        self.sre = SelfRegulationEngine()
        self._brain = None  # lazy init
        self.code_engine = CodeEngine(max_rounds=self.MAX_CODE_ROUNDS)
        self.perception = get_perception()
        self.intent_router = get_intent_router(self._ke)
        self.retry_mgr = CircuitBreaker("pipeline")
        self.long_memory = get_long_memory(DATA_DIR)
        self.learner = get_learner(DATA_DIR)
        # ★ 统一学习闭环 (2026-09-19): learn_loop 是**唯一**的「学到就生效」通路 ——
        #   规则存数据 (data/learned_rules.db), 绝不改源码; 带置信/测量/衰减。
        from core.learn_loop import get_learn_loop
        self.learn_loop = get_learn_loop(DATA_DIR)
        # ★ 能力缺口台账 (2026-09-19): learn_loop 记**知识**缺口 (答不上来),
        #   gap_ledger 记**能力**缺口 (工具/技能干不了) —— 互补不重叠。
        #   有了它, "该造什么能力"从"靠感觉猜"变成"看账排序"。
        #   与 learn_loop 同一收口: _on_route_done (不往 process() 插 if)。
        from core.gap_ledger import get_gap_ledger
        self.gap_ledger = get_gap_ledger(DATA_DIR)
        self.mcp = get_mcp_client(DATA_DIR)
        self.regulator = SelfRegulationEngine()
        self.model_router = get_model_router(DATA_DIR)
        self.orchestrator = get_orchestrator(data_dir=DATA_DIR)
        self.skill_registry = get_skill_registry()
        self.skill_executor = get_executor(gateway=self.gateway, registry=self.skill_registry, ke=self._ke)
        self.task_planner = TaskPlanner(self.model_client, self.gateway.plugin_mgr, mcp_client=self.mcp)
        self.plan_executor = PlanExecutor(self.gateway, knowledge_engine=self._ke)
        self.task_watcher = get_watcher(DATA_DIR, self.gateway, self.model_client)
        self.scheduler = get_scheduler(pipeline=self)
        self.hive_mind = get_hive_mind(model_client=self.model_client, gateway=self.gateway, plugins=self.gateway.plugin_mgr)
        self.cmds = CommandHandler(self)
        self.modes = ModeManager(DATA_DIR)   # ★ 三模式闸门 (Ask/Plan/Craft)
        self.backup = get_backup(PROJECT_ROOT)
        self.session_snap = take_snapshot(str(PROJECT_ROOT))
        self.validator = OutputValidator()  # 机制2: 输出验证器
        self._degraded = set()
        self._wm = None
        # ★ 路由表 (P1): "谁先裁决"的**唯一真相** —— 不再靠行号。
        #   表里的 priority 决定顺序; process() 在旧链路前先问表 (返回 None 才继续走旧链路, 加性)。
        self.routes = RouteRegistry()
        self._build_routes()
        # Register default agents from config
        for model_name in config:
            if model_name not in ("auto",):
                self.hive.register(model_name, model=model_name,
                                   description=f"{config[model_name].get('model', model_name)} model")
        if self.scheduler:
            try: self.scheduler.start()
            except Exception: self._degraded.add("scheduler")
        if self._degraded:
            logger.warning("Pipeline degraded: %s", ", ".join(sorted(self._degraded)))
        logger.info("Pipeline init — plugins: %d, sessions: %d, uptime: %.2fs",
                     len(self.plugins._plugins), self.sm.stats()["total"],
                     time.time() - self._startup)

    def list_models(self):
        models = [{"id": "auto", "name": "Tiger.M.M (Auto)", "local": True}]
        for k, v in self.config.items():
            models.append({"id": k, "name": v.get("model", k), "local": False})
        if self.auto_guide._check_ollama():
            models.append({"id": "ollama", "name": "Ollama (local)", "local": True})
        return models

    def _load_prefs(self):
        try:
            p = DATA_DIR / "prefs.json"
            if p.exists(): self._prefs = json.loads(p.read_text(encoding='utf-8'))
        except Exception: self._prefs = {}

    def _save_prefs(self):
        try: (DATA_DIR / "prefs.json").write_text(json.dumps(self._prefs, ensure_ascii=False), encoding='utf-8')
        except Exception: pass

    # ═══════════════════════════════════════════════════════════════
    # 学习闭环 (LearnLoop): 观察 → 生效 → 测量
    # ═══════════════════════════════════════════════════════════════

    def _on_route_done(self, message, ctx, out, route_name):
        """routes.dispatch 的**统一**路由后观察点 → 学习闭环。

        为什么需要它: "每轮对话后看一眼学到了什么" 必须对所有路由一视同仁。
        塞进 process() 加 if 违反"新能力只准在 routes.py 声明"; 散在各路由里会漏。
        所以挂在 dispatch 收尾 (含"没人接手"的兜底路径)。

        probe 流量**绝不学习** (硬不变量): 验证流量不能造规则/改用户数据。
        """
        if ctx.get("probe"):
            return
        ll = getattr(self, "learn_loop", None)
        if ll is None:
            return
        try:
            prev_user, prev_asst = "", ""
            for t in reversed(self.memory.recent(8) or []):
                role = t.get("role")
                txt = (t.get("text") or "").strip()
                if not txt:
                    continue
                if role == "user" and txt == (message or "").strip():
                    continue          # 本轮用户消息 (process 已先记) → 不是"上一轮"
                if role == "user" and not prev_user:
                    prev_user = txt
                elif role == "assistant" and not prev_asst:
                    prev_asst = txt
                if prev_user and prev_asst:
                    break
            got = ll.observe(message, response=(out or {}).get("response"),
                             route=route_name, prev_user=prev_user, prev_assistant=prev_asst)
            if got and isinstance(out, dict):
                out.setdefault("learned", sorted(got.keys()))     # 可观测, 不打扰用户
            # ★ 能力缺口记账 (2026-09-19): 同一个收口, 各记各的账。
            #   检测是**纯函数** (core.gap_ledger.classify), 只认"具体失败信号"
            #   与"模型兜底且承认干不了" → 正常问答不会进账 (否则台账全是噪音)。
            gl = getattr(self, "gap_ledger", None)
            if gl is not None:
                try:
                    hit = gl.record(message, (out or {}).get("response"), route_name or "")
                    if hit and isinstance(out, dict):
                        out.setdefault("gap", hit.get("category"))     # 可观测, 不打扰用户
                except Exception as e:
                    # ★ 2026-09-21: 原来是 logger.debug 静默吞 —— 学习闭环记不上账却无人知
                    from core.result import degrade as _deg
                    _deg("能力缺口未记账", f"{type(e).__name__}: {str(e)[:60]}", "pipeline:learn")
            # 衰减每 20 轮扫一次 (便宜; 不当场花钱)
            self._learn_rounds = getattr(self, "_learn_rounds", 0) + 1
            if self._learn_rounds % 20 == 0:
                try:
                    ll.decay()
                except Exception as _de:
                    # ★ 2026-09-21: 原来是 `except Exception: pass` —— 规则永不衰减也无人知
                    from core.result import degrade as _deg
                    _deg("学习规则未衰减", f"{type(_de).__name__}: {str(_de)[:60]}", "pipeline:learn")
        except Exception as e:
            # ★ 2026-09-21: 原来是 logger.debug 静默吞 —— **学习失败是"越用越傻"的根因**,
            #   必须让用户看得见 (配合 core/result 的降级出声机制)。
            from core.result import degrade as _deg
            _deg("学习闭环记录失败", f"{type(e).__name__}: {str(e)[:60]}", "pipeline:learn")
            logger.debug("learn_loop observe failed: %s", e)

    # ── 370 教学句 ──
    def _match_learn_teach(self, message, ctx):
        """用户在教系统 (以后/默认/总是/永远/记住…) → 记成偏好而非执行。

        严格用 require_start=True: 必须**句首**就是教学词 —— 保守, 免得抢走
        "把文件发给涛哥" 这类真命令。match 保持**纯**(解析用 learn_loop 的纯函数)。
        """
        if self._is_explicit_model(ctx):
            return False
        ll = getattr(self, "learn_loop", None)
        if ll is None:
            return False
        pref = ll.parse_preference(message, require_start=True)
        if not pref:
            return False
        ctx["_teach_pref"] = pref
        return True

    async def _run_learn_teach(self, message, ctx):
        pref = ctx.get("_teach_pref")
        if not pref:
            return None
        key, val = pref
        # 不在这里写库 (统一由 _on_route_done 的 observe 落库) —— 否则同一条会 +2 权重
        return {"response": (f"记住了：{key} = {val}\n以后按这个来。"
                             f"\n（/learn list 看全部 · /learn forget <编号> 删除）"),
                "intent": "learn_teach", "model": "learn-loop",
                "elapsed": self._elapsed(ctx), "pref_key": key}

    # ── 292 学到的东西 ──
    def _match_learned(self, message, ctx):
        if self._is_explicit_model(ctx):
            return False
        if not getattr(self, "learn_loop", None):
            return False
        rule = self.learn_loop.matches(message)      # 纯查询, 不计数
        if not rule:
            return False
        ctx["_learned_rule"] = rule
        return True

    async def _run_learned(self, message, ctx):
        rule = ctx.get("_learned_rule")
        if not rule:
            return None
        self.learn_loop.note_hit(rule["id"])         # 计数在 run 里 (match 必须纯)
        return {"response": rule["value"], "intent": "learned",
                "model": "learn-loop", "elapsed": self._elapsed(ctx),
                "learned_id": rule["id"]}

    # ── /learn 命令 (人工门) ──
    async def _run_cmd_ledger(self, message, ctx):
        """/ledger —— 日常干活账 (只读, 不写状态)。"""
        try:
            from core.task_ledger import get_task_ledger
            txt = get_task_ledger().render()
        except Exception as e:
            txt = f"干活账读不出来: {type(e).__name__}: {e}"
        return {"response": txt, "intent": "ledger", "model": "local",
                "route": "cmd.ledger", "elapsed": self._elapsed(ctx)}

    async def _run_cmd_learn(self, message, ctx):
        """/learn — 学习闭环的查看与人工门。

        /learn                  概况 (学了多少/命中多少/待学问题)
        /learn list [kind]      列出规则 (active)
        /learn stale            列出休眠的
        /learn forget <id>      删除某条
        /learn reject <id>      否决某条
        /learn activate <id>    启用某条 (含休眠的复活)
        /learn pending          待学问题池
        /learn answer <问题> -> <答案>   给待学问题补答案 (直接变成规则)
        /learn decay            手动跑一次衰减
        """
        parts = message.strip().split(None, 2)
        sub = (parts[1].lower() if len(parts) > 1 else "stats")
        arg = (parts[2].strip() if len(parts) > 2 else "")
        ll = getattr(self, "learn_loop", None)
        el = lambda: self._elapsed(ctx)
        base = {"intent": "learn", "model": "local", "route": "cmd.learn"}
        if ll is None:
            return {**base, "response": "学习闭环未初始化。", "elapsed": el()}

        if sub in ("stats", "help", "?"):
            s = ll.stats()
            lines = ["学习闭环 — 学到的东西会**立即生效**, 被纠正会自动降级/撤销",
                     f"  活跃: {s['by_status'].get('active', 0)} 条"
                     f" (偏好 {s['by_kind'].get('preference', 0)} · 答案 {s['by_kind'].get('answer', 0)})",
                     f"  已撤销: {s['by_status'].get('rejected', 0)} · 休眠: {s['by_status'].get('stale', 0)}",
                     f"  生效命中: {s['hits']} 次 · 被纠正: {s['misses']} 次",
                     f"  待学问题: {s['open_questions']} 个",
                     "",
                     "  /learn list [preference|answer]   /learn stale   /learn pending",
                     "  /learn forget|reject|activate <id>   /learn decay",
                     "  /learn answer <问题> -> <答案>"]
            return {**base, "response": "\n".join(lines), "elapsed": el(), "stats": s}

        if sub == "list":
            kind = arg if arg in ("preference", "answer") else None
            rows = ll.list_rules(status="active", kind=kind, limit=30)
            if not rows:
                return {**base, "response": "还没学到任何东西。教一句试试: 「以后默认发给涛哥」", "elapsed": el()}
            out = [f"活跃规则 {len(rows)} 条:"]
            for r in rows:
                mark = "★稳固" if r["weight"] >= 2 else "·低置信"
                out.append(f"  #{r['id']} [{r['kind']}] {mark} 命中{r['hits']}/纠正{r['misses']}")
                out.append(f"      {r['key'][:40]} → {r['value'][:60]}")
            out.append("")
            out.append("删: /learn forget <id> · 否决: /learn reject <id>")
            return {**base, "response": "\n".join(out), "elapsed": el()}

        if sub == "stale":
            rows = ll.list_rules(status="stale", limit=30)
            if not rows:
                return {**base, "response": "没有休眠的规则。", "elapsed": el()}
            out = [f"休眠规则 {len(rows)} 条 (久未命中或被淘汰, 数据还在):"]
            for r in rows:
                out.append(f"  #{r['id']} [{r['kind']}] {r['key'][:36]} → {r['value'][:40]}")
                if r.get("note"):
                    out.append(f"      原因: {r['note'][:60]}")
            out.append("")
            out.append("复活: /learn activate <id> · 删除: /learn forget <id>")
            return {**base, "response": "\n".join(out), "elapsed": el()}

        if sub in ("forget", "reject", "activate"):
            if not arg.isdigit():
                return {**base, "response": f"用法: /learn {sub} <id>", "elapsed": el()}
            rid = int(arg)
            rule = ll.get(rid)
            if not rule:
                return {**base, "response": f"没有 #{rid} 这条规则。用 /learn list 看编号。", "elapsed": el()}
            ok = (ll.forget(rid) if sub == "forget" else
                  ll.reject(rid) if sub == "reject" else ll.activate(rid))
            verb = {"forget": "已删除", "reject": "已否决", "activate": "已启用"}[sub]
            return {**base, "response": f"{verb} #{rid}: {rule['key'][:40]} → {rule['value'][:40]}",
                    "elapsed": el(), "ok": bool(ok)}

        if sub == "pending":
            qs = ll.open_questions(limit=20)
            if not qs:
                return {**base, "response": "没有待学问题。", "elapsed": el()}
            out = [f"待学问题 {len(qs)} 个 (问过但我没答上, 补答案就学会):"]
            for q in qs:
                out.append(f"  [{q['count']}次] {q['question'][:70]}")
            out.append("")
            out.append("补答案: /learn answer <问题> -> <答案>")
            return {**base, "response": "\n".join(out), "elapsed": el()}

        if sub == "answer":
            if "->" not in arg:
                return {**base, "response": "用法: /learn answer <问题> -> <答案>", "elapsed": el()}
            q, a = (x.strip() for x in arg.split("->", 1))
            if not q or not a:
                return {**base, "response": "问题和答案都不能空。", "elapsed": el()}
            r = ll.answer_open_question(q, a)
            return {**base, "response": f"学会了: {q[:40]} → {a[:60]}\n以后这么问就答这个。",
                    "elapsed": el(), "rule_id": r.get("id")}

        if sub == "decay":
            d = ll.decay()
            return {**base, "response": f"衰减完成: 休眠 {d['stale']} 条 · 淘汰 {d['evicted']} 条。",
                    "elapsed": el(), "decay": d}

        return {**base, "response": f"未知子命令 '{sub}'。用 /learn 看用法。", "elapsed": el()}

    def _learn_pref(self, intent, model, success=True):
        if success: self._prefs[intent] = model; self._save_prefs()

    def _get_pref_model(self, intent): return self._prefs.get(intent)


    def _guard_fix_context(self, message: str):
        """Vague fix/modify requests without context → collect context, offer Chinese options.
        Returns dict with options if context missing, None if OK to proceed."""
        import re, os, glob

        msg = message.strip()

        # 1. Vague fix keywords
        fix_kw = ['修复', '修一下', '改一下', '改改', '修修', '解决', '处理一下',
                  'fix', 'debug', '修bug', '改bug', '修这个', '改这个',
                  '帮我修', '帮我改', '帮我修复', '帮我解决']
        if not any(kw in msg for kw in fix_kw):
            return None

        # 2. Already has context?
        has_file = bool(re.search(r'[\w/\\-]+\.(py|json|md|txt|yaml|toml|cfg)', msg))
        has_symptom = any(kw in msg for kw in [
            '报错', '错误', 'error', '崩了', '不行', '不对', '打不开',
            '没反应', '闪退', '卡住', '没显示', '失败', '出问题', '异常',
            '坏了', '不正常', '报错信息', '日志', '提示', '警告'])
        has_line = bool(re.search(r'(第?\d+行|line\s*\d+|L\d+)', msg, re.IGNORECASE))
        if has_file or has_symptom or has_line:
            return None

        # 3. Collect context
        options = []
        project_root = str(PROJECT_ROOT)

        # a) Recently modified .py files (last 24h)
        recent = []
        cutoff = time.time() - 86400
        try:
            for f in glob.glob(os.path.join(project_root, '**', '*.py'), recursive=True):
                if '__pycache__' in f or 'backup' in f.lower():
                    continue
                try:
                    mtime = os.path.getmtime(f)
                    if mtime > cutoff:
                        rel = os.path.relpath(f, project_root)
                        recent.append((rel, mtime))
                except OSError:
                    pass
        except Exception:
            pass
        recent.sort(key=lambda x: -x[1])

        if recent:
            top = recent[0][0]
            options.append(f"最近改过 {top}")
            if len(recent) > 1:
                options.append(f"还有 {recent[1][0]} 等{len(recent)}个文件最近动过")

        # b) Known issues from memory
        try:
            if hasattr(self, 'memory') and self.memory:
                mem = self.memory.build_context_for_model('bug 错误 问题 fix')
                if mem and len(mem) > 10:
                    for line in mem.split('\n'):
                        line = line.strip('- •· 	')
                        if any(k in line for k in ['bug','错误','fix','问题','修','崩','报错']):
                            if line and len(line) > 6:
                                options.append(f"之前记过：{line[:60]}")
                            if len(options) >= 5:
                                break
        except Exception:
            pass

        # c) Stats
        try:
            errs = self.stats.get('errors', 0) if hasattr(self, 'stats') else 0
            if errs > 0:
                options.append(f"本次运行 {errs} 个错误")
        except Exception:
            pass

        if not options:
            return None

        lines_out = ["说清楚点，你想修哪个？"]
        for i, opt in enumerate(options[:5], 1):
            lines_out.append(f"  {i}. {opt}")
        lines_out.append("  0. 都不是，我详说")

        return {
            "response": "\n".join(lines_out),
            "intent": "fix_context_guard",
            "model": "local",
            "elapsed": 0.01
        }

    # ── L4 Decomposition ──
    def _get_entity_context(self):
        if self._ke is None:
            from core.knowledge import KnowledgeEngine
            self._ke = KnowledgeEngine(DATA_DIR)
        lines = []
        for name, info in self._ke.entities.items():
            lines.append(f"  {name}: {json.dumps(info, ensure_ascii=False)}")
        for name, info in self._ke.environment.items():
            lines.append(f"  [{name}] -> {info['value']}")
        return "\n".join(lines)

    def _get_tool_catalog(self):
        return "\n".join(f"  {n}" for n in self.plugins._plugins) or "  (none)"

    async def _decompose_task(self, message, model_name):
        entity_ctx = self._get_entity_context()
        tool_cat = self._get_tool_catalog()
        system = f"Available tools:\n{tool_cat}\n\nKnown entities:\n{entity_ctx}\n\nBreak the task into steps. Return JSON array: [{{\"tool\":\"...\",\"action\":\"...\",\"params\":{{...}}}}]"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": f"Task: {message}"}]
        try:
            raw = await self.model_client.generate(model_name, messages, temperature=0.1, max_tokens=3000)
            text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
            if text:
                # JSON extraction (model text fallback)
                m = re.search(r'\[.*\]', text, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group())
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning("Decomposition failed: %s", e)
        return None

    async def _execute_steps(self, steps):
        ctx = {"_steps": [], "_success": True}
        for step in steps:
            tool = step.get("tool", "")
            action = step.get("action", "execute")
            params = step.get("params", {})
            try:
                result = await self.gateway.call(tool, action=action, **params)
                ctx["_steps"].append({"skill": tool, "tool": tool, "result": result})
                if isinstance(result, dict) and not result.get("success", True):
                    ctx["_success"] = False; break
            except Exception as e:
                ctx["_steps"].append({"skill": tool, "tool": tool, "result": {"success": False, "error": str(e)}})
                ctx["_success"] = False; break
        return ctx

    # ── Intent Reasoner ──
    async def _handle_reasoner(self, message, t0):
        if self._reasoner is None: return None
        try: ir = self._reasoner.reason(message)
        except Exception: return None
        if ir.get('confidence', 0) < 0.5 or ir.get('intent') == 'unknown': return None
        slots = ir.get('slots', {})
        entities = ir.get('entities', {})
        if ir['intent'] == 'send' and slots.get('to') and slots.get('path'):
            path = slots['path']
            try:
                from storage.file_secure import safe_list_dir
                items = safe_list_dir(path)
                lines = []
                for item in items[:30]:
                    sz = item.stat().st_size if not item.is_dir() else 0
                    lines.append("  " + item.name + ("/" if item.is_dir() else "") + " (" + str(sz) + "B)")
                listing = "\n".join(lines) or "(empty)"
            except Exception as e: listing = "(error: " + str(e) + ")"
            who = entities.get('who', '?')
            wn = entities.get('where', {}).get('name', '?') if entities.get('where') else '?'
            return {"response": "[" + who + "] " + wn + "目录:\n" + listing + "\n\n要发哪个文件？回复文件名。",
                    "intent": "file_send_list", "model": "local", "elapsed": round(time.time() - t0, 2),
                    "_pending_file_send": {"dir": path, "to": slots["to"]}}
        if ir['intent'] == 'list' and slots.get('path'):
            try:
                from storage.file_secure import safe_list_dir
                items = safe_list_dir(slots['path'])
                lines = []
                for item in items[:50]:
                    sz = item.stat().st_size if not item.is_dir() else 0
                    lines.append("  " + item.name + ("/" if item.is_dir() else "") + " (" + str(sz) + "B)")
                return {"response": "\n".join(lines) or "(empty)", "intent": "list_dir",
                        "model": "local", "elapsed": round(time.time() - t0, 2)}
            except Exception: pass
        return None

    # ═══ Main Process Pipeline ═══

    def _classify_intent(self, message):
        """意图门：在关键词匹配前判断消息类型。"""
        msg = message.strip()
        # ── 显式工具调用语法 → 走 command 路径，进入 function calling 管线 ──
        _tool_syntax_markers = [
            'file_ops(', 'shell_exec(', 'send_email(', 'system_info(',
            'windows_desktop(', 'web_search(', 'openmeteo(', '/tool ',
            'action=read', 'action=write', 'action=list', 'action=delete',
        ]
        if any(m in msg for m in _tool_syntax_markers):
            return 'command'
        task_prefixes = [
            '工程题', '任务：', '任务:', '帮我写', '帮我实现', '帮我建',
            '实现一个', '新建一个', '新建agent ', '新建文件',
            '写一个agent', '写代码', '构建', '开发一个', '创建一个',
            '请帮我写', '请实现', '请新建', '请构建',
            '# 工程', '# 任务', '## 工程', '## 任务',
            '给你一个任务', '给你任务',
        ]
        # 2026-08-20 v3: 含文件扩展名的消息优先走 command (文件操作), 不误判 task
        # 例: "新建agent_test.txt文档保存到桌面" 是写文件, 不是工程任务
        if any(msg.startswith(p) for p in task_prefixes):
            _ext_re = re.search(r'\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip)\b', msg)
            if _ext_re:
                return 'command'
            return 'task'
        task_markers = [
            '在 core/ 下新建', '在core/下新建', '在 tools/ 下新建',
            '实现 class', '新建 .py', '写一个 agent',
            '新建模块', '重构', '添加功能',
            '集成', '写一个新插件', '新建插件', '长一个新能力',
            '写到 tools/', '创建插件',
        ]
        if any(m in msg[:40] for m in task_markers):
            return 'task'
        if len(msg) > 150:
            return 'task'
        if '\n' in msg:
            return 'task'
        return 'command'

    def _mcp_refresh_bg(self):
        """后台线程: 刷新 MCP 外部服务器工具列表(不阻塞启动)。"""
        try:
            import asyncio as _aio
            # ⚠ 本线程在 __init__ 早期启动, self.mcp 可能尚未赋值 → 安全解析
            _mcp = getattr(self, "mcp", None) or getattr(getattr(self, "gateway", None), "mcp", None)
            if _mcp is None:
                logger.warning("MCP background refresh skipped: mcp client not ready")
                return
            _aio.run(_mcp.refresh_all())
            _n = len(_mcp.get_all_tools())
            logger.info("MCP refresh done: %d external tool(s) available", _n)
        except Exception as _e:
            logger.warning("MCP background refresh failed: %s", _e)

    def aclose_resources(self):
        """返回一个 awaitable, 用于关闭本 pipeline 持有的外部子进程 (MCP)。

        入口点退出时调用 —— 否则 MCP 子进程变孤儿, 且 asyncio 传输留到 GC 才收
        (那时 loop 已关 → "Event loop is closed")。
        返回 coroutine 而非 async def, 方便入口点用 `await ...` 或包进 asyncio.run。
        """
        mcp = getattr(self, "mcp", None) or getattr(getattr(self, "gateway", None), "mcp", None)
        if mcp is not None and hasattr(mcp, "aclose"):
            return mcp.aclose()

        async def _noop():
            return None
        return _noop()

    def _lay_state_enabled(self) -> bool:
        """「执行前铺状态」是否打开 —— **默认关**。

        用户要求输出干净(不能出现"虎哥理解"/调试行), 所以默认静默;
        需要看"Agent 怎么理解你这句话"时用 `/state on` 打开。
        开关落在 data/prefs.json 的 `lay_state` 键 (与既有的 regex_passthrough 同类)。
        """
        try:
            return bool(self._prefs.get("lay_state", False))
        except Exception:
            return False

    def set_lay_state(self, arg: str = "") -> str:
        """查询/切换「执行前铺状态」开关, 返回给用户看的一行说明。

        arg: '' 查询 | 'on' 打开 | 'off' 关闭
        抽成方法是为了可测 —— CLI 的 /state 命令直接调它 (CLI REPL 本身难自动化)。
        """
        a = (arg or "").strip().lower()
        if a in ("on", "off"):
            self._prefs["lay_state"] = (a == "on")
            self._save_prefs()
            return ("[铺状态] 已打开 — 每次执行前会显示理解摘要" if a == "on"
                    else "[铺状态] 已关闭 — 输出只留结果")
        state = "开" if self._lay_state_enabled() else "关"
        return f"[铺状态] 当前: {state}   (/state on 打开, /state off 关闭)"

    def _lay_state(self, message, ext_model=None):
        """Pre-execution understanding — 默认**静默**。

        契约 (2026-09-18 显式化, 此前实现与测试相左 → canonical 长期 3 failed):
            默认      → (None, False)     用户只看到结果, 没有"虎哥理解"噪声
            /state on → (state_msg, needs_confirm)  打印理解 + 任务模式等确认
        不管开关如何都写 tmm_output.log —— 审计需要, 但不占用户屏幕。
        """
        msg = message.strip()
        output_log = Path(__file__).parent.parent / "tmm_output.log"
        intent = self._classify_intent(message)
        model_name = ext_model or "auto"
        
        # 构建理解摘要
        if msg.startswith('/'):
            understanding = f"执行内置命令: {msg.split()[0]}"
        elif intent == 'task':
            summary = msg[:100].replace('\n', ' ').strip()
            if len(msg) > 100:
                summary += "…"
            understanding = f"任务模式 — {summary}（模型: {model_name}）"
        else:
            summary = msg[:100].replace('\n', ' ').strip()
            if len(msg) > 100:
                summary += "…"
            understanding = f"对话/指令 — {summary}（模型: {model_name}）"
        
        # 写审计日志 (始终写, 与开关无关)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{ts}] 虎哥理解 | intent={intent} | model={model_name} | msg={msg[:200]}\n"
        try:
            with open(output_log, 'a', encoding='utf-8') as f:
                f.write(log_line)
        except Exception:
            pass
        
        if not self._lay_state_enabled():
            return None, False        # 默认静默: 不打印、不打断

        state_msg = f"{GOLD}[虎哥理解]{RST} {understanding}"
        needs_confirm = (intent == 'task')  # 任务模式需确认
        return state_msg, needs_confirm

    async def _process_task(self, message, t0):
        """任务模式：LLM 多轮 agent loop，可调工具。"""
        logger = logging.getLogger("core.pipeline")
        import os as _os_pt, re as _re_pt
        model_name = "deepseek"
        project_root = str(PROJECT_ROOT)
        system = (
            "你是 TMM 工程助手。项目根目录: " + project_root + "。"
            "收到工程任务请逐步完成。先读代码再写代码。\n"
            "完成后必须逐条自检并输出：\n"
            "① 改动清单: 具体改了哪些文件、什么位置\n"
            "② 验证方式: 如何确认改动正确（测试通过/MD5比对/shell验证等）\n"
            "③ 失败项: 哪些没通过、为什么\n"
            "缺少任何一项视为未完成。"
            "你可以用以下方式操作文件:"
            "1. 输出代码块（自动写入）："
            "   # File: core/xxx.py"
            "   ```python"
            "   ...code..."
            "   ```"
            "2. 或用工具命令："
            "   /tool file_read path=core/xxx.py"
            "   /tool file_list path=core/"
            "   /tool shell_exec cmd=dir"
            "代码块会自动写入对应文件，结果反馈给你。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": message}
        ]
        last_text = ""
        for round_num in range(8):
            try:
                raw = await self.model_client.generate(
                    model_name, messages, temperature=0.3, max_tokens=4000
                )
            except Exception as e:
                return {"response": f"任务失败(第{round_num+1}轮): {e}",
                        "intent": "task_error", "model": "local",
                        "elapsed": round(time.time() - t0, 2)}
            text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
            last_text = text
            tool_results = []
            # --- 自动提取代码块 ---
            code_blocks = _re_pt.findall(
                r'(?:#?\s*File:\s*(\S+)\s*\n)?```(?:python|py)?\n(.*?)```',
                text, _re_pt.DOTALL
            )
            for file_hint, code in code_blocks:
                code = code.strip()
                if not code or len(code) < 10:
                    continue
                if file_hint and not _os_pt.path.isabs(file_hint):
                    file_path = _os_pt.path.join(project_root, file_hint)
                else:
                    file_path = _os_pt.path.join(project_root, "core", "output.py")
                try:
                    _os_pt.makedirs(_os_pt.path.dirname(file_path), exist_ok=True)
                    with open(file_path, 'w', encoding='utf-8') as fout:
                        fout.write(code)
                    tool_results.append(f"[OK] 已写入 {file_path} ({len(code)} 字符)")
                except Exception as e:
                    tool_results.append(f"[ERR] 写入 {file_path}: {e}")
            # --- 解析 /tool 命令 ---
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("/tool "):
                    result = await self._execute_tool_line(stripped)
                    tool_results.append(f"/tool {stripped[6:]} -> {result}")
            if not tool_results:
                return {"response": text[:5000], "intent": "task",
                        "model": model_name, "elapsed": round(time.time() - t0, 2),
                        "code_rounds": round_num + 1}
            feedback = "\n".join(tool_results)
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": feedback})
        return {"response": last_text[:5000], "intent": "task_max_rounds",
                "model": model_name, "elapsed": round(time.time() - t0, 2),
                "code_rounds": 8}

    async def _execute_tool_line(self, line):
        """执行单条 /tool 命令。"""
        parts = line.split(None, 2)
        if len(parts) < 2:
            return "用法: /tool <name> [key=value ...]"
        tool_name = parts[1]
        raw_input = parts[2] if len(parts) > 2 else ""
        ALIASES = {
            "file_read": ("file_ops", "read"), "file_write": ("file_ops", "write"),
            "file_list": ("file_ops", "list"), "file_copy": ("file_ops", "copy"),
            "file_move": ("file_ops", "move"), "file_delete": ("file_ops", "delete"),
            "file_search": ("file_ops", "search"), "file_walk": ("file_ops", "walk"),
            "read_file": ("file_ops", "read"), "write_file": ("file_ops", "write"),
            "list_dir": ("file_ops", "list"), "search_files": ("file_ops", "search"),
            "mkdir": ("file_ops", "mkdir"), "rm": ("file_ops", "delete"),
            "cp": ("file_ops", "copy"), "mv": ("file_ops", "move"),
            "shell_exec": ("shell_exec", None), "web_search": ("web_search", "search"),
        }
        call_kwargs = {}
        if "=" in raw_input:
            # Parse key=value pairs; use regex for path= values to preserve backslashes
            import re as _re_pt
            for m in _re_pt.finditer(r'(\w+)=("([^"]+)"|\'([^\']+)\'|(\S+))', raw_input):
                k = m.group(1)
                v = m.group(3) or m.group(4) or m.group(5)
                call_kwargs[k.strip()] = v
            if not call_kwargs:
                # Fallback: simple split
                for tok in raw_input.split():
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        call_kwargs[k.strip()] = v.strip().strip('"').strip("'")
        if "content_base64" in call_kwargs:
            import base64 as _b64
            try:
                call_kwargs["content"] = _b64.b64decode(
                    call_kwargs.pop("content_base64")).decode("utf-8")
            except Exception:
                pass
        try:
            if tool_name in ("shell_exec",):
                result = await self.gateway.call("shell_exec", cmd=raw_input)
            elif tool_name in ("web_search",):
                result = await self.gateway.call("web_search", query=raw_input, action="search")
            elif tool_name in ("code", "python", "py", "run_py"):
                # CodeEngine: sandboxed execution with syntax check
                self.code_engine.new_session("cli")
                import re as _re_ce
                code_match = _re_ce.search(r'```(?:python)?\s*\n(.+?)```', raw_input, _re_ce.DOTALL)
                code = code_match.group(1) if code_match else raw_input
                fname = f"script_{int(time.time())}.py"
                ce_result = self.code_engine.run_pipeline(fname, code)
                lines = [f"[code] syntax={'OK' if ce_result.syntax_ok else 'FAIL'} "
                         f"success={ce_result.success} ({ce_result.elapsed:.1f}s)"]
                if ce_result.stdout:
                    lines.append(f"stdout:\n{ce_result.stdout[:3000]}")
                if ce_result.stderr and not ce_result.success:
                    lines.append(f"stderr:\n{ce_result.stderr[:1000]}")
                return "\n".join(lines)
            elif tool_name in ALIASES:
                plugin_name, action = ALIASES[tool_name]
                if action:
                    call_kwargs["action"] = action
                result = await self.gateway.call(plugin_name, **call_kwargs)
            else:
                result = await self.gateway.call(tool_name, **call_kwargs)
            if isinstance(result, dict):
                if result.get("success"):
                    data = result.get("data", result.get("result", ""))
                    if data:
                        import json as _json_pt
                        return _json_pt.dumps(data, ensure_ascii=False, default=str)[:800]
                    return result.get("output", "ok")[:500]
                return f"ERR: {result.get('error', str(result))}"[:200]
            return str(result)[:500]
        except Exception as e:
            return f"ERR: {e}"[:200]

    def get_last_turns(self, n: int = 6) -> list:
        """Return last n messages for session recovery."""
        try:
            return list(self.sessions.messages[-n:])
        except Exception:
            return []

    def _get_brain(self):
        if self._brain is None:
            # Ensure deps are ready
            if self._ke is None:
                from core.knowledge import KnowledgeEngine
                self._ke = KnowledgeEngine(DATA_DIR)
            if self._reasoner is None:
                from core.intent_reasoner import IntentReasoner
                self._reasoner = IntentReasoner(self._ke.entities, self._ke.environment)
            from core.workflow import WorkflowEngine
            if self._we is None:
                self._we = WorkflowEngine(DATA_DIR, self._ke)
            self._brain = TigerBrain(
                self.auto_guide, self._ke, self._we,
                self._reasoner, self.gateway.plugin_mgr, self.model_client
            )
            # Share memory
            self._brain.mem = self.memory
        return self._brain

    async def process(self, message, ext_model=None, user=None, mode_override=None,
                      probe=False):
        """唯一入口 —— 跑完由这里**统一收尾落库**。

        ★ 2026-09-19 修 (实测确认的真缺陷): 原来只有 CLI (core/cli.py / 旧
          pipeline.run_cli) 会写 data/chat_sessions.db, 而 Web / 桌面端 / relay /
          scheduler 走的这条 process() **只写内存 deque** (TigerMemory, maxlen=50)
          → 关掉引擎历史就没了。
          实测证据: 桌面端发 3 条 → /history 有(resp 内存) 但库里搜不到;
          重启引擎 → 2 条历史变 0 条; 全库统计 add_message 只在 CLI 两处被调用。
        现在: 所有入口都经这里 → 写库一处收口 (与 memory.add_turn 同一守卫 probe)。
        """
        # ★ 2026-09-20: 把 probe 状态告知模型客户端 → 探针流量**不记用量账**
        #   (否则验证/门禁跑的调用会混进真实用量统计, 数字虚高)。
        #   用 contextvar + token 恢复: 嵌套调用 (plan→craft 二次进 process) 也正确。
        _mc_token = None
        try:
            from core import model_client as _mcmod
            _mc_token = _mcmod.set_probe(probe)
        except Exception:
            _mc_token = None
        try:
            result = await self._process_impl(message, ext_model=ext_model, user=user,
                                              mode_override=mode_override, probe=probe)
        finally:
            if _mc_token is not None:
                try:
                    _mcmod.reset_probe(_mc_token)
                except Exception:
                    pass
        if not probe:
            self._persist_turn(message, result)
        return result

    def _persist_turn(self, message, result):
        """把一轮对话落进 chat_sessions.db (user + assistant)。

        ★ 只在这里写 —— 内部自调用 (_process_impl) 不经此函数, 所以 plan→craft
          这种"一条消息跑两遍"的场景**不会重复入库**。
        写库失败绝不影响对话本身 (与 CLI 原来的 try/except 口径一致)。
        """
        try:
            r = result if isinstance(result, dict) else {}
            intent = r.get("intent", "general")
            model = r.get("model", "local")
            elapsed = r.get("elapsed", 0)
            self.sessions.add_message("user", message, intent, model, elapsed)
            self.sessions.add_message("assistant", r.get("response", ""), intent, model, elapsed)
        except Exception:
            # ★ 不静默: 落库失败要看得见 (实测坑: 跨线程 sqlite 报错被 debug 吞掉,
            #   表现为"历史悄悄不落库", 排查半天)。对话本身不受影响。
            logger.warning("persist turn failed (对话正常, 仅历史未入库)", exc_info=True)

    async def _process_impl(self, message, ext_model=None, user=None, mode_override=None,
                            probe=False):
        if self._ke is None:
            from core.knowledge import KnowledgeEngine
            self._ke = KnowledgeEngine(DATA_DIR)
        if self._reasoner is None:
            from core.intent_reasoner import IntentReasoner
            self._reasoner = IntentReasoner(self._ke.entities, self._ke.environment)
        t0 = time.time()
        # ── 990 危险操作拦截已迁移到 core/routes.py (route: regulator.dangerous) ──

        self.stats["total"] += 1
        logger.debug("process[#%d] model=%s msg=%.60s",
                     self.stats["total"], ext_model or "auto", message)

        # Auto-record session for tracking
        sid = self.sm.spawn(message, model=ext_model or "auto")
        self._last_sid = sid

        # Memory: record user turn
        # probe=True (验证/探针流量) 不写记忆 —— 否则测试会污染用户会话历史
        if not probe:
            self.memory.add_turn("user", message)
        self.backup.auto_tick()  # auto backup every 2h

        # ── 950 三模式闸门已迁移到 core/routes.py (route: mode.gate) ──
        #   闸门只处理 ask/plan; craft(默认) 不受影响 → 路由表 match 里判断

        # ── /voice 已迁移到 core/routes.py (route: cmd.voice, priority=1000) ──

        # ── 当前模式 (三模式闸门的输入; 闸门本身在路由表 mode.gate, 950) ──
        try:
            _eff_mode = mode_override or self.modes.get()
        except Exception:
            _eff_mode = "craft"

        # ── 路由表裁决 (P1) ──
        # 内部命令已迁到 core/routes.py 声明 (优先级 = Route.priority, 不再靠行号)。
        # 返回 None = 表里没有接手 → 继续走下面的旧链路 (加性, 不破坏旧行为)。
        _routed = await self.routes.dispatch(message, {
            "ext_model": ext_model, "probe": probe, "t0": t0, "user": user,
            "eff_mode": _eff_mode, "mode_override": mode_override,
        }, owner=self)
        if _routed is not None:
            return _routed

        # ── 600 分析预读 已迁移到 core/routes.py ──
        #   analysis.preread_reuse / analysis.office_first / analysis.goto_model

        # ── 360 Brain 决策树 已迁移到 core/routes.py (route: brain.route) ──
        #   位置须高于 SmartRoute/IR chain: 原顺序 Brain 最先 (实测被 IR chain 抢会误报'已收到')


        # ═══════════════════════════════════════════════════════════
        # 守卫的**副作用**部分 (对所有未被路由接手的消息都要跑 —— 与旧内联等价):
        #   风险分级只记日志; 约束解析(严禁/禁止/必须…)供后续模型路径使用。
        # 拦截逻辑本身在路由表 (guard.input_filter, 590)。
        # ═══════════════════════════════════════════════════════════
        try:
            logger.debug("process[#%d] risk=%s", self.stats["total"],
                         classify_command(message).value)
            self.sre.parse_user_constraints(message)
        except Exception:
            pass

        # 兜底 (理论上到不了: model.fallback 是终止路由, 永远匹配)
        return {"response": "虎哥暂时没法回答。", "intent": "error", "model": "local",
                "elapsed": round(time.time() - t0, 2)}


    # ═══════════════════════════════════════════════════════════════
    # 三模式实现: Ask(问答) / Plan(规划) / Craft(实干)
    # ═══════════════════════════════════════════════════════════════

    _ASK_SYS = (
        "You are Tiger.M.M (MARY III), a local AI assistant on Windows. "
        "You are in ASK MODE (问答模式).\n"
        "RULES:\n"
        "1. ONLY answer the question. Do NOT execute anything — no file reads/writes, "
        "no commands, no system operations, no tool calls.\n"
        "2. NEVER claim you performed an action (created a file, sent an email, opened an app). "
        "You have no tools in this mode. If the user asks you to DO something, "
        "tell them to switch to 实干(Craft) mode.\n"
        "3. Respond in Chinese. Plain text ONLY — no markdown: no **bold**, no ## headers, "
        "no - bullets, no `code fences`. Use numbers for lists, blank lines for paragraphs.\n"
        "4. Give complete, detailed answers — not one-liners."
    )

    _PLAN_SYS = (
        "You are Tiger.M.M (MARY III), a local AI assistant on Windows. "
        "You are in PLAN MODE (规划模式). Your job is to produce an executable plan, "
        "NOT to execute it.\n"
        "RULES:\n"
        "1. Output 3 to 7 numbered steps. Each step is ONE action, verb first.\n"
        "2. Be concrete: say WHAT gets done and WITH WHAT (exact file path / tool / command). "
        "Never invent paths or facts the user did not give you.\n"
        "3. Do NOT execute anything and do NOT claim anything has been done already.\n"
        "4. End with exactly this line: 确认后开始执行。\n"
        "5. Respond in Chinese. Plain text ONLY — no markdown symbols."
    )

    def _ask_messages(self, message, system):
        """Ask/Plan 模式的消息组装 — 带上会话历史, 不挂工具。"""
        msgs = [{"role": "system", "content": system}]
        try:
            _hist = self.sessions.recent(6)
            _h = []
            for x in _hist:
                c = str(x.get("content", ""))
                if not c.strip() or "【文件" in c:
                    continue
                if x.get("role") == "assistant":
                    c = c[:1500]
                else:
                    c = c[:400]
                _h.append({"role": x.get("role", "user"), "content": c})
            if _h:
                msgs += _h
        except Exception as _e:
            # ★ 2026-09-24 修: 原为 `pass` —— 同上, 静默无历史。
            _warn_hist_read_failed("ask", _e)
        msgs.append({"role": "user", "content": message})
        return msgs

    @staticmethod
    def _mode_pick_model(ext_model):
        return ext_model if (ext_model and ext_model != "auto") else "deepseek"

    def _with_ask_hint(self, text: str, message: str) -> str:
        """问答模式统一出口提示: 要干活的请求必须当场说清不执行。

        ★ 2026-09-23: 抽成方法是为了覆盖**所有出口** —— 实测只在成功那条 return 加提示,
          交付包(无凭据)走"模型返回错误"那条 ⇒ 用户看到报错却仍不知自己被模式拦着。
        """
        try:
            if self._looks_like_request(message):
                return (str(text).rstrip() +
                        "\n\n（当前是**问答模式**：只回话、不执行。要我真去干，"
                        "说 /mode craft 切到实干。）")
        except Exception:
            pass
        return str(text)

    async def _process_ask(self, message, ext_model, t0, probe=False):
        """Ask 模式 — 纯问答: 不挂工具, 不执行任何操作。"""
        model_name = self._mode_pick_model(ext_model)
        try:
            r = await asyncio.wait_for(
                self.model_client.generate(
                    model_name,
                    self._ask_messages(message, self._ASK_SYS),
                    temperature=0.7, max_tokens=3000),
                timeout=90)
        except Exception as e:
            return {"response": self._with_ask_hint(f"[问答模式] 模型调用失败: {e}", message),
                    "model": model_name, "elapsed": round(time.time() - t0, 2),
                    "mode": "ask", "trace": []}
        if isinstance(r, dict) and r.get("error"):
            return {"response": self._with_ask_hint(f"[问答模式] 模型返回错误: {r['error']}", message),
                    "model": model_name, "elapsed": round(time.time() - t0, 2),
                    "mode": "ask", "trace": []}
        text = ((r or {}).get("text") or "").strip()
        if not text:
            _t = (r or {}).get("think") or ""
            if sum(1 for ch in _t if '\u4e00' <= ch <= '\u9fff') >= 2:
                text = _t
        if not text:
            text = "[问答模式] 模型没有给出内容，换个说法再试试。"
        # 记账 + 感知学习
        try:
            _usage = (r or {}).get("usage")
            if _usage and _usage.get("total_tokens"):
                from core.token_tracker import get_tracker
                get_tracker().record(model_name, _usage)
        except Exception:
            pass
        try:
            if not probe and getattr(self, "perception", None):
                self.perception.analyze(message, text, memory=self.memory)
        except Exception:
            pass
        return {"response": self._with_ask_hint(text[:6000], message),
                "elapsed": round(time.time() - t0, 2), "mode": "ask",
                "tool_results": 0, "trace": []}

    @staticmethod
    def _fmt_plan_step(i, s):
        tool = s.get("tool", "?")
        act = s.get("action") or ""
        desc = s.get("description") or s.get("desc") or ""
        params = s.get("params") or {}
        p = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(params.items())[:4])
        line = f"  {i}. {tool}"
        if act:
            line += f" / {act}"
        if desc:
            line += f" — {desc}"
        if p:
            line += f"\n     参数: {p}"
        return line

    def _format_plan_text(self, message, steps):
        goal = message.replace("\n", " ").strip()
        if len(goal) > 120:
            goal = goal[:120] + "…"
        lines = ["[规划模式] 方案已生成，等你确认。", "",
                 f"目标：{goal}", "", f"共 {len(steps)} 步："]
        for i, s in enumerate(steps, 1):
            lines.append(self._fmt_plan_step(i, s))
        lines += ["",
                  "回复「执行」我就按这个方案动手。「取消」放弃。",
                  "要改哪一步直接说，我重出方案。"]
        return "\n".join(lines)

    async def _plan_text_via_model(self, message, ext_model, prev=None):
        """规划器拆不出可执行步骤时的兜底: 让模型出文本方案(不挂工具)。"""
        model_name = self._mode_pick_model(ext_model)
        user = message
        if prev and prev.get("text"):
            user = ("（上一版方案如下，用户提出了新意见，请据此重出方案）\n"
                    + prev["text"][:1200] + "\n\n用户现在说：" + message)
        msgs = [{"role": "system", "content": self._PLAN_SYS},
                {"role": "user", "content": user}]
        text = ""
        try:
            r = await asyncio.wait_for(
                self.model_client.generate(model_name, msgs,
                                           temperature=0.4, max_tokens=1500),
                timeout=90)
            text = ((r or {}).get("text") or "").strip()
        except Exception as e:
            logger.warning("plan text model failed: %s", e)
        if not text:
            text = ("[规划模式] 没能生成方案（模型无响应）。\n\n"
                    "可以直接把要做的事说清楚，或者切到实干模式边做边看。")
        return text, model_name

    async def _plan_propose(self, message, ext_model, t0, prev=None, persist=True):
        """Plan 模式 — 出方案, 不执行, 等用户确认。

        persist=False (验证流量): 只返回方案, **不写 data/mode.json** —— 否则测试会改用户的
        模式状态与待确认方案 (实测污染: 留下 mode=plan + pending_text)。
        """
        # ★★ 2026-09-25 加 (用户实测缺陷): 清单型输入**不能**编成一条线性链 ——
        #   实测 26 题清单被规划成线性链, 第 10/19 步挂掉 → 后面全部作废。
        #   识别为清单时改为"逐个独立执行"的方案 (彼此不依赖, 一个失败不影响其他)。
        try:
            from core.task_planner import split_independent_tasks, format_batch_plan_text
            _batch = split_independent_tasks(message)
        except Exception as _be:
            logger.warning("batch split failed: %s", _be)
            _batch = []
        if len(_batch) >= 3:
            text = format_batch_plan_text(_batch)
            if persist:
                self.modes.set_pending([], message, text)
            return {"response": text, "intent": "plan_proposal", "model": "planner",
                    "elapsed": round(time.time() - t0, 2), "mode": "plan",
                    "pending_confirm": True, "plan": [],
                    "batch_items": len(_batch), "plan_message": message, "trace": []}

        steps = []
        try:
            if getattr(self, "task_planner", None):
                raw = await self.task_planner.plan(message)
                if raw.get("plan") and len(raw["plan"]) > 1:
                    steps = raw["plan"]
                elif raw.get("error"):
                    logger.info("planner 未出步骤: %s", str(raw.get("error"))[:140])
        except Exception as e:
            logger.warning("planner failed, 用文本方案兜底: %s", e)

        if steps:
            text = self._format_plan_text(message, steps)
            if persist:
                self.modes.set_pending(steps, message, text)
            return {"response": text, "intent": "plan_proposal", "model": "planner",
                    "elapsed": round(time.time() - t0, 2), "mode": "plan",
                    "pending_confirm": True, "plan": steps,
                    "plan_message": message, "trace": []}

        text, model_used = await self._plan_text_via_model(message, ext_model, prev)
        if persist:
            self.modes.set_pending([], message, text)
        return {"response": text, "intent": "plan_proposal", "model": model_used,
                "elapsed": round(time.time() - t0, 2), "mode": "plan",
                "pending_confirm": True, "plan": [],
                "plan_message": message, "trace": []}

    async def _run_plan(self, pend, ext_model, t0, probe=False):
        """Plan 模式 — 用户确认后执行方案。"""
        steps = pend.get("steps") or []
        orig = (pend.get("message") or "").strip()
        self.modes.clear_pending()
        # ★ 2026-09-25: 确认时若原话是**清单** → 逐个独立执行 (不跑线性链)
        try:
            from core.task_planner import split_independent_tasks
            _batch = split_independent_tasks(orig)
        except Exception:
            _batch = []
        if len(_batch) >= 3:
            return await self._run_batch(_batch, ext_model, t0, probe=probe)
        if steps:
            try:
                if self.plan_executor and self._ke is not None:
                    self.plan_executor.ke = self._ke
                result = await self.plan_executor.execute(steps)
                trace = [{"round": r.get("step", i + 1),
                          "tool": r.get("tool", "?"),
                          "args": r.get("description", ""),
                          "result": r.get("result", ""),
                          "success": bool(r.get("success"))}
                         for i, r in enumerate(result.get("results") or [])]
                body = self.plan_executor.format_summary(result)
                if not result.get("success"):
                    errs = result.get("errors") or ["未知错误"]
                    body = ("[规划模式] 执行未全部成功 ("
                            + str(result.get("steps_ok", 0)) + "/"
                            + str(result.get("steps_total", 0)) + " 步)\n"
                            + body + "\n首个错误：" + str(errs[0])[:200])
                return {"response": body, "intent": "plan_exec", "model": "local",
                        "elapsed": round(time.time() - t0, 2), "mode": "plan",
                        "trace": trace}
            except Exception as e:
                logger.warning("plan execute failed: %s — 回落实干路径", e)
        if not orig:
            return {"response": "方案已失效（没有可执行步骤），请重新说一次需求。",
                    "intent": "plan", "model": "local",
                    "elapsed": round(time.time() - t0, 2)}
        # 纯文本方案 → 用实干路径把最初那条原话执行一遍
        # ★ 走 _process_impl (不是 process) —— 否则外层包装会再写一次库, 同一条消息入两次
        _r = await self._process_impl(orig, ext_model=ext_model, mode_override="craft",
                                      probe=probe)
        try:
            _r["intent"] = "plan_exec"
            _r["mode"] = "plan"
        except Exception:
            pass
        return _r

    async def _run_batch(self, items, ext_model, t0, probe=False):
        """逐个**独立**执行清单项 —— 各自走自己的路由, 一项失败不影响其他。

        为什么不用 PlanExecutor: 规划器会把这些彼此无关的条目编成一条**线性链**,
        线性链里任何一步失败都会连累后面 (实测 26 题挂在第 10/19 步, 剩余全不作废)。
        这里改成: 每项当**一条独立消息**重新走正常路由 (该走技能的走技能),
        各自收自己的结果; 失败的只记一笔, 继续跑下一项。

        ★ 用 _process_impl (不是 process) —— 与 _run_plan 底部同款做法,
          否则外层包装会再写一次库, 一条消息入两次。
        """
        results = []
        for _it in items:
            _label = str(_it.get("label") or "")
            _task = str(_it.get("text") or "").strip()
            if not _task:
                continue
            try:
                _r = await self._process_impl(_task, ext_model=ext_model,
                                              mode_override="craft", probe=probe)
                _route = str((_r or {}).get("route") or (_r or {}).get("intent") or "?")
                _resp = str((_r or {}).get("response") or "")
                # ★★ 2026-09-25 修 (自测抓到的假绿): 原来只看"是否以 ✗ 开头" ——
                #   而规划链路的失败是 "Step 1 (file_ops): <err>" 形态 (无 ✗),
                #   于是**失败的项被计成成功** (实测 3/4 里混进一个假绿)。
                #   现在按多种失败形态判定。
                _ok = _batch_item_ok(_resp)
                results.append({"label": _label, "task": _task, "ok": _ok,
                                "route": _route, "resp": _resp})
            except Exception as _e:
                logger.warning("batch item failed (%s): %s", _label, _e)
                results.append({"label": _label, "task": _task, "ok": False,
                                "route": "error", "resp": f"{type(_e).__name__}: {_e}"})

        _ok_n = sum(1 for x in results if x["ok"])
        lines = [f"\n=== 清单执行: {_ok_n}/{len(results)} 项成功 (逐项独立, 互不阻塞) ==="]
        for _i, _x in enumerate(results, 1):
            _tag = (_x["label"] + " ") if _x["label"] else ""
            lines.append(f"  {'✅' if _x['ok'] else '❌'} [{_i}] {_tag}{_x['task'][:60]}")
            _first = (_x["resp"].strip().splitlines() or [""])[0][:110]
            lines.append(f"        route={_x['route']}  {_first}")
        _fails = [x for x in results if not x["ok"]]
        if _fails:
            lines.append("\n未成功 " + str(len(_fails)) + " 项: "
                         + ", ".join((x["label"] or x["task"][:14]) for x in _fails))
        trace = [{"round": _i + 1, "tool": _x["route"], "args": _x["task"][:80],
                  "result": _x["resp"][:200], "success": _x["ok"]}
                 for _i, _x in enumerate(results)]
        return {"response": "\n".join(lines), "intent": "plan_exec", "model": "local",
                "elapsed": round(time.time() - t0, 2), "mode": "plan", "trace": trace}

    # ═══════════════════════════════════════════════════════════════════
    # 技能 DAG 执行层 (SKILL.md 干净路径: 匹配 → 规则提参 → 逐步执行)
    #   设计要点: **不走模型** —— 本地小模型(1.5B)无法把技能说明转成工具调用
    #   (实测: 只把技能名当文本念出来, 甚至编造 shell 命令), 所以参数用规则从
    #   消息里提取, 步骤按 SKILL.md 的 DAG 顺序执行。
    #   位置: 在现有链路(三模式闸门 / IR chain / knowledge / AutoGuide) **之后**,
    #   只接住前面都没处理的请求 —— 纯加性, 不改动任何既有行为。
    # ═══════════════════════════════════════════════════════════════════
    def _skill_match(self, message: str, trigger_only: bool = False):
        """按 triggers/tags 打分匹配 SKILL.md 技能。返回 (name, dag, score) 或 None。

        打分与 skill_loader.match_context 一致 (triggers +3 / tags +2 / tools +1),
        但这里要拿到 **DAG 本体** 才能执行 —— match_context 只回文本。

        trigger_only=True: 只认 **triggers 命中** 的技能 (tags 只是弱线索, 不算)。
        用于"强触发词优先于 IR chain"的场景 —— 见 process() 里技能层前置块。
        """
        try:
            from core.skill_loader import get_skill_index
            eng = get_skill_index()
            idx = eng.get_index()
            msg = message.lower()
            best, best_score, best_trig = None, 0, False
            for name, sk in idx.items():
                score, hit_trig = 0, False
                for t in getattr(sk, "triggers", []) or []:
                    if t and t.lower() in msg:
                        score += 3
                        hit_trig = True
                for t in getattr(sk, "tags", []) or []:
                    if t and t.lower() in msg:
                        score += 2
                for t in getattr(sk, "requires_tools", []) or []:
                    if t and t.lower() in msg:
                        score += 1
                # ★ 2026-09-20 修: trigger_only 时**先按"触发命中"筛**, 再比分数。
                #   原逻辑是"全局 argmax 后再检查冠军有没有触发词" → 只要**别的技能**
                #   靠 tags/tools 拿了更高分, 带触发词的技能就被挤掉 → 整条路返回 None。
                #   实测: "给电脑做个体检 存到 D:/x/" 落到 IR chain 写了个错文件。
                if trigger_only and not hit_trig:
                    continue
                if score > best_score:
                    best, best_score, best_trig = name, score, hit_trig
            if not best:
                return None
            dag = eng.get_dag(best)
            if not dag or not dag.is_valid() or not dag.steps:
                return None
            return (best, dag, best_score)
        except Exception as e:
            logger.debug("skill match failed: %s", e)
            return None

    def _entity_resolve(self, name: str, field: str = "email"):
        """按实体名或别名查 entities.json, 返回指定字段。纯本地, 不调模型。"""
        try:
            if not self._ke or not name:
                return None
            ents = getattr(self._ke, "entities", {}) or {}
            n = str(name).strip()
            # 精确名 → 别名 (最长优先, 避免"大哥"误配到别的)
            for key, info in ents.items():
                if n == key or n == (info or {}).get("name"):
                    if (info or {}).get(field):
                        return info[field]
            cands = []
            for key, info in ents.items():
                for a in ((info or {}).get("aliases") or []):
                    if a and a in n:
                        cands.append((len(a), info))
            if cands:
                cands.sort(key=lambda x: -x[0])
                if cands[0][1].get(field):
                    return cands[0][1][field]
        except Exception as e:
            logger.debug("entity resolve failed: %s", e)
        return None

    # "存到/保存到/导出到 <目标>" —— 目标可能是**目录**(无扩展名), 这正是不修就落桌面的原因
    _OUT_TARGET = __import__("re").compile(
        r"(?:存到|保存到|保存为|存为|另存为|导出到|导出为|输出到|放到|放在|写到|写入)"
        r"\s*[:：]?\s*([^\s，,。；;、）)]+)")

    #: 指代词 (用户说"这个短文/那篇报告/刚才那个文件"时, 目标在上文)
    _ANAPH_REF = ("这个", "这篇", "这首", "这段", "这份", "这本", "这张",
                  "那个", "那篇", "那首", "那段", "那份", "那本", "那张",
                  "刚才那个", "刚才那", "刚刚那个", "上面那个", "上面那篇",
                  "前面那个", "前面那篇", "刚才的", "上面的", "前面的")

    def _looks_anaphoric(self, message: str) -> bool:
        """句子里有没有'指向上文'的词 (这个/那篇/刚才那个…)。"""
        m = message or ""
        return any(w in m for w in self._ANAPH_REF)

    def _last_artifact_path(self, limit: int = 20) -> str:
        """最近对话里出现过的、**磁盘上真实存在**的文件路径。

        用途: `朗读这个短文` —— 消息里没有路径, "这个短文"指的是上一轮刚生成的文件。
        只认真实存在的文件 (避免把模型随口一提的名字拿去当路径)。
        """
        try:
            hist = self.sessions.recent(limit) or []
        except Exception:
            return ""
        for h in reversed(hist):
            c = str(h.get("content") or "")
            for m in re.finditer(r"[A-Za-z]:[\\/][^\s，。；;、！？?'\"<>|]+?\.[A-Za-z0-9]{1,5}", c):
                cand = m.group(0).rstrip(".,;)]")
                try:
                    if os.path.isfile(cand):
                        return cand
                except Exception:
                    continue
        return ""

    def _skill_params(self, message: str, dag) -> dict:
        """规则提取 DAG 所需参数 (不走模型)。缺的参数留空, 由调用方决定报错还是放行。

        同类多参数按**出现顺序**分配 (如"把 a.txt 拷成 b.txt" → 第1个 path 给 src, 第2个给 dst)。
        """
        params = {}
        schema = getattr(dag, "params_schema", None) or {}

        # 先按类型归组 schema 里的参数名, 保持声明顺序
        by_type = {}
        for pname, pspec in schema.items():
            by_type.setdefault((pspec or {}).get("type", ""), []).append(pname)

        for pname, pspec in schema.items():
            ptype = (pspec or {}).get("type", "")
            if ptype == "entity":
                field = (pspec or {}).get("field", "email")
                try:
                    ents = getattr(self._ke, "entities", {}) or {}
                    hit = None
                    for key, info in ents.items():
                        for nm in [key] + list((info or {}).get("aliases") or []):
                            if nm and nm in message and (not hit or len(nm) > len(hit[0])):
                                hit = (nm, info)
                    if hit and (hit[1] or {}).get(field):
                        params[pname] = hit[1][field]
                        params[f"_{pname}_name"] = hit[0]
                except Exception:
                    pass

        # path: 收集消息里的路径 → 分给 path 参数。
        # ★ 2026-09-19 深测修: 原来只有一个来源 (带扩展名的路径), 于是
        #   "写周报，存到 D:/报告/" 这种**给目录不给文件名**的说法抽不到 →
        #   技能参数为空 → 工具静默用默认值落桌面 (用户明确指定了位置却拿不到)。
        #   现在: ① 显式 "存到/导出到 <目标>" 优先给**输出型**参数 (出目录也认, 工具再补文件名);
        #         ② 带扩展名的路径按序给**输入型**参数; ③ 剩下的给输出型 (保持旧行为)。
        path_names = by_type.get("path", [])
        if path_names:
            found = [m.group(1).strip() for m in re.finditer(
                # ★ 2026-09-20: 补媒体/字幕后缀 —— 原来只有文档/图片类,
                #   于是"转写这段录音 D:/a.mp3" / "给 D:/v.mp4 抽帧" **抽不到路径**
                #   → 技能拿到空 path → 工具必然失败 (新增音视频类技能时实跑暴露)。
                r'([\w\-./\\:]*\.(?:txt|docx?|pptx?|pdf|py|md|xlsx?|csv|json|png|jpe?g|zip|'
                r'mp[34]|wav|m4a|flac|ogg|aac|mov|mkv|avi|webm|gif|bmp|webp|srt|ass|lrc))',
                message, re.I)]
            # 保序去重
            seen, uniq = set(), []
            for f in found:
                if f not in seen:
                    seen.add(f)
                    uniq.append(f)
            # ★★ 2026-09-24 加 (用户实测: "朗读这个短文" 连挂两次):
            #   消息里**没有路径**, 但"这个短文/那篇报告"指的是上一轮刚生成的文件 ⇒
            #   原来 uniq 为空 → path 参数留空 → 工具报 "No text to speak"。
            #   现在: 有指代词 + 上文能找到真实存在的产物文件 → 用它兜底。
            if not uniq and self._looks_anaphoric(message):
                _la = self._last_artifact_path()
                if _la:
                    uniq = [_la]
                    logger.info("skill params: 指代'这个/那篇' → 兜底用上一轮产物 %s", _la)
            out_names = [p for p in path_names if re.search(r"out|save|dest|target", p, re.I)]
            in_names = [p for p in path_names if p not in out_names]
            used = set()
            # ① "存到 X" → 输出型参数 (X 可能是目录)
            m_out = self._OUT_TARGET.search(message)
            if m_out and out_names:
                _tgt = m_out.group(1).strip().strip('"\'“”').rstrip("的")
                if _tgt and not re.match(r"^(这里|那里|哪|哪都|桌面|文档|下载)$", _tgt):
                    for _pn in out_names:
                        params[_pn] = _tgt
                    used.add(_tgt)
            # ①b 只有一个 path 参数 + 有显式"存到 X" → 它必然是输出目标
            #     (make-image 这种参数名就叫 `path`, 猜不出输入/输出; 只有一个就没有歧义)
            if m_out and not out_names and len(path_names) == 1 and path_names[0] not in params:
                _tgt1 = m_out.group(1).strip().strip('"\'“”').rstrip("的")
                if _tgt1 and not re.match(r"^(这里|那里|哪|哪都|桌面|文档|下载)$", _tgt1):
                    params[path_names[0]] = _tgt1
                    used.add(_tgt1)
            # ② 输入型: 按序吃带扩展名的路径
            for i, pname in enumerate(in_names):
                _rest_i = [u for u in uniq if u not in used]
                if i < len(_rest_i):
                    params[pname] = _rest_i[i]
                    used.add(_rest_i[i])
            # ③ 输出型还没拿到 → 吃剩下的带扩展名路径 (旧行为)
            if out_names:
                _rest = [u for u in uniq if u not in used]
                for i, pname in enumerate(out_names):
                    if pname in params:
                        continue
                    if i < len(_rest):
                        params[pname] = _rest[i]
            else:
                for i, pname in enumerate(path_names):
                    if i < len(uniq):
                        params[pname] = uniq[i]

        # text: 引号内内容优先, 否则关键词之后; 多个 text 参数按序分配
        text_names = by_type.get("text", [])
        # ★ 2026-09-20 加: URL 型 text 参数 —— 参数名暗示网址时优先抽链接。
        #   实测缺陷: text 参数原来只从「引号内」或「写入/内容:」后抽,
        #   于是"总结这个网页 https://x" 的 url 参数**恒空** → 网页类技能根本跑不起来。
        _url_names = [p for p in text_names if re.search(r"url|link|网址|链接|site", p, re.I)]
        if _url_names:
            _mu = re.search(r"https?://[^\s，。；、）)】\"']+", message or "")
            if _mu:
                params[_url_names[0]] = _mu.group(0).rstrip(".,;)]")
        if text_names:
            vals = []
            m = re.search(r'[「"“]([^」"”]{1,200})[」"”]', message)
            if m:
                vals.append(m.group(1).strip())
            else:
                for mm in re.finditer(r'(?:写入|内容|正文|说)[:：]?\s*([^\s，,。]{1,200})', message):
                    if mm.group(1).strip():
                        vals.append(mm.group(1).strip())
            # ★★ 2026-09-24 修: **别把文件路径当成要念/要处理的正文**。
            #   实测 (用户发火那句): `朗读"C:\...\窗台上的光.txt"` → 引号里的路径
            #   被当 text 填进技能 ⇒ voice 收到 text=路径 ⇒ **把路径念出来**、正文没念。
            #   判据: 值像路径 (含分隔符/盘符, 或带已知扩展名) **且** (磁盘上真存在
            #   或技能同时声明了 path 参数 —— 说明这路径该归 path 参数)。
            def _looks_like_path(_v: str) -> bool:
                _s = (_v or "").strip().strip('"').strip("'")
                if not _s or "\n" in _s:
                    return False
                if len(_s) > 260:
                    return False
                _win = bool(re.match(r"^[A-Za-z]:[\\/]", _s))
                _ext = bool(re.search(r"\.(?:txt|docx?|pptx?|pdf|py|md|xlsx?|csv|json|png|jpe?g|"
                                      r"zip|mp[34]|wav|m4a|flac|ogg|aac|mov|mkv|avi|webm|"
                                      r"gif|bmp|webp|srt|ass|lrc)$", _s, re.I))
                _sep = ("\\" in _s) or ("/" in _s)
                if not (_win or _ext or _sep):
                    return False
                try:
                    if os.path.isfile(_s):
                        return True
                except Exception:
                    pass
                # 不是真实文件时, 只有"技能同时有 path 参数"才把它判为路径
                return bool(path_names)
            vals = [v for v in vals if not _looks_like_path(v)]
            for i, pname in enumerate(text_names):
                if i < len(vals):
                    params[pname] = vals[i]

            # ★★ 2026-09-24 加 (用户实测: 把《夜思》整首**直接贴**过来 + "朗读。" → 报
            #   "No text to speak"; 只有带引号或给路径才行):
            #   用户在"念"类场景里最自然的说法就是**直接贴正文 + 一个动词**,
            #   而上面的规则只认「引号里」或「说：」后面 ⇒ 贴的诗看不见。
            #   兜底: 若 text 全空 **且这个技能是"念/播报"类** (步骤里用 voice),
            #   就把整条消息**去掉句首/句尾那个念的动作词**当作正文。
            #   ★ 只对 voice 类技能生效 —— 否则"写周报"这种生成型指令会被误当正文。
            if text_names and not any(params.get(p) for p in text_names):
                _uses_voice = any((s or {}).get("tool") == "voice"
                                  for s in (getattr(dag, "steps", None) or []))
                if _uses_voice:
                    _rest = (message or "").strip()
                    # 只在**句首/句尾**剥动作词 (不在中间剥, 免得把正文里的"读/念"弄坏)
                    _VERBS = ("语音播报", "说出来给我听", "念给我听", "读给我听",
                              "念一下", "念一遍", "念出来", "读出来", "读一下", "读一遍",
                              "朗读", "播报", "念读", "念", "读")
                    for _v in sorted(_VERBS, key=len, reverse=True):
                        _tail = (r"\s*[。.！!，,、；;：:]?\s*$")
                        _head = (r"^\s*[。.！!，,、；;：:]?\s*")
                        if re.search(re.escape(_v) + _tail, _rest):
                            _rest = re.sub(re.escape(_v) + _tail, "", _rest, count=1).rstrip()
                            break
                        if re.search(_head + re.escape(_v), _rest):
                            _rest = re.sub(_head + re.escape(_v), "", _rest, count=1).lstrip()
                            break
                    # 剥掉动作词后可能留下句首/句尾的孤立标点 ("朗读：xxx" → "：xxx")
                    _rest = _rest.strip().strip("。.！!，,、；;：: ").strip()
                    # 太短 / 像路径 → 不猜 (宁可不做, 也别念错东西)
                    if len(_rest) >= 6 and not _looks_like_path(_rest):
                        params[text_names[0]] = _rest
                        logger.info("speak: text 为空 → 兜底把**贴过来的正文**当要念的内容 (len=%d)",
                                    len(_rest))
        return params

    def _tmpl_lookup(self, kind: str, path: str, params: dict, step_out: dict, message: str):
        """查一个占位符的值。"""
        if kind == "message":
            return message
        if kind == "params":
            return params.get(path, "")
        # steps.<id>[.<field>]
        parts = path.split(".", 1)
        b = step_out.get(parts[0])
        if isinstance(b, dict):
            return b.get(parts[1], "") if len(parts) > 1 else b
        return b if len(parts) == 1 else ""

    def _resolve_tmpl(self, val, params: dict, step_out: dict, message: str):
        """解析步骤输入里的占位符。

        两种形态:
          ①整个值就是占位符  `"$steps.read.content"` → 返回**原对象**(保留类型, 如 list)
          ②字符串内插值      `"用户要求: $message"`   → 逐处替换(非字符串值转 str)
        支持 `$message`(用户原话) / `$params.X` / `$steps.<id>.<field>`。
        """
        if not isinstance(val, str) or "$" not in val:
            return val
        s = val.strip()
        m = re.fullmatch(r"\$(message|params\.[\w\-]+|steps\.[\w\-]+(?:\.[\w\-]+)?)", s)
        if m:
            spec = m.group(1)
            kind, _, path = spec.partition(".")
            return self._tmpl_lookup(kind, path, params, step_out, message)

        def _sub(mo):
            kind, _, path = mo.group(1).partition(".")
            v = self._tmpl_lookup(kind, path, params, step_out, message)
            if isinstance(v, (dict, list)):
                import json as _json
                return _json.dumps(v, ensure_ascii=False)
            return "" if v is None else str(v)

        return re.sub(r"\$(message|params\.[\w\-]+|steps\.[\w\-]+(?:\.[\w\-]+)?)", _sub, val)

    # 生成类技能的"确实要产出"判据 —— 防"帮我看看这份周报"被误当成生成请求
    # ★ 2026-09-19 深测修: 原来不认"画/绘制/设计" → "画个架构图"(diagram 技能自己的
    #   自然说法) 被 guard_generation 判成"不是要产出" → 技能放弃 → 掉到 plugin.keyword
    #   拿空 code 调 plantuml → "No PlantUML code provided"。给绘图类动词补上。
    # ★ 2026-09-20 加: "带明确来源"也算要产出。
    #   为什么: 含 llm 步骤的技能被 _looks_like_creation 守着 (防抢"读类"请求),
    #   但"总结这个网页 https://x" / "转写 D:/a.mp3" 这类**有素材、要产出成品**的
    #   请求没有"写/生成"字样 → 技能永远接不到 (实测: 它们全落到通用兜底)。
    #   判据收紧到"必须带链接或文件路径", 所以"总结一下这个文档"(无路径) 仍被排除
    #   (tests/test_output_path_contract.py 里那条负例照旧成立)。
    _SOURCE_HINT = __import__("re").compile(
        r"https?://|\b[A-Za-z]:[\\/]|\.(?:mp[34]|wav|m4a|flac|ogg|mov|mkv|avi|webm|"
        r"docx?|pptx?|pdf|xlsx?|csv|txt|md|png|jpe?g|gif|webp|zip|json)\b", __import__("re").I)

    _CREATION_WORDS = ("写", "做", "生成", "起草", "拟一份", "拟个", "整理成", "输出",
                       "出一份", "出一个", "帮我拟", "给我写", "做个", "来一份",
                       "画", "绘制", "作图", "出图", "设计", "制图", "画一张", "画个")

    @classmethod
    def _looks_like_creation(cls, message: str) -> bool:
        return any(w in message for w in cls._CREATION_WORDS)

    @classmethod
    def _has_explicit_source(cls, message: str) -> bool:
        """消息里带**明确的素材来源** (链接 / 文件路径) —— 处理类请求的特征。

        与 _looks_like_creation 配合: 二者任一成立, 含 llm 步骤的技能才可接管。
        ★ 只认"链接或带后缀的文件路径", 不认泛泛的"这个文档" —— 否则会开始抢读类请求
          (那条负例 "总结一下这个文档" 必须仍然被排除)。
        """
        return bool(cls._SOURCE_HINT.search(message or ""))

    # ═══════════════════════════════════════════════════════════════
    # 路由表 (P1, 2026-09-19) — 从 process() 抽出的顺序 if → 声明式注册
    #   优先级 = Route.priority (见 core/routes.py 的分带说明)
    #   每抽一批跑一次 scripts/verification/verify_routing_matrix.py (37 句基线)
    #   返回 None = 交给后面的兜底链路 (加性, 不破坏旧行为)
    # ═══════════════════════════════════════════════════════════════

    def _build_routes(self):
        """声明全部路由 —— 这是"谁先裁决"的唯一真相。"""
        reg = self.routes.register
        R = Route

        # ── 990 危险操作拦截 (原位置: 一切之前) ──
        reg(R(name="regulator.dangerous", priority=P_REGULATOR,
              note="系统核心文件保护: 删除C盘/格式化… → 拦截",
              match=self._match_regulator, run=self._run_regulator))

        # ── 950 三模式闸门 (ask → 纯问答 / plan → 提案-确认) ──
        reg(R(name="mode.gate", priority=P_GATE, note="三模式闸门 (plan 会落盘 pending)",
              match=self._match_mode_gate, run=self._run_mode_gate,
              writes_state=True, probe_safe=True))

        # ── 内部命令 (斜杠命令最高优先, 与旧行为一致) ──
        reg(R(name="cmd.stats", priority=P_COMMAND, note="/stats 运行状态",
              match=lambda m, c: m.strip() == "/stats", run=self._run_cmd_stats))
        reg(R(name="cmd.cost", priority=P_COMMAND, note="/cost token 用量",
              match=lambda m, c: m.strip() == "/cost", run=self._run_cmd_cost))
        # /stats-old 与 /stats **共用同一个实现** (旧代码是两份拷贝 —— DRY)
        reg(R(name="cmd.stats_old", priority=P_COMMAND, note="/stats-old 兼容别名",
              match=lambda m, c: m.strip() == "/stats-old", run=self._run_cmd_stats))
        # ★ 2026-09-19 深测修: /mode 与 /modes 原来**没有路由** → 在 web 端敲 /mode 掉到模型,
        #   模型编出"当前处于标准交互模式"(错)。现在按 CLI 的口径回真实模式信息。
        reg(R(name="cmd.mode", priority=P_COMMAND, note="/mode 查看三模式 (只读)",
              match=lambda m, c: m.strip() in ("/mode", "/modes"), run=self._run_cmd_mode))
        # ★ 能力缺口台账 (2026-09-19): /gap 看账 / why 细节 / drop 丢弃 / done 已造
        #   writes_state=True + probe_safe=False → **验证流量不会改台账**
        #   (probe 流量下 dispatch 会跳过它, 见 routes.py 的硬不变量)
        reg(R(name="cmd.gap", priority=P_COMMAND,
              note="/gap 能力缺口台账 (why / rescue 本地自救 / outside 该出门找 / plan|done|drop)",
              match=lambda m, c: m.strip() == "/gap" or m.strip().startswith("/gap "),
              run=self._run_cmd_gap,
              writes_state=True, probe_safe=False, probe_gated=True,
              requires=("gap_ledger",)))
        # ★ 能力自补闭环 (2026-09-21): /autofill [--apply] [--net]
        #   默认 **dry-run** (不联网不落盘); --apply 才真跑; --net 才允许联网去找。
        #   writes_state=True → 会动技能库 (纳库/退回); probe 流量由路由表硬不变量跳过。
        reg(R(name="cmd.autofill", priority=P_COMMAND,
              note="/autofill 能力自补闭环 (缺→找→甄别→取形式重编→验证→纳库)",
              match=lambda m, c: m.strip() == "/autofill" or m.strip().startswith("/autofill "),
              run=self._run_cmd_autofill,
              writes_state=True, probe_safe=False, probe_gated=True))
        # ★ 对外检索 (第 3 步): /depot search|assess|adopt|installed
        #   writes_state=True → 会下载候选到沙箱 / 装依赖 / 写已装清单
        #   probe_safe=False → **验证流量绝不下载安装任何东西** (硬不变量拦)
        reg(R(name="cmd.depot", priority=P_COMMAND,
              note="/depot 对外检索+安全评估 (search / assess / adopt / skill-assess / skill-adopt / installed)",
              match=lambda m, c: m.strip() == "/depot" or m.strip().startswith("/depot "),
              run=self._run_cmd_depot,
              writes_state=True, probe_safe=False, probe_gated=True))
        reg(R(name="cmd.tool", priority=P_COMMAND, note="/tool 直接调工具",
              match=lambda m, c: m.strip().startswith("/tool "), run=self._run_cmd_tool))
        # ★ 角色专家层 (2026-09-20): /experts 列出本机专家 (只读, 不改任何状态)
        reg(R(name="cmd.experts", priority=P_COMMAND,
              note="/experts 列出角色专家 (用 @名字 指定身份, 例如 @数据分析)",
              match=lambda m, c: m.strip() == "/experts" or m.strip().startswith("/experts "),
              run=self._run_cmd_experts))
        reg(R(name="cmd.hive", priority=P_COMMAND, note="/hive 蜂群拆解",
              match=lambda m, c: m.strip().startswith("/hive"), run=self._run_cmd_hive,
              requires=("hive_mind",)))
        reg(R(name="cmd.voice", priority=P_COMMAND, note="/voice 语音输入",
              match=self._match_cmd_voice, run=self._run_cmd_voice))
        # ★ 2026-09-21 接线: scripts/watchdog.py 的巡检逻辑早就写好了, 但**没有任何入口调用它**
        #   (文件在 = 功能不在)。现在接成一条只读命令; "有事自动推微信"属运维, 走计划任务,
        #   不放进产品默认路径 (免得用户没同意就收到推送)。
        reg(R(name="cmd.watchdog", priority=P_COMMAND,
              note="/watchdog 本机巡检查看 (磁盘水位/桌面新增/下载量, 只读不推送)",
              match=lambda m, c: m.strip() == "/watchdog", run=self._run_cmd_watchdog))
        # ★ 技能工厂 (2026-09-19): /skill build|list|approve|reject
        #   writes_state=True → 候选落盘 / 提升会改 tmm_skills 并 reload 引擎;
        #   probe_safe=False → **验证流量不会造候选、更不会动正式技能** (硬不变量拦)
        reg(R(name="cmd.skill", priority=P_COMMAND,
              note="/skill 技能工厂 (build 编译 / list 候选 / approve 生效 / reject 丢弃)",
              match=lambda m, c: m.strip() == "/skill" or m.strip().startswith("/skill "),
              run=self._run_cmd_skill, writes_state=True, probe_safe=False, probe_gated=True))
        # /learn 的写操作 (forget/reject/activate/answer) 会改学习库 → 同 /skill: probe 禁走
        reg(R(name="cmd.ledger", priority=P_COMMAND,
              note="/ledger 日常干活账 (干成的活 / 能力 / 成功率)",
              match=lambda m, c: m.strip() == "/ledger",
              run=self._run_cmd_ledger, probe_safe=True))

        reg(R(name="cmd.learn", priority=P_COMMAND,
              note="/learn 学习闭环 (查看 / 人工门 / 补答案 / 衰减)",
              match=lambda m, c: m.strip() == "/learn" or m.strip().startswith("/learn "),
              run=self._run_cmd_learn, writes_state=True, probe_safe=False, probe_gated=True))

        # ── 用户显式 @模型 (最高优先: 用户的选择 > 一切启发式) ──
        # ★ 2026-09-19 修复: 旧代码里这个 return 在 **plugin/search/open 之后**,
        #   实测 4/7 的 @deepseek 消息被抢: "@deepseek 搜索…" → search,
        #   "@deepseek 北京天气" → plugin:openmeteo, "@deepseek 打开百度" → search,
        #   "@deepseek 上海坐标" → plugin:nominatim。
        #   用户偏好"切模型须绕过自动路由直接生效(最高优先)" → 提到 900。
        #   例外 ollama: 不支持 function calling, 保留其既有工具路由 (与旧行为一致)。
        reg(R(name="model.explicit", priority=P_EXPLICIT_MODEL, note="@模型 → 直通所选模型",
              match=lambda m, c: Level4Pipeline._is_explicit_model(c),
              run=self._run_model_explicit))

        # ── 技能强触发词 (比所有启发式都具体 → 高于 planner/analysis/search) ──
        reg(R(name="skill.trigger_first", priority=P_SKILL, note="技能 triggers 命中 → 走 DAG",
              match=self._match_skill_trigger, run=self._run_skill_trigger_first))

        # ── TaskPlanner: 多步任务 ──
        reg(R(name="planner.multi_step", priority=P_PLANNER, note="2+ 多步词 → 拆解执行",
              match=self._match_planner, run=self._run_planner,
              requires=("task_planner",)))

        # ── 600 分析 / 预读 ──
        reg(R(name="analysis.preread_reuse", priority=P_ANALYSIS,
              note="无路径追问(数量/统计) → 复用上次预读文件",
              match=self._match_preread_reuse, run=self._run_preread_reuse))
        reg(R(name="analysis.office_first", priority=P_ANALYSIS,
              note="表格/PDF 明确诉求 → intent_router 工具链 + 喂模型",
              match=self._match_office_first, run=self._run_office_first,
              requires=("intent_router",)))
        reg(R(name="analysis.goto_model", priority=P_ANALYSIS,
              note="分析类 → 直通模型 (图片→ollama / 文档→预读)",
              match=self._match_analysis, run=self._run_analysis))

        # ── 500 插件关键词 (天气/翻译等) ──
        reg(R(name="plugin.keyword", priority=P_PLUGIN, note="插件关键词 → 直接调用",
              match=self._match_plugin_keyword, run=self._run_plugin_keyword))
        reg(R(name="plugin.geocode", priority=P_GEOCODE, note="坐标/经纬度 → nominatim (显式模式 > 模糊追问)",
              match=self._match_geocode, run=self._run_geocode))

        # ── 420 打开站点 (须在 search 之前: "打开百度" 里含"百度") ──
        reg(R(name="open.site", priority=P_OPEN, note="打开XX → 浏览器",
              match=self._match_open_site, run=self._run_open_site))

        # ── 400 搜索 ──
        reg(R(name="search.web", priority=P_SEARCH, note="搜索/百度/搜 → web_search",
              match=self._match_search, run=self._run_search))

        # ── 360 Brain 决策树 (须高于 SmartRoute/IR chain: 恢复原顺序 Brain 最先) ──
        reg(R(name="brain.route", priority=P_BRAIN, note="Brain 决策树 (quick_match/classify/route)",
              match=self._match_brain, run=self._run_brain))

        # ── 350 复杂消息直通模型 (须高于 IR chain: 保持旧顺序 SmartRoute → IR chain) ──
        reg(R(name="route.smart", priority=P_SMART, note="复杂度>=3 → 直通 deepseek",
              match=self._match_smart_route, run=self._run_smart_route))

        # ── 300 IR chain (写文件/发文件/发邮件; 规则链 → 工具 → 喂模型/直接返回) ──
        reg(R(name="ir.chain", priority=P_IR_CHAIN, note="intent_router 规则链执行",
              match=self._match_ir_chain, run=self._run_ir_chain,
              requires=("intent_router",)))

        # ── 590 输入守卫 / 预过滤 (原位置: analysis 之后, plugin 之前) ──
        reg(R(name="guard.input_filter", priority=P_GUARD,
              note="空/纯符号/过短/危险内容 → 直接拒绝",
              match=self._match_input_guard, run=self._run_input_guard))

        # ── 290~245 本地知识/技能/引导/工作流/检索 兜底带 ──
        reg(R(name="l4.knowledge", priority=P_L4, note="实体学习/查询/技能执行",
              match=self._match_l4_knowledge, run=self._run_l4_knowledge, requires=("_ke",)))
        reg(R(name="skill.dag_catchall", priority=P_SKILL_CATCH,
              note="技能 DAG 兜底 (tags 弱线索; 800 已试过强触发词)",
              match=lambda m, c: True, run=self._run_skill_catchall))
        # ── 370 教学句 (学习闭环的"被教"端) ──
        #   位置**高于 brain(360)/route.smart(350)/ir.chain(300)**: 用户说"以后默认发给涛哥"
        #   是在**教系统**, 不是在下"发邮件"的命令。实测事故: 该句被 IR chain 当发送执行 →
        #   真调 send_email(subject=body="以后默认 涛哥") → 垃圾邮件 + 谎报"涛哥已收到"。
        #   命中后**不在这里学** (免得与 _on_route_done 的 observe 重复计数) ——
        #   只给回执, 学习仍由统一观察点完成。
        reg(R(name="learn.teach", priority=P_LEARN_TEACH,
              note="教学句 (以后/默认/总是/永远/记住…) → 记成偏好, 不执行",
              match=self._match_learn_teach, run=self._run_learn_teach,
              requires=("learn_loop",)))

        reg(R(name="autoguide.route", priority=P_AUTOGUIDE, note="identity/help/greeting",
              match=self._match_autoguide, run=self._run_autoguide, requires=("auto_guide",)))

        # ── 292 学到的东西 (学习闭环的"生效"端) ──
        #   priority 介于 L4知识(290) 与 AutoGuide(280) 之间:
        #   用户**教过**的答案优先于**内置**罐头答案, 但不抢真的工具执行 (搜索/写文件)。
        reg(R(name="learned.answer", priority=P_LEARNED,
              note="用户教过的问答 (learn_loop) — 压过静态罐头",
              match=self._match_learned, run=self._run_learned,
              requires=("learn_loop",)))
        reg(R(name="fixguard.context", priority=P_FIXGUARD, note="模糊修复请求 → 给上文选项",
              match=self._match_fixguard, run=self._run_fixguard))
        reg(R(name="l1.precheck", priority=P_L1, note="实体+动作 → 任务分解执行",
              match=self._match_l1, run=self._run_l1))
        reg(R(name="l2.reasoner", priority=P_L2, note="Intent Reasoner",
              match=lambda m, c: True, run=self._run_l2))
        reg(R(name="l3.workflow", priority=P_L3, note="Workflow Engine",
              match=lambda m, c: True, run=self._run_l3))
        reg(R(name="email.regex_send", priority=P_EMAIL, note="正则直发邮件",
              match=self._match_email_regex, run=self._run_email_regex))
        reg(R(name="url.open", priority=P_URLOPEN, note="URL → 浏览器",
              match=self._match_url_open, run=self._run_url_open))
        reg(R(name="fts.search", priority=P_FTS, note="历史对话全文检索",
              match=self._match_fts, run=self._run_fts))

        # ── 100 模型兜底 (终止路由) ──
        reg(R(name="model.fallback", priority=P_MODEL, note="本地免费模型兜底 (终止)",
              match=lambda m, c: True, run=self._run_model_fallback))
        # ── 150 缺信息自查 / 行动判定 (★ 2026-09-21 短板1+B) ──
        #   只对"本来要落给模型"的消息生效 (150 低于所有工具/技能路由 → 不抢任何既有能力);
        #   实测: "帮我看看这100个Excel怎么归类" 原来停在门口要路径, 现在先只读自查再答。
        reg(R(name="lookup.prefill", priority=P_LOOKUP,
              note="缺信息先只读自查 + 该动手就明说 (不抢工具/技能)",
              match=self._match_lookup, run=self._run_lookup))
        # ── 145 ★ 自然话「理解兜底」 ──
        #   用户的原则 (2026-09-23 原话): "自然语言类似我这种表达的很多, 而且很普遍,
        #   所以 TMM 要理解这种方式"。补词是打地鼠 (补了'放桌面'还有'搁桌面'/'扔桌面'…);
        #   正确做法: **判据认不出来时, 让大模型读懂再决定怎么干**。
        #   实测依据: 同一个规划器对"整一首诗搁我桌面上"/"来首诗扔桌面"全部产出了正确计划
        #   (桌面/诗.txt), 理解能力本来就在, 缺的是**路由层不把话交给它**。
        #   位置 145: 只接"一切都认不出、本来要落给模型"的句子 —— 不抢任何既有能力。
        reg(R(name="understand.request", priority=P_UNDERSTAND,
              note="自然话理解兜底: 判据认不出 → 让规划器读懂再干 (读不懂则让位给模型)",
              match=self._match_understand, run=self._run_understand,
              requires=("task_planner",)))
        # ── 110 ★ 「把<指代>存到<目标>」: 没有可指的现成内容 → 自己写一份再落盘 ──
        #   低处 (110) 才接: 不抢任何工具/技能/规划能力; 只接"本来要落给模型"的句子。
        reg(R(name="save.referent_gen", priority=P_SAVE_GEN,
              note="把这首诗/这段存到桌面: 无现成内容 → 自己生成再落盘",
              match=self._match_save_referent, run=self._run_save_referent))

    # 不支持 function calling 的本地模型 → 保留其既有工具路由 (不做 @模型 直通)
    _NO_TOOLCALL = {"ollama"}

    @staticmethod
    def _is_explicit_model(ctx) -> bool:
        em = ctx.get("ext_model")
        return bool(em and em != "auto" and em not in Level4Pipeline._NO_TOOLCALL)

    # 不变量: 这三条路由都不写用户状态 (纯读/计算/调外部模型), 声明为 probe_safe
    async def _run_model_explicit(self, message, ctx):
        """900: 用户显式选了模型 → 直通, 不让任何本地启发式插手。"""
        return await self._process_external(message, ctx["ext_model"],
                                            ctx.get("t0", time.time()),
                                            probe=ctx.get("probe", False))

    #: 「工具调查型」动作 (每个动作段独立计一次) —— 用于判断"要不要成链"
    #: ★ 2026-09-22 补词 (语料实测漏判驱动): 裸"看"("看许可证") 与系统操作类("杀掉/停/启动")
    #   之前不在表里 ⇒ "先查这个包的信息，再看许可证，然后给个建议" 被判成单步。
    #: ★ 词表口径: 单字动词只保留**不会藏在别的词里**的那些。
    #   裸"算"曾把"预**算**"算成查询动作 ⇒ 把 write-proposal 那条基线请求判成多步 (实测回归 48/49)。
    #   所以"算"只收 `算一下/算算`; 同理"看"收 `看看/看下/看一下/看` 但"看"待观察 (暂留)。
    _QACT = ('查', '看看', '看下', '看一下', '看', '读', '列出', '列一下', '统计', '算一下', '算算',
             '比较', '汇总', '挑出', '翻', '提取', '转写', '搜索', '检索')
    _WACT = ('写', '生成', '整理', '存', '导出', '保存', '制作', '做', '出', '翻译', '改成', '转成', '发给',
             '杀', '删', '停', '启动', '运行', '执行', '打开', '安装', '清理')
    _NEG_PRE = ('先别', '别急', '不急着', '先不', '暂时不', '不用先', '先不用', '不急')

    def _step_evidence(self, message: str):
        """把消息切成「动作段」, 数出证据 → (查询型动作数, 总动作数, 是否有明确落盘目标)。

        ★ 为什么不再数连接词 (2026-09-22, 依据自家 62 条标注语料实测):
          旧判据是"含 ≥2 个连接词(先/再/然后…)" + 兜底"1 个连接词 + 落盘"。
          实测 plan 召回只有 **10/20 = 50%**: 7 条真多步被漏 (用户多数时候**不写连接词**,
          直接"查一下A和B，整理成报告"), 同时 2 条废话/犹豫被误判成多步 ("然后再把这句发给涛哥"、
          "先不急着写，先看看再说")。⇒ 判据应该看**有几个动作**, 不是**有几个连接词**。
          这也是 ADaPT 的思路: 按"需要做几件事/执行者能不能一次干完"决定, 而非句子形态。
        """
        m = (message or "").strip()
        if not m:
            return 0, 0, False
        segs = [s.strip() for s in re.split(
            r'[，,。;；!？?、]|然后再|然后|接着|之后|再|并且|并|同时|最后|随后|以及', m) if s.strip()]
        n_q = sum(1 for s in segs if any(v in s for v in self._QACT))
        n_all = sum(1 for s in segs if any(v in s for v in self._QACT + self._WACT))
        target = re.search(r'(存到|保存到|写到|写入|存成|导出到|另存为)\s*\S', m) is not None
        if not target:
            # ★ 2026-09-23 补 (用户实测原话 "写一首诗放桌面"):
            #   口语里的落盘方向是"放/存/写 + 目录词", 常常**没有"到"** ("放桌面"/"存桌面"),
            #   只认书面式(存到/保存到/写到…)会漏 ⇒ 落盘目标判不出来 ⇒ 规划链不接 ⇒
            #   落到知识层, 把**指令尾巴**("一首诗放桌面")当正文写进自动命名的 doc_<时间戳>.txt。
            #   实测对照: "写一首诗保存到桌面"→planner ✓ / "写一首诗放桌面"→l4.knowledge ✗(6 字垃圾)。
            #   只认**目录词**搭配落盘动词, 不认裸"放"。
            target = re.search(r'(?:放|存|写|发|丢|摆|贴)(?:到|在|进|入)?\s*'
                               r'(?:桌面|文档|下载|desktop|documents|downloads)', m, re.I) is not None
        return n_q, n_all, target

    def _planner_preempts(self, message: str) -> bool:
        """★ 多步「工具调查型」请求 → 规划链**优先于**技能抢占 (2026-09-22 加, 语料实测驱动)。

        依据: 语料里 3 条真多步被技能抢走, 每条都只做了请求的一半 ——
          · "先看看模型用量，再统计一下今天花了多少钱" → model-usage 只统计了用量
          · "查一下 ollama 有哪些模型，看看哪些能看图，整理成表格" → look-at-image 命中"看图"
          · "把这三个文件都读一遍，比较一下不同，写成一份对比" → speak-aloud 命中"读一遍"
        判据要求"**查询型**动作 ≥2"或"总动作 ≥2 且含查询型" —— 这样
        "先写背景，再写推广方案"(纯生成型, 命中的是 write-proposal 自己的 DAG) 不受影响,
        路由基线里那条必须保持走技能。
        """
        m = (message or "").strip()
        if any(kw in m for kw in self._NEG_PRE):
            return False
        n_q, n_all, _t = self._step_evidence(m)
        return n_q >= 2 or (n_all >= 2 and n_q >= 1)

    def _match_skill_trigger(self, message, ctx):
        """800: 技能 triggers 命中 (只认决定性短语, 弱线索 tags 不算)。"""
        if self._is_explicit_model(ctx):
            return False                      # 用户选了模型 → 不抢
        if self._planner_preempts(message):
            return False                      # ★ 多步工具调查型 → 让规划链接管 (不做半件事)
        return self._skill_match(message, trigger_only=True) is not None

    async def _run_skill_trigger_first(self, message, ctx):
        """走技能 DAG。require_llm 只接管含生成步骤或纯产出型的技能;
        guard_generation 要求消息像"要产出" → 不命中返 None 交后面的链路。"""
        try:
            return await self._skill_dag_exec(message, ctx.get("t0", time.time()),
                                              trigger_only=True, require_llm=True,
                                               probe=ctx.get("probe", False))
        except Exception as _sde:
            logger.debug("skill dag (trigger-first) failed: %s", _sde)
            return None

    def _trigger_dominant(self, message: str, dag) -> bool:
        """触发词**主导整句** → 用户就是在点这个技能的名 (2026-09-22 加)。

        判据: 命中的触发词里最长的那个, 长度 >= 4 且覆盖消息的 >= 60%。
        用途: 只在 trigger-first 路径里豁免"产出守卫"——
        用户说技能自己的名字时, 不该还要求他额外写"帮我做/生成"。
        反例(不该放行): "帮我看看这份周报" 不含任何技能触发词 → 走不到这儿。
        """
        msg = (message or "").strip()
        if not msg:
            return False
        trigs = [t for t in ((getattr(dag, "raw_source", None) or {}).get("triggers") or []) if t]
        hit = [t for t in trigs if t.lower() in msg.lower()]
        if not hit:
            return False
        longest = max(hit, key=len)
        return len(longest) >= 4 and (len(longest) / len(msg)) >= 0.6

    def _match_planner(self, message, ctx):
        """700: 多步任务判定 (2026-09-22 重写, 依据自家 62 条标注语料)。

        旧判据 = 数连接词 ("先/再/然后" ≥2) + 兜底 "1 连接词 + 落盘目标"。
        实测: plan 召回 **50%** —— 7 条真多步漏判(用户常不写连接词), 2 条废话/犹豫误判。
        新判据 (两问的落地):
          ① 查询型动作 ≥2          → "查A，看B，整理成C" 这类**工具调查型**必然多步
          ② 总动作 ≥2 且含查询型   → "查X 然后写Y" 这类跨域多步
          ③ 查询型 ≥1 且明确落盘目标 → "读一下X，存到Y"(只 1 个连接词也算)
        外加否定/犹豫屏蔽 ("先别…/先不急着…") 与"直接做"前缀豁免。
        纯生成型多步 ("先写背景，再写方案") **不判多步** —— 那是技能自己的 DAG 该干的活
        (否则会打坏路由基线里 write-proposal 的行为)。
        """
        m = (message or "").strip()
        if not m:
            return False
        if any(kw in m[:12] for kw in ('直接做', '直接', '别走规划', '手动')):
            return False                      # "直接" 前缀跳过规划
        if any(kw in m for kw in self._NEG_PRE):
            return False                      # 否定/犹豫: "先不急着写，先看看再说"
        n_q, n_all, target = self._step_evidence(m)
        if n_q >= 2 or (n_all >= 2 and n_q >= 1) or (n_q >= 1 and target):
            return True
        # ★ 2026-09-22 补 (真引擎实跑抓到的**最后一环**): "写X保存到Y" 是一个
        #   **生成 + 落盘** 的两步任务, 但它没有连接词、也没有"查询型动作" ⇒ 上面三条
        #   都判不到它 ⇒ 落到知识库/IR 链, 那里只做"写盘"一步, 于是把**指令原文**写进文件
        #   (实测 `写一首诗保存到桌面大哥.txt` → 文件内容 "写一首")。
        #   判据: ① **生成型动词** (写/作/生成/拟/编/画/出一…) 开头或出现
        #         ② **明确落盘目标** (带扩展名的文件名 或 目录词)
        #         ③ **没有显式正文标记** (否则就是"把这段话写进去", 单步即可)
        #   ⇒ 判为多步, 让规划链 出 [llm 生成内容 → 写盘] 两步。
        #   技能(800)优先于此(700): "写周报存到桌面" 仍由技能自己产文档, 不受影响。
        _gen = re.search(r'(?:^|[，,。；;\s])(?:帮我|给我|请)?\s*'
                         r'(写|作|生成|创作|拟|编|画|出一|来个|做个)', m)
        # 显式正文: 有冒号形态 ("内容:xxx") 也含"是/为"形态 ("内容是xxx") ——
        # 实测漏了后者时, `写一个hello.txt内容是xxx` 被判成多步 (它其实单步即可)。
        _has_body = bool(re.search(r'(?:内容|正文|文字)\s*(?:[:：]|是|为|如下)', m))
        # 落盘目标的**口语形态**也算: "到桌面大哥.txt"(无"存到") 里带扩展名的文件名
        # 就是明确落点 (实测 9/10 里差的那条正是它)。用共享规则抽文件名。
        _tgt = target
        if not _tgt:
            try:
                from core.nl_paths import filename_candidates as _fc
                _tgt = bool(_fc(m))
            except Exception:
                _tgt = bool(re.search(r'\.(?:txt|docx?|pdf|xlsx?|md|csv|json|png|jpe?g|pptx?)', m, re.I))
        if not _tgt:
            # ★ 2026-09-23 补 (用户实测原话 "写一首诗放桌面" → 知识层写出 6 字垃圾):
            #   口语的落盘动词是**开放集合** (放/搁/丢/扔/挪/搬…), 上面那条例句只列了几个,
            #   实测漏掉 "搁桌面" ⇒ 又漏判。改看**形态**: 句子**以目录词收尾**且是生成型口令
            #   ⇒ "写X放到某目录" ⇒ 生成 + 落盘 (走规划链)。
            #   ★ 只在这个分支放宽 (此处已要求 _gen): "看看桌面" 这类读请求 n_q>=1 但无生成
            #     动词, 走上面 n_q/target 那条分支, 读不到这里 ⇒ 不会被误判成落盘。
            _tgt = re.search(r'(?:桌面|文档|下载|desktop|documents|downloads)\s*$', m,
                             re.I) is not None
        if _gen and _tgt and not _has_body:
            logger.debug("[PLAN] 生成型动词+落盘目标 → 两步(生成→写盘): %r", m[:50])
            return True
        return False

    async def _run_planner(self, message, ctx):
        """拆解并执行 (工作记忆隔离: 沙箱创建 → 步骤轨迹 → 结束销毁)。"""
        try:
            plan = await self.task_planner.plan(message)
            _steps = (plan or {}).get('plan') or []
            if not _steps:
                return None                   # 没出方案 → 交给后面链路 (与旧行为一致)
            if len(_steps) == 1:
                # ★ 2026-09-22 修 (真引擎实跑抓到的**最后一环**): 旧守卫是 `len > 1`,
                #   把**单步但内容实打实**的计划也扔了。实测 `写一首诗保存到桌面大哥.txt`:
                #   规划器真写出了一首诗放在 content 里 → 被这句扔掉 → 落到知识库, 那里
                #   只做"写盘"一步, 把**指令原文**("写一首")写进文件。
                #   判据: 单步计划**只有**在"落盘动作 + 内容非空且不像占位"时才执行;
                #   否则仍交回后面链路 (保持旧行为, 不放大规划链的作用面)。
                _s0 = _steps[0] or {}
                _kw = _s0.get('params') or {}
                _body = str(_kw.get('content') or _kw.get('text') or '').strip()
                _is_write = (_s0.get('tool') in ('file_ops', 'tiger_office')
                             or _s0.get('action') in ('write', 'write_word', 'save'))
                # ★ 2026-09-23 加: 内容若是**引用占位符**($开头) 或**字典文本**({...}) —
                #   说明它没解析成真内容; 长度守卫拦不住 ($stepN... 有 13 个字符)。
                #   实测: 写盘内容 = `{}` (2 字节), 还报"成功"。
                _placeholder = (str(_body).lstrip().startswith(("$", "{", "["))
                                or str(_body).strip() in ("{}", "[]"))
                if not (_is_write and len(_body) >= 8) or _placeholder:
                    logger.debug("[PLAN] 单步计划内容不足/是占位符 → 交回后面链路: %r", _body[:40])
                    return None
            self._pending_plan = plan['plan']
            if self._ke and self.plan_executor:
                self.plan_executor.ke = self._ke
            _tid = "plan_" + str(int(time.time() * 1000))
            _wm = None
            try:
                from core.working_memory import get_working_memory
                _wm = get_working_memory()
                _wm.create(_tid, context=message[:200])
                for _s in plan['plan']:
                    _wm.push(_tid, "tool",
                             str(_s.get('tool')) + "(" + str(_s.get('action', '')) + ") "
                             + str(_s.get('params', {}))[:200])
            except Exception:
                _wm = None                    # 工作记忆故障不阻塞主流程
            logger.warning("Plan steps: %s",
                           [(s.get('tool'), s.get('params')) for s in plan['plan']])
            result = await self.plan_executor.execute(plan['plan'])
            if _wm:
                try:
                    for _st in (result.get('results') or [])[:10]:
                        _wm.push(_tid, "result", str(_st)[:300])
                    _wm.destroy(_tid)
                except Exception:
                    pass
            return {"response": (self.plan_executor.format_summary(result)
                                 if result.get('success') else str(result.get('errors', ['?'])[0])),
                    "intent": "plan", "model": "local", "elapsed": self._elapsed(ctx)}
        except Exception as e:
            logger.error("TaskPlanner: %s\n%s", e, traceback.format_exc())
            return None

    # ── 600 分析 / 预读 ──
    # 触发词表 (与旧内联代码逐字一致)
    _ANALYSIS_KW = ['分析', '方案', '建议', '推荐', '哪些', '有什么', '如何', '怎么集成',
                    '怎么用', '能不能', '是否', '评估', '对比', '适合', '然后', '接着', '再', '最后', '并', '同时']
    _COUNT_KW = ["数量", "次数", "多少", "统计", "几个", "分布", "占比"]
    _OFFICE_KW = ["分析表格", "表格分析", "统计表格", "表格统计", "表格数据", "表格里", "表里",
                  "提取列", "列数据", "列内容", "合并pdf", "合并PDF", "提取pdf", "提取PDF",
                  "pdf合并", "PDF合并"]
    _PATH_RE = r'[A-Za-z]:[\\/]'
    _IMG_RE = r'[A-Za-z]:[\\/][^\n"\' ]*?\.(?:png|jpg|jpeg|gif|webp|bmp)'
    _DOC_RE = (r'[A-Za-z]:[\\/][^\n"\' ]*?\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|log|zip)')

    def _match_preread_reuse(self, message, ctx):
        """无路径追问 (数量/统计) → 复用上一轮预读的文件 (会话记忆)。"""
        if re.search(self._PATH_RE, message):
            return False
        lp = self._last_preread or {}
        return bool(lp.get("path") and any(w in message for w in self._COUNT_KW))

    async def _run_preread_reuse(self, message, ctx):
        lp = (self._last_preread or {}).get("path")
        if not lp:
            return None
        logger.info("process: 复用上次预读文件 %s (追问: %.30s)", lp, message)
        try:
            return await self._analysis_preread(message, lp, ctx.get("ext_model"),
                                                ctx.get("t0", time.time()),
                                                probe=ctx.get("probe", False))
        except Exception as _de:
            logger.warning("preread-reuse failed: %s", _de)
            return None

    def _match_office_first(self, message, ctx):
        """表格/PDF 类明确诉求 → 走 tiger_office 专业统计 (而非原始文本预读)。"""
        return any(w in message for w in self._OFFICE_KW)

    async def _run_office_first(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        ext_model = ctx.get("ext_model")
        try:
            _cl = self.intent_router.classify(message)
            if not (_cl.get("actions") and _cl["actions"][0] in
                    ("office_analyze", "office_extract", "office_pdf", "office_word")):
                return None                     # 意图不符 → 交给下一条路由 (fall-through)
            _chain = self.intent_router.build_chain(_cl)
            if not _chain:
                return None
            # 动作名: 优先用 chain 元素自带 action, 兜底 ACTION_INTENTS 模块级常量
            # (IntentRouter 无类属性!)
            _intent = _cl["actions"][0]
            _action = _chain[0].get("action") or \
                __import__("core.intent_router", fromlist=["ACTION_INTENTS"]).ACTION_INTENTS.get(_intent, {}).get("action", "analyze_table")
            _trace, _prev = [], None
            for _s in _chain:
                _prev = await self.gateway.call(_s["tool"], **dict(_s.get("params", {}), action=_action))
                _trace.append({"round": len(_trace) + 1, "tool": _s["tool"],
                               "args": str(_s.get("params", {}))[:800],
                               "result": str(_prev)[:2000],
                               "success": bool(isinstance(_prev, dict) and _prev.get("success"))})
            _out = str(_prev.get("output", str(_prev))) if isinstance(_prev, dict) else str(_prev)
            if isinstance(_out, dict):
                _out = _out.get("output", str(_out)[:3000])
            _msg = (f"{message}\n\n【{_intent} 工具结果】\n{str(_out)[:3000]}\n\n"
                    f"请基于以上工具输出回答用户的问题, 用简洁的中文总结。")
            return await self._process_external(_msg, ext_model or "deepseek", t0, pre_trace=_trace,
                                                probe=ctx.get("probe", False))
        except Exception as _de:
            logger.warning("office pre-route failed: %s", _de)
            return None

    def _match_analysis(self, message, ctx):
        return any(kw in message for kw in self._ANALYSIS_KW) and len(message) > 15

    async def _run_analysis(self, message, ctx):
        """分析类 → 直通模型。图片路径必须走本地 ollama (视觉编码), 文档路径先预读。"""
        t0 = ctx.get("t0", time.time())
        ext_model = ctx.get("ext_model")
        if re.search(self._IMG_RE, message, re.IGNORECASE):
            return await self._process_external(message, ext_model or "ollama", t0,
                                                probe=ctx.get("probe", False))
        _doc = re.search(self._DOC_RE, message, re.IGNORECASE)
        if _doc:
            _p = _doc.group(0).strip().strip('"\'')
            try:
                _r = await self._analysis_preread(message, _p, ext_model, t0,
                                                  probe=ctx.get("probe", False))
                if _r:
                    return _r
            except Exception as _de:
                logger.warning("analysis pre-read failed: %s", _de)
        # ── ★ 2026-09-21 短板1 + B: 缺信息先只读自查; 该动手就明说别干聊 ──
        message = self._enrich_lookup(message)
        return await self._process_external(message, ext_model or "deepseek", t0,
                                            probe=ctx.get("probe", False))

    def _enrich_lookup(self, message: str) -> str:
        """缺信息 → 只读自查出候选; 该动手 → 附"优先调工具"指示。**不改用户消息本身**, 只追加。

        收口: TMM_SELF_LOOKUP_DIRS 可把扫描范围限定到沙箱 (门禁/隔离跑用)。
        """
        try:
            from core import self_lookup as _SL
            if _SL.wants_lookup(message):
                _cands = _SL.discover(message, extra_dirs=[str(DATA_DIR.parent / "tmp")])
                message = message + _SL.format_candidates(message, _cands)
                logger.info("lookup.prefill <- 候选 %d 个", len(_cands))
            _directive = _SL.format_action_directive(message)
            if _directive:
                message = message + _directive
        except Exception as _le:
            from core.result import degrade as _deg
            _deg("缺信息自查未能运行", f"{type(_le).__name__}: {str(_le)[:60]}", "pipeline:lookup")
        return message

    def _match_lookup(self, message, ctx):
        """150: 只接"本来要落给模型"的消息 (150 低于所有工具/技能路由, 不抢既有能力)。"""
        if self._is_explicit_model(ctx):
            return False                      # @模型 由 900 接走, 不让本地启发式插手
        try:
            from core import self_lookup as _SL
            # ★ 2026-09-23: **创作请求**不归"缺信息自查"管 —— 让位给 understand.request(145)
            #   交给规划器 (实测交给本层会被本地弱模型处理: 时而吐模型内部工具标记乱码,
            #   时而写出成品, 不稳定)。
            if self._looks_like_creation_request(message):
                return False
            return bool(_SL.wants_lookup(message) or _SL.wants_action(message))
        except Exception:
            return False

    async def _run_lookup(self, message, ctx):
        """缺信息/要动手 → 补一手再交模型 (省钱: 同 model.fallback, 默认本地 ollama)。"""
        t0 = ctx.get("t0", time.time())
        message = self._enrich_lookup(message)
        model_to_use = ctx.get("ext_model") or "ollama"
        return await self._process_external(message, model_to_use, t0,
                                            probe=ctx.get("probe", False))
    # ── 110 ★ 「把<指代>存到<目标>」: 没有可指的现成内容 → **自己写一份**再落盘 ──
    #   起因 (2026-09-23 用户指定): "你让他自己生成任意内容进行就可以"。
    #   改前基线 (真引擎实测, scripts/verification/_probe_pronoun_save.py):
    #   `把这首诗保存到桌面` → route=model.fallback, 零工具调用, 回复"请把诗贴出来"
    #   —— 诚实, 但把话头推回给用户; 用户要的是**自己生成一份再存**。
    #   位置 110: 低于 lookup.prefill(150)、高于 model.fallback(100) —— 只接"本来要落给模型"
    #   的句子, 且**上一轮真没有可指内容**时才生效 (有内容仍由模型带历史处理, 旧行为不变)。
    _SAVE_VERBS = ('保存到', '保存进', '存到', '存进', '写入到', '写入', '写到', '写进',
                   '放到', '放在', '存成', '另存为', '导出到')
    _REF_WORDS = ('这', '那', '此', '上面', '刚才', '之前', '刚写')
    #: 要"提炼/翻译/改写/总结"某物的句子 → 手里**必须**先有那个东西, 不许自己编 (保持诚实反问)
    _REF_DERIVED = ('要点', '摘要', '总结', '梗概', '大意', '提炼', '概括', '翻译', '改写', '润色')
    #: (消息里的内容名词, 生成时的说法, 默认文件名) —— 长名词在前, 免得"诗"抢走别的说法
    _GEN_NOUNS = (
        ('对联', '一副对联', '对联.txt'),
        ('文章', '一篇短文', '文章.txt'),
        ('故事', '一个小故事', '故事.txt'),
        ('文案', '一段文案', '文案.txt'),
        ('报告', '一份简报', '报告.txt'),
        ('方案', '一份方案', '方案.txt'),
        ('提纲', '一份提纲', '提纲.txt'),
        ('代码', '一段 Python 代码', '代码.txt'),
        ('日记', '一篇日记', '日记.txt'),
        ('通知', '一则通知', '通知.txt'),
        ('清单', '一份清单', '清单.txt'),
        ('诗', '一首短诗 (中文, 四到八行)', '诗.txt'),
        ('词', '一首词', '词.txt'),
        ('内容', '一段文字', '内容.txt'),
    )
    _SAVE_DIRS = (('桌面', 'Desktop'), ('desktop', 'Desktop'),
                  ('文档', 'Documents'), ('documents', 'Documents'),
                  ('下载', 'Downloads'), ('downloads', 'Downloads'))

    @classmethod
    def _gen_noun(cls, message: str):
        """命中内容名词 → (生成说法, 默认文件名); 没命中返回 None。"""
        m = message or ""
        for key, kind, fname in cls._GEN_NOUNS:
            if key in m:
                return kind, fname
        return None

    def _has_recent_content(self, minlen: int = 40) -> bool:
        """上一轮 assistant 是否**真有**可指的现成内容 → 有就不抢, 交模型带历史处理。"""
        try:
            hist = self.sessions.recent(6) if hasattr(self, "sessions") else []
        except Exception:
            return False
        for h in hist or []:
            if h.get("role") == "assistant" and len(str(h.get("content") or "").strip()) >= minlen:
                return True
        return False

    def _save_targets(self, message: str) -> bool:
        """句子里有落盘目标: 目录词 / 盘符路径 / 带扩展名的文件名。"""
        m = message or ""
        if any(w in m.lower() for w in ('桌面', '文档', '下载', 'desktop', 'documents', 'downloads')):
            return True
        if re.search(self._PATH_RE, m):
            return True
        try:
            from core.nl_paths import filename_candidates as _fc
            return bool(_fc(m))
        except Exception:
            return bool(re.search(r'\.(?:txt|docx?|pdf|md|csv|json|py)', m, re.I))

    def _referent_missing_content(self, message: str) -> bool:
        """「把<指代>存到<目标>」但手里**没有**可指内容 —— 本类句子的公共判据。

        ★ 2026-09-23 (真引擎实跑抓出): `把这首诗保存到 D:\\...\\诗.txt` 原来被
          **ir.chain(300)** 抢走, 它把从指令里抠出来的碎片当正文写盘 ——
          实测落盘内容是 `这首诗 到` (5 字), 而且**覆盖**了同名旧文件。
          同一句在"桌面"说法下走的是 model.fallback(诚实反问) —— 两条路口径不一致。
          修法: 判据收在一处, 让 ir.chain **让位**给 save.referent_gen(110) 自己生成。
        判据四件套 (都成立才算):
          ① 落盘动词 ② 内容名词 (知道该生成什么) ③ 指代词 (不是自带真内容)
          ④ 落盘目标 ⑤ 上一轮**没有**可指的现成内容
        """
        m = (message or "").strip()
        if not m:
            return False
        if not any(v in m for v in self._SAVE_VERBS):
            return False
        if any(w in m for w in self._REF_DERIVED):
            return False                      # 要"提炼/翻译/改写"某物的 → 手里一定得有那个东西, 别自己编
        if not self._gen_noun(m):
            return False
        if not any(w in m for w in self._REF_WORDS):
            return False
        if not self._save_targets(m):
            return False
        return not self._has_recent_content()

    def _match_save_referent(self, message, ctx):
        """110: 只接"把<指代>存到<目标>"且**没有可指内容**的句子。"""
        if self._is_explicit_model(ctx):
            return False
        m = (message or "").strip()
        if not m or len(m) > 80:
            return False                      # 长句多半是真任务/多步 → 不接
        return self._referent_missing_content(m)

    @staticmethod
    def _strip_fence(text: str) -> str:
        """模型偶发加 ``` 围栏 → 去掉 (落盘内容要干净)。"""
        t = (text or "").strip()
        if t.startswith("```"):
            t = re.sub(r'^```[A-Za-z0-9_+-]*\s*', '', t)
            t = re.sub(r'\s*```\s*$', '', t)
        return t.strip()

    def _save_resolve_path(self, message: str, default_name: str) -> Path:
        """定落点: 目录词 → 家目录对应夹; 盘符路径 → 原样; 带扩展名的名字 → 用那个名字。"""
        home = Path(os.path.expanduser("~"))
        fname = default_name
        target_dir = None
        try:
            from core.nl_paths import filename_candidates as _fc
            cands = _fc(message)
            if cands:
                fname = cands[0]
        except Exception:
            pass
        mp = re.search(r'([A-Za-z]:[\\/][^\s"\'\uFF0C\u3002\u3001\uFF1B,;:]+)', message or "")
        if mp:
            p = mp.group(1).rstrip('/\\')
            if re.search(r'\.[A-Za-z0-9]{1,5}$', p):       # 结尾是文件名 → 拆开
                fname = Path(p).name
                target_dir = Path(p).parent
            else:
                target_dir = Path(p)
        if target_dir is None:
            target_dir = home / "Desktop"
            for w, d in self._SAVE_DIRS:
                if w in (message or "").lower():
                    target_dir = home / d
                    break
        path = target_dir / fname
        if path.exists():                     # 重名 → 加序号, 不覆盖用户已有文件
            stem, suf = path.stem, path.suffix
            for i in range(2, 100):
                cand = target_dir / f"{stem}_{i}{suf}"
                if not cand.exists():
                    return cand
        return path

    async def _run_save_referent(self, message, ctx):
        """自己写一份 → 落盘 → 如实说清"没有现成内容, 我现写的"。"""
        hit = self._gen_noun(message)
        if not hit:
            return None
        kind, default_name = hit
        try:
            path = self._save_resolve_path(message, default_name)
        except Exception as _pe:
            logger.debug("save.referent_gen 落点解析失败: %s", _pe)
            return None
        model_name = self._mode_pick_model(ctx.get("ext_model"))
        try:
            r = await asyncio.wait_for(self.model_client.generate(
                model_name,
                [{"role": "system",
                  "content": "你是中文写作助手。只输出正文本身: 不要标题、不要解说、不要 markdown 符号。"},
                 {"role": "user", "content": f"请写{kind}，主题自定。直接输出正文。"}],
                temperature=0.8, max_tokens=800), timeout=60)
        except Exception as _ge:
            logger.warning("save.referent_gen 生成失败: %s", _ge)
            return None                       # 生成不出来 → 交回后面链路 (保持诚实反问, 不编)
        text = self._strip_fence(str((r or {}).get("text") or ""))
        if len(text) < 8:
            return None
        try:
            res = await self.gateway.call("file_ops", action="write", path=str(path), content=text)
        except Exception as _we:
            return {"response": f"我自己写了一份，但存到 {path} 失败: {_we}",
                    "intent": "save.gen", "model": model_name, "elapsed": self._elapsed(ctx)}
        if not (isinstance(res, dict) and res.get("success")):
            _err = (res or {}).get("output") or (res or {}).get("error") or res
            return {"response": f"我自己写了一份，但存到 {path} 失败: {_err}",
                    "intent": "save.gen", "model": model_name, "elapsed": self._elapsed(ctx)}
        logger.info("save.referent_gen: 自生成 %d 字 → %s", len(text), path)
        return {"response": (f"上一轮没有可指的现成内容，我自己写了{kind}，已存到 {path}"
                             f"（{len(text)} 字）\n\n{text}"),
                "intent": "save.gen", "model": model_name, "elapsed": self._elapsed(ctx)}


    # ── 145 ★ 自然话理解兜底 ──────────────────────────────────
    #   "像要动手/要产出"的判据 —— 只看**有没有这件事的线索**, 判断交给模型。
    #   ★ 判据故意做得**宽**: 漏判 = 又掉进关键词层闷头执行 (打地鼠); 误判的代价只是
    #     多一次规划调用, 模型说"这不是要动手"就返回 None 让位 (成本可控, 方向正确)。
    _REQ_PROD = ('写', '作', '生成', '创作', '拟', '编', '画', '整', '弄', '来', '搞', '出一', '做个')
    _REQ_SAVE = ('存', '保存', '放', '搁', '扔', '丢', '塞', '写进', '写入', '写到', '导出',
                 '另存', '发到', '备份', '归到')
    _REQ_DIR = ('桌面', '文档', '下载', 'desktop', 'documents', 'downloads')
    _REQ_NOUN = ('诗', '对联', '故事', '文章', '短文', '文案', '报告', '方案', '提纲', '清单',
                 '表格', '日记', '通知', '代码', '脚本', '图片', '海报', '总结', '摘要', '计划',
                 '邮件', '列表', '文件', '目录', '截图')
    _REQ_NOGO = ('什么是', '是什么', '怎么理解', '为什么', '如何', '怎么', '教我', '讲一下',
                 '解释一下', '你觉得', '区别', '原理', '不要', '别', '先别', '不用')

    @classmethod
    def _looks_like_request(cls, message: str) -> bool:
        """这句话像不像"要我动手/要我产出点什么"。宽判据 (判断交给模型)。"""
        m = (message or "").strip()
        if not m or len(m) > 120:
            return False
        if m.startswith("/"):
            return False
        low = m.lower()
        if any(w in m for w in cls._REQ_NOGO):
            return False                       # 问概念/求解释/否定 → 不是让我动手
        has_dir = any(d in low for d in cls._REQ_DIR)
        has_save = any(w in m for w in cls._REQ_SAVE)
        has_noun = any(w in m for w in cls._REQ_NOUN)
        has_prod = any(w in m for w in cls._REQ_PROD)
        if has_save and (has_dir or has_noun):
            return True                        # "…存到桌面" / "把报告放文档里"
        if has_prod and has_dir:
            return True                        # "写点东西搁桌面"
        if has_prod and has_noun:
            return True                        # "整一首诗" / "弄个小故事"
        return False

    def _match_understand(self, message, ctx):
        """145: 只接"一切都认不出、本来要落给模型"的动手请求。"""
        if self._is_explicit_model(ctx):
            return False                       # 用户点了 @模型 → 不插手 (最高优先)
        m = (message or "").strip()
        if not self._looks_like_request(m):
            return False
        # 让位给比本路由更靠下的既有能力 (加性: 不改它们的行为)
        # ★ 2026-09-23 修 (真引擎抓到的回归): 句子带**指代词**且上一轮**真有可指内容**时
        #   不许本路由接管 —— 那种情况该带**会话历史**交给模型 (它能把上文那首原样写下来)。
        #   实测回归: "把这首诗保存到桌面"(上文已有一首诗) 被本路由抢走 → 规划器看不到历史 →
        #   写了一个 2 字节的 `{}` 到 诗.txt 还报成功。
        if (any(w in m for w in self._REF_WORDS) and self._has_recent_content()):
            return False
        if self._referent_missing_content(m):
            return False                       # 110 save.referent_gen (指代缺内容 → 自己生成)
        # ★ 2026-09-23: 只对**非创作**请求让位 —— 实测创作请求交给 lookup 会被本地弱模型
        #   吐出一堆模型内部工具标记乱码; 创作该由 understand.request 交给会写的模型。
        if self._match_lookup(m, ctx) and not self._looks_like_creation_request(m):
            return False                       # 150 lookup.prefill (只读自查)
            return False                       # 150 lookup.prefill (只读自查)
        return True

    async def _run_understand(self, message, ctx):
        """让规划器(大模型)读懂这句话 → 能拆出计划就执行; 拆不出就让位给模型。

        ★ 复用 _run_planner: 它自带"单步计划必须有落盘+真内容"的守卫,
          所以这里**不会**因为模型乱拆而写垃圾 —— 拆不出来就 return None,
          照旧落到 model.fallback (与旧行为一致)。
        """
        try:
            out = await self._run_planner(message, ctx)
        except Exception as _ue:
            logger.debug("understand.request 让位 (规划器异常): %s", _ue)
            return None
        if out:
            logger.info("understand.request: 判据认不出 → 规划器读懂了并执行")
        else:
            logger.debug("understand.request: 规划器也没读懂 → 让位给后面的链路")
        return out

    # ── 500 插件关键词 ──
    # 问句外壳 —— 城市类插件的入参清洗 (查一下/看看… + 怎么样/多少度…)
    _CITY_PREFIX_RE = re.compile(
        r'^(?:请|帮忙|帮我|麻烦)?\s*(?:查一下|查一查|查|搜一下|搜|看看|看|问一下|问|了解|知道)\s*')
    # 时间词也要剥 ("广州今天天气如何" → 广州); 放在天气词之前
    _CITY_TIME_RE = re.compile(
        r'(?:今天|明天|后天|昨天|现在|此刻|当前|这周|本周末|周末|上午|中午|下午|晚上|今晚|早上|夜里)')
    _CITY_SUFFIX_RE = re.compile(
        r'\s*(?:的)?\s*(?:天气|气温|温度|气候)?\s*(?:怎么样|如何|怎?么样|多少度|多少|呢|啊|呀|吧|吗|了|的)?\s*$')

    @classmethod
    def _clean_city_name(cls, s: str) -> str:
        """从"北京天气怎么样""查一下东京的"里剥出城市名。
        只用于 city 参数 (query 保持原样, 免得破坏翻译等其他插件)。"""
        out = s.strip()
        for _ in range(3):                     # 反复剥 (前缀动词/后缀疑问词可叠加)
            new = cls._CITY_PREFIX_RE.sub('', out, count=1)
            new = cls._CITY_TIME_RE.sub('', new)   # 时间词 (今天/明天…)
            new = new.strip()
            new = cls._CITY_SUFFIX_RE.sub('', new, count=1).strip()
            if new == out:
                break
            out = new
        return out or s.strip()

    _PLUGIN_SKIP_MSG_KW = ['然后', '接着', '先', '再', '并', '同时', '步骤', '第一步', '发给']

    def _match_plugin_keyword(self, message, ctx):
        """插件关键词路由。跳过: 多步消息 / 带 URL 的消息 (见下方 URL 事故注释)。"""
        if any(kw in message for kw in self._PLUGIN_SKIP_MSG_KW):
            return False
        if ('http://' in message) or ('https://' in message):
            return False
        # ★ 2026-09-23 (用户原则: "自然语言类似我这种表达的很多…TMM 要理解这种方式"):
        #   自然话的**动手请求**不许被插件关键词抢走 —— 实测
        #   "帮我弄个小故事存到文档里" 被 kb_search 的"文档"关键词抢走 → 答"知识库中未找到相关内容"。
        #   让位给底部的 understand.request(145), 由大模型读懂再干。
        #   只放过"像动手"的句子; 正常的知识库提问(如"文档里讲了什么")照旧走插件。
        if self._looks_like_request(message):
            logger.debug("[PLUGIN] 动手请求不让插件关键词接管: %r", message[:40])
            return False
        m = self.gateway.plugin_mgr.match_keywords(message)
        # file_ops/shell_exec 需要精确 action+path/cmd, 关键词路由的 query= 映射会破坏参数
        # ("读取 F:\x" → query="F:\x" 但 file_ops 只认 path) → 交给模型 function calling
        if m and m[0][0] in ("file_ops", "shell_exec"):
            return False
        return bool(m)

    async def _run_plugin_keyword(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        m = self.gateway.plugin_mgr.match_keywords(message)
        if not m:
            return None
        name, kw, _plg = m[0]
        try:
            query = message.replace(kw, "").strip()
            # ★ 2026-09-19 修 (真 bug, VPN 通了才暴露): 原来 city=query 直接就是
            #   "只删掉关键词"的剩余 → "北京天气怎么样"→city="北京怎么样"(未找到城市),
            #   "查一下东京的天气"→city="查一下东京的"。插件再聪明也救不了脏输入。
            #   query 保持原样(其他插件依赖), 只把 **city** 用清洗过的。
            city_q = self._clean_city_name(query)
            result = await self.gateway.plugin_mgr.call(name, query=query, city=city_q)
            if isinstance(result, dict) and result.get("success"):
                data = result.get("output", result.get("data", result.get("result", result)))
                return {"response": str(data)[:2000], "intent": f"plugin:{name}",
                        "model": "local", "elapsed": self._elapsed(ctx)}
            return {"response": str(result)[:2000], "intent": f"plugin:{name}",
                    "model": "local", "elapsed": self._elapsed(ctx)}
        except Exception:
            return None                          # 异常 → 让后面的路由接 (与旧行为一致)

    _GEO_KW = ['坐标', '经纬度', '经度', '纬度', 'geo', 'geocode']
    _GEO_RE = r'(?:查一下|查|搜)?\s*([^\s，,。的]{2,6}?)\s*(?:的)?\s*(?:坐标|经纬度)'

    def _match_geocode(self, message, ctx):
        return any(kw in message for kw in self._GEO_KW) or bool(re.search(self._GEO_RE, message))

    async def _run_geocode(self, message, ctx):
        """坐标查询: 必须**城市词 + 坐标词**同时具备才算 (单纯"在哪里"走普通对话)。"""
        city = None
        _m = re.search(self._GEO_RE, message)
        if _m:
            city = _m.group(1).strip()
        else:
            for kw in self._GEO_KW:
                if kw in message:
                    prefix = message.split(kw)[0].strip()
                    for qw in ['查一下', '查', '搜一下', '搜']:
                        prefix = prefix.replace(qw, '').strip()
                    if prefix:
                        city = prefix
                    break
        if not city:
            return None                          # 没城市词 → 交给后面的链路
        try:
            result = await self.gateway.plugin_mgr.call("nominatim", city=city)
            if isinstance(result, dict) and result.get("success"):
                data = result.get("data", {})
                return {"response": f"  {city}: {data.get('lat')}, {data.get('lon')}\n  "
                                    f"{data.get('display_name', city)}",
                        "intent": "geocode", "model": "local",
                        "elapsed": self._elapsed(ctx), "code_rounds": 0}
            return {"response": f"{city}: 坐标查询失败", "intent": "geocode", "model": "local",
                    "elapsed": self._elapsed(ctx)}
        except Exception as e:
            return {"response": f"坐标查询失败: {e}", "intent": "error", "model": "local",
                    "elapsed": self._elapsed(ctx)}

    # ── 420 打开站点 ──
    _OPEN_SITES = ["百度", "谷歌", "Google", "B站", "bilibili", "知乎", "淘宝", "京东"]
    _SITE_URL = {"百度": "https://www.baidu.com", "谷歌": "https://www.google.com",
                 "Google": "https://www.google.com", "B站": "https://www.bilibili.com",
                 "bilibili": "https://www.bilibili.com", "知乎": "https://www.zhihu.com",
                 "淘宝": "https://www.taobao.com", "京东": "https://www.jd.com"}

    def _match_open_site(self, message, ctx):
        return "打开" in message and any(s in message for s in self._OPEN_SITES)

    async def _run_open_site(self, message, ctx):
        site = next((s for s in self._OPEN_SITES if s in message), None)
        if not site:
            return None
        logger.warning("FAST-PATH HIT: 打开%s (msg=%s)", site, message[:50])
        try:
            url = self._SITE_URL.get(site, f"https://www.{site}.com")
            for browser in [r"C:/Program Files/Google/Chrome/Application/chrome.exe",
                            r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]:
                if os.path.exists(browser):
                    __import__("subprocess").Popen([browser, url])
                    return {"response": f"已打开{site}", "intent": "open_browser",
                            "model": "local", "elapsed": self._elapsed(ctx)}
            __import__("subprocess").Popen(['start', url], shell=True)
            return {"response": f"已打开{site}", "intent": "open_browser",
                    "model": "local", "elapsed": self._elapsed(ctx)}
        except Exception as e:
            return {"response": f"打开{site}失败: {e}", "intent": "open_browser",
                    "model": "local", "elapsed": self._elapsed(ctx)}

    # ── 400 搜索 ──
    _SEARCH_KW = ['搜索', '百度', 'google', '搜']
    _SEARCH_TASK_KW = ['给你一个任务', '任务：', '目标：', '要求：', '做三件事', '查一下京都',
                       '把天气', '做成插件', '你要做', '你去', '帮我写', '帮我实现',
                       '写一个', '集成', '新建插件']
    _SEARCH_EXCLUDE = ['邮件', '收件箱', '天气', '气温', '下雨', '截图', '截屏', '文件',
                       '桌面', '磁盘', '硬盘', '系统', 'txt', 'pdf', 'doc']

    def _match_search(self, message, ctx):
        """搜索。跳过: 任务指派 / 领域请求 (邮件/天气/文件…那些有自己的层)。"""
        if any(kw in message for kw in self._SEARCH_TASK_KW):
            return False
        if any(w in message for w in self._SEARCH_EXCLUDE):
            return False
        return any(kw in message for kw in self._SEARCH_KW)

    async def _run_search(self, message, ctx):
        q = message
        for kw in self._SEARCH_KW:
            if kw in q:
                q = q.split(kw, 1)[-1].strip()
                break
        # ★★ 2026-09-24 修 (真 bug, 实测): 只在关键词处切一刀会**留下口语填充词** ——
        #   "搜索一下 Python 装饰器" → 切掉"搜索" → "一下 Python 装饰器"
        #   → 搜出无关结果 (实测搜到"南海水产研究所")。
        #   本路由 (search.web, P_SEARCH) 抢在 IR chain 之前, 所以 build_chain 里
        #   那套 known/filler 剥离**根本不会跑到** —— 必须在这里补。
        #   注意: 按**长度倒序**剥, 免得"一下"没剥完就被"个"截断。
        q = q.strip()
        for _f in ("一下", "一哈", "一个", "一次", "看看", "看下", "帮我", "给我",
                   "搜搜", "查查", "个", "下"):
            if q.startswith(_f) and len(q) > len(_f):
                q = q[len(_f):].strip()
                break
        q = q.strip("，。、！？：:;,.!?\"'（）() ").strip()
        q = q or message
        try:
            result = await asyncio.wait_for(
                self.gateway.call("web_search", query=q, action="search"), timeout=10.0)
            if isinstance(result, dict) and result.get("success"):
                return {"response": result.get("output", result.get("result", "")),
                        "intent": "search", "model": "local",
                        "elapsed": self._elapsed(ctx), "code_rounds": 0}
            return {"response": f"[搜索] {result}", "intent": "search", "model": "local",
                    "elapsed": self._elapsed(ctx), "code_rounds": 0}
        except Exception as e:
            return {"response": f"搜索失败: {e}", "intent": "search", "model": "local",
                    "elapsed": self._elapsed(ctx), "code_rounds": 0}

    # ── 990 危险操作拦截 ──
    _DANGEROUS_KW = ['删除C盘', '删除系统', '格式化', '清空C盘', '删除Windows', '删掉系统']
    # ★ 2026-09-19 深测修: 原表只认 6 个**固定短语**, 换个说法就漏网
    #   (实测 "删掉整个项目目录" 没被拦, 只是恰好被下游参数校验挡住)。
    #   现在用"危险动作 × 高危目标"双条件判定 —— 两个都要中才算危险,
    #   所以"删掉桌面的 test.txt"这类正常请求不受影响。
    _DANGER_ACT = ('删除', '删掉', '清了', '清空', '格式化', '抹掉', '格掉', '卸载', '毁掉')
    # ★ 2026-09-22 深测补 (第二轮): 目标表缺"整块项目/整个工程"这一类 ——
    #   实测 "删掉整个项目目录" 动作词命中(删掉)但目标词一个不中 ('整个目录' 不是它的子串),
    #   于是没被拦, 只是恰好目标不存在 (File not found) 才没出事 —— **靠运气**。
    #   补 '项目目录'/'整个项目'/'项目文件夹'/'全部文件'; ★ 仍**不**放光杆 '目录',
    #   否则 "删除临时目录" 这类正常请求会被误拦 (反向用例钉在 verify_danger_guard.py)。
    _DANGER_TGT = ('c盘', 'd盘', 'e盘', 'f盘', '系统盘', 'windows', '系统目录', 'c:\\', 'c:/',
                   '整个目录', '全部删', '所有文件', '项目根', 'codex-distill',
                   '注册表', '用户目录', 'system32',
                   '项目目录', '整个项目', '项目文件夹', '全部文件', '整个工程')

    def _is_dangerous(self, message):
        m = (message or "").lower()
        if any(kw in message for kw in self._DANGEROUS_KW):     # 老表 (精确短语) 仍然认
            return True
        return any(a in m for a in self._DANGER_ACT) and any(t in m for t in self._DANGER_TGT)

    def _match_regulator(self, message, ctx):
        if not (hasattr(self, 'regulator') and self.regulator):
            return False
        return self._is_dangerous(message)

    async def _run_regulator(self, message, ctx):
        return {"response": "[约束] 系统核心文件保护：此操作已被拦截。如需执行请明确授权。",
                "intent": "blocked", "model": "local", "elapsed": self._elapsed(ctx)}

    # ── 950 三模式闸门 ──
    def _match_mode_gate(self, message, ctx):
        eff = ctx.get("eff_mode") or "craft"
        if eff not in ("ask", "plan"):
            return False
        s = message.strip()
        if not s or s.startswith(("/", "@", "!", "！")):
            return False
        return True

    async def _run_mode_gate(self, message, ctx):
        eff = ctx.get("eff_mode") or "craft"
        t0 = ctx.get("t0", time.time())
        ext_model = ctx.get("ext_model")
        probe = bool(ctx.get("probe"))
        _s = message.strip()
        try:
            if eff == "ask":
                return await self._process_ask(message, ext_model, t0, probe=probe)
            # ── plan 模式 ──
            _pend = self.modes.get_pending()
            if _pend and ModeManager.is_cancel(_s):
                self.modes.clear_pending()
                return {"response": "已取消该方案。说一下你想怎么改，或者直接换个需求。",
                        "intent": "plan", "model": "local",
                        "elapsed": round(time.time() - t0, 2)}
            if _pend and ModeManager.is_confirm(_s):
                return await self._run_plan(_pend, ext_model, t0, probe=probe)
            # 新需求 / 对旧方案的修改意见 → 重新出方案
            # ★ probe=True 不落盘 —— data/mode.json 是**用户状态**, 测试跑一遍会把
            #   用户的模式与待确认方案改掉 (实测污染) → 验证必须零副作用。
            return await self._plan_propose(message, ext_model, t0, prev=_pend,
                                            persist=not probe)
        except Exception as _me:
            logger.warning("mode gate failed (%s): %s — 回落到实干路径", eff, _me)
            return None       # 与原逻辑一致: 闸门失败则继续走实干链路

    # ── 1000 cmd.voice ──
    def _match_cmd_voice(self, message, ctx):
        return message.strip() == "/voice"

    async def _run_cmd_voice(self, message, ctx):
        ext_model = ctx.get("ext_model")
        import os as _o, tempfile as _tp, time as _tv2
        t0v = time.time()
        try: import sounddevice as _sd, soundfile as _sf, numpy as _np, whisper as _wh
        except ImportError as e: return {"response":f"缺依赖:{e}","intent":"voice","model":"local","elapsed":0.1}
        try:
            print("🎤 录音中 5", flush=True); _tv2.sleep(1)
            print("🎤 录音中 4", flush=True); _tv2.sleep(1)
            print("🎤 录音中 3", flush=True); _tv2.sleep(1)
            print("🎤 录音中 2", flush=True); _tv2.sleep(1)
            print("🎤 录音中 1", flush=True); _tv2.sleep(1)
            print("🎤 录音中 ✓", flush=True)
            audio = _sd.rec(int(5*16000),samplerate=16000,channels=1,dtype='float32'); _sd.wait()
            fp = _o.path.join(_tp.gettempdir(),f"tmm_v_{int(_tv2.time())}.wav")
            _sf.write(fp,audio,16000)
            if not hasattr(self,'_wm') or self._wm is None:
                self._wm = _wh.load_model("small")
            r = self._wm.transcribe(fp,language="zh")
            t = (r.get("text","")or"").strip()
            if not t: return {"response":"🎤 没听到声音","intent":"voice","model":"local","elapsed":round(_tv2.time()-t0v,2)}
            inner = await self.process(t,ext_model=ext_model)
            rt = inner.get("response",str(inner))
            try:
                import edge_tts, asyncio as _ai, re as _re2
                # ★ 2026-09-21 修 (与 tools/voice.py 同一个坑的**第二处**): 原来这里写死
                #   `from playsound import playsound` —— 本机没装 playsound → ImportError 被下面
                #   的 except 吞掉 → /voice 的**语音回复静默消失**(只回文字, 不报错)。
                #   现在复用 tools/voice.py 的三级兜底链 (playsound → pygame → 报错)。
                from tools.voice import _play_audio as _play_audio_shared
                cl = _re2.sub(r'[\U0001F300-\U0001F9FF]','',rt[:800])
                cl = _re2.sub(r'[*_#>`|]','',cl)
                cl = _re2.sub(r'\n{2,}','。',cl).replace('\n','，').strip()
                t2 = _o.path.join(_tp.gettempdir(),f"tmm_tts_{int(_tv2.time())}.mp3")
                for _a in range(3):
                    try:
                        await edge_tts.Communicate(cl,"zh-CN-YunxiNeural").save(t2)
                        if _o.path.getsize(t2)>1000: break
                        _o.remove(t2)
                    except Exception:
                        if _a<2: await _ai.sleep(1)
                        else: raise
                try:
                    _play_audio_shared(t2)
                except Exception as _pe:
                    # ★ 2026-09-21: 原来播放失败只进日志(logger.warning) —— 用户以为语音也回了。
                    #   实测踩过 (本机缺 playsound → 语音回复静默消失)。现在必须出声。
                    from core.result import degrade as _deg
                    _deg("语音播报失败", f"{type(_pe).__name__}: {str(_pe)[:60]}", "pipeline:/voice")
                finally:
                    _o.remove(t2)
            except Exception as e:
                # ★ 2026-09-21: 整段 TTS 失败也要出声 (原来只 logger.warning → 用户以为语音回了)
                from core.result import degrade as _deg
                _deg("语音回复未生成", f"{type(e).__name__}: {str(e)[:60]}", "pipeline:/voice")
                logger.warning("TTS: %s", e)
            return {"response":f"🎤 你说: {t}\n\n{rt}","intent":"voice","model":"local","elapsed":round(_tv2.time()-t0v,2)}
        except Exception as e:
            logger.error("Voice: %s",e)
            return {"response":f"语音失败: {e}","intent":"voice","model":"local","elapsed":round(_tv2.time()-t0v,2)}

    async def _run_cmd_watchdog(self, message, ctx):
        """`/watchdog` —— 本机巡检 (磁盘水位 / 桌面新增 / 下载目录大小)。**只读, 不推送。**

        复用 `scripts/watchdog.py::sense()` (单一实现, 不另写一份判据);
        要"异常时自动推微信"就挂计划任务跑同一个脚本 (见 docs/恢复.md)。
        """
        try:
            import importlib.util as _iu
            from pathlib import Path as _P
            _f = _P(__file__).resolve().parent.parent / "scripts" / "watchdog.py"
            _sp = _iu.spec_from_file_location("_tmm_watchdog", str(_f))
            _m = _iu.module_from_spec(_sp)
            _sp.loader.exec_module(_m)
            issues = _m.sense()
        except Exception as e:
            return {"response": f"巡检不可用: {e}", "intent": "watchdog",
                    "model": "local", "elapsed": 0.1}
        if not issues:
            return {"response": "✅ 本机巡检: 磁盘水位/桌面/下载都正常",
                    "intent": "watchdog", "model": "local", "elapsed": 0.1}
        body = "\n".join(f"  · {i}" for i in issues)
        return {"response": f"⚠ 本机巡检发现问题 {len(issues)} 条:\n{body}",
                "intent": "watchdog", "model": "local", "elapsed": 0.1}

    # ── 590 输入守卫 / 预过滤 ──
    @staticmethod
    def _guard_kind(message):
        """返回 'empty' / 'noise' / 'short' / 'blocked' / None。纯函数。"""
        s = message.strip()
        if not s:
            return "empty"
        meaningful = [c for c in s if c.isalnum() or '\u4e00' <= c <= '\u9fff']
        if len(meaningful) == 0:
            return "noise"
        if len(meaningful) == 1 and len(s) <= 2:
            return "short"
        return None if run_input_guards(message).passed else "blocked"


    async def _run_cmd_skill(self, message, ctx):
        """/skill — 技能工厂 (编译候选 → 质检 → 自测 → 人工确认)。

        子命令:
            /skill                 用法 + 现状
            /skill list            列出候选 (未生效)
            /skill build <需求>    编译一个候选技能 (不生效)
            /skill approve <名字>  确认生效 (复检 → 移入 tmm_skills → reload)
            /skill reject <名字>   丢弃候选
        """
        parts = message.strip().split(None, 2)
        sub = (parts[1].lower() if len(parts) > 1 else "help")
        arg = (parts[2].strip() if len(parts) > 2 else "")
        from core.skill_factory import SkillFactory
        from core import skill_loader as SL
        eng = SL.get_skill_engine()
        # gateway 传进去 → 工厂能做"实跑门" (真工具跑一遍, 选错工具就拦下)
        f = SkillFactory(generate=self.model_client.generate, engine=eng, gateway=self.gateway)
        el = lambda: round(time.time() - ctx.get("t0", time.time()), 2)

        if sub in ("help", "?", "-h", "--help"):
            n_ok = len([p for p in (SL.SKILLS_DIR).iterdir() if (p / "SKILL.md").is_file()]) if SL.SKILLS_DIR.is_dir() else 0
            cands = f.list_candidates()
            body = [
                "技能工厂 —— 把一句话需求编译成可执行 DAG 技能",
                f"  现有正式技能: {n_ok} 个 · 候选: {len(cands)} 个",
                "",
                "  /skill build <需求>     编译候选 (质检+自测通过才留, 不生效)",
                "  /skill list             列出候选",
                "  /skill approve <名字>   确认生效",
                "  /skill reject <名字>    丢弃候选",
                "",
                "四道门: 编译 → 质检 → 自测(链路可达) → **人工确认**",
            ]
            if cands:
                body += ["", "待确认候选:"] + [
                    f"  · {c.get('name')} — {(c.get('description') or '')[:50]}"
                    f"{'  ⚠ ' + str(c.get('errors'))[:60] if c.get('errors') else ''}" for c in cands]
            return {"response": "\n".join(body), "intent": "skill_factory", "model": "local",
                    "elapsed": el(), "route": "cmd.skill"}

        if sub == "list":
            cands = f.list_candidates()
            if not cands:
                return {"response": "没有候选技能。用 `/skill build <需求>` 编译一个。",
                        "intent": "skill_factory", "model": "local", "elapsed": el()}
            lines = [f"候选技能 {len(cands)} 个 (未生效):"]
            for c in cands:
                lines.append(f"  · {c.get('name')}  {c.get('steps')}步  {c.get('at','')}")
                lines.append(f"      {(c.get('description') or '')[:70]}")
                if c.get("triggers"):
                    lines.append(f"      触发: {' / '.join(c['triggers'][:5])}")
                if c.get("errors"):
                    lines.append(f"      ✗ 质检问题: {str(c['errors'])[:100]}")
            lines.append("")
            lines.append("确认: /skill approve <名字>   ·   丢弃: /skill reject <名字>")
            return {"response": "\n".join(lines), "intent": "skill_factory", "model": "local", "elapsed": el()}

        if sub == "build":
            if not arg:
                return {"response": "用法: /skill build <用一句话说清你要什么>", "intent": "skill_factory",
                        "model": "local", "elapsed": el()}
            r = await f.build(arg)
            if not r.get("ok"):
                return {"response": f"✗ 编译未通过 [{r.get('stage')}]:\n  " +
                                    "\n  ".join(str(e) for e in (r.get("errors") or [])[:6]),
                        "intent": "skill_factory", "model": "local", "elapsed": el(), "route": "cmd.skill"}
            st = r.get("self_test") or {}
            lines = [f"✓ 候选已就绪: {r['name']} (未生效)", f"  路径: {r['path']}",
                     f"  步骤: {st.get('steps')}", f"  工具链路: {[c[0] for c in (st.get('called') or [])]}",
                     f"  自测: {'链路可达 ✓' if st.get('ok') else '未跑(或未过)'}"]
            if r.get("warnings"):
                lines.append(f"  ⚠ 提示: {str(r['warnings'])[:150]}")
            lines.append(f"\n生效: /skill approve {r['name']}   ·   丢弃: /skill reject {r['name']}")
            return {"response": "\n".join(lines), "intent": "skill_factory", "model": "local",
                    "elapsed": el(), "route": "cmd.skill"}

        if sub == "approve":
            if not arg:
                return {"response": "用法: /skill approve <候选名>", "intent": "skill_factory",
                        "model": "local", "elapsed": el()}
            r = f.promote(arg)
            if not r.get("ok"):
                return {"response": f"✗ 未生效: {r.get('error')}" +
                                    (f"\n  " + "\n  ".join(str(e) for e in r.get("errors") or [])) if r.get("errors")
                                    else f"✗ 未生效: {r.get('error')}",
                        "intent": "skill_factory", "model": "local", "elapsed": el()}
            return {"response": (f"✓ 技能已生效: {r['name']}  ({r.get('steps')} 步)\n"
                                 f"  触发: {' / '.join(r.get('triggers') or [])}\n"
                                 f"  引擎重载: {r.get('reloaded')}"),
                    "intent": "skill_factory", "model": "local", "elapsed": el(), "route": "cmd.skill"}

        if sub == "reject":
            if not arg:
                return {"response": "用法: /skill reject <候选名>", "intent": "skill_factory",
                        "model": "local", "elapsed": el()}
            ok = f.reject(arg)
            return {"response": (f"✓ 已丢弃候选: {arg}" if ok else f"✗ 候选不存在: {arg}"),
                    "intent": "skill_factory", "model": "local", "elapsed": el()}

        return {"response": f"未知子命令 '{sub}'。用 /skill 看用法。", "intent": "skill_factory",
                "model": "local", "elapsed": el()}


    def _match_input_guard(self, message, ctx):
        return self._guard_kind(message) is not None

    async def _run_input_guard(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        kind = self._guard_kind(message)
        s = message.strip()
        if kind == "empty":
            return {"response": "虎哥在。有事说事。", "intent": "empty_input",
                    "model": "local", "elapsed": round(time.time() - t0, 3), "code_rounds": 0}
        if kind == "noise":
            return {"response": "说人话。", "intent": "noise", "model": "local",
                    "elapsed": round(time.time() - t0, 3), "code_rounds": 0}
        if kind == "short":
            return {"response": f"'{s}'？说完整点。", "intent": "too_short", "model": "local",
                    "elapsed": round(time.time() - t0, 3), "code_rounds": 0}
        _g = run_input_guards(message)
        logger.warning("process[#%d] blocked by guard: %s", self.stats["total"], _g.reason)
        try:
            self.sre.parse_user_constraints(message)   # 与旧内联一致: 拦截前也解析约束
        except Exception:
            pass
        return {"response": f"[GUARD] {_g.reason}", "intent": "blocked", "model": "guard",
                "elapsed": 0, "code_rounds": 0}

    # ── 290 L4 knowledge ──
    def _match_l4_knowledge(self, message, ctx):
        return self._ke is not None

    async def _run_l4_knowledge(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        ext_model = ctx.get("ext_model")
        user = ctx.get("user")
        try:
            lr = self._ke.learn_entity(message, user)
            if lr.get("success"):
                return {"response": lr["response"], "intent": "entity_learn",
                        "model": "local", "elapsed": round(time.time() - t0, 2)}
            # ★ 2026-09-22 加守卫 (真引擎实跑抓到的第二层根因):
            #   实测 `写一首诗保存到桌面大哥.txt` 被 **entity_query** 抢走 ——
            #   因为文件名里含"大哥", 而"大哥"是涛哥的联系人**别名** ⇒ 命中了实体查询,
            #   回了一张联系人气泡 (完全答非所问)。
            #   判据: 消息里若同时有**带扩展名的文件名** + **写/存类动词**, 那是在写文件,
            #   不是在问某个实体 ⇒ 跳过实体查询 (让写路径/规划链接手)。
            #   反面不误伤: 问"大哥的邮箱"不含文件名 ⇒ 照旧走实体查询。
            _fn_like = re.search(r'[^\s，。、；：,;！!？?"\'（）()\[\]]{1,60}?\.'
                                 r'(?:txt|text|docx?|pdf|xlsx?|pptx?|md|json|csv|png|jpe?g|log|mp3|mp4)',
                                 message)
            _write_like = re.search(r'(写|存|保存|写入|放到|放到|生成|导出|另存)', message)
            if _fn_like and _write_like:
                logger.debug("[L4] 有文件名+写类动词 → 跳过实体查询 (避免'大哥'这类别名抢走写盘请求)")
            else:
                qr = self._ke.query_entity(message, user)
                if qr.get("found"):
                    return {"response": qr["response"], "intent": "entity_query",
                            "model": "local", "elapsed": round(time.time() - t0, 2)}
            parsed = self._ke.parse(message)
            sk, sd, ss = parsed["skill"]
            if sk and ss > 0.02:
                entities_found = parsed.get("entities", [])
                paths_found = parsed.get("paths", [])
                has_params = bool(parsed.get("params"))
                needs_no_input = sd and not sd.get("params")
                if ((entities_found or paths_found or has_params or needs_no_input)
                        and (not ext_model or ext_model == "auto")):
                    result = await self._ke.execute(parsed, self.gateway.plugin_mgr)
                    if result.get("success"):
                        return {"response": f"OK {sk}: {result.get('output', 'done')}",
                                "intent": "knowledge_exec", "model": "local",
                                "elapsed": round(time.time() - t0, 2)}
                    # 执行失败 → 诚实报错。不能落到模型那条路: 模型看不到工具失败,
                    # 往往会声称"已完成"(幻觉)。
                    _emsg = str(result.get("error") or "").strip()
                    if _emsg:
                        _hint = ""
                        if sk == "写文件" and not (parsed.get("filename") or ""):
                            _hint = " 请带上文件名，例如: 在桌面新建 note.txt 写入 你好"
                        return {"response": f"✗ {sk} 没执行成功: {_emsg}{_hint}",
                                "intent": "knowledge_exec", "model": "local",
                                "elapsed": round(time.time() - t0, 2)}
        except Exception:
            logger.debug("l4 knowledge failed", exc_info=True)
        return None

    # ── 285 技能 DAG 兜底 ──
    async def _run_skill_catchall(self, message, ctx):
        try:
            return await self._skill_dag_exec(message, ctx.get("t0", time.time()),
                                               probe=ctx.get("probe", False))
        except Exception as _sde:
            logger.debug("skill dag (catchall) failed: %s", _sde)
            return None

    # ── 280 AutoGuide ──
    def _match_autoguide(self, message, ctx):
        if self._is_explicit_model(ctx):
            return False
        try:
            ag = self.auto_guide.respond(message)
        except Exception:
            return False
        if not ag:
            return False
        # greeting 但含投递动作 → 不接管 (让真实链路处理)
        if (ag.get('intent') == "greeting"
                and any(kw in message for kw in ['发给', '发送', '通知', '告诉', '汇报', '发邮件'])):
            return False
        ctx["_ag"] = ag
        return True

    async def _run_autoguide(self, message, ctx):
        ag = ctx.get("_ag")
        if not ag:
            return None
        intent = ag.get('intent', 'general')
        if intent in ("identity", "help", "greeting"):
            self._learn_pref(intent, ctx.get("ext_model") or "auto")
            return {"response": ag.get('response', ''), "intent": intent,
                    "model": ag.get('model', 'auto'),
                    "elapsed": self._elapsed(ctx)}
        return None

    # ── 275 FixGuard ──
    def _match_fixguard(self, message, ctx):
        try:
            r = self._guard_fix_context(message)
        except Exception:
            return False
        if not r:
            return False
        ctx["_fixctx"] = r
        return True

    async def _run_fixguard(self, message, ctx):
        return ctx.get("_fixctx")

    # ── 270 L1: 实体+动作 → 分解执行 ──
    def _match_l1(self, message, ctx):
        if not self._ke:
            return False
        has_entity = any(k.lower() in message.lower() for k in self._ke.entities)
        has_action = any(kw in message for kw in
                         ['通知', '告诉', '汇报', '报告', '发给', '发送', '打包', '整理', '汇总'])
        return has_entity and has_action

    async def _run_l1(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        model_to_use = ctx.get("ext_model") or "deepseek"
        try:
            steps = await self._decompose_task(message, model_to_use)
            if steps and len(steps) >= 2:
                _c = await self._execute_steps(steps)
                if _c["_success"]:
                    if self._we is not None:
                        self._we.learn(message, _c["_steps"])
                    sd = " -> ".join(s["skill"] for s in _c["_steps"])
                    return {"response": f"[L4] {sd}", "intent": "workflow_decomposed",
                            "model": f"{model_to_use}+L4", "elapsed": round(time.time() - t0, 2)}
        except Exception:
            logger.debug("l1 precheck failed", exc_info=True)
        return None

    # ── 265 L2: Intent Reasoner ──
    async def _run_l2(self, message, ctx):
        try:
            return await self._handle_reasoner(message, ctx.get("t0", time.time()))
        except Exception:
            logger.debug("l2 reasoner failed", exc_info=True)
            return None

    # ── 260 L3: Workflow Engine ──
    async def _run_l3(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        try:
            from core.workflow import WorkflowEngine
            if self._we is None:
                self._we = WorkflowEngine(DATA_DIR, self._ke)
            wf = self._we.match(message)
            if wf:
                tasks = [{"text": message, "parsed": self._ke.parse(message)} for _ in wf["steps"]]
                _c = await self._we.execute(tasks, self.gateway.plugin_mgr)
                if _c.get("_success"):
                    sd = " -> ".join(s["skill"] for s in _c["_steps"])
                    return {"response": f"OK workflow: {sd}", "intent": "workflow_exec",
                            "model": "local", "elapsed": round(time.time() - t0, 2)}
            tasks_raw = self._we.parse(message)
            if len(tasks_raw) >= 2:
                _c = await self._we.execute(tasks_raw, self.gateway.plugin_mgr)
                if _c.get("_success"):
                    self._we.learn(message, _c["_steps"])
                    sd = " -> ".join(s["skill"] for s in _c["_steps"])
                    return {"response": f"OK {sd}", "intent": "workflow_exec",
                            "model": "local", "elapsed": round(time.time() - t0, 2)}
        except Exception:
            logger.debug("l3 workflow failed", exc_info=True)
        return None

    # ── 255 邮件正则直发 ──
    _MAIL_RE = r'(?:发邮件|发送邮件|mail)\s*(?:给)?\s*(\S+@\S+\.\S+)'

    def _match_email_regex(self, message, ctx):
        if not self._regex_passthrough:
            return False
        return re.search(self._MAIL_RE, message) is not None

    async def _run_email_regex(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        _m = re.search(self._MAIL_RE, message)
        if not _m:
            return None
        to_addr = _m.group(1).strip().rstrip('.,;!?，。；！？')
        _subj_m = re.search(r'主题[：:]\s*(.+?)(?:[,，]\s*内容|[,，]\s*正文|$)', message)
        subject = _subj_m.group(1).strip() if _subj_m else None
        _body_m = re.search(r'(?:内容|正文)[：:]\s*(.+?)$', message)
        body = _body_m.group(1).strip() if _body_m else None
        if to_addr and subject and body:
            try:
                result = await self.gateway.plugin_mgr.call("send_email", to=to_addr,
                                                            subject=subject, body=body)
                if isinstance(result, dict) and result.get("success"):
                    return {"response": f"OK 邮件已发送 -> {to_addr}", "intent": "email_send",
                            "model": "local", "elapsed": round(time.time() - t0, 2)}
            except Exception as e:
                return {"response": f"X 邮件失败: {e}", "intent": "error", "model": "local",
                        "elapsed": round(time.time() - t0, 2)}
        return {"response": "虎哥可以帮你发邮件。请提供收件人、主题和内容。\n例如: 发邮件给 xxx@example.com 主题:你好 内容:正文",
                "intent": "email_prompt", "model": "local", "elapsed": round(time.time() - t0, 2)}

    # ── 250 URL → 浏览器 ──
    _URL_RE = r'(https?://[^\s]+)'

    def _match_url_open(self, message, ctx):
        return bool(self._regex_passthrough and re.search(self._URL_RE, message))

    async def _run_url_open(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        _m = re.search(self._URL_RE, message)
        if not _m:
            return None
        import webbrowser
        webbrowser.open(_m.group(1))
        return {"response": "[OK] opened " + _m.group(1)[:60], "intent": "desktop",
                "model": "local", "elapsed": round(time.time() - t0, 2), "code_rounds": 0}

    # ── 245 FTS 历史检索 ──
    def _match_fts(self, message, ctx):
        return bool(self._regex_passthrough)

    async def _run_fts(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        try:
            from storage.fts_search import get_fts
            results = get_fts().search(message, limit=5)
            if results:
                lines = ["找到相关的历史对话："]
                for r in results[:3]:
                    lines.append(f"  \u2022 {r.get('text', str(r))[:80]}")
                return {"response": "\n".join(lines), "intent": "fts_search",
                        "model": "auto", "elapsed": round(time.time() - t0, 2)}
        except Exception:
            logger.debug("fts search failed", exc_info=True)
        return None

    # ── 100 模型兜底 (终止) ──
    async def _run_model_fallback(self, message, ctx):
        t0 = ctx.get("t0", time.time())
        # 省钱：默认本地免费 ollama。复杂消息已由 smart-route 提前升级 deepseek。
        model_to_use = ctx.get("ext_model") or "ollama"
        try:
            return await self._process_external(message, model_to_use, t0,
                                                probe=ctx.get("probe", False))
        except Exception as e:
            logger.error("process[#%d] fallback error: %s", self.stats["total"], e)
            return {"response": f"虎哥暂时没法回答。({str(e)[:100]})", "intent": "error",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}

    # ── 360 Brain 决策树 ──
    def _match_brain(self, message, ctx):
        """只在没显式选模型时启用 (与旧内联一致)。命中判断在 run 里 ——
        没命中返回 None, dispatch 继续试下一条 (fall-through)。"""
        em = ctx.get("ext_model")
        return not (em and em != "auto")

    async def _run_brain(self, message, ctx):
        em = ctx.get("ext_model")
        try:
            brain = self._get_brain()
            brain_result = brain._quick_match(message, em)
            if brain_result:
                logger.debug("process[#%d] brain quick match: %s",
                             self.stats["total"], brain_result.get("intent"))
                return brain_result
            route = brain._classify(message, em)
            if route != "model":
                routed = await brain._route(route, message, ctx.get("t0", time.time()))
                if routed:
                    logger.debug("process[#%d] brain route=%s", self.stats["total"], route)
                    return routed
        except Exception:
            logger.debug("brain route failed", exc_info=True)
        return None

    # ── 350 复杂消息直通模型 ──
    _SMART_TOOL_MARKERS = ['file_ops(', 'shell_exec(', 'send_email(', 'system_info(',
                           'windows_desktop(', 'web_search(', '/tool ']
    _SMART_CODE_PATTERNS = ['```', 'def ', 'class ', 'import ', 'async def',
                            'action=read', 'action=write']
    _SMART_MULTI_STEP = ['先读', '然后', '再写', '逐个', '每个文件', '全部读', '依次',
                         '第一步', '第二步', '1.', '2.', '3.', '步骤']

    @classmethod
    def _complexity_score(cls, msg: str) -> int:
        """复杂度评分 (从旧内联 Smart Route 逐字迁移)。"""
        c = 0
        if len(msg) > 300:
            c += 1
        if len(msg) > 800:
            c += 2
        if len(msg) > 2000:
            c += 2
        _tool_count = sum(1 for m in cls._SMART_TOOL_MARKERS if m in msg)
        if _tool_count >= 2:
            c += 2
        if _tool_count >= 4:
            c += 1
        if '\n' in msg:
            c += 1
        if any(p in msg for p in cls._SMART_CODE_PATTERNS):
            c += 2
        if any(p in msg for p in cls._SMART_MULTI_STEP):
            c += 2
        return c

    def _match_smart_route(self, message, ctx):
        if not self._prefs.get("smart_route", True):
            return False
        em = ctx.get("ext_model")
        if em and em != "auto":
            return False
        return self._complexity_score(message) >= 3

    async def _run_smart_route(self, message, ctx):
        logger.info("process smart-route: complexity=%d, skipping local handlers -> deepseek",
                    self._complexity_score(message))
        return await self._process_external(message, "deepseek", ctx.get("t0", time.time()),
                                            probe=ctx.get("probe", False))

    # ── 300 IR chain ──
    #: 显式正文标记 —— 有它就说明用户**真给了正文**, 别当碎片拦
    _BODY_MARK = re.compile(r'(?:内容|正文|文字)\s*(?:[:：]|是|为|如下)')

    def _instruction_tail_content(self, message: str, content: str) -> bool:
        """这段"内容"是不是**只是指令的碎片** (而不是用户想要的正文)。

        用途: 让"半懂就动手"的链路让位给理解层 —— 实测
          · "帮我弄个小故事存到文档里" → ir.chain 写 4 字 output_<ts>.txt
          · "随手写首诗摆桌面上"      → 知识层写 6 字 doc_<ts>.txt
        判据 (任一成立即碎片): ① 空/太短 (<8 字)  ② 就是消息里原样抠出来的一段
        反面保护: 消息里若有**显式正文标记** (内容:/正文/如下…), 一律不算碎片。
        """
        c = str(content or "").strip()
        if self._BODY_MARK.search(message or ""):
            return False
        if len(c) < 8:
            return True
        return c in (message or "")

    def _looks_like_creation_request(self, message: str) -> bool:
        """是不是"**创作**一件新东西"的请求 (而不是检索/搬运已有材料)。

        判据: 产出动词 + 内容名词 (且没有显式正文标记)。
        用途: 创作请求**不许**被检索型规则链服务 —— 检索链产不出新内容, 只会把
              "查不到"或指令碎片写进文件 (实测 "帮我弄个小故事存到文档里" → 4 字垃圾)。
              让位给 understand.request(145), 由规划器真正写出来。
        反面: "查Agent邮件然后保存到桌面" (检索+搬运) 不命中 → 规则链照旧。
        """
        m = message or ""
        if self._BODY_MARK.search(m):
            return False
        has_prod = any(v in m for v in ('写', '作', '生成', '创作', '弄', '整', '来', '搞', '编', '画'))
        has_noun = any(n in m for n in ('诗', '对联', '故事', '文章', '短文', '文案', '报告',
                                        '方案', '提纲', '清单', '日记', '通知', '总结', '摘要',
                                        '计划', '代码', '脚本'))
        # ★ 2026-09-23 (考卷 B3 实测: "帮我弄份周报放文档里" 被规则链抢走 → 答"缺少问题"):
        #   只靠**名词表**就是打地鼠 ("周报"不在表里就漏) —— 用户明确否掉这条路线。
        #   再补一条**形态**判据: 产出动词 + 量词 + 落盘去向 = "生成一件东西再落盘"。
        #   "弄/份/放文档里" ⇒ 命中; 而 "把你好世界写入a.txt"(无产出名词/量词) 不命中。
        _MEASURE = ('份', '个', '篇', '首', '张', '幅', '条', '段', '套', '版', '本', '封', '则')
        _SAVEISH = ('存到', '存进', '保存到', '放到', '放进', '搁到', '扔到', '丢到', '摆到',
                    '写入', '写到', '写进', '放文档', '放桌面', '存桌面', '搁桌面', '扔桌面',
                    '存文档', '放下载', '存到桌面', '保存下来')
        if has_prod and (any(x in m for x in _MEASURE) and any(x in m for x in _SAVEISH)):
            return True
        return has_prod and has_noun

    def _match_ir_chain(self, message, ctx):
        """规则链命中才算 (classify → 有 actions → build_chain 有结果)。"""
        # ★ 2026-09-23 让位 (真引擎实跑抓出): 「把<指代>存到<目标>」而**手里没有可指内容**时,
        #   规则链只会把指令碎片当正文写盘 (实测 `把这首诗保存到 D:\...\诗.txt` →
        #   文件内容 = `这首诗 到`, 并且**覆盖**了同名旧文件)。这类句子该由
        #   save.referent_gen(110) 自己生成内容再落盘 —— 见 _referent_missing_content。
        try:
            if self._referent_missing_content(message):
                logger.info("ir.chain 让位 (指代内容缺失) → save.referent_gen: %r", message[:40])
                return False
        except Exception:
            pass
        # ★ 2026-09-23 第二段让位 (实测 "帮我弄个小故事存到文档里" → 写 4 字 output_<ts>.txt):
        #   **创作型请求**不许被检索型规则链服务 —— 检索链产不出新内容, 只会把
        #   "查不到"或指令碎片写进文件。让 understand.request(145) 交给规划器真正写出来。
        try:
            if self._looks_like_creation_request(message):
                logger.info("ir.chain 让位 (创作型请求) → understand.request: %r", message[:40])
                return False
        except Exception:
            pass
        try:
            cl = self.intent_router.classify(message)
        except Exception:
            return False
        # 有投递意图但无动作 → 自动补动作 (与旧内联逻辑一致)
        if cl.get("delivery") and not cl.get("actions"):
            if cl["delivery"] == "email":
                cl["actions"] = ["send_email"]
            elif cl["delivery"] == "save":
                cl["actions"] = ["file_write"]
        if not (cl.get("actions") and len(cl["actions"]) >= 1):
            return False
        try:
            if not self.intent_router.build_chain(cl):
                return False
        except Exception:
            # ★ 2026-09-19: 原来纯静默 return False → 消息会**掉到 l4.knowledge 去真执行**
            #   (实测: build_chain 抛 NameError → "发邮件…" 被当知识库技能 → 真的发了 SMTP)。
            #   静默降级必须留痕, 否则这类问题只能靠撞大运发现。
            logger.warning("ir.chain build_chain failed — 该消息将交给后面的链路", exc_info=True)
            return False
        ctx["_ir_cl"] = cl          # 传给 run, 避免重复 classify
        return True

    # 显式发信要素: 邮箱地址 / 主题: / 内容: / 正文: / 附件 — 有这些才算"用户真要发信"
    _re_mail = __import__("re").compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|主题\s*[:：]|内容\s*[:：]|正文\s*[:：]|附件")
    # ★ 2026-09-23 加: 合法收件人的判据 (显式 ASCII —— 含中文的"收件人"是解析错, 不是邮箱)
    _re_email_ok = __import__("re").compile(
        r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')

    async def _run_ir_chain(self, message, ctx):
        cl = ctx.get("_ir_cl")
        if not cl:
            return None
        return await self._ir_chain_exec(message, cl, ctx.get("ext_model"),
                                         ctx.get("t0", time.time()),
                                         probe=ctx.get("probe", False))

    async def _ir_chain_exec(self, message, cl, ext_model, t0, probe=False):
        """IR chain 执行 —— **从 process() 整块迁移, 逻辑逐字保留** (2026-09-19)。
        原为 try/except:pass 静默吞异常; 现在记 debug 日志后返回 None (交给后面的链路),
        对用户无行为差异, 但出错不再是黑盒。
        """
        try:
                # ★ 2026-09-19: 不再重复 classify —— _match_ir_chain 已分类并把结果放在 cl
                #   (原来 match 一次 + run 又一次 = 每条 IR 消息白跑一遍分类)
                # Auto-fill action when delivery present but no action
                if cl.get("delivery") and (not cl.get("actions") or len(cl["actions"]) == 0):
                    if cl["delivery"] == "email":
                        cl["actions"] = ["send_email"]
                    elif cl["delivery"] == "save":
                        cl["actions"] = ["file_write"]
                if cl.get("actions") and len(cl["actions"]) >= 1:
                    chain = self.intent_router.build_chain(cl)
                    if chain:
                        new_chain = []
                        has_send = any(s["tool"] == "send_email" for s in chain)
                        for s in chain:
                            new_chain.append(s)
                            if s["tool"] == "check_mail" and has_send:
                                new_chain.append({"tool":"check_mail","params":{"action":"read"}})
                        chain = new_chain
                        # ★ 2026-09-19 守卫: 唯一一步就是 send_email 且**没有正文** →
                        #   工具必然失败(或发出空信)。别硬发, 直接问用户要四要素。
                        #   实测触发: "给涛哥写封邮件" → chain=[send_email] params={} (无 to/主题/正文)
                        if len(chain) == 1 and chain[0].get("tool") == "send_email":
                            _sp0 = chain[0].get("params") or {}
                            _body0 = str(_sp0.get("body") or "").strip()
                            # ★★ 2026-09-19 加固 (实测事故): 光判"没正文"不够 ——
                            #   "把文件发给涛哥" 的正文是**从消息里抠出来的碎片**("把文件 涛哥"),
                            #   于是真发了封垃圾邮件给收件人 (回复还谎报"涛哥已收到")。
                            #   判定: 消息里没有**显式发信要素** (邮箱地址 / 主题: / 内容: / 正文:
                            #   / 附件) → 就不是发信请求 → 别硬发, 问用户要内容。
                            # ★ 注意: _re_mail 是**类属性** —— 方法里必须 self. 引用;
                            #   裸用会 NameError, 被外层 except 吞掉 → 掉到 l4.knowledge 出乱码 (踩过)
                            _explicit_mail = bool(self._re_mail.search(message))
                            # ★★ 2026-09-23 加 (实测事故): 原判据只看"像不像发信" ——
                            #   "济南旅游攻略 发给someone@example.com" 有邮箱 ⇒ 放行,
                            #   但收件人槽是空的 ⇒ 拿空 to 去发 ⇒ SMTP 错误: {}
                            #   现在**必须解析出合法收件人**才放行。
                            _to0 = str(_sp0.get("to") or "").strip()
                            if not _to0:
                                _to0 = str(((cl.get("slots") or {}).get("to_addr") or "")).strip()
                            if not _to0:
                                _rc0 = (cl.get("slots") or {}).get("recipient") or ""
                                if _rc0 and hasattr(self, "_ke") and self._ke:
                                    _rr0 = self._ke.lookup_entity_field(_rc0, "email")
                                    _to0 = str((_rr0 or {}).get("email", _rc0) or "") if _rr0 else ""
                            if (not _body0) or (not _explicit_mail) or (not _to0) \
                                    or not self._re_email_ok.fullmatch(_to0):
                                return {"response": "虎哥可以帮你发邮件。请提供收件人、主题和内容。\n"
                                                    "例如: 发邮件给 xxx@example.com 主题:你好 内容:正文",
                                        "intent": "email_prompt", "model": "local",
                                        "elapsed": round(time.time() - t0, 2), "trace": []}
                        prev = None
                        ir_trace = []
                        for s in chain:
                            p = s.get("params",{}).copy()
                            # Merge step-level action into params (build_chain puts it on step, not params)
                            if s.get("action"):
                                p["action"] = s["action"]
                            if prev and prev.get("emails"):
                                for s2 in chain:
                                    if s2["tool"]=="check_mail" and s2.get("params",{}).get("action")=="read":
                                        s2["params"]["mail_id"] = str(prev["emails"][0]["id"])
                                        break
                            if s["tool"]=="send_email" and not str(p.get("to") or "").strip():
                                # ★ 2026-09-23: 消息里明写的邮箱优先 (空/缺 to 都补上)
                                _em6 = str(((cl.get("slots") or {}).get("to_addr") or "")).strip()
                                recip = "" if _em6 else cl.get("slots",{}).get("recipient","")
                                if _em6:
                                    p["to"] = _em6
                                elif recip and hasattr(self,"_ke") and self._ke:
                                    r = self._ke.lookup_entity_field(recip,"email")
                                    p["to"] = r.get("email", recip) if r else ""   # 不做个人邮箱兜底 (2026-09-20 分发整改)
                                elif not p.get("to"):
                                    p["to"] = ""                                # 同上: 缺收件人就诚实失败
                            if s["tool"]=="send_email" and prev and prev.get("output"):
                                import re
                                body = prev["output"]
                                body = re.sub(r"<[^>]+>","",body)
                                body = re.sub(r"https?://[^\s]+","[链接]",body)
                                body = re.sub(r"\r","",body)
                                body = re.sub(r"\n{3,}","\n\n",body)
                                p["body"] = body.strip()
                                p["subject"] = "Fwd: Agent邮件"
                            # 替换 $prev.output / $prev.path 占位符为上一轮真实结果
                            if prev is not None:
                                # 同 1577/1624 的规矩: 不要 str(prev) 兜底 (会把裸 dict 塞进正文)
                                _prev_out = (prev.get("output", prev.get("result", ""))
                                             if isinstance(prev, dict) else prev)
                                if not _prev_out and isinstance(prev, dict) and prev.get("error"):
                                    _prev_out = f"✗ {prev['error']}"
                                _prev_path = prev.get("path", "") if isinstance(prev, dict) else ""
                                for _pk, _pv in list(p.items()):
                                    if isinstance(_pv, str):
                                        _pv = _pv.replace("$prev.output", str(_prev_out))
                                        _pv = _pv.replace("$prev.path", str(_prev_path))
                                        p[_pk] = _pv
                            prev = await self.gateway.call(s["tool"],**p)
                            ir_trace.append({"round": len(ir_trace)+1, "tool": s["tool"],
                                             "args": str(p)[:800], "result": str(prev)[:2000],
                                             "success": bool(isinstance(prev, dict) and prev.get("success"))})
                        if len(cl["actions"]) == 1 and cl["actions"][0] == "check_mail":
                            # 同样不要 str(prev) 兜底 —— 用户会直接看到这行
                            mail_output = (prev.get("output") if isinstance(prev, dict) else prev)
                            if not mail_output and isinstance(prev, dict):
                                mail_output = (f"✗ {prev['error']}" if prev.get("error")
                                               else prev.get("result", ""))
                            return {"response": str(mail_output or "")[:3000], "intent": "check_mail",
                                    "model": "local", "elapsed": round(time.time()-t0, 2), "trace": ir_trace}
                        if len(chain) == 1 and cl.get("delivery") in (None, "show"):
                            output = (prev.get("output", prev.get("result"))
                                      if isinstance(prev, dict) else prev)
                            if isinstance(output, dict):
                                output = output.get("output", str(output)[:3000])
                            # 无 output/result 时不要 str(prev) —— 会把裸 dict 打印给用户
                            # (实测: "写文件" → {'success': False, 'error': '写入内容为空...'})
                            if not output:
                                _err = prev.get("error") if isinstance(prev, dict) else None
                                output = f"✗ {_err}" if _err else ""
                            # 🔴 office 工具 (2026-08-19): tiger_office 结果喂模型分析
                            # 不直接返回原始统计文本, 拼进消息让模型答人话 (工具做数据加工, 模型做分析)
                            if cl["actions"][0] in ("office_analyze", "office_extract", "office_pdf", "office_word"):
                                try:
                                    _of_out = str(output)[:3000]
                                    _of_msg = (
                                        f"{message}\n\n"
                                        f"【{cl['actions'][0]} 工具结果】\n{_of_out}\n\n"
                                        f"请基于以上工具输出回答用户的问题, 用简洁的中文总结。"
                                    )
                                    return await self._process_external(_of_msg, ext_model or "deepseek", t0,
                                                                        pre_trace=ir_trace, probe=probe)
                                except Exception:
                                    pass  # 失败则 fall through 返回原始输出
                            # 🔴 url_shared (2026-08-24): firecrawl 抓取结果喂模型总结
                            # (与 office 工具同套路: 规则层抓取, 模型基于内容说话 — 本地模型不主动调工具)
                            if cl["actions"][0] == "url_shared":
                                try:
                                    _url_out = str(output)
                                    if isinstance(prev, dict) and not prev.get("success"):
                                        return {"response": "抓取网页失败: " + _url_out[:300],
                                                "intent": "url_shared", "model": "local",
                                                "elapsed": round(time.time()-t0, 2), "trace": ir_trace}
                                    _url_limit = 2000 if ext_model == "ollama" else 12000
                                    _url_feed = _url_out[:_url_limit]
                                    _url_src = (cl.get("slots", {}) or {}).get("url", "")
                                    _url_msg = (
                                        f"{message}\n\n"
                                        f"【网页 {_url_src} 的内容如下（前 {len(_url_feed)} 字符，"
                                        f"共 {len(_url_out)} 字符，截断因上下文限制）】\n"
                                        f"{_url_feed}\n\n"
                                        f"请基于以上网页内容回答用户的问题，用简洁的中文总结，不要凭空编造。"
                                    )
                                    return await self._process_external(_url_msg, ext_model or "deepseek", t0,
                                                                        pre_trace=ir_trace, probe=probe)
                                except Exception:
                                    pass  # 失败则 fall through 返回原始输出
                            # 🔴 file_read + 分析意图 → 走预读+模型 (2026-08-19):
                            # 消息含"什么/哪些/内容/这一列"等 → 用户要的是提取答案,
                            # 直接返回整表(原样吐回)体验差; 复用预读路径喂模型+列值提取
                            if cl["actions"][0] == "file_read" and any(
                                w in message for w in ["什么", "哪些", "内容", "这一列", "都有", "包含", "对应", "列", "行", "数据", "值"]
                            ):
                                try:
                                    _fr = self.intent_router.build_chain(cl)
                                    _path = ""
                                    for _s in (_fr or []):
                                        if _s.get("tool") == "file_ops":
                                            _path = str(_s.get("params", {}).get("path", ""))
                                    if _path:
                                        return await self._analysis_preread(message, _path, ext_model, t0,
                                                                            probe=probe)
                                except Exception:
                                    pass
                            return {"response": str(output)[:3000], "intent": cl["actions"][0],
                                    "model": "local", "elapsed": round(time.time()-t0, 2), "trace": ir_trace}
                    # Check actual send result
                    if has_send and isinstance(prev, dict) and not prev.get("success", True):
                        err = prev.get("error", "发送失败")
                        return {"response": f"✗ {err}", "intent": "chain", "model": "local", "elapsed": round(time.time()-t0, 2), "trace": ir_trace}
                    if has_send:
                        return {"response":"涛哥已收到。","intent":"chain","model":"local","elapsed":round(time.time()-t0,2), "trace": ir_trace}
                    # 2026-08-20 v3: 非邮件链(delivery=save 等) → 返回工具实际输出, 不吞结果
                    # 原代码无条件返回"涛哥已收到" — 文件保存成功也被吞成"已收到"
                    _chain_out = (prev.get("output", prev.get("result"))
                                  if isinstance(prev, dict) else prev)
                    if isinstance(_chain_out, dict):
                        _chain_out = _chain_out.get("output", str(_chain_out)[:3000])
                    # 没有 output/result 时**不要** str(prev) —— 那会把裸 dict 打印给用户
                    # (实测: "写文件" → 用户看到 {'success': False, 'error': '写入内容为空...'})
                    # 有 error 就干净地报错, 否则给空串
                    if not _chain_out:
                        _err = prev.get("error") if isinstance(prev, dict) else None
                        _chain_out = f"✗ {_err}" if _err else ""
                    return {"response": str(_chain_out)[:3000], "intent": "chain",
                            "model": "local", "elapsed": round(time.time()-t0, 2), "trace": ir_trace}
        except Exception:
            logger.debug("ir chain failed", exc_info=True)
            return None

    # ── 视觉能力判定 (2026-09-19, 2026-09-19 二次修订) ──
    # 名字里带这些标记的模型才算"能看图"; 可用环境变量 TMM_VISION_MODELS 追加(逗号分隔)。
    # ⚠ 这只用于**本地 ollama 查不到**的云端模型 (按名字提示)。
    _VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "moondream", "gemma3", "internvl", "glm-4v")
    _OLLAMA_CAPS_TTL = 600
    _ollama_caps_cache = {}

    @classmethod
    def _ollama_endpoint(cls) -> str:
        import os as _os
        host = (_os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
        if not host.startswith("http"):
            host = "http://" + host
        return host.rstrip("/")[:-3].rstrip("/") if host.rstrip("/").endswith("/v1") else host.rstrip("/")

    @classmethod
    def _ollama_capabilities(cls, model_id: str):
        """问 ollama 该模型**自报**的能力 (权威, 别按名字猜)。
        实测教训: 按名字猜会错 —— gemma4-12b-fullgpu 名字里没有 "gemma3" 却真能看图;
                  而它之所以"看不见"其实是 ollama 库路径没加载 (F 盘库)。
        返回 None = 查不到(ollama 没跑/模型不存在) → 调用方退回名字提示, **不当成"不能"**。
        """
        if not model_id:
            return None
        now = time.time()
        hit = cls._ollama_caps_cache.get(model_id)
        if hit and now - hit[0] < cls._OLLAMA_CAPS_TTL:
            return hit[1]
        try:
            import json as _j, urllib.request as _u
            req = _u.Request(cls._ollama_endpoint() + "/api/show",
                             data=_j.dumps({"model": model_id}).encode(),
                             headers={"Content-Type": "application/json"})
            d = _j.loads(_u.urlopen(req, timeout=5).read().decode())
            caps = set(d.get("capabilities") or [])
            cls._ollama_caps_cache[model_id] = (now, caps)
            return caps
        except Exception:
            return None

    @classmethod
    def _can_see_images(cls, model_name: str) -> bool:
        """该模型**真的**能看见图片吗?
        以前不判断 → 图片 base64 跟着兜底传给纯文本模型, 模型对着看不见的图**编造内容**
        (实测: 2.1MB base64 塞给 deepseek, 它输出"我直接看图描述内容: …"其实在瞎说/复读历史)。
        宁可承认看不见, 也不给假答案。
        判定顺序: ① ollama 自报能力(权威) → ② 名字提示(云端模型)。
        """
        caps = cls._ollama_capabilities(model_name)
        if caps is not None:
            return "vision" in caps
        import os as _os
        extra = [x.strip().lower() for x in (_os.environ.get("TMM_VISION_MODELS") or "").split(",") if x.strip()]
        nm = (model_name or "").lower()
        return bool(nm) and (any(h in nm for h in cls._VISION_HINTS) or nm in extra)

    def _resolved_model_id(self, model_name: str) -> str:
        """配置别名 → 实际模型名 (ollama 的 model_id 才是 /api/show 要的名字)。"""
        try:
            cfg = self.model_client._get_model_config(model_name) or {}
            return str(cfg.get("model") or model_name)
        except Exception:
            return model_name

    def _elapsed(self, ctx, nd=2):
        return round(time.time() - ctx.get("t0", time.time()), nd)

    async def _run_cmd_stats(self, message, ctx):
        total = self.stats.get("total", 0)
        errors = self.stats.get("errors", 0)
        h, rem = divmod(int(time.time() - self._startup), 3600)
        m, s = divmod(rem, 60)
        return {"response": f"运行: {h}h{m}m{s}s | 请求: {total} | 错误: {errors}",
                "intent": "stats", "model": "local", "elapsed": self._elapsed(ctx, 3)}

    async def _run_cmd_mode(self, message, ctx):
        """`/mode` `/modes` —— 回真实的三模式信息 (与 CLI 的 /mode 同口径)。"""
        try:
            cur = self.modes.info()
            allm = self.modes.all()
        except Exception as e:
            return {"response": f"模式信息读取失败: {e}", "intent": "mode_info", "model": "local"}
        lines = ["运行模式 (三模式)"]
        for k in cur.get("order", ["ask", "plan", "craft"]):
            v = allm.get(k, {})
            mark = "●" if k == cur.get("mode") else "○"
            lines.append(f"  {mark} {k:<6} {v.get('cn', '')}  {v.get('desc', '')}")
        lines.append("  切换: /ask 问答 | /plan 规划 | /craft 实干")
        return {"response": chr(10).join(lines), "intent": "mode_info",
                "model": "local", "elapsed": self._elapsed(ctx), "trace": []}

    # ══════════════════════════════════════════════════════════════
    # /depot —— 第 3 步: 对外检索 + 安全评估 + 采用
    #   铁律: 只读检索 · 先测安全再操作 · 只搬形式自造实现 · 可追溯
    # ══════════════════════════════════════════════════════════════
    def _depot_dir(self):
        d = PROJECT_ROOT / "tmp" / "_depot"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _depot_assess(self, target: str) -> dict:
        """对一个候选做完整评估: 元数据 → 下载 → 静态扫描 → 裁决。
        返回 {ev, verdict, text}。**只读 + 静态, 不执行任何候选代码。**"""
        from core import depot as DP
        ev = {"scanned": False, "risks": [], "blockers": []}
        scan = {}
        if "/" in target and not target.lower().startswith(("http",)):
            # owner/repo → GitHub 元数据 (仓库类只能看元数据, 不下载内容)
            rows = DP.search_github(f"repo:{target}", limit=1) or []
            if rows:
                r = rows[0]
                ev.update({"source": "github", "repo": r["repo"], "license": r["license"] or "",
                           "archived": r["archived"], "pushed_at": r["pushed_at"],
                           "stars": r["stars"], "summary": r["description"],
                           "name": r["repo"],
                           "url": r["url"], "blockers": []})
            else:
                ev.update({"source": "github", "repo": target, "name": target,
                           "blockers": [], "license": ""})
        else:
            m = DP.pypi_meta(target)
            if not m:
                return {"ev": {}, "verdict": None,
                        "text": f"✗ PyPI 上没有 '{target}' (或网络不可达)。名字拼对了吗?"}
            ev.update(m)
            ev["name"] = m["name"]
            if m.get("wheel_url"):
                w = DP.download_wheel(m["wheel_url"], self._depot_dir())
                if w:
                    scan = DP.scan_wheel(w)
                    ev["scanned"] = bool(scan.get("ok"))
                    ev["risks"] = scan.get("risks") or []
                    ev["blockers"] = list(scan.get("blockers") or [])
                    ev["scan_detail"] = {k: scan.get(k) for k in
                                         ("file_count", "py_files", "pth_files", "setup_py")}
                    try:
                        w.unlink()          # 扫完即删, 不留候选文件
                    except Exception:
                        pass
        v = DP.verdict(ev)
        return {"ev": ev, "verdict": v, "scan": scan,
                "text": DP.render_verdict(ev.get("name") or target, ev, v)}

    async def _skill_material(self, target: str) -> dict:
        """取外部技能素材 (owner/repo) —— **只读文本**, 不下载文件、不执行任何东西。

        看两处 (都是纯文本): repo 的 README 与 可能的 SKILL.md 路径 (raw 域名)。
        拿不到就诚实说 (返回 text="" 并带原因)。
        """
        from core import depot as DP
        import urllib.request
        # ★ 精确定位: 支持 "owner/repo#技能名" 与 "owner/repo/子路径"
        #   (技能仓库常含上百个技能, 按仓库整体取只会拿到任意的那个 —— 没法用)
        want_name = ""
        if "#" in target:
            target, want_name = target.split("#", 1)
            want_name = want_name.strip()
        _seg = [x for x in target.split("/") if x]
        repo = "/".join(_seg[:2]) if len(_seg) >= 2 else target
        subpath = "/".join(_seg[2:]) if len(_seg) > 2 else ""
        rows = DP.search_github(f"repo:{repo}", limit=1) or []
        meta = rows[0] if rows else {}
        out = {"name": meta.get("repo") or repo, "source": "github",
               "license": meta.get("license") or "", "archived": meta.get("archived"),
               "pushed_at": meta.get("pushed_at"), "stars": meta.get("stars"),
               "summary": meta.get("description") or "", "text": "", "note": ""}
        # GitHub API 拿仓库详情 (含默认分支/描述) —— 只读
        try:
            d = DP._get_json("https://api.github.com/repos/" + repo)
            out["name"] = d.get("full_name") or out["name"]
            out["license"] = (d.get("license") or {}).get("spdx_id") or out["license"]
            out["archived"] = bool(d.get("archived"))
            out["pushed_at"] = (d.get("pushed_at") or "")[:10]
            out["summary"] = d.get("description") or out["summary"]
            branch = d.get("default_branch") or "main"
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:80]}"
            # ★ "读不到" ≠ "没有许可证": GitHub 未认证限流 (403 rate limit) 很常见。
            #   留空会被下游当成"无许可证 → 不采用", 那是**假结论** (违反"失败必须诚实")。
            out["license_error"] = msg
            out["note"] = f"仓库元数据读不到 ({msg}) —— 无法判定, 不是'没有许可证'"
            return out
        # ★ 先找仓库里真正的 SKILL.md (技能本体 → 形式最准)。
        #   实测: 技能仓库几乎都把 SKILL.md 放在子目录 (skills/<name>/SKILL.md),
        #   只试根目录会退化成"只有 README", 抽出来的形式很粗 (多出安装/通知类噪声步骤)。
        #   用 GitHub 树 API 递归找 (只读), 命中就用它当主素材。
        skill_paths = []
        try:
            # ★ 2026-09-22: 树 API 可能因**出网抖动**失败 (实测 `URLError: _ssl.c:989
            #   handshake operation timed out`; 配额 5000/5000 未耗尽, 不是限流) ——
            #   抖动多为瞬时, 先重试三次再判定失败。
            tree, _te = None, None
            for _try in range(3):
                try:
                    tree = DP._get_json(
                        f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1")
                    _te = None
                    break
                except Exception as _e:
                    _te = _e
                    if _try < 2:
                        time.sleep(1.5)
            if tree is None:
                raise _te
            all_sm = [e["path"] for e in (tree.get("tree") or [])
                      if str(e.get("path", "")).lower().endswith("skill.md")]
            out["skill_count"] = len(all_sm)
            if subpath:
                all_sm = [x for x in all_sm if x.lower().startswith(subpath.lower().rstrip("/") + "/")]
            if want_name:
                # ★ 指定了名字就必须命中那一个 —— **不许**悄悄换成别的技能
                #   (实测踩到: 名字写错时旧代码回退到任意一个, 结果"看的是 A, 报告的是 B")
                hit_sm = ([x for x in all_sm if want_name.lower() in x.lower()]
                          or [x for x in all_sm
                              if Path(x).parent.name.lower() == want_name.lower()])
                if not hit_sm:
                    # ★ 2026-09-20 修 (真缺陷: 静默换成别的): 这里只往 note 里塞一句
                    #   "仓库里没有叫 X 的技能", 但**主结论仍是** render_plan 的
                    #   "✓ 可重编 <仓库>" —— 用户要的是某个技能, 拿到的却是整个仓库的评估,
                    #   正是本段注释声称要防的"悄悄换成别的"。
                    #   实测: 门禁断言时红时绿 (取决于 LLM 是否复述那行附注)。
                    #   修法: 显式标记 + 上层**短路**, 让诚实的拒绝成为主结论。
                    out["note"] = (f"仓库里没有叫 '{want_name}' 的技能 "
                                   f"(共 {len(all_sm)} 个 SKILL.md) —— 名字核对一下")
                    out["skill_missing"] = want_name      # ← 上层据此短路, 不再给仓库级结论
                    out["skill_count"] = len(all_sm)
                    return out
                all_sm = hit_sm
            skill_paths = all_sm[:3]
        except Exception as _tree_fail:
            skill_paths = []
            # ★ 2026-09-22 修 (真缺陷: 依赖取不到, 却给了另一个答案):
            #   用户点名了具体技能(`#name`), 而**枚举技能清单失败** ⇒ 无法确认它是否存在。
            #   旧代码在这里静默 `skill_paths = []`, 往下退化成"按 README 评估整个仓库",
            #   主结论变成 `✓ 可重编 <仓库>` —— 要技能却拿到仓库级结论, 正是本模块声称
            #   要防的"悄悄换成别的"; 门禁红不红还取决于当时网络抖动 (假红来源)。
            #   修法: 显式标记 + 上层**诚实短路**, 并把失败原因带上 (可指导下一步)。
            if want_name:
                out["skill_unavailable"] = want_name
                out["skill_unavailable_reason"] = f"{type(_tree_fail).__name__}: {str(_tree_fail)[:90]}"
                out["note"] = (f"这次取不到 '{want_name}' 的技能清单 "
                               f"({out['skill_unavailable_reason']}) —— 无法确认它是否存在")
                return out
        # 素材顺序: 真 SKILL.md 最优先 → 根 README → 根 SKILL.md
        for path in (skill_paths + ["README.md", "SKILL.md", "skill.md"]):
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
            try:
                b = DP._get(url, timeout=20)
                txt = b.decode("utf-8", "replace")
                if txt.strip():
                    out["text"] = (out["text"] + "\n" + txt)[:20000]
                    out["paths_found"] = out.get("paths_found", []) + [path]
            except Exception:
                continue
            if out.get("paths_found") and str(path).lower().endswith("skill.md"):
                break        # 已拿到技能本体, 不再拼 README (免得又混进安装/贡献噪声)
        if not out["text"]:
            out["note"] = ("没取到文本素材 (README/SKILL.md 都没有或读不到)"
                           + (f"; 该仓库有 {out.get('skill_count')} 个 SKILL.md, "
                              f"可用 owner/repo#技能名 指定" if out.get("skill_count") else ""))
        return out

    async def _run_cmd_depot(self, message, ctx):
        """`/depot` —— 对外检索 / 安全评估 / 采用。"""
        from core import depot as DP
        parts = message.strip().split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else "help"
        arg = parts[2].strip() if len(parts) > 2 else ""
        el = lambda: self._elapsed(ctx, 1)

        if sub in ("installed", "list"):
            return {"response": DP.render_installed(), "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el()}

        if sub in ("help", "?", "-h", "--help"):
            body = [
                "对外检索 (第 3 步) —— 只读检索 · 先测安全 · 只搬形式自造实现",
                "",
                "  /depot search <关键词>     到 GitHub 搜候选 (只读, 不下载)",
                "  /depot assess <包名>       ★ 完整安全评估 (元数据 + 下载+静态扫描 + 裁决)",
                "  /depot assess <owner/repo> 评估一个 GitHub 仓库 (只看元数据)",
                "  /depot adopt <包名>        verdict=safe 才采用 (装依赖 + 记已装清单)",
                "  /depot installed           已装清单 (来源/许可证/时间 可追溯)",
                "  /depot skill-assess <o/r>  ★ 看外部技能的**形式** (只读文本, 不搬文件)",
                "  /depot skill-adopt <o/r>   ★ 取形式 → **我们自己的工厂**重编成候选",
                "",
                "安全评估做什么: 许可证分档 · 维护状态 · Python 兼容 · **包内容静态扫描**",
                "  (.pth 文件 / setup.py / 执行外部命令 / eval+base64 / 网络 / 注册表 / 凭据路径)",
                "  · 包名混淆检测。**只统计不执行** —— 扫描过程不 import 也不跑候选代码。",
                "",
                "★ 有阻断项一律不采用; 缺证据(没扫到内容)不给 safe。",
            ]
            inst = DP.load_installed()
            if inst:
                body += ["", DP.render_installed()]
            return {"response": chr(10).join(body), "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el()}

        if sub == "search":
            if not arg:
                return {"response": "用法: /depot search <关键词>", "intent": "depot",
                        "model": "local", "route": "cmd.depot"}
            import asyncio as _a
            rows = await _a.get_event_loop().run_in_executor(None, lambda: DP.search_github(arg, 8))
            if not rows:
                return {"response": f"没搜到 (网络/限流都可能)。稍后再试, 或换个关键词。",
                        "intent": "depot", "model": "local", "route": "cmd.depot", "elapsed": el()}
            lines = [f"GitHub 候选 {len(rows)} 个 (按星排序, 只读检索):"]
            for r in rows:
                lic = r["license"] or "无许可证"
                flag = " [archived]" if r["archived"] else ""
                lines.append(f"  {r['repo']:<40} ★{r['stars']:<6} {lic:<14} "
                             f"{r['pushed_at']}{flag}")
                if r["description"]:
                    lines.append(f"      {r['description'][:80]}")
            lines.append("")
            lines.append("下一步: /depot assess <owner/repo> 或 /depot assess <PyPI包名>")
            return {"response": chr(10).join(lines), "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el()}

        if sub in ("assess", "check", "audit"):
            if not arg:
                return {"response": "用法: /depot assess <PyPI包名 | owner/repo>", "intent": "depot",
                        "model": "local", "route": "cmd.depot"}
            r = await self._depot_assess(arg)
            return {"response": r["text"], "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el(),
                    "depot_verdict": (r["verdict"] or {}).get("level")}

        if sub in ("skill-assess", "sa"):
            if not arg:
                return {"response": "用法: /depot skill-assess <owner/repo>  (只看形式, 不搬文件)",
                        "intent": "depot", "model": "local", "route": "cmd.depot"}
            from core import skill_adopt as SA
            m = await self._skill_material(arg)
            if m.get("skill_unavailable"):
                # ★ 2026-09-22: 点名了技能但**清单取不到**(出网抖动) ⇒ 不许退化成
                #   "评估整个仓库" (那等于回答了另一个问题)。诚实说 + 给下一步。
                _un = m["skill_unavailable"]
                _repo = "/".join([x for x in arg.split("#")[0].split("/") if x][:2])
                return {"response": (f"✗ 这次取不到 '{_un}' 的技能清单 "
                                     f"({m.get('skill_unavailable_reason') or '原因未知'})\n"
                                     f"  —— 无法确认它是否存在, 也没有拿整个仓库顶替。\n"
                                     f"  · 稍后重试 (出网抖动多为瞬时)\n"
                                     f"  · 想看仓库整体: /depot skill-assess {_repo}"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "elapsed": el(), "skill_unavailable": _un}
            if m.get("license_error"):
                # ★ 2026-09-22 修 (真缺陷: "读不到"被当成"没有许可证"):
                #   `_skill_material` 读不到仓库元数据时会写 `license_error` + 说明
                #   —— 但**没有调用方消费它** ⇒ 一路走到 assess, 许可证字段空 → blocked
                #   ⇒ 回一句"✗ 不采用 <repo> —— 只取形式, 自己重编"。
                #   那是**假结论**: 我们只是没读到, 不是它没许可证 (违反"失败必须诚实")。
                #   实测: 全量套件里 verify_skill_adopt 因此红 1 条 (上游抖动的时刻)。
                _le = str(m.get("license_error") or "")
                _repo = "/".join([x for x in arg.split("#")[0].split("/") if x][:2])
                return {"response": (f"✗ 读不到 {_repo} 的仓库元数据 ({_le})\n"
                                     f"  —— 无法判定, 也**不是**\"没有许可证\"; 不予重编。\n"
                                     f"  · 稍后重试 (出网抖动/未认证限流多为瞬时)\n"
                                     f"  · 只读许可/形式: /depot skill-assess {_repo}"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "elapsed": el(), "depot_blocked": True, "license_error": _le}

            if m.get("skill_missing"):
                # ★ 2026-09-20 修 (静默换成别的): 指定了技能名但仓库里没有 → **短路**,
                #   不给"✓ 可重编 <仓库>"这种仓库级结论 (那等于回答了另一个问题)。
                _mm = m["skill_missing"]
                _cnt = m.get("skill_count") or 0
                _repo = "/".join([x for x in arg.split("#")[0].split("/") if x][:2])
                return {"response": (f"✗ 仓库里没有叫 '{_mm}' 的技能"
                                     + (f" (共 {_cnt} 个 SKILL.md)" if _cnt else "")
                                     + " —— 没有对它做评估, 也不会拿别的技能顶替。\n"
                                     f"  · 想看仓库整体: /depot skill-assess {_repo}\n"
                                     f"  · 名字写错了? 该仓库含 {_cnt} 个 SKILL.md, 用 "
                                     f"/depot skill-assess {_repo}#<准确目录名> 指定"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "elapsed": el(), "skill_missing": _mm}
            plan = SA.assess_external(m)
            body = [SA.render_plan(plan)]
            if m.get("note"):
                body += ["", f"  素材说明: {m['note']}"]
            if m.get("paths_found"):
                body += [f"  只读了纯文本: {', '.join(m['paths_found'])} (没下载任何文件)"]
            if m.get("skill_count") and "#" not in arg:
                _repo = "/".join([x for x in arg.split("/") if x][:2])
                body += [f"  该仓库含 {m['skill_count']} 个 SKILL.md —— 要指定某一个用: "
                         f"/depot skill-assess {_repo}#技能名"]
            return {"response": chr(10).join(body), "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el(),
                    "skill_adopt_blocked": plan.blocked}

        if sub in ("skill-adopt",):
            if not arg:
                return {"response": "用法: /depot skill-adopt <owner/repo>  "
                                    "(只取形式, 用我们自己的工厂重编成候选)",
                        "intent": "depot", "model": "local", "route": "cmd.depot"}
            from core import skill_adopt as SA
            from core.skill_factory import SkillFactory
            from core import skill_loader as SL
            m = await self._skill_material(arg)
            if m.get("skill_unavailable"):
                # ★ 2026-09-22: 点名了技能但清单取不到(出网抖动) ⇒ **不重编任何东西**
                _un = m["skill_unavailable"]
                _repo = "/".join([x for x in arg.split("#")[0].split("/") if x][:2])
                return {"response": (f"✗ 这次取不到 '{_un}' 的技能清单 "
                                     f"({m.get('skill_unavailable_reason') or '原因未知'})\n"
                                     f"  —— 不予重编 (不确认它是否存在, 也不拿别的技能顶替)。\n"
                                     f"  · 稍后重试 (出网抖动多为瞬时)\n"
                                     f"  · 想按仓库整体重编: /depot skill-adopt {_repo}"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "elapsed": el(), "skill_adopt_blocked": True, "skill_unavailable": _un}
            if m.get("license_error"):
                # ★ 2026-09-22 修 (真缺陷: "读不到"被当成"没有许可证"):
                #   `_skill_material` 读不到仓库元数据时会写 `license_error` + 说明
                #   —— 但**没有调用方消费它** ⇒ 一路走到 assess, 许可证字段空 → blocked
                #   ⇒ 回一句"✗ 不采用 <repo> —— 只取形式, 自己重编"。
                #   那是**假结论**: 我们只是没读到, 不是它没许可证 (违反"失败必须诚实")。
                #   实测: 全量套件里 verify_skill_adopt 因此红 1 条 (上游抖动的时刻)。
                _le = str(m.get("license_error") or "")
                _repo = "/".join([x for x in arg.split("#")[0].split("/") if x][:2])
                return {"response": (f"✗ 读不到 {_repo} 的仓库元数据 ({_le})\n"
                                     f"  —— 无法判定, 也**不是**\"没有许可证\"; 不予重编。\n"
                                     f"  · 稍后重试 (出网抖动/未认证限流多为瞬时)\n"
                                     f"  · 只读许可/形式: /depot skill-assess {_repo}"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "elapsed": el(), "depot_blocked": True, "license_error": _le}

            if m.get("skill_missing"):
                # ★ 2026-09-20 修 (静默换成别的): 要的是某个技能, 缺了就**不能**拿整个仓库
                #   去重编 —— 产物会是另一个东西, 用户还以为是它要的那个。
                _mm = m["skill_missing"]
                _cnt = m.get("skill_count") or 0
                _repo = "/".join([x for x in arg.split("#")[0].split("/") if x][:2])
                return {"response": (f"✗ 仓库里没有叫 '{_mm}' 的技能"
                                     + (f" (共 {_cnt} 个 SKILL.md)" if _cnt else "")
                                     + " —— 不予重编 (不拿别的技能顶替)。\n"
                                     f"  · 想按仓库整体重编: /depot skill-adopt {_repo}\n"
                                     f"  · 名字写错了? /depot skill-assess {_repo} 先看清单"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "elapsed": el(), "skill_adopt_blocked": True, "skill_missing": _mm}
            plan = SA.assess_external(m)
            if plan.blocked:
                return {"response": (SA.render_plan(plan) + chr(10) +
                                     "✗ 不予重编 (安全闸在前) —— 许可证不合格不碰。"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "skill_adopt_blocked": True}
            need = SA.describe_form(plan.capability, plan.steps_hint)
            f = SkillFactory(generate=self.model_client.generate,
                             engine=SL.get_skill_engine(), gateway=self.gateway)
            res = await f.build(need, name_hint=plan.name_hint)
            body = [SA.render_plan(plan), "", "—— 用我们自己的工厂重编 ——"]
            if res.get("ok"):
                st = res.get("self_test") or {}
                body += [f"✓ 候选已就绪: {res['name']}  (未生效)",
                         f"  步骤: {st.get('steps')} · 工具链路: {[c[0] for c in (st.get('called') or [])]}",
                         f"  下一步: /skill approve {res['name']}  (人工确认后才生效)"]
                try:
                    DP.record_installed(res["name"], {
                        "kind": "skill", "source": "我们自己的技能工厂 (形式参考: " + (m.get("name") or arg) + ")",
                        "license": "自有 (无外部代码)", "version": "candidate",
                        "url": f"https://github.com/{arg}", "verdict": "own",
                        "note": f"取形式重编: 步骤 {'→'.join(plan.steps_hint)}; 未生效待确认"})
                except Exception:
                    pass
                return {"response": chr(10).join(body), "intent": "depot", "model": "local",
                        "route": "cmd.depot", "elapsed": el(), "skill_adopt_ok": True}
            body += [f"✗ 工厂没过 [{res.get('stage')}]:",
                     "  " + chr(10) + "  ".join(str(e)[:110] for e in (res.get("errors") or [])[:4])]
            # ★ 闭环: 工厂没过 = 我们真缺这块能力 → 记进缺口台账 (带失败原文, 便于分类)。
            #   这样"出去找 → 自己编 → 编不出来"的结论会留在账上, 而不是说完就散。
            _err = " / ".join(str(e) for e in (res.get("errors") or [])[:3])
            gl = getattr(self, "gap_ledger", None)
            if gl is not None:
                try:
                    hit = gl.record(f"[外部形式] {plan.capability or plan.name_hint}",
                                    f"✗ {plan.name_hint} 本地重编失败: {_err}", "depot.skill-adopt")
                    if hit:
                        body.append(f"  (已记入缺口台账 #{hit.get('id')} [{hit.get('category')}] "
                                    f"—— /gap why {hit.get('id')} 看本地怎么治)")
                except Exception as e:
                    logger.debug("gap record (skill-adopt) failed: %s", e)
            body.append("  (本地工具组合不出来 → 真缺这块能力)")
            return {"response": chr(10).join(body), "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el(), "skill_adopt_ok": False}

        if sub in ("adopt", "install"):
            if not arg:
                return {"response": "用法: /depot adopt <PyPI包名>", "intent": "depot",
                        "model": "local", "route": "cmd.depot"}
            # ★ --reviewed "理由": 仅当风险点**全在入口路径**(reviewable) 时可用 ——
            #   把人核对的结论写进已装清单 (可追溯), 而不是"悄悄绕过闸门"。
            reviewed = ""
            mm = re.search(r'--reviewed\s+"([^"]+)"|--reviewed\s+(\S.*)$', arg)
            if mm:
                reviewed = (mm.group(1) or mm.group(2) or "").strip()
                arg = arg[:mm.start()].strip()
            r = await self._depot_assess(arg)
            v = r["verdict"]
            if not v:
                return {"response": r["text"], "intent": "depot", "model": "local", "route": "cmd.depot"}
            ev = r["ev"]
            allowed = v["level"] == "safe" or (bool(v.get("reviewable")) and bool(reviewed))
            if not allowed:
                extra = ""
                if v.get("reviewable"):
                    extra = (chr(10) + '  (风险点全在入口路径 → 可用 /depot adopt '
                             + arg + ' --reviewed "核对结论" 放行)')
                elif v["level"] == "caution":
                    extra = chr(10) + "  (库路径上有风险点 / 证据不足 → 闸门不放行)"
                return {"response": (r["text"] + extra + chr(10) +
                                     f"✗ 不予采用 (verdict={v['level']}) —— 安全闸在前, 不放行。"),
                        "intent": "depot", "model": "local", "route": "cmd.depot",
                        "depot_verdict": v["level"]}
            # ★ safe → 采用: 装依赖 + 记清单 (可追溯)
            import subprocess as _sp
            name = ev.get("name") or arg
            try:
                pr = _sp.run([sys.executable, "-m", "pip", "install", name],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=420)
                ok = pr.returncode == 0
                tail = (pr.stdout or pr.stderr or "").strip().splitlines()[-3:]
            except Exception as e:
                ok, tail = False, [f"{type(e).__name__}: {e}"]
            DP.record_installed(name, {
                "kind": "dep", "source": ev.get("source") or "pypi",
                "license": ev.get("license"), "version": ev.get("version"),
                "url": ev.get("project_urls", {}).get("Homepage") or ev.get("url"),
                "verdict": v["level"], "installed": ok,
                "reviewed": reviewed or None,
                "note": (f"人工核对: {reviewed}; " if reviewed else "")
                        + "静态扫描通过; " + ("已装" if ok else "装失败"),
            })
            body = [r["text"], ""]
            body.append(("✓ 已装上 (我们的环境多了这个能力)" if ok else "✗ 安装失败"))
            body += ["  " + str(x)[:110] for x in tail]
            body.append(f"  已记入清单: /depot installed")
            return {"response": chr(10).join(body), "intent": "depot", "model": "local",
                    "route": "cmd.depot", "elapsed": el(), "depot_verdict": v["level"]}

        return {"response": ("用法: /depot search <关键词> · /depot assess <包名|owner/repo> · "
                             "/depot adopt <包名> · /depot installed"),
                "intent": "depot", "model": "local", "route": "cmd.depot"}

    async def _run_cmd_autofill(self, message, ctx):
        """`/autofill` —— 能力自补闭环。默认 dry-run: 不联网、不落盘, 只报会做什么。"""
        args = message.strip()[len("/autofill"):].strip()
        apply_ = "--apply" in args
        net = "--net" in args
        try:
            from core import autofill as _AF
            rep = _AF.run_cycle(apply=apply_, allow_network=net)
            body = _AF.render(rep)
            if not apply_:
                body += chr(10) + "(dry-run。真跑: /autofill --apply; 允许联网: 再加 --net)"
        except Exception as _e:
            from core.result import degrade as _deg
            _deg("能力自补闭环未能运行", f"{type(_e).__name__}: {str(_e)[:60]}", "pipeline:autofill")
            body = f"✗ 闭环跑不动: {type(_e).__name__}: {str(_e)[:120]}"
        return {"response": body, "intent": "autofill", "model": "local",
                "route": "cmd.autofill", "elapsed": self._elapsed(ctx, 1), "trace": []}

    async def _run_cmd_gap(self, message, ctx):
        """`/gap` —— 能力缺口台账。看账 / 看细节 / 丢弃 / 标记已造。"""
        gl = getattr(self, "gap_ledger", None)
        if gl is None:
            return {"response": "缺口台账未启用。", "intent": "gap", "model": "local"}
        args = message.strip()[4:].strip()
        try:
            if not args:
                return {"response": gl.render(), "intent": "gap", "model": "local",
                        "elapsed": self._elapsed(ctx, 1), "trace": []}
            parts = args.split()
            sub = parts[0].lower()
            gid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            if sub in ("why", "show") and gid:
                g = gl.get(gid)
                if not g:
                    return {"response": f"没有 #{gid} 这条缺口。", "intent": "gap", "model": "local"}
                cat = gl.CAT_CN.get(g["category"], g["category"])
                from core.self_rescue import render_plan
                txt = (f"#{g['id']} [{cat}] 出现 {g['count']} 次 · 状态 {g['status']}\n"
                       f"  原话: {str(g['sample'])[:200]}\n"
                       f"  卡在哪: {str(g['detail'])[:300]}\n"
                       f"  当时由 {g['route'] or '(没人接手)'} 接手\n"
                       f"  归一化签名: {g['signature']}\n"
                       f"  ---- 本地自救方案 ----\n  "
                       + render_plan(g).replace(chr(10), chr(10) + "  "))
                return {"response": txt, "intent": "gap", "model": "local",
                        "elapsed": self._elapsed(ctx, 1), "trace": []}
            if sub in ("drop", "forget") and gid:
                ok = gl.forget(gid)
                return {"response": (f"已丢弃缺口 #{gid}。" if ok else f"没有 #{gid}。"),
                        "intent": "gap", "model": "local"}
            if sub in ("done", "built") and gid:
                ok = gl.set_status(gid, "built", "已造")
                return {"response": (f"已标记 #{gid} 为造好了 (不再算值得造)。" if ok else f"没有 #{gid}。"),
                        "intent": "gap", "model": "local"}
            if sub in ("plan", "building") and gid:
                ok = gl.set_status(gid, "building", "已排期")
                return {"response": (f"已标记 #{gid} 为在造。" if ok else f"没有 #{gid}。"),
                        "intent": "gap", "model": "local"}
            if sub in ("rescue", "fix") and gid:
                g = gl.get(gid)
                if not g:
                    return {"response": f"没有 #{gid} 这条缺口。", "intent": "gap", "model": "local"}
                from core.self_rescue import attempt, render_plan, status_for
                # 不带 nf (no factory) 才真花调用; /gap rescue <id> 默认真试
                use_factory = "nf" not in parts[2:]
                factory = None
                if use_factory:
                    from core.skill_factory import SkillFactory
                    from core import skill_loader as SL
                    factory = SkillFactory(generate=self.model_client.generate,
                                           engine=SL.get_skill_engine(), gateway=self.gateway)
                res = await attempt(g, factory)
                st, note = status_for(res)
                gl.set_status(gid, st, note)
                _cat_cn = gl.CAT_CN.get(g["category"], g["category"])
                lines = ["#%s [%s] 本地自救" % (gid, _cat_cn), "  %s" % res.detail]
                if res.next_step:
                    lines.append(f"  → {res.next_step}")
                lines.append(f"  (账本状态: {g['status']} → {st})")
                if not res.attempted:
                    lines.append("")
                    lines.append(render_plan(g))
                return {"response": chr(10).join(lines), "intent": "gap", "model": "local",
                        "elapsed": self._elapsed(ctx, 1), "trace": [], "gap_rescue": res.kind,
                        "route": "cmd.gap"}

            if sub == "outside":
                from core.self_rescue import local_unsalvageable
                out = local_unsalvageable(gl)
                if not out:
                    return {"response": "没有『本地救不了』的缺口 —— 先 /gap rescue 试过才准出门。",
                            "intent": "gap", "model": "local", "route": "cmd.gap"}
                lines = [f"该出门找的缺口 {len(out)} 项 (本地已试过、救不了):"]
                for g in out:
                    _c = gl.CAT_CN.get(g["category"], g["category"])
                    lines.append("  #%s x%s [%s] %s" % (g["id"], g["count"], _c,
                                                        str(g["sample"])[:50]))
                lines.append("  (这些才是联网检索的输入 —— 没在本地试过的不算)")
                return {"response": chr(10).join(lines), "intent": "gap", "model": "local",
                        "route": "cmd.gap"}

            if sub == "stats":
                return {"response": str(gl.stats()), "intent": "gap", "model": "local"}
        except Exception as e:
            return {"response": f"缺口台账操作失败: {e}", "intent": "gap", "model": "local"}
        return {"response": ("用法: /gap 看账 · /gap why <id> 细节(+自救方案) · "
                             "/gap rescue <id> 本地自救 (加 nf 只看方案不花调用) · "
                             "/gap outside 看『该出门找』的 · /gap plan|done|drop <id> · /gap stats"),
                "intent": "gap", "model": "local"}

    async def _run_cmd_cost(self, message, ctx):
        from core.token_tracker import get_tracker
        return {"response": f"💰 Token用量:\n{get_tracker().summary()}",
                "intent": "cost", "model": "local", "elapsed": self._elapsed(ctx, 3)}

    async def _run_cmd_experts(self, message, ctx):
        """`/experts` —— 列出角色专家 (只读)。

        角色层做在 experts/*.md: 一份文件一个专家 (人设 + 输出规范 + 优先技能/工具)。
        用法: 在请求里写 `@专家名`, 例如 `@数据分析 把这100个Excel分析一下`。
        ★ 只读: 不改任何状态, 不写文件 (所以没有 writes_state / probe_gated)。
        """
        try:
            from core import experts as _EX
            body = _EX.render_list()
            parts = message.strip().split(None, 1)
            if len(parts) > 1 and parts[1].strip():
                # `/experts <名字>` → 展开单个专家, 便于核对它到底会注入什么
                want = parts[1].strip().lstrip("@")
                rec = _EX.load_all().get(want)
                if not rec:
                    avail = ", ".join(sorted(_EX.load_all())) or "(无)"
                    return {"response": (f"没有叫 '{want}' 的专家 —— 现有: {avail}\n"
                                         f"(不会拿别的专家顶替)"),
                            "intent": "experts", "model": "local", "route": "cmd.experts"}
                body = f"{want} 会注入的内容:\n\n{_EX.system_block(rec)}"
            return {"response": body, "intent": "experts", "model": "local",
                    "route": "cmd.experts", "elapsed": self._elapsed(ctx, 2),
                    "count": len(_EX.load_all())}
        except Exception as e:
            return {"response": f"专家层读取失败: {type(e).__name__}: {e}",
                    "intent": "experts", "model": "local", "route": "cmd.experts"}

    async def _run_cmd_tool(self, message, ctx):
        parts = message.strip().split(None, 2)
        if len(parts) < 2:
            return None                      # 落到兜底链路 (与旧行为一致)
        tool_name = parts[1]
        tool_input = parts[2] if len(parts) > 2 else ""
        try:
            if tool_name in ("run", "exec", "shell_exec"):
                result = await self.gateway.call("shell_exec", cmd=tool_input)
            else:
                call_kwargs = {}
                if "=" in tool_input:
                    for _m in re.finditer(r'(\w+)=("([^"]+)"|\'([^\']+)\'|(\S+))', tool_input):
                        call_kwargs[_m.group(1).strip()] = (_m.group(3) or _m.group(4)
                                                           or _m.group(5))
                if not call_kwargs:
                    call_kwargs["input"] = tool_input
                result = await self.gateway.call(tool_name, **call_kwargs)
            return {"response": str(result)[:5000], "intent": "tool", "model": "local",
                    "elapsed": self._elapsed(ctx), "code_rounds": 0}
        except Exception as e:
            return {"response": f"[Err] /tool {tool_name}: {e}", "intent": "error",
                    "model": "local", "elapsed": self._elapsed(ctx), "code_rounds": 0}

    async def _run_cmd_hive(self, message, ctx):
        parts = message.strip().split(None, 1)
        if len(parts) > 1:
            try:
                sub = await self.hive_mind.decompose(parts[1])
                result = await self.hive_mind.execute(sub.get("sub_tasks", []))
                return {"response": self.hive_mind.format_summary(result), "intent": "hive",
                        "model": "local", "elapsed": self._elapsed(ctx)}
            except Exception as e:
                return {"response": f"Hive: {e}", "intent": "error", "model": "local",
                        "elapsed": self._elapsed(ctx)}
        return {"response": "用法: /hive <复杂任务>", "intent": "system", "model": "local",
                "elapsed": self._elapsed(ctx)}

    @staticmethod
    def _is_pure_producer(dag) -> bool:
        """纯产出型技能 = 不含"写文件/发邮件/查邮件"工具 (如 plantuml 出图)。

        为什么单独放行: 这类技能的 triggers 精确 (架构图/流程图), 但**没有 llm 步骤**
        → 会被 require_llm 挡在早路由外 → 落到 IR chain 的"写文件"模式被抢走。
        实测缺陷: "画个架构图存到 x.png" → chain/file_ops (用户要图, 却去写文件);
        而不带路径的 "画个架构图" → skill_dag/diagram 正常。
        排掉 file_ops/send_email/check_mail 是为了**不动**已工作正常的文件/邮件流
        (实测放宽到全部工具时, "把桌面文件发给涛哥" 会从"列目录请确认"退化成直接执行)。
        """
        tools = {str((s or {}).get("tool")) for s in (getattr(dag, "steps", None) or []) if isinstance(s, dict)}
        return not (tools & {"file_ops", "send_email", "check_mail"})

    async def _skill_dag_exec(self, message: str, t0: float, trigger_only: bool = False,
                              require_llm: bool = False, guard_generation: bool = True,
                              probe: bool = False):
        """技能 DAG 干净路径。返回结果 dict, 或 None (交给后面的兜底链路)。

        trigger_only=True     → 只认 triggers 命中的技能 (强触发词优先场景)
        require_llm=True      → 只接管**含生成步骤(llm)** 或**纯产出型**(不含写文件/发邮件)的技能
                                (纯产出型没有 llm 步骤, 否则会被 IR chain 的写文件模式抢走)
        guard_generation=True → 若技能含 llm 步骤, 要求消息像"要产出"的请求
                                (有写/做/生成等动词), 否则放行走后面的链路
        """
        hit = self._skill_match(message, trigger_only=trigger_only)
        if not hit:
            return None
        name, dag, score = hit
        # 自守: _skill_match 已校验 steps 非空, 但本函数直接调用时 (验证/测试)
        # 空 steps 会让下面 dag.steps[-1] 抛 IndexError —— 干净地返回 None 更好
        if not getattr(dag, "steps", None):
            return None
        _has_llm = any((s or {}).get("tool") == "llm" for s in dag.steps)
        if require_llm and not (_has_llm or self._is_pure_producer(dag)):
            return None
        # ★ 2026-09-20 加: `answer_mode: true` 的技能**豁免产出守卫**。
        #   为什么: 守卫本意是别让"生成类"技能抢走"读类"请求 (如"帮我看看这份周报")。
        #   但**查询类**技能 (翻对话史 / 看用量 / 查服务) 的形态恰恰是
        #   "问一句话 -> 技能取数 -> 模型总结", 既没有"写/生成"字样、也没有文件/链接,
        #   于是被守卫一律挡下 → 实测 4 个新技能全部落到通用兜底
        #   ("我们之前聊过双色球吗" 被当普通闲聊)。
        #   所以做成**技能显式声明**(opt-in), 而不是放松全局守卫 —— 默认行为不变。
        _answer_mode = bool((getattr(dag, "raw_source", None) or {}).get("answer_mode"))
        # ★ 2026-09-22 加: **触发词主导整句**时也豁免产出守卫 (trigger-first 路径才生效)。
        #   为什么: 实测"电脑状态报告"是 pc-checkup 自己的触发词, 但消息里没有"写/做/生成"
        #   → 守卫拦下 → 落到 brain.route 的**罐头兜底**, 那条 4 步链条根本没跑,
        #   用户拿到一句"OK 系统状态: ..."就完事了。
        #   判据是"用户点的是这个技能的名", 与"生成类技能抢走泛化请求"是两件事 ——
        #   泛化请求(如"帮我看看这份周报")压根不含该技能的触发词, 所以不会被误放。
        if guard_generation and _has_llm and not (
                self._looks_like_creation(message)
                or self._has_explicit_source(message)
                or _answer_mode
                or (trigger_only and self._trigger_dominant(message, dag))):
            return None
        params = self._skill_params(message, dag)

        # 必填参数缺失 → 诚实告知缺什么 (不要静默放行让模型去猜)
        missing = [p for p, spec in (dag.params_schema or {}).items()
                   if (spec or {}).get("required") and not params.get(p)]
        # ★ 2026-09-22 修 (用户实测: "把这首诗保存到桌面" 被 send-file-to-contact 抢走,
        #   回一句"缺必填参数: file、recipient"): 走 **catch-all**(非 trigger-only) 时,
        #   技能是靠 tags 这类**弱线索**选中的 —— 若它的必填参数**一个都不在消息里**,
        #   说明用户根本没说这件事, 不该由它接管 (否则就是把请求错认成另一个任务)。
        #   交给后面的链路 (模型) 处理。trigger-first 路径不受影响 (用户点名的仍如实提示缺参数)。
        #   ★ 2026-09-22 二次修: 判据必须是"**必填项**全缺", 不是"所有参数全缺" ——
        #   上一版拿 missing 跟 `params_schema` 全长比, 只要技能多一个**可选**参数就失效
        #   (实测: send-file-to-contact 的 schema 比必填项多, 于是"以后默认发给涛哥"仍被抢走)。
        #   ★ 2026-09-22 三次修 (被 verify_skill_dag_layer 抓出): 让位的条件必须是
        #     "**纯弱线索**选中的" —— 即**连触发词都没命中**。夹具技能 test-copy-file 的
        #     triggers 是 [测试拷贝], 消息"测试拷贝"命中触发词 ⇒ 用户就是在说这件事,
        #     必须**如实提示缺参数** (原来我一律 return None → 该门 2 条红)。
        _req_all = [p for p, spec in (dag.params_schema or {}).items() if (spec or {}).get("required")]
        _trig_hit = bool(self._skill_match(message, trigger_only=True)) if _req_all else True
        if (missing and not trigger_only and _req_all and len(missing) == len(_req_all)
                and not _trig_hit):
            return None
        if missing:
            label = "、".join(missing)
            return {"response": f"✗ {name} 缺必填参数: {label}。补上再试。",
                    "intent": "skill_dag", "model": "local",
                    "elapsed": round(time.time() - t0, 2), "skill": name,
                    "missing": missing}

        step_out = {}
        for i, step in enumerate(dag.steps):
            sid = step.get("id", f"step_{i}")
            tool = step.get("tool", "")
            action = step.get("action", "")
            on_error = step.get("on_error", "stop")

            # 解析 $params.X / $steps.<id>.<field> / $message
            # 支持两种形态: ①整个值就是占位符(保留原类型) ②字符串内插值(转字符串)
            resolved = {}
            for k, v in (step.get("input") or {}).items():
                resolved[k] = self._resolve_tmpl(v, params, step_out, message)

            try:
                if tool == "knowledge":
                    # SKILL.md 用 knowledge 步骤做实体解析 → 本地查表实现 (不再是 stub)
                    val = self._entity_resolve(resolved.get("name", ""),
                                               resolved.get("field", "email"))
                    res = ({"success": True, "email": val} if val
                           else {"success": False, "error": f"实体 '{resolved.get('name','')}' 没有 {resolved.get('field','email')}"})
                elif tool == "llm":
                    # ★ 让技能能"生成内容" (不只搬运) —— WorkBuddy 那类"AI 写文档"的核心
                    _prompt = str(resolved.get("prompt") or "").strip()
                    if not _prompt:
                        res = {"success": False, "error": "llm 步骤缺 prompt"}
                    else:
                        _model = str(resolved.get("model") or "deepseek")
                        _msgs = []
                        if resolved.get("system"):
                            _msgs.append({"role": "system", "content": str(resolved["system"])})
                        _msgs.append({"role": "user", "content": _prompt})
                        _r = await self.model_client.generate(
                            _model, _msgs,
                            temperature=float(resolved.get("temperature", 0.7)),
                            max_tokens=int(resolved.get("max_tokens", 3000)))
                        if _r.get("error"):
                            res = {"success": False, "error": f"llm({_model}): {_r['error']}"}
                        else:
                            res = {"success": True, "text": _r.get("text", ""),
                                   "output": _r.get("text", "")}
                elif self.gateway:
                    res = await self.gateway.call(tool, action=action, **resolved)
                else:
                    res = {"success": False, "error": "无 ToolGateway"}
            except Exception as e:
                res = {"success": False, "error": str(e)}

            ok = bool(isinstance(res, dict) and res.get("success", res.get("ok")))
            # 支持 SKILL.md 的 output 别名 (步骤声明 output: content → $steps.<id>.content)
            alias = step.get("output")
            if isinstance(res, dict):
                step_out[sid] = dict(res)
                if alias and not step_out[sid].get(alias):
                    step_out[sid][alias] = res.get("output", res.get("result", ""))
            else:
                step_out[sid] = {"output": res}

            if not ok:
                err = (res or {}).get("error") or (res or {}).get("output") or "执行失败"
                if on_error == "stop":
                    # ★ 2026-09-21: 技能失败 → 记进使用账 (驱动"不好用就退回候选池")
                    #   验证流量**绝不记账** (probe=True): 实测门禁跑技能时把 healthcheck/pc-checkup
                    #   写进了用户真使用账 —— 与产物台账同一类污染, 同一个根因。
                    if not probe:
                        try:
                            from core import skill_usage as _SU
                            _SU.record(name, False, route="skill_dag", note=str(err)[:120])
                        except Exception:
                            pass
                    return {"response": f"✗ {name} 第 {i+1} 步({sid}) 失败: {err}",
                            "intent": "skill_dag", "model": "local",
                            "elapsed": round(time.time() - t0, 2), "skill": name,
                            "trace": [{"step": k, "ok": bool(v.get("success", v.get("ok")))}
                                      for k, v in step_out.items()]}

        last_step = dag.steps[-1]
        if last_step.get("tool") == "knowledge" or not last_step.get("tool"):
            # 末步只是内部解析(knowledge), 没有用户可见产出 → 交回后续链路
            return None
        last = step_out.get(last_step.get("id", f"step_{len(dag.steps)-1}")) or {}
        out = last.get("output", last.get("result", ""))
        # ★ 2026-09-21: 技能跑成 → 记进使用账 (驱动"用着好就留在正式库"); probe 不记
        if not probe:
            try:
                from core import skill_usage as _SU
                _SU.record(name, True, route="skill_dag")
            except Exception:
                pass
        return {"response": f"✓ {name}: {str(out)[:1500]}" if out else f"✓ {name} 执行完毕",
                "intent": "skill_dag", "model": "local",
                "elapsed": round(time.time() - t0, 2), "skill": name,
                "steps": [s.get("id") for s in dag.steps]}

    def _skill_context(self, message: str, compact: bool = False) -> str:
        """匹配到的技能说明 — 注入 system prompt, 由模型自己决定调哪些工具 (方案 A: 只注入不强制)。

        对齐 OpenClaw/Hermes 的 SKILL.md 机制: description/triggers 命中 → 技能正文进上下文。
        这是**加性**改动: 不改路由、不排序、不截断选工具、不加 fallback —— 只让模型看见说明书。

        边界: IR chain (process() L1483) 在模型之前拦截简单指令, 那些请求到不了这里;
        本注入只影响"走到模型这条路"的请求。
        """
        try:
            from core.skill_loader import get_skill_index
            ctx = get_skill_index().match_context(message, max_skills=2)
            if not ctx:
                return ""
            if compact:
                # 本地小模型 ctx 紧(8K): 只留技能名+说明, 去掉正文。
                # 只能按"2 空格缩进的条目行"筛 —— match_context 的正文 bullet 在 0 列
                # 也是 "- " 开头, 用 ln.strip().startswith("- ") 会把正文一起放进来 (实测踩到)。
                keep = [ln for ln in ctx.split("\n")
                        if ln.startswith("[Skills") or ln.startswith("  - ")]
                ctx = "\n".join(keep)[:600]
            return ctx
        except Exception as e:
            logger.debug("skill context failed: %s", e)
            return ""

    async def _process_external(self, message, model_name, t0, pre_trace=None, probe=False):
        """Structured tool calling — model gets OpenAI tool schemas, returns structured tool_calls."""
        import json as _json
        
        # Build system prompt (lean — tools sent separately via API)
        project_root = str(PROJECT_ROOT)
        # ★ 2026-09-20 分发整改: 桌面/主目录**派生** (原来是写死家目录字面量,
        #   换台机器就把别人引到不存在的路径)。本机派生值与原字面量完全一致。
        #   ★ 兜底要真兜底: 别写 `os.environ.get("USERPROFILE") or str(Path.home())` ——
        #     实测清掉 USERPROFILE/HOMEDRIVE/HOMEPATH 后 Path.home() 会抛
        #     RuntimeError("Could not determine home directory.")。逐级试到最后才安全。
        _home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
        if not _home:
            _hd, _hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
            _home = (_hd + _hp) if (_hd and _hp) else ""
        if not _home:
            try:
                _home = str(Path.home())
            except Exception:
                import tempfile as _tf
                _home = _tf.gettempdir()
        _desktop = os.path.join(_home, "Desktop")
        system = (
            "You are Tiger.M.M (MARY III), a local AI agent running on Windows. "
            "Respond in Chinese. Be direct, practical, and helpful. "
            "When you need to do something (check time, run a command, search, get weather, etc.), "
            "use the available tools. After using a tool, incorporate the result into your response. "
            "Always respond in plain text — no markdown formatting.\n"
            "\n# CRITICAL RULES\n"
            "1. PROJECT ROOT is " + project_root + ". Relative paths like 'core/pipeline.py' "
            "resolve to this directory. Use it for all file operations.\n"
            "2. For file paths, ALWAYS use the exact paths the user provided. "
            "Do NOT guess, invent, or assume paths — the user knows their filesystem.\n"
            "3. The user's Desktop is at " + _desktop + ". "
            "Do NOT use Administrator or other usernames.\n"
            "4. When reading source code files, use the exact relative paths the user mentions. "
            "If user says 'core/pipeline.py', the full path is " + project_root + "\\core\\pipeline.py.\n"
            "5. Read files COMPLETELY before writing any output based on them.\n"
            "6. OUTPUT PATH: When the user asks to save/write a file, ALWAYS use the absolute "
            "Desktop path: " + _desktop + "\\FILENAME. Never use relative paths "
            "like '.\\file.txt' or bare filenames — they resolve to " + _home + ", "
            "not the Desktop. The correct format is " + _desktop + "\\FILENAME.\n"
            "7. When naming output files, use a meaningful name (e.g., 'TigerMM架构说明书.md'), "
            "not auto-generated timestamps like 'doc_20260727_151730.txt'.\n"
            "8. HARD RULES (ABSOLUTE — violating these is a critical error):\n"
            "   - 需要读取文件、写文件、获取时间/日期、统计文件数量、查磁盘空间/进程时, "
            "绝对禁止直接给出答案。必须先调用工具 (file_ops / system_info) 拿到真实结果, 再基于结果回答。\n"
            "   - 禁止编造时间、日期、文件数量、磁盘占用等数字 — 没有工具返回值就没有这些数字。\n"
            "   - 复杂多步骤任务: 一次只做一步。每轮最多调用2个工具, 等待工具返回后再进行下一步。\n"
            "   - 如果无法调用工具完成操作, 诚实说明做不到, 禁止声称已执行。"
        )

        # ── Experts 角色层 (2026-09-20, 借鉴通用 Agent 平台的 "Experts 面") ──
        # 加性: 没命中任何专家 → system 与原来**逐字节一致** (不改变既有行为)。
        # `@名字` 命中 → 追加人设+输出规范; `@了不存在但很像的` → 追加一句诚实提示
        # (不静默顶替 —— 这条是今天在 /depot 上踩过的教训, 这里从设计上避开)。
        try:
            from core import experts as _EX
            _exp, _why = _EX.match(message)
            if _exp:
                system = system + "\n\n" + _EX.system_block(_exp)
            elif isinstance(_why, str) and _why.startswith("unknown:"):
                system = system + "\n\n" + _EX.hint_block(_why.split(":", 1)[1])
        except Exception as _ee:                      # 专家层出问题不许影响主流程
            logger.debug("expert layer skipped: %s", _ee)
        
        # Inject project context
        # 🔴 ollama 精简 (2026-08-20): gemma4-12b ctx 8192 硬限, 项目上下文/知识图谱/实体/记忆
        # 对本地小模型是负担非帮助 → 只保留核心规则, 减少 token 防空回复
        _is_ollama = (model_name == "ollama")
        if not _is_ollama:
            proj_ctx = self.perception.get_project_context()
            if proj_ctx:
                system = f"# Project Context\n{proj_ctx}\n\n---\n\n{system}"
        
        # Inject entity knowledge (contacts)
        if not _is_ollama and hasattr(self, '_ke') and self._ke and hasattr(self._ke, 'entities'):
            ents = []
            try:
                for name, info in list(self._ke.entities.items())[:10]:
                    email = info.get('email', '')
                    if email:
                        ents.append(f"{name}: {email}")
            except Exception:
                pass
            if ents:
                system += f"\n\n# Known Contacts\n" + "\n".join(ents)

        # Inject knowledge graph (semantic relations — 知识图谱, 关系推理)
        if not _is_ollama:
            try:
                if hasattr(self, 'perception') and self.perception:
                    _graph_ctx = self.perception.get_graph_context()
                    if _graph_ctx:
                        system += f"\n\n{_graph_ctx}"
            except Exception:
                pass

        # Inject memory
        if not _is_ollama:
            mem_ctx = self.memory.build_context_for_model(message)
            if mem_ctx:
                system += f"\n\n# Recent Memory\n{mem_ctx}"

        # Inject learned preferences (学习闭环的「生效」端 —— 用户教过就遵守)
        if not _is_ollama and getattr(self, "learn_loop", None):
            try:
                _lh = self.learn_loop.hints()
                if _lh:
                    system += f"\n\n{_lh}"
            except Exception:
                pass

        # Inject knowledge base context (RAG pre-check — natural language auto-matches documents)
        # 关键词路由没命中时才走到这里; 用语义检索兜底, 命中高相似度片段则注入上下文让模型基于文档回答
        if not _is_ollama:
            try:
                from core.kb_store import MemoryVectorStore
                _kbs = MemoryVectorStore()
                if _kbs.count() > 0:
                    _hits = _kbs.search(message, top_k=3)
                    if _hits and _hits[0]["similarity"] >= 0.48:
                        _kb_ctx = _kbs.format_context(_hits)
                        system += (
                            f"\n\n# Knowledge Base Context\n"
                            f"以下是用户知识库中检索到的相关内容(最高相似度{_hits[0]['similarity']:.2f})。\n"
                            f"如果用户问题与这些内容相关，请基于它们回答，并注明参考来源；如果不相关，忽略这段内容正常回答。\n"
                            f"{_kb_ctx}"
                        )
            except Exception:
                pass
        
        # Inject matched Skills (SKILL.md 机制 — 只给说明书, 模型自己决定用哪些工具)
        # 方案 A: 不强制走技能 DAG, 不加路由 hack。本地模型 ctx 紧 → compact(只给名字+说明)
        _skill_ctx = self._skill_context(message, compact=_is_ollama)
        if _skill_ctx:
            system += (
                f"\n\n{_skill_ctx}\n"
                f"上面是匹配到的技能说明。若与用户请求相关, 按它的步骤执行 (它列出的工具都已提供)。"
                f"不相关则忽略, 正常回答。"
            )

        # Inject constraints
        if self.sre.user_constraints:
            constraint_text = "\n".join(
                f"  - {c['source_text']}" for c in self.sre.user_constraints[:5]
            )
            system += f"\n\n# Constraints (MUST obey)\n{constraint_text}"
        
        # ── 任务收尾自检（机制1: 纯祈使任务，不含疑问/讨论） ──
        _task_kw = ['创建','构建','修复','部署','测试','安装','修改','添加','删除','生成','编译',
                    'create','build','fix','deploy','test','install','modify','add','delete',
                    'generate','compile','运行','执行','配置','setup','configure','run','execute',
                    '重构','refactor','优化','optimize','新建','移除','remove','更新','update']
        _is_question = any(q in message for q in ['?','？','什么','怎么','如何','为什么','算','吗','呢'])
        _is_imperative = any(kw in message for kw in _task_kw)
        if _is_imperative and not _is_question:
            system += (
                "\n\n# ⚠ 回答结束前自检：①改了什么文件 ②验证数字（行数/MD5/测试数） ③失败项。缺一不可结束。"
            )
        
        messages = [{"role": "system", "content": system}, {"role": "user", "content": message}]

        # 🔴 会话历史注入 (2026-08-19): 无历史时追问("各自数量是多少")模型一无所知 → 瞎说。
        # 注入最近历史, 过滤含【文件的预读大消息(重复塞内容浪费token)
        # 2026-08-20 v3: assistant 长内容(诗/代码)不截断 — "把这首诗写进X" 需要完整指代;
        # user 消息截断200字符, 总条数限制防 ollama 8K ctx 爆
        try:
            # ★★ 2026-09-24 修 (用户实测: "我问他刚才是不是写了一篇短文, 他无法正面回答"):
            #   原来固定只注入最近 **4 条 (2 轮)**, 而"刚才/上面/那篇"这类**追问**
            #   要指代更早的轮次 —— 实测那篇短文在 11 条消息之前 ⇒ 完全看不见
            #   ⇒ 它只能诚实答"我无法确认" (用户因此发火)。
            #   修法: 认出追问词 → 放宽窗口到 16 条, 但**收紧单条截断 + 按字符预算收口**,
            #   总 token 仍有界 (防 ollama ctx 爆); 非追问句维持原行为 (4 条)。
            _ANAPH = ("刚才", "刚刚", "上面", "前面", "之前", "那篇", "这篇", "那个", "这个",
                      "上一次", "上次", "前一条", "刚那", "上面那", "前面那", "之前那",
                      "我说过", "让你做", "聊到")
            _ref = any(w in (message or "") for w in _ANAPH)
            _want = 16 if _ref else 4
            _a_lim, _u_lim = (400, 150) if _ref else (1500, 200)
            _budget = 1500 if _ref else 4000
            _hist = self.sessions.recent(_want) if hasattr(self, "sessions") else []
            if _hist:
                _hist_msgs = []
                for _h in reversed(_hist):      # 从**最新**往回挑, 预算用完就停
                    _c = str(_h.get("content", ""))
                    if "【文件" in _c or not _c.strip():
                        continue  # 跳过预读内容/空消息
                    _c = _c[:_a_lim] if _h.get("role") == "assistant" else _c[:_u_lim]
                    if len(_c) > _budget:
                        break
                    _budget -= len(_c)
                    _hist_msgs.append({"role": _h.get("role", "user"), "content": _c})
                _hist_msgs.reverse()            # 恢复时间顺序
                if _hist_msgs:
                    # 历史插在 system 之后、当前消息之前
                    messages = [{"role": "system", "content": system}] + _hist_msgs + [
                        {"role": "user", "content": message}]
        except Exception as _e:
            # ★ 2026-09-24 修: 原为 `pass` —— 库故障时**静默无历史**, 用户无从知晓。
            _warn_hist_read_failed("general", _e)

        # ── 视觉模型：检测图片路径并编码 ──
        # ★ 2026-09-19: 原来只判 model_name=="ollama" 就编码 —— 但 ollama 未必有视觉模型,
        #   而且**兜底模型**(deepseek/mimo) 会带着这份 base64 一起去 → 纯文本模型编造图片内容。
        #   现在: ① 模型真能看图才编码; ② 不能看 → 自动走 vision 工具; ③ 工具不可用 → 诚实说明。
        _img_paths_probe = []
        try:
            import re as _vre0
            for _m0 in _vre0.finditer(
                    r'[\"\']?([A-Za-z]:[\\/][^\n\"\' ]*?\.(?:png|jpg|jpeg|gif|webp|bmp))[\"\']?',
                    message, _vre0.IGNORECASE):
                if Path(_m0.group(1)).exists():
                    _img_paths_probe.append(_m0.group(1))
        except Exception:
            pass

        _mid_probe = self._resolved_model_id(model_name)
        if _img_paths_probe and not self._can_see_images(_mid_probe):
            # 当前模型看不见图 → 交给 vision 工具 (真视觉), 绝不把 base64 塞给纯文本模型
            try:
                _vr = await self.gateway.call("vision", image=_img_paths_probe[0], question=message)
                if isinstance(_vr, dict) and _vr.get("success") and str(_vr.get("output") or "").strip():
                    _vt = str(_vr["output"]).strip()
                    return {"response": _vt[:4000], "intent": "vision", "model": _vr.get("model", "vision"),
                            "elapsed": round(time.time() - t0, 2), "trace": pre_trace or []}
                _verr = str((_vr or {}).get("error") or "") if isinstance(_vr, dict) else ""
            except Exception as _ve:
                _verr = f"{type(_ve).__name__}: {_ve}"
            return {"response": (f"你给了图片(`{Path(_img_paths_probe[0]).name}`)，但当前模型看不到图片内容，"
                                 f"视觉工具也没成功({_verr[:120]})。我不会瞎猜图里有什么 —— "
                                 f"请检查视觉工具凭证(环境变量 DASHSCOPE_API_KEY 或 keys.json 的 qwen.key)。"),
                    "intent": "vision_unavailable", "model": "local",
                    "elapsed": round(time.time() - t0, 2), "trace": pre_trace or []}

        if model_name == "ollama" and self._can_see_images(self._resolved_model_id(model_name)):
            import re as _vre, base64 as _v64
            _img_paths = []
            # 匹配 Windows 绝对路径 + 图片扩展名
            _img_re = _vre.compile(
                r'["\']?([A-Za-z]:[\\/][^\n"\' ]*?\.(?:png|jpg|jpeg|gif|webp|bmp))["\']?',
                _vre.IGNORECASE)
            for _m in _img_re.finditer(message):
                _p = _m.group(1)
                if Path(_p).exists():
                    _img_paths.append(_p)
            if _img_paths:
                _parts = [{"type": "text", "text": message}]
                for _ip in _img_paths[:3]:
                    try:
                        _data = Path(_ip).read_bytes()
                        _b64 = _v64.b64encode(_data).decode()
                        _ext = _ip.rsplit(".", 1)[-1].lower()
                        _mime = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg",
                                 "gif":"image/gif","webp":"image/webp","bmp":"image/bmp"}.get(_ext, "image/png")
                        _parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{_mime};base64,{_b64}"}
                        })
                        logger.info("vision: encoded %s (%d bytes)", Path(_ip).name, len(_data))
                    except Exception as _ve:
                        logger.warning("vision: skip %s: %s", _ip, _ve)
                if len(_parts) > 1:
                    messages[1]["content"] = _parts
        
        # Get tool schemas (gemma4-12b 已实测支持工具调用 — 2026-08-18, ollama 也发 schema)
        from core.tool_schema import get_all_schemas
        tools_schema = get_all_schemas(self.gateway.plugin_mgr, mcp_client=getattr(self, "mcp", None))
        # 🔴 ollama 工具数精简 (2026-08-20): gemma4-12b num_ctx=8192 硬限,
        # 全量18工具schema 10086字符≈5000 tokens + system prompt 就顶到上限 → 多轮累积后空回复。
        # 只留核心8工具 (4368字符), 足够覆盖文件/时间/搜索/邮件/知识库常见操作。
        if model_name == "ollama" and tools_schema:
            _ollama_core = {"system_info", "file_ops", "shell_exec", "web_search",
                            "send_email", "check_mail", "kb_search", "openmeteo"}
            _kept = [s for s in tools_schema
                     if s.get("function", {}).get("name") in _ollama_core]
            if _kept:
                logger.info("process <- ollama tools %d -> %d (ctx 8192 硬限)",
                            len(tools_schema), len(_kept))
                tools_schema = _kept
        
        max_rounds = 5 if model_name == "ollama" else 8  # 本地12B长链不稳, 5轮封顶 (2026-08-20)
        _reads_done = []  # enforce read-before-write: track files read this session
        total_tool_results = 0
        # 🔴 预读记录注入 trace: 文档预读(TMM 主动读文件喂模型)不算模型工具调用,
        # 但不注入会导致审计误报"声称文件成功但无 file_ops 调用 — 幻觉"
        trace = list(pre_trace or [])  # 工具调用轨迹(web透传): [{round,tool,args,result,success}]
        
        _fail_tracker = {}  # 熔断计数器: {tool_name: consecutive_failures}
        for round_i in range(max_rounds):
            # ── Auto-compaction check (Grok 85% threshold) ──
            if round_i > 0 and len(messages) > 8:
                from core.compactor import should_compact, compact_messages
                if should_compact(messages, model_name):
                    recent, compaction_prompt = compact_messages(messages)
                    if compaction_prompt:
                        logger.info("process compaction: %d msgs -> summarizing %d to keep",
                                   len(messages), len(recent))
                        try:
                            compact_result = await self.model_client.generate(
                                "ollama",  # use local model for compaction (free)
                                [{"role": "user", "content": compaction_prompt}],
                                temperature=0.3, max_tokens=300
                            )
                            summary = compact_result.get("text", "") or "Conversation continued."
                            from core.compactor import apply_compaction
                            messages = apply_compaction(messages, summary)
                            logger.info("process compaction: done — %d msgs after", len(messages))
                        except Exception as e:
                            logger.warning("process compaction failed: %s", e)
            logger.info("process -> model=%s round=%d tools=%d sys_chars=%d",
                       model_name, round_i + 1, len(tools_schema) if tools_schema else 0, len(system))

            # ── 任务分级调度 (2026-08-19 实测画像) ──
            # 简单对话: 短消息+无任务词 → think=False + 1024 (快3.4倍, max_tokens太小会空回复所以下限1024)
            # 文件预读分析: user含【文件 → think=False + 2048 (内容已喂, 无需思考)
            # 复杂任务: 默认 think=True + 4096
            _cur_user = messages[-1].get("content", "") if messages else ""
            _cur_user_s = _cur_user if isinstance(_cur_user, str) else ""
            _is_simple = (
                round_i == 0
                and len(_cur_user_s) <= 120
                and not any(k in _cur_user_s for k in _task_kw)
                and "【文件" not in _cur_user_s
                and "```" not in _cur_user_s
            )
            _is_preread = round_i == 0 and "【文件" in _cur_user_s
            _think_override = False if (_is_simple or _is_preread) else None
            _max_tokens_override = 2048 if _is_simple else (2048 if _is_preread else 4096)

            result = await self.model_client.generate(
                model_name, messages, temperature=0.7, max_tokens=_max_tokens_override,
                tools=tools_schema if round_i == 0 else tools_schema,
                think=_think_override
            )
            # Track token usage
            from core.token_tracker import get_tracker
            usage = result.get("usage")
            if usage and usage.get("total_tokens"):
                get_tracker().record(model_name, usage)
            
            if result.get("error"):
                logger.error("process[#%d] model error: %s", self.stats["total"], result["error"])
                chain = {"deepseek":"mimo","mimo":"ollama","ollama":"deepseek","qwen":"deepseek"}
                fb = chain.get(model_name)
                if fb:
                    logger.info("fallback: %s -> %s", model_name, fb)
                    try:
                        # ★ 2026-09-19 双保险: 兜底模型(deepseek/mimo 等)看不见图,
                        #   把 messages 里的 base64 图片部分剥掉 —— 否则纯文本模型
                        #   会对着看不见的图**编造内容**(实测 2.1MB base64 → 假描述)。
                        _fb_msgs = messages
                        # 兜底模型也同样先解析成真实模型名再判定
                        if model_name == "ollama" or fb == "ollama" or not self._can_see_images(self._resolved_model_id(fb)):
                            _fb_msgs = []
                            for _m in messages:
                                if isinstance(_m, dict) and isinstance(_m.get("content"), list):
                                    _txt = " ".join(p.get("text", "") for p in _m["content"]
                                                    if isinstance(p, dict) and p.get("type") == "text")
                                    _fb_msgs.append({"role": _m.get("role", "user"),
                                                     "content": (_txt + " [图片已省略: 当前模型无法看图]").strip()})
                                else:
                                    _fb_msgs.append(_m)
                        r2 = await self.model_client.generate(fb, _fb_msgs, temperature=0.7,
                            max_tokens=8192 if fb=="ollama" else 4096,
                            tools=(tools_schema if round_i==0 else None))
                        if not r2.get("error"):
                            if r2.get("tool_calls"):
                                result = r2; model_name = fb; tool_calls = r2.get("tool_calls"); continue
                            return {"response":(r2.get("text")or"").strip()[:5000],"intent":"general","model":f"{fb}(fallback)","elapsed":round(time.time()-t0,2),"trace":trace}
                    except Exception: pass
                return {"response":f"网络异常，已尝试{model_name}。","intent":"error","model":"local","elapsed":round(time.time()-t0,2),"trace":trace}
            
            tool_calls = result.get("tool_calls")

            # ★★ 2026-09-24 修 (21 轮真实泄漏): 本地模型 (mimo) 把工具调用**写在正文里**,
            #   而这里只认 OpenAI 结构化 tool_calls ⇒ 那坨标签被当"文本回复"直接交给用户,
            #   工具**从未执行** (实测 21 轮, 全 general 路由; 17 轮回复以标签开头、零正文)。
            #   兜底: 结构化为空时, 试着从正文解析工具标签 —— 解析到就按工具调用继续走。
            #   ★ 解析不到 → 保持原行为, 一个字不改 (纯加性)。
            if not tool_calls:
                _parsed = None
                try:
                    _parsed = parse_text_tool_calls(result.get("text") or "")
                except Exception as _pe:
                    logger.debug("process <- parse_text_tool_calls 异常(忽略): %s", _pe)
                if _parsed:
                    # 转成执行分支要的形状 {"function": {"name":…, "arguments":…}}
                    # (arguments 用 dict; 5436 处对 str/dict 都兼容)
                    tool_calls = [{"function": {"name": c.get("name", ""),
                                                "arguments": c.get("arguments") or {}}}
                                  for c in _parsed if c.get("name")]
                    # 与既有"本地模型一轮多个工具易乱序"同口径, 限一步一走
                    tool_calls = tool_calls[:_LOCAL_MODEL_MAX_TOOLS_PER_ROUND]
                    logger.info("process <- 从正文解析出 %d 个工具调用 (文本格式兜底): %s",
                                len(tool_calls),
                                [t2["function"]["name"] for t2 in tool_calls])

            if not tool_calls:
                # Model returned text — done
                # 🔴 think 兜底防英文推理泄漏: ollama 推理过程(英文)不能当回复,
                # 仅当 text 空且 think 含中文字符时才用 think 兜底
                response_text = result.get("text", "") or ""
                if not response_text.strip():
                    _t = result.get("think", "") or ""
                    _cn = sum(1 for ch in _t if '\u4e00' <= ch <= '\u9fff')
                    if _cn >= 2:  # 中文推理/思考 → 可当回复
                        response_text = _t
                # ── 机制3: 空内容 → 强制重试 (2026-08-20 硬防线) ──
                # 模型返回空/纯英文推理 → 不直接结束, 提示重新输出合法工具标签
                if not response_text.strip() and round_i < max_rounds - 1:
                    logger.warning("process <- EMPTY response round=%d, forcing retry", round_i)
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content":
                        "你没有输出思考与工具调用。请重新分析任务，输出合法工具标签。"
                        "若任务需要读取文件/获取时间/统计数据，必须先调用工具。"})
                    continue
                if not response_text.strip():
                    response_text = "（模型无输出）请重试或换一种问法。"
                elapsed = round(time.time() - t0, 2)
                logger.info("process <- model=%s elapsed=%.2fs len=%d",
                           model_name, elapsed, len(response_text))
                # ── 机制4+2: 系统类问题/幻觉声称 → 强制工具调用回喂 (2026-08-20 v2 硬防线) ──
                # v2: 不再要求 total_tool_results==0 — 模型调了1个工具(如 system_info 时间)
                # 就声称"写了文件+读了文件"(无 file_ops 轨迹) 也是幻觉, 必须拦。
                if round_i <= 1:
                    _needs_tool = _sys_question(message) and not _trace_has_sys_tool(trace, message)
                    _halluc_findings = []
                    # 声称检测: 声称完成操作但 trace 无对应工具 → 幻觉 (audit 规则1/3 已做支撑判定)
                    # 不用 _claims_action 粗判(只看词不看 trace) — 会误伤"有工具链但声称词多"的合法回答
                    try:
                        from core.audit_agent import audit_response
                        _hf = audit_response(message, response_text, trace)
                        _halluc_findings = [f for f in _hf
                                            if f.get("type") in ("unsupported_claim", "unsupported_path",
                                                                 "mcp_hallucination")]
                    except Exception as _ae:
                        # ★ 2026-09-21: 原来是 pass —— 审计层没跑成却无人知道, 等于自查形同虚设
                        from core.result import degrade as _deg
                        _deg("审计层未能运行", f"{type(_ae).__name__}: {str(_ae)[:60]}", "pipeline:audit")
                    if _needs_tool or _halluc_findings:
                        _reasons = []
                        if _needs_tool:
                            _reasons.append("该问题(时间/文件统计/磁盘/进程)必须先调用工具获取真实数据, 禁止直接编造数字")
                        for _f in _halluc_findings:
                            _reasons.append(str(_f.get("detail", ""))[:120])
                        _warn_text = "\n".join(f"- {r}" for r in _reasons[:3])
                        logger.warning("process <- hallucination intercept round=%d: %s",
                                       round_i, _reasons[0][:80] if _reasons else "")
                        messages.append({"role": "assistant", "content": response_text[:3000]})
                        messages.append({"role": "user", "content":
                            f"⚠ 框架拦截: 你的回答声称完成了操作, 但工具轨迹中缺少对应的工具调用。\n"
                            f"{_warn_text}\n"
                            "请重新分析任务：\n"
                            "1. 确定需要的工具并逐一调用(时间→system_info action='time'; 写文件→file_ops action='write' path=纯路径 content=内容; 读文件→file_ops action='read')\n"
                            "2. 每个操作必须有真实的工具调用轨迹支撑\n"
                            "3. 拿到工具返回结果后再总结回答, 禁止声称未执行的操作"})
                        continue
                # ── 机制2+3: 输出验证 + 证据链检测(闭环) ──
                evidence = check_evidence(response_text)
                if not evidence["passed"]:
                    claims = ",".join(evidence["claim_patterns"])
                    logger.warning("validator: 嘴炮 — claims=[%s] no evidence in response", claims)
                    try:
                        messages.append({"role": "assistant", "content": response_text[:3000]})
                        _ev_prompt = (
                            "⚠ 你声称" + claims + "但没有提供验证证据。重新回答，必须包含：\n"
                            "① 改动了哪些文件（完整路径）\n"
                            "② 如何验证（具体数字：MD5/行数/测试通过数/实际输出）\n"
                            "③ 如有失败项如实列出\n"
                            "用中文回答，不要markdown。"
                        )
                        messages.append({"role": "user", "content": _ev_prompt})
                        r2 = await self.model_client.generate(
                            model_name, messages, temperature=0.3, max_tokens=2000
                        )
                        response_text = (r2.get("text", "") or r2.get("think", "") or response_text)
                        evidence = check_evidence(response_text)
                    except Exception as _ee:
                        # ★ 2026-09-21: 原来是 pass —— 重答失败被吞, 用户看到的是"未经验证"的回答
                        from core.result import degrade as _deg
                        _deg("证据链重答失败", f"{type(_ee).__name__}: {str(_ee)[:60]}", "pipeline:evidence")
                # ── 审计 Agent: 推理链谬误/幻觉检查(独立于证据链自检, 不阻断) ──
                _audit_findings = []
                try:
                    from core.audit_agent import audit_response, audit_with_llm, format_report
                    _audit_findings = audit_response(message, response_text, trace)
                    # 规则审计放行且有 >=2 步工具轨迹 → LLM 深度审计(免费/低耗)
                    if not _audit_findings and trace and len(trace) >= 2:
                        _v = await audit_with_llm(self.model_client, model_name,
                                                  message, response_text, trace)
                        if _v.get("verdict") in ("suspicious", "hallucination"):
                            _audit_findings = [{
                                "type": "llm_" + str(_v.get("verdict")),
                                "severity": "medium",
                                "detail": "LLM审计: " + str(_v.get("reason", ""))[:100],
                            }]
                    _audit_report = format_report(_audit_findings)
                    if _audit_report:
                        response_text = response_text[:4500] + _audit_report
                        # 🔴 2026-08-24: warning 降级 info — 审计发现会追加进回复给用户,
                        # 控制台 WARNING 是纯噪音(每次有发现都刷)
                        logger.info("audit agent: %d findings (tools=%d)",
                                    len(_audit_findings), len(trace or []))
                except Exception:
                    pass
                # ── 感知学习: analyze 本轮回合 → 提取实体/关系/纠正/模式 ──
                try:
                    # ★ probe 守卫 (2026-09-20 修): 原来这里**没有** probe 保护, 而 probe=True
                    #   本该表示"验证流量不写用户数据"。实测后果: 验证器跑真 pipeline 时把
                    #   探针句子写进了用户真实 data/perception.json (纠正/实体/关系)。
                    if (not probe) and hasattr(self, 'perception') and self.perception:
                        self.perception.analyze(message, response_text, memory=self.memory)
                except Exception:
                    pass
                return {"response": response_text[:5000], "intent": "general",
                        "model": model_name, "elapsed": elapsed,
                        "code_rounds": round_i, "tool_results": total_tool_results,
                        "evidence_passed": evidence["passed"], "trace": trace}
            
            # Model wants to use tools
            logger.info("process <- model=%s tool_calls=%d", model_name, len(tool_calls))
            
            # ── 本地模型并行工具截断: gemma4 一轮吐多个 tool_calls 易乱序/幻觉 (2026-08-20) ──
            if model_name == "ollama" and len(tool_calls) > _LOCAL_MODEL_MAX_TOOLS_PER_ROUND:
                logger.warning("process <- ollama tool_calls %d -> cap %d (one step at a time)",
                               len(tool_calls), _LOCAL_MODEL_MAX_TOOLS_PER_ROUND)
                tool_calls = tool_calls[:_LOCAL_MODEL_MAX_TOOLS_PER_ROUND]
            
            # Add assistant message with tool_calls
            assistant_msg = {"role": "assistant", "content": result.get("text") or None}
            if model_name != "ollama":
                # 在线模型标准格式; gemma4 对 tool_calls 历史理解差 → 不附加 (2026-08-20 实测)
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            
            # Execute each tool call
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_result = None  # guard: may not be set if blocked or exception
                try:
                    _raw_args = fn.get("arguments", "{}")
                    # Ollama /api/chat 的 arguments 是 dict; OpenAI 兼容接口是 JSON 字符串 (2026-08-20)
                    args = _json.loads(_raw_args) if isinstance(_raw_args, str) else (_raw_args or {})
                except _json.JSONDecodeError:
                    args = {}
                _tc = {"round": round_i + 1, "tool": tool_name, "args": str(args)[:800]}
                
                # ── Read-before-write 已移除 (2026-08-20 v3) ──
                # 原规则: write 前必须 read, 防"凭空编造内容覆盖文件"。
                # 问题: 纯写入任务("把你好世界写入x.txt")模型第一轮直接 write,
                # 无前置工具 → 被拦一次 → 重试才成功; 旧代码无豁免则永远拦截 → 用户看到"没执行"。
                # 现防线已足够: 幻觉拦截v2(声称写但 trace 无 file_ops→拦) + file_ops路径校验(指令词污染→拒)
                # + 空content拒绝 + verify_fix。write 直接执行。
                _is_write = (tool_name == 'file_ops' and args.get('action') == 'write')
                _is_read = (tool_name == 'file_ops' and args.get('action') == 'read')
                # 执行 via gateway
                try:
                    # ── Permission gate: shell command safety filter ──
                    if tool_name == "shell_exec":
                        cmd = args.get("command", "") or args.get("cmd", "")
                        safety = _check_shell_safety(cmd)
                        if safety == "block":
                            tool_result = {"success": False, "error": f"危险命令已拦截: {cmd[:60]}", "output": "禁止执行。请用安全替代方案（如 file_ops 操作文件、system_info 查系统信息）。"}
                        elif safety == "warn":
                            logger.warning("process <- tool shell_exec GRAY: %s", cmd[:80])
                            tool_result = await self.gateway.call(tool_name, **args)
                        else:
                            tool_result = await self.gateway.call(tool_name, **args)
                    else:
                        tool_result = await self.gateway.call(tool_name, **args)
                    # ── 智能截断：读文件给大空间，其他工具保持紧凑 ──
                    if isinstance(tool_result, dict) and tool_result.get('success'):
                        raw = str(tool_result)
                        meta = tool_result.get('meta', '')
                        output = tool_result.get('output', '')
                        complete = tool_result.get('complete', None)
                        if tool_name == 'file_ops' and args.get('action') == 'read':
                            if len(output) > 12000:
                                result_text = output[:12000]
                                if complete is True:
                                    hint = "\n\n[READ COMPLETE: " + str(meta) + " | Full content " + str(len(output)) + " chars, showing first 12000]"
                                else:
                                    total = tool_result.get('total_lines', '?')
                                    hint = "\n\n[PAGE 1: " + str(meta) + " | " + str(len(output)) + " chars, showing first 12000. Use offset=LINE to continue]"
                                result_text = result_text + hint
                            elif complete is True:
                                result_text = output + "\n\n[" + str(meta) + "]"
                            else:
                                result_text = output
                        elif len(raw) > 8000:
                            result_text = raw[:8000] + "\n\n[Content truncated at 8000 chars]"
                        else:
                            result_text = tool_result.get("output", raw) if isinstance(tool_result, dict) else raw
                    else:
                        result_text = str(tool_result)[:3000]
                    total_tool_results += 1
                    logger.info("process <- tool %s -> %s", tool_name, str(tool_result)[:100])
                    # Track file reads
                    if _is_read and isinstance(tool_result, dict) and tool_result.get('success'):
                        _reads_done.append(str(args.get('path', 'unknown')))
                except Exception as e:
                    result_text = f"Error: {e}"
                    logger.error("process <- tool %s error: %s", tool_name, e)
                
                # Add tool result to messages
                if model_name == "ollama":
                    # gemma4 不认 role=tool → 用 user 角色注入工具结果 (2026-08-20 实测:
                    # role=tool 历史会导致空回复/重复调工具; user 注入正常总结)
                    messages.append({
                        "role": "user",
                        "content": f"【{tool_name} 工具返回】\n{result_text}"
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_text
                    })

                # 收集工具调用轨迹(供web透传)
                _tc["result"] = str(result_text)[:2000]
                _tc["success"] = bool(isinstance(tool_result, dict) and tool_result.get("success")) if tool_result is not None else False
                trace.append(_tc)
                
                # ── Verify→Fix: 分类→恢复→上报 ──
                if tool_result is not None and isinstance(tool_result, dict) and not tool_result.get("success", False):
                    from core.verify_fix import classify_error, get_fix_hint, format_escalation
                    import asyncio as _asyncio
                    _err_type = classify_error(tool_result)
                    _fkey = tool_name + "_" + _err_type
                    _fcount = _fail_tracker.get(_fkey, 0) + 1
                    _fail_tracker[_fkey] = _fcount

                    if _err_type in ("timeout", "network", "rate") and _fcount <= 2:
                        # Retryable: backoff + hint
                        _delay = {"timeout": 2, "network": 3, "rate": 5}.get(_err_type, 1)
                        _hint = get_fix_hint(tool_name, _err_type)
                        messages[-1]["content"] += f"\n⚠ {tool_name} {_hint} (第{_fcount}次重试)\n"
                        await _asyncio.sleep(_delay)
                    elif _err_type in ("perm", "not_found"):
                        # Not retryable: suggest alternative immediately
                        _hint = get_fix_hint(tool_name, _err_type)
                        messages[-1]["content"] += f"\n⚠ {tool_name} {_hint}\n"
                    elif _err_type == "auth":
                        # Auth failure: escalate immediately
                        _esc = format_escalation(tool_name, _err_type, _fcount,
                                                 str(tool_result.get("error", "")))
                        messages[-1]["content"] += f"\n{_esc}\n"
                    else:
                        # Unknown: one retry then escalate
                        if _fcount <= 1:
                            _hint = get_fix_hint(tool_name, _err_type)
                            messages[-1]["content"] += f"\n⚠ {tool_name} {_hint} (第{_fcount}次重试)\n"
                        else:
                            _esc = format_escalation(tool_name, _err_type, _fcount,
                                                     str(tool_result.get("error", "")))
                            messages[-1]["content"] += f"\n{_esc}\n"
                else:
                    # Success: reset fail count for this tool
                    for k in list(_fail_tracker.keys()):
                        if k.startswith(tool_name + "_"):
                            del _fail_tracker[k]
        
        # Max rounds exceeded — force final response
        messages.append({"role": "user", "content": "Please provide your final answer now based on the tool results above."})
        result = await self.model_client.generate(model_name, messages, temperature=0.7, max_tokens=4096)
        elapsed = round(time.time() - t0, 2)

        # ── 感知学习: analyze 本轮回合 (工具循环耗尽出口) ──
        _final_text = (result.get("text", "") or "无法完成。")
        try:
            if (not probe) and hasattr(self, 'perception') and self.perception:
                self.perception.analyze(message, _final_text, memory=self.memory)
        except Exception:
            pass

        return {"response": _final_text[:5000],
                "intent": "general", "model": model_name, "elapsed": elapsed,
                "code_rounds": max_rounds, "tool_results": total_tool_results,
                "trace": trace}

    async def _analysis_preread(self, message, doc_path, ext_model, t0, probe=False):
        """文档预读 + xlsx 列值智能提取 → 内容喂模型 (本地模型不主动调工具, 需 TMM 加持)。
        返回: 处理结果 dict (成功/失败), 无匹配返回 None。"""
        try:
            _read = await self.gateway.call("file_ops", action="read", path=doc_path)
            if isinstance(_read, dict) and _read.get("success"):
                _full_content = str(_read.get("output", ""))
                _content = _full_content[:2000]
                # 🔴 xlsx 列值智能提取 (2026-08-19): 用户问"某列"时直接读列值喂模型,
                # 否则模型拿到整表不知道提取什么 → 原样吐回表格
                _col_extra = ""
                if doc_path.lower().endswith((".xlsx", ".xlsm")):
                    import re as _cre
                    _quoted = _cre.findall(r'[“"]([^”"]{1,12})[”"]', message)
                    _col_candidates = []
                    for _q in _quoted:
                        _col_candidates.append(_q)
                    # 常见问法: "XX列" / "XX这一列" / "XX里面"
                    for _pat in [r'([\u4e00-\u9fffA-Za-z0-9]{2,12})列', r'([\u4e00-\u9fffA-Za-z0-9]{2,12})这一列']:
                        _col_candidates += _cre.findall(_pat, message)
                    # 🔴 无列名时复用上次预读的列 (追问"数量多少"场景)
                    if not _col_candidates and self._last_preread and self._last_preread.get("cols"):
                        _col_candidates = list(self._last_preread["cols"])
                    try:
                        import openpyxl as _xl
                        _wb = _xl.load_workbook(doc_path, read_only=True, data_only=True)
                        _ws = _wb.active
                        _hdr = None
                        for _row in _ws.iter_rows(values_only=True):
                            if any(c is not None for c in _row):
                                _hdr = [str(c).strip() if c is not None else "" for c in _row]
                                break
                        if _hdr:
                            # 🔴 表头词匹配: 消息里出现的表头列名也加入候选
                            # (如 "第一行接单部门都有哪些" — "接单部门"裸在消息里)
                            # 必须在 openpyxl 读取后、且不受 _col_candidates 是否为空影响
                            for _h in _hdr:
                                if _h and len(_h) >= 2 and _h in message and _h not in _col_candidates:
                                    _col_candidates.append(_h)
                            _col_parts = []
                            for _cn in _col_candidates:
                                if _cn in _hdr:
                                    _idx = _hdr.index(_cn)
                                    # 🔴 频次统计 (2026-08-19): 之前去重+30个提前break,
                                    # 模型拿不到"出现次数"信息 → 问数量/次数时瞎说/猜"各1次"
                                    from collections import Counter as _Counter
                                    _cnt = _Counter()
                                    _scan_rows = 0
                                    for _r2 in _ws.iter_rows(min_row=2, values_only=True):
                                        if _r2 and _idx < len(_r2) and _r2[_idx] is not None:
                                            _v = str(_r2[_idx]).strip()
                                            if _v:
                                                _cnt[_v] += 1
                                                _scan_rows += 1
                                            if _scan_rows >= 2000:  # 最多扫2000行防卡
                                                break
                                    _uniq = list(_cnt.keys())
                                    _cnt_str = "、".join(f"{k}×{v}" for k, v in _cnt.most_common(30))
                                    _col_parts.append(
                                        f"列「{_cn}」的唯一值({len(_uniq)}个, 共统计{sum(_cnt.values())}行): {_cnt_str}"
                                    )
                            if _col_parts:
                                _col_extra = "\n\n【列值提取】\n" + "\n".join(_col_parts)
                        _wb.close()
                    except Exception:
                        _col_extra = ""
                _analyze_msg = (
                    f"{message}\n\n"
                    f"【文件 {doc_path} 的内容如下（前 {len(_content)} 字符，"
                    f"共 {len(_full_content)} 字符，截断因本地模型上下文限制）】\n"
                    f"{_content}{_col_extra}\n\n"
                    f"请基于以上文件内容回答用户的问题，不要凭空编造。"
                )
                # 🔴 预读记录注入 trace → 审计不误报"无 file_ops 调用=幻觉"
                _pre_trace = [{
                    "round": 0, "tool": "file_ops",
                    "args": {"action": "read", "path": doc_path},
                    "result": f"pre-read {len(_full_content)} chars (truncated to {len(_content)})",
                    "success": True,
                }]
                # 🔴 会话记忆: 记录本次预读 (无路径追问"数量多少"复用)
                self._last_preread = {"path": doc_path, "cols": list(_col_candidates)}
                return await self._process_external(_analyze_msg, ext_model or "deepseek", t0,
                                                    pre_trace=_pre_trace, probe=probe)
            return {"response": f"读文件失败: {str(_read)[:200]}", "intent": "error",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}
        except Exception as _de:
            logger.warning("analysis pre-read failed: %s", _de)
            return None

def create_app(config: dict):
    """Create FastAPI app (if available)."""
    if not HAS_FASTAPI:
        return None
    app = FastAPI(title="Tiger.M.M", version="4.2")
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])
    
    class ChatRequest(BaseModel):
        message: str
        model: str = "auto"
        ext_model: str = None
    
    @app.post("/chat")
    async def chat(req: ChatRequest):
        try:
            result = await pipeline.process(req.message, req.ext_model or req.model)
            return {"response": result.get("response",""), "intent": result.get("intent","general"), "model": result.get("model","unknown"), "elapsed": result.get("elapsed",0)}
        except Exception as e:
            logger.error(f"Chat error: {e}")
            raise HTTPException(500, f"Internal error: {e}")
    
    @app.get("/models")
    async def models():
        return {"models": pipeline.list_models()}
    
    return app


# ═══ CLI ═══
async def run_cli(pipeline_obj, model_client_obj):
    """Tiger.M.M CLI — built into pipeline."""
    if sys.platform == "win32":
        import ctypes
        try: ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception: pass
    
    print(FRAMEWORK_TEXT)
    logger.info("CLI started — pid=%d", os.getpid())

    # Tool list — two columns, CN/EN
    _rule("TOOLS")
    print(f"  {GOLD}│{RST} {BOLD}TOOLS{RST}        {DIM}直接说话，虎哥干活{RST}                      {GOLD}│{RST}")
    pass  # replaced by _rule() above
    print()
    # Build tool list from plugin registry
    tool_display = []
    for name in sorted(pipeline_obj.plugins._plugins.keys()):
        tool_display.append((name, name))
    if not tool_display:
        tool_display = [
            ("send_email","发邮件"), ("system_info","系统状态"),
            ("web_search","查信息"), ("windows_desktop","截图"),
            ("file_ops","文件操作"), ("shell_exec","运行命令"),
        ]
    per_row = 3
    for i in range(0, len(tool_display), per_row):
        row_items = tool_display[i:i+per_row]
        line_parts = []
        for cn, en in row_items:
            cn_short = cn[:8] if len(cn) > 8 else cn
            line_parts.append(f"{GOLD}{cn_short:<8s}{RST} {DIM}{en:<16s}{RST}")
        print(f"  {''.join(line_parts)}")

    print()
    current_model = "auto"
    # ★ 2026-09-20: 显式初始化 (同 core/cli.py —— 原来靠 `in dir()` 兜, 静态检查看不到)
    _pending_inbox = None

    while True:
        model_tag = f" {DIM}@{current_model}{RST}" if current_model != "auto" else ""
        try:
            _mi = pipeline_obj.modes.info()
            _mode_tag = f" {GOLD}[{_mi['cn']}]{RST}"
        except Exception:
            _mode_tag = ""
        prompt = f"{GOLD}Tiger.M.M{_mode_tag}{model_tag} >>>{RST} "
        try: raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            logger.info("CLI interrupted — sessions: %d", pipeline_obj.stats.get("total", 0))
            print(); break
        
        # Check for y/n Inbox confirmation
        if raw.lower() in ("y", "yes") and '_pending_inbox' in dir() and _pending_inbox:
            from core.inbox import get_inbox as _gi2, RESOLVE_ALLOW
            inbox = _gi2()
            inbox.resolve(_pending_inbox.id, RESOLVE_ALLOW)
            import subprocess as _sp
            print(f"{GREEN}[Inbox] Executing...{RST}")
            _all_out = ""
            for ci, cmd in enumerate(_pending_inbox.cmds, 1):
                print(f"  {DIM}[{ci}/{len(_pending_inbox.cmds)}] {cmd[:80]}{RST}")
                try:
                    r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=60, encoding='gbk', errors='replace')
                    out = (r.stdout.strip() or r.stderr.strip())
                    _all_out += out + "\n"
                    if r.returncode == 0:
                        print(f"  {GREEN}OK{RST}")
                        for ol in out.split("\n")[:20]:
                            print(f"    {ol[:120]}")
                    else:
                        _all_out += out[:500] + "\n"
                        print(f"  {RED}FAIL{RST} {out[:500]}")
                except Exception as e:
                    _all_out += str(e) + "\n"
                    print(f"  {RED}\u2717{RST} {e}")
            # Feed execution results back to model for the next turn
            _results_text = f"\n[COMMAND RESULTS]\n{_all_out}\n[/COMMAND RESULTS]"
            _pending_inbox = None
            # Don't continue — let this go to pipeline.process() as feedback
            # Fall through to normal processing below
        elif raw.lower() in ("n", "no") and '_pending_inbox' in dir() and _pending_inbox:
            from core.inbox import get_inbox as _gi3, RESOLVE_DENY
            inbox = _gi3()
            inbox.resolve(_pending_inbox.id, RESOLVE_DENY)
            print(f"{DIM}[Inbox] Skipped{RST}")
            user_input = f"(上一轮执行结果)\n{_all_out[:2000]}\n继续分析并按要求执行。"
            _pending_inbox = None
        
        if not raw: continue
        user_input = raw
        if user_input.startswith('/task '):
            sid = user_input[6:].strip()
            sm = getattr(pipeline_obj, 'sm', None)
            if sm and sid:
                output = sm.read_output(sid)
                if output:
                    print(f"\n{GOLD}═══ {sid} ═══{RST}")
                    print(output[:3000])
                else:
                    print(f"{DIM}任务 {sid} 无输出或不存在{RST}")
            else:
                print(f"{DIM}SessionManager 未初始化{RST}")
            continue

        if user_input in ('/quit', '/exit', 'quit', 'exit'):
            logger.info("CLI exit — sessions: %d", pipeline_obj.stats.get("total", 0))
            break
        
        if user_input in ('/help', '/h', 'help', '帮助'):
            _rule("虎哥说明书")
            print(f"""
直接说话：
  {GOLD}发桌面文件给涛哥{RST}       给老王发邮件
  {GOLD}桌面有什么文件{RST}         老王的邮箱
  {GOLD}系统状态{RST}               截图
  {GOLD}记住：张三 邮箱 z@t.com{RST}
  {GOLD}先截图再发给老王{RST}       搜一下xxx
  {GOLD}列出 core 目录的文件{RST}

命令：
  {GOLD}/mode{RST}     查看运行模式 (问答/规划/实干)
  {GOLD}/ask{RST}      切到问答模式 — 只回答，不动手
  {GOLD}/plan{RST}     切到规划模式 — 先出方案，确认后执行
  {GOLD}/craft{RST}    切到实干模式 — 直接动手 (默认)
  {GOLD}/tools{RST}     列出所有插件
  {GOLD}/regex{RST}     正则快捷通道开关
  {GOLD}/stats{RST}     引擎状态统计
  {GOLD}/tasks{RST}     列出所有任务会话
  {GOLD}/spawn{RST}     后台执行任务
  {GOLD}/inbox{RST}     查看审批队列
  {GOLD}/allow{RST}     批准审批项
  {GOLD}/deny{RST}      拒绝审批项
  {GOLD}/hive{RST}     蜂巢多模型协作
  {GOLD}/task{RST}      查看任务输出
  {GOLD}/search{RST}    搜索对话历史
  {GOLD}/sessions{RST}  列出历史会话
  {GOLD}/exit{RST}      退出

切换模型：
  {GOLD}@deepseek{RST}   @mimo   @auto   @ollama
""")
            continue

        if user_input.strip() == '/tasks':
            sm = getattr(pipeline_obj, 'sm', None)
            if sm:
                st = sm.stats()
                print(f"\n{GOLD}═══ 任务会话 ═══{RST}")
                print(f"  总计: {st['total']} | 运行中: {st['running']} | 完成: {st['done']} | 错误: {st['errors']}")
                sessions = sm.list_sessions(limit=10)
                if sessions:
                    status_icon = {'idle': '○', 'running': '●', 'done': '✓', 'error': '✗', 'killed': '⊘'}
                    for s in sessions:
                        icon = status_icon.get(s['status'], '?')
                        print(f"  {icon} {GOLD}{s['id']}{RST} {DIM}{s['model']}{RST} {s['task'][:60]}")
                        if s['elapsed'] > 0:
                            print(f"    {DIM}{s['elapsed']:.1f}s {s['status']}{RST}")
                else:
                    print(f"  {DIM}暂无任务{RST}")
            else:
                print(f"{DIM}SessionManager 未初始化{RST}")
            continue

        if user_input.strip() == '/backup':
            result = pipeline_obj.backup.create("manual")
            if result:
                print(f"{GREEN}[backup] {result['name']}{RST} {result['size_mb']}MB, {result['file_count']}files, {result['elapsed_s']}s")
            else:
                print(f"{RED}[backup] 失败{RST}")
            continue

        if user_input.strip() == '/reload':
            import shutil, importlib
            root = Path(__file__).parent.parent
            for d in root.rglob("__pycache__"):
                shutil.rmtree(d, ignore_errors=True)
            # Force reload all modules
            for mod_name in ["core.pipeline", "core.intent_router", "core.auto_guide", "core.model_router",
                             "core.http_bridge", "core.model_client"]:
                try:
                    mod = sys.modules.get(mod_name)
                    if mod:
                        importlib.reload(mod)
                except Exception: pass
            print(f"{GREEN}[reload] 已重载核心模块 + 清除pycache{RST}")
            # Also hot-reload knowledge base
            try:
                kb_result = pipeline_obj.auto_guide.reload()
                print(f"{GREEN}[reload] {kb_result['output']}{RST}")
            except Exception: pass
            continue

        if user_input.strip() == '/backups':
            items = pipeline_obj.backup.list_backups()
            if items:
                print(f"\n{GOLD}═══ 备份列表（保留最近10个）═══{RST}")
                for b in items:
                    print(f"  {GREEN}{b['name']}{RST} {b['size_mb']}MB {DIM}{b['created']}{RST}")
            else:
                print(f"{DIM}暂无备份{RST}")
            continue

        if user_input.startswith('/spawn '):
            task = user_input[7:].strip()
            if task:
                sm = getattr(pipeline_obj, 'sm', None)
                if sm:
                    sid = sm.spawn(task)
                    print(f"{GREEN}[spawn] {sid}{RST} {task[:60]}")
                else:
                    print(f"{DIM}SessionManager 未初始化{RST}")
            else:
                print(f"{DIM}用法: /spawn 搜索今天的新闻{RST}")
            continue

        if user_input.strip() == '/inbox':
            from core.inbox import get_inbox as _gi
            inbox = _gi()
            pending = inbox.list_pending()
            if pending:
                print(f"\n{GOLD}═══ Inbox — {len(pending)} pending ═══{RST}")
                for item in pending:
                    print(f"  {GOLD}{item.id}{RST} {DIM}{item.created_at[:19]}{RST}")
                    print(f"    {item.title}")
                    for line in item.body.split(chr(10))[:8]:
                        print(f"    {line}")
                    print(f"  {DIM}/allow {item.id}  |  /deny {item.id}{RST}")
                    print()
            else:
                print(f"{DIM}Inbox empty — nothing pending{RST}")
            continue

        if user_input.startswith('/allow '):
            from core.inbox import get_inbox as _gi2, RESOLVE_ALLOW
            item_id = user_input[7:].strip()
            inbox = _gi2()
            if inbox.resolve(item_id, RESOLVE_ALLOW):
                item = inbox.get(item_id)
                print(f"{GREEN}[allow] {item_id}{RST}")
                if item and item.cmds:
                    import subprocess as _sp
                    for ci, cmd in enumerate(item.cmds, 1):
                        print(f"  {DIM}[{ci}/{len(item.cmds)}] {cmd[:80]}{RST}")
                        try:
                            r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=60, encoding='gbk', errors='replace')
                            out = (r.stdout.strip() or r.stderr.strip())
                            if r.returncode == 0:
                                print(f"  {GREEN}OK{RST}")
                                for ol in out.split(chr(10))[:10]:
                                    print(f"    {ol[:120]}")
                            else:
                                print(f"  {RED}FAIL{RST} {out[:300]}")
                        except Exception as e:
                            print(f"  {RED}✗{RST} {e}")
            else:
                print(f"{DIM}Item {item_id} not found or already resolved{RST}")
            continue

        if user_input.startswith('/deny '):
            from core.inbox import get_inbox as _gi3, RESOLVE_DENY
            item_id = user_input[6:].strip()
            inbox = _gi3()
            if inbox.resolve(item_id, RESOLVE_DENY):
                print(f"{DIM}[deny] {item_id}{RST}")
            else:
                print(f"{DIM}Item {item_id} not found or already resolved{RST}")
            continue

            continue

        if user_input.strip() == '/hive':
            hive = getattr(pipeline_obj, 'hive', None)
            if hive:
                agents = hive.list_agents()
                st = hive.stats()
                print(f"\n{GOLD}═══ 蜂巢 Hive — {st['agents']} agents ═══{RST}")
                for a in agents:
                    caps = ', '.join(a['capabilities']) if a['capabilities'] else 'general'
                    print(f"  {GOLD}{a['name']}{RST} {DIM}model={a['model']}{RST} {a.get('description', '')}")
                print(f"  {DIM}channels: {st['channels']}, messages: {st['total_messages']}{RST}")
            else:
                print(f"{DIM}Hive not initialized{RST}")
            continue

        if user_input.startswith('/hive broadcast '):
            task = user_input[17:].strip()
            hive = getattr(pipeline_obj, 'hive', None)
            if hive and task:
                print(f"{GOLD}[hive] broadcasting to {len(hive._agents)} agents...{RST}")
                results = await hive.broadcast(task)
                for r in results:
                    icon = f"{GREEN}✓{RST}" if r.success else f"{RED}✗{RST}"
                    print(f"  {icon} {GOLD}{r.agent}{RST} {DIM}{r.elapsed:.1f}s{RST}")
                    print(f"    {r.response[:200]}")
            else:
                print(f"{DIM}用法: /hive broadcast 搜索今天的新闻{RST}")
            continue

        if user_input.startswith('/search '):
            query = user_input[8:].strip()
            if query:
                results = pipeline_obj.sessions.search(query, limit=5)
                if results:
                    print(f"\n{GOLD}  Found {len(results)} results:{RST}")
                    for r in results:
                        role_mark = "You" if r['role'] == 'user' else "TMM"
                        print(f"  {DIM}[{role_mark}]{RST} {r['content'][:120]}")
                else:
                    print(f"\n  {DIM}No results for: {query}{RST}")
            else:
                print(f"\n  {DIM}Usage: /search <keyword>{RST}")
            continue

        if user_input in ('/sessions',):
            sessions = pipeline_obj.sessions.list_sessions(10)
            print(f"\n{GOLD}  Recent sessions:{RST}")
            for s in sessions:
                print(f"  {DIM}#{s['id']}{RST} [{s['messages']}msgs] {s['title']}")
            continue

        if user_input in ('/regex',):
            pipeline_obj._regex_passthrough = not pipeline_obj._regex_passthrough
            state = "开启" if pipeline_obj._regex_passthrough else "关闭"
            pipeline_obj._prefs["regex_passthrough"] = pipeline_obj._regex_passthrough
            pipeline_obj._save_prefs()
            print(f"{GOLD}[Regex] 正则快捷通道: {state}{RST}")
            print(f"  {DIM}开启=邮箱/URL/FTS本地快速处理 | 关闭=全部走模型推理{RST}")
            continue

        if user_input in ('/stats',):
            total = pipeline_obj.stats.get('total', 0)
            errors = pipeline_obj.stats.get('errors', 0)
            uptime = time.time() - getattr(pipeline_obj, '_startup', time.time())
            h = int(uptime // 3600)
            m = int((uptime % 3600) // 60)
            s = int(uptime % 60)
            plugins = getattr(pipeline_obj.gateway, 'plugin_mgr', None)
            plugin_count = len(plugins._plugins) if plugins and hasattr(plugins, '_plugins') else 0
            print(f"""
{GOLD}═══ 虎哥状态 ═══{RST}
  运行时间: {h}h {m}m {s}s
  处理请求: {total} 次
  错误次数: {errors} 次
  已加载插件: {plugin_count}
  AutoGuide规则: 463条
""")
            continue

        # ── 三模式命令 (Ask / Plan / Craft) ──
        _mcmd = user_input.strip().lower()
        if _mcmd in ('/ask', '/plan', '/craft'):
            _new = _mcmd[1:]
            if pipeline_obj.modes.set(_new):
                _mi = pipeline_obj.modes.info()
                print(f"{GOLD}[模式]{RST} 已切到 {_mi['cn']} ({_mi['en']}) — {_mi['desc']}")
            else:
                print(f"{RED}[模式] 切换失败{RST}")
            continue
        if _mcmd in ('/mode', '/modes'):
            _cur = pipeline_obj.modes.info()
            _all = pipeline_obj.modes.all()
            print(f"\n{GOLD}═══ 运行模式 ═══{RST}")
            for _k in _cur.get('order', ['ask', 'plan', 'craft']):
                _v = _all.get(_k, {})
                _mark = f"{GREEN}●{RST}" if _k == _cur['mode'] else f"{DIM}○{RST}"
                print(f"  {_mark} {GOLD}{_k:<6}{RST} {_v.get('cn','')}  {DIM}{_v.get('desc','')}{RST}")
            print(f"{DIM}  切换: /ask 问答 | /plan 规划 | /craft 实干{RST}")
            continue

        if user_input.startswith('@'):
            target = user_input[1:].strip()
            if target in {m['id'] for m in pipeline_obj.list_models()} or target == 'auto':
                current_model = target
            continue
        
        t0 = time.time()

        # --- Pre-execution state lay ---
        _ext_model = current_model if current_model != "auto" else None
        _state_msg, _needs_confirm = pipeline_obj._lay_state(user_input, _ext_model)
        if _state_msg:
            print(_state_msg)
        if _needs_confirm:
            try:
                _confirm = input(f"[2m  [回车执行 / 输入修正][0m ").strip()
                if _confirm and not _confirm.startswith('/') and not _confirm.startswith('@'):
                    user_input = _confirm
                    _state_msg2, _ = pipeline_obj._lay_state(user_input, _ext_model)
                    if _state_msg2:
                        print(_state_msg2)
            except (EOFError, KeyboardInterrupt):
                continue

        # --- Intent gate ---
        try:
            if pipeline_obj._classify_intent(user_input) == 'task':
                result = await pipeline_obj._process_task(user_input, t0)
            else:
                print(f"{DIM}[DEBUG] calling process: msg={user_input[:40]} ext_model={current_model}{RST}")
                result = await pipeline_obj.process(user_input, ext_model=current_model if current_model != "auto" else None)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"\n[Intent gate error: {e}]")
            continue

        # ── 规划模式: 方案待确认 → 先展示方案, 再交互确认 ──
        if isinstance(result, dict) and result.get('pending_confirm'):
            _ptxt = result.get('response', '')
            if _ptxt:
                print()
                print(_ptxt)
            try:
                _cf = input(f"{DIM}  [回车/执行 = 执行该方案 · 输入新需求 = 重出方案 · n = 取消]{RST} ").strip()
            except (EOFError, KeyboardInterrupt):
                _cf = 'n'
            if _cf.lower() in ('n', 'no', 'q', '取消', '算了'):
                pipeline_obj.modes.clear_pending()
                print(f"{DIM}[规划模式] 已取消，方案作废。{RST}")
                continue
            _cf = _cf or '执行'
            try:
                result = await pipeline_obj.process(
                    _cf, ext_model=current_model if current_model != "auto" else None)
            except Exception as e:
                print(f"{RED}[规划模式] 执行出错: {e}{RST}")
                continue

        try:
            elapsed = time.time() - t0
            model = result.get('model', 'local')
            response = result.get('response', '')
            print()
            print(f"{DIM}[{model} {elapsed:.1f}s]{RST}")
            print(response)
            
            # -- Auto-extract shell commands from model response -> Inbox --
            _pending_inbox = None
            import re as _re2
            shell_blocks = _re2.findall(r'```(?:shell|bash|cmd|sh)?\s*\n(.+?)```', response, _re2.DOTALL)
            if shell_blocks:
                cmds = []
                for b in shell_blocks:
                    cmds.extend([l.strip() for l in b.strip().split('\n') if l.strip()])
                if cmds:
                    from core.inbox import get_inbox as _gi
                    inbox = _gi()
                    session_id = "cli-" + str(hash(str(os.getcwd())))[:8]
                    _pending_inbox = inbox.create_approval(
                        session_id=session_id, cmds=cmds,
                        summary=f"Model proposed {len(cmds)} command(s)"
                    )
                    print(f"{GOLD}[Inbox] {len(cmds)} cmd(s) - y=execute, n=skip{RST}")
            else:
                _pending_inbox = None
            
            # --- Session logging ---
            # ★ 2026-09-19: 本函数 (pipeline.run_cli) 是**死代码** (活跃入口是
            #   core/cli.py 的 run_cli, main.py 只 import 那个)。这里的写库已删除:
            #   落库口径统一到 process() 的 _persist_turn, 留着会造成双写。
            print()
            if model and model not in ('local', 'auto'):
                current_model = 'auto' if 'auto' in model else model
        except Exception as e:
            elapsed = time.time() - t0
            print()
            pass
            print(f"{RED}出错: {e}{RST}")
            print()

    try: await model_client_obj.close()
    except Exception: pass


# ═══ Main ═══
async def main(port=8800):
    global agent
    from core.model_client import ModelClient
    config = {}
    keys_file = DATA_DIR / "keys.json"
    if keys_file.exists():
        try: config = json.loads(keys_file.read_text(encoding='utf-8'))
        except Exception: pass
    if "deepseek" not in config:
        config["deepseek"] = {"api_key": os.environ.get("DEEPSEEK_API_KEY", ""), "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}
    logger.info("Config: %d models loaded", len(config))
    model_client = ModelClient(config)
    pipeline = Level4Pipeline(model_client, config)
    agent = pipeline
    app = create_app(pipeline)
    if app is None:
        print("FastAPI not installed. Run: pip install fastapi uvicorn")
        return
    import uvicorn
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(cfg)
    print(FRAMEWORK_TEXT)
    logger.info("MARY III v4.2 on http://0.0.0.0:%d", port)
    try: await server.serve()
    except asyncio.CancelledError: logger.info("Shutdown")
    finally: logger.info("Server stopped")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="store_true"); ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--no-check", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(port=args.port))
        # ── 900 @模型 / 800 技能强触发词 / 700 TaskPlanner 已迁到 core/routes.py ──
        #   _build_routes: model.explicit / skill.trigger_first / planner.multi_step
        #   @模型 由 900 优先接住 —— 旧代码里它排在 plugin/search 之后, 实测 4/7 被抢。


# ═══════════════════════════════════════════════════════════════════
#  降级出声 (2026-09-21) —— 在 process() 外面包一层
# ═══════════════════════════════════════════════════════════════════
#  为什么包一层, 而不是在 process() 内部插:
#    process() 有几十个 return 点, 逐点追加降级提示**必然漏**; 包一层 =
#    一个点管全部回合。且完全加性: 没有降级登记时, 返回值与包装前逐字节一致。
#    只在**最外层**回合 reset/汇总 (嵌套调用自己会 process, 不能把外层的登记冲掉)。
#
#  它让"没办成却像办成了"变成**用户看得见**:
#    入口 reset → 各层 except 里 degrade(...) → 出口把 "⚠ 已降级: …" 追到回复末尾
# ★★ 2026-09-24 修 (真 bug, 实测): 原来是个**模块级 dict** —— 长驻进程 (web_server)
#   里 `-= 1` 在异常路径上失败一次, 计数就永久 >0, 此后 outer 恒为 False:
#     · 记账 (task_ledger.record) 被静默跳过 → 引擎不重启就**再也不记账**
#       (实测: 引擎里连发多轮, 账本 192→192 不动; 重启后同一条立刻 192→193)
#     · 降级提示也不出
#   换 contextvars: 每个 asyncio Task 各持一份, reset(token) 保证配对归零。
import contextvars as _ctxvars

_pipeline_depth_var: "_ctxvars.ContextVar[int]" = _ctxvars.ContextVar("tmm_pipeline_depth", default=0)


def _batch_item_ok(resp) -> bool:
    """清单批处理里, 判断某一项**真的成功了**吗 (按多种失败形态)。

    ★ 2026-09-25 加: 只认 "✗ 开头" 会把规划链路的失败 (`Step N (tool): <err>`)
      误判成成功 (自测抓到假绿)。判据:
        · 空回复            → 失败
        · ✗ / ❌ / ⏭️ 开头   → 失败
        · "Step N (tool):" 开头 (工具/规划错误行形态) → 失败
        · 正文含 ❌ / ⏭️     → 失败 (计划汇总里的失败/跳过标记)
        · 含"执行未全部成功" → 失败
    """
    import re as _re_ok
    s = str(resp or "").strip()
    if not s:
        return False
    head = s.splitlines()[0].strip()
    if head.startswith(("✗", "❌", "⏭")):
        return False
    if _re_ok.match(r"^Step\s+\d+\s*[（(]", head):
        return False
    for _m in ("执行未全部成功", "未执行成功", "没有执行成功"):
        if _m in s[:400]:
            return False
    if "❌" in s or "⏭️" in s:
        return False
    return True

# ═══════════════════════════════════════════════════════════════════════
# 文本工具调用解析 (2026-09-24 加)
# ═══════════════════════════════════════════════════════════════════════
# 为什么需要 (从 428 轮真实对话里数出来的):
#   助手轮次 214 里有 **21 轮** 的正文是一坨工具标签, 工具**从未执行**,
#   用户看到的就是那坨 XML —— 且全部落在 general 路由 (本地 mimo 17 次)。
#   真因: 本文件的对话循环在 `if not tool_calls:` 处**只认 OpenAI 结构化
#   message.tool_calls**; 而同一循环的提示词却在要求模型"输出**合法工具标签**"
#   —— 协议有头无尾, 解析器从来没写 (全仓搜工具标签/DSML = 0 处)。
#   实测两种形态:
#     A  <tool_call> <function=NAME> <parameter=K>V</parameter> </function> </tool_call>
#     B  <|DSML|tool_calls> <|DSML|invoke name="NAME"> <|DSML|parameter name="K" ...>V</...>
#   (竖线可能被模型写成全角/转义变体, 归一后处理)
# ★ 设计: 解析**失败就返回空**, 调用方保持原行为一个字不改 —— 纯加性。

#: 竖线变体 (模型常写成全角/转义)
_PIPE_VARIANTS = ("\uff5c", "\u2502", "\u258f")


def _norm_pipes(text: str) -> str:
    s = text
    for ch in _PIPE_VARIANTS:
        s = s.replace(ch, "|")
    return s


def parse_text_tool_calls(text: str) -> list:
    """把模型写在正文里的工具标签解析成 [{"name":…, "arguments":{…}}]。

    辨认两种实测形态。解析不到 → 返回 [] (**不是异常**), 调用方据此保持原行为。
    """
    s = _norm_pipes(str(text or ""))
    if not s or ("function" not in s and "invoke" not in s):
        return []
    calls = []

    # 形态 A: function=NAME … parameter=K>V
    for m in re.finditer(r"<function\s*=\s*([\w.\-]+)\s*>(.*?)</function\s*>",
                         s, re.S | re.I):
        name, body = m.group(1).strip(), m.group(2)
        args = {}
        for pm in re.finditer(r"<parameter\s*=\s*([\w.\-]+)\s*>(.*?)</parameter\s*>",
                              body, re.S | re.I):
            args[pm.group(1).strip()] = _textarg(pm.group(2).strip())
        if name:
            calls.append({"name": name, "arguments": args})

    # 形态 B: invoke name="NAME" … parameter name="K" …>V
    if not calls:
        for m in re.finditer(r"invoke\s+name\s*=\s*[\"']([\w.\-]+)[\"']\s*>(.*?)"
                             r"(?=invoke\s+name\s*=|\Z)", s, re.S | re.I):
            name, body = m.group(1).strip(), m.group(2)
            args = {}
            for pm in re.finditer(r"parameter\s+name\s*=\s*[\"']([\w.\-]+)[\"'][^>]*>"
                                  r"(.*?)(?=parameter\s+name\s*=|invoke\s+name\s*=|\Z)",
                                  body, re.S | re.I):
                args[pm.group(1).strip()] = _textarg(pm.group(2).strip())
            if name:
                calls.append({"name": name, "arguments": args})
    return calls


def _textarg(v):
    """参数值归一: 去残留闭合标签/引号; JSON 字面量还原成真类型。"""
    v = re.sub(r"</?[\w|.\-]+\s*>", "", str(v or "")).strip()
    v = v.strip().strip('"').strip("'").strip()
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", v):
        try:
            return int(v)
        except Exception:
            return v
    return v




async def _process_with_degrade(self, *args, **kwargs):
    from core import result as _R
    _depth = _pipeline_depth_var.get()
    outer = _depth == 0
    if outer:
        _R.reset()
    # ★ token 式 set/reset: 无论正常返回/异常/取消, finally 里都精确还原,
    #   不会再出现"减不回 0"的泄漏 (旧写法在异常路径上就漏)。
    _tok = _pipeline_depth_var.set(_depth + 1)
    try:
        out = await _orig_process(self, *args, **kwargs)
    finally:
        try:
            _pipeline_depth_var.reset(_tok)
        except Exception:
            _pipeline_depth_var.set(_depth)
    if outer and isinstance(out, dict):
        # ★ 2026-09-23: 日常干活账 —— 每轮真实对话记一条 (类别/路由/成败/工具)。
        #   放在**最外层** (与降级汇总同一位置): 一个点覆盖 process 的几十个 return;
        #   只在真人会话写 (TMM_LIVE 门禁在 ledger 内部, 失败即闭)。
        try:
            from core.task_ledger import get_task_ledger
            _gw = getattr(self, "gateway", None)
            _tools = list(getattr(_gw, "turn_tools", []) or [])
            get_task_ledger().record(
                str(args[0] if args else (kwargs.get("message") or "")),
                str(out.get("response") or ""),
                str(out.get("route") or out.get("intent") or ""),
                tools=_tools, elapsed=out.get("elapsed") or 0)
        except Exception as _le:
            logger.debug("task_ledger 记账失败: %s", _le)
        finally:
            try:
                _gw.turn_tools = []                   # 本轮清空 (下一轮重新记)
            except Exception:
                pass
        _mk = _R.format_marker()
        if _mk:
            out["response"] = str(out.get("response") or "") + _mk
            out["degraded"] = _R.items()
        _R.reset()
    return out


_orig_process = Level4Pipeline.process
Level4Pipeline.process = _process_with_degrade
