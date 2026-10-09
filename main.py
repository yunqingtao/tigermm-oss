"""
MARY III v4.2.0 — Modular entry point with startup security checks.
Phase 1-2 complete: config, core, sandbox, gateway, storage, utils.
Security: 6-point startup self-check + encrypted storage migration.
"""
import sys
import os
import asyncio
import logging
import json
from pathlib import Path
import shutil

# Ensure parent is on path for imports
_mary3_root = Path(__file__).parent
sys.path.insert(0, str(_mary3_root))
sys.path.insert(0, str(_mary3_root.parent))

from config.settings import (
    PROJECT_ROOT, DATA_DIR, DEFAULT_PORT, DEFAULT_HOST,
    CRYPTO_KEY, MAX_CODE_ROUNDS, CHAT_RETENTION_DAYS,
    MEMORY_DB, PLUGIN_ALLOW_DIR, USERS_FILE, KEYS_FILE,
    SENSITIVE_FILES, TOOLS_DIR,
)
from config.crypto import init_crypto, secure_load, secure_save
import re
import time

# --- Startup timestamp ---
import time as _cts

# ★ 真人会话标记: 记账类功能(技能使用账/产物台账)只认这个信号 ——
#   测试/门禁没有它, 所以永远写不进用户真数据 (fail-closed)。
# ★★ 2026-09-25 加: TMM_PROBE 在场时**不许**把这个进程标成"真人会话"。
#   原来这句无条件 setdefault ⇒ 探针只要 `from main import _load_config` 拿配置,
#   整个进程就变成"真人会话", 忘了设 TMM_PROBE 的探针会把测试流量记进用户真账本
#   (实测: 一条真账本被多记 6 轮)。探针的隔离约定是设 TMM_PROBE=1, 这里尊重它。
import os as _os_live
if _os_live.environ.get("TMM_PROBE") not in ("1", "true", "yes"):
    _os_live.environ.setdefault("TMM_LIVE", "1")
_st_ts = _cts.time()
_ts_f = Path(_os_live.environ["TMM_LAST_STARTUP"]) if _os_live.environ.get("TMM_LAST_STARTUP") else (DATA_DIR / "last_startup.txt")
# ★ --exec (非交互 / 评测 / CI) 不写这个时间戳: 它是"真人会话从哪一轮开始"的锚点
#   (core/command_handler.py 靠它算 /changes)。被 89 轮评测反复改写的话, 用户
#   一开 CLI 就看到一堆"评测进程改过的东西"。--exec 自己的隔离沙箱里另有它
#   (见 scripts/verification/_probe_env.py 的 ISOLATED 表)。
if not any(_a == "--exec" or _a.startswith("--exec=") for _a in sys.argv[1:]):
    _ts_f.parent.mkdir(parents=True, exist_ok=True)
    _ts_f.write_text(str(_st_ts))

# --- Init ---
init_crypto(CRYPTO_KEY)
for d in [DATA_DIR, DATA_DIR / "memory", DATA_DIR / "code_pool", PLUGIN_ALLOW_DIR]:
    d.mkdir(parents=True, exist_ok=True)
os.environ["MAX_CODE_ROUNDS"] = str(MAX_CODE_ROUNDS)

logging.basicConfig(
    level=logging.INFO,
    format="\033[38;5;214m%(asctime)s [%(levelname)s] %(name)s: %(message)s\033[0m"
)
# Suppress noisy loggers
logging.getLogger("core.pipeline").setLevel(logging.WARNING)
logging.getLogger("core.backup").setLevel(logging.WARNING)
logger = logging.getLogger("mary3")


# ============================================================
# Startup security self-check (6 points)
# ============================================================
def startup_self_check():
    """Run at startup: validate all 6 high-risk areas."""
    print("=" * 60)
    print("  MARY III v4.2.0 -- Security Self-Check")
    print("=" * 60)

    issues = []

    # 1. Encrypt & key permissions
    try:
        init_crypto(CRYPTO_KEY)
        st = CRYPTO_KEY.stat()
        if st.st_mode & 0o077:
            try:
                os.chmod(CRYPTO_KEY, 0o600)
                print("  [OK] Crypto: key permissions fixed (0o600)")
            except Exception:
                issues.append("Crypto: key file permissions too open")
                print("  [WARN] Crypto: key permissions too open")
        else:
            print("  [OK] Crypto: initialized, key permissions OK (0o600)")
    except Exception as e:
        issues.append("Crypto: init failed - " + str(e))
        print("  [FAIL] Crypto: " + str(e))

    # 2. Old plaintext chat history
    # Scan ALL paths for chat_history plaintext
    import glob as _glob
    old_chats = [
        DATA_DIR / "chat_history.txt",
        PROJECT_ROOT.parent / "data" / "chat_history.txt",
    ]
    # Also scan recursively
    for p in [DATA_DIR, PROJECT_ROOT.parent / "data"]:
        if p.exists():
            for f in _glob.glob(str(p / "**" / "chat_history*"), recursive=True):
                fp = Path(f)
                if fp.suffix in ('.txt', '.json') and fp not in old_chats:
                    old_chats.append(fp)
    found = False
    for old_file in old_chats:
        if old_file.exists():
            try:
                old_file.unlink()
                print("  [WARN] Deleted old plaintext: " + str(old_file))
            except Exception as e:
                issues.append("Old plaintext still exists: " + str(old_file))
                print("  [FAIL] Cannot delete: " + str(old_file))
            found = True
    for old_file in old_chats:
        if old_file.exists():
            try:
                old_file.unlink()
                print("  [WARN] Deleted old plaintext: " + str(old_file))
            except Exception as e:
                issues.append("Old plaintext still exists: " + str(old_file))
                print("  [FAIL] Cannot delete: " + str(old_file))
            found = True
    if not found:
        print("  [OK] Chat history: no plaintext residue")

    # 3. SQLite connection pool
    try:
        from storage.sqlite_store import MemoryStore
        MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
        test_store = MemoryStore(MEMORY_DB)
        test_store.remember("__self_test__", "ok", ttl_days=1)
        val = test_store.recall("__self_test__")
        test_store.forget("__self_test__")
        test_store.close()
        if val == "ok":
            print("  [OK] SQLite: connection pool healthy (WAL mode)")
        else:
            issues.append("SQLite: read-back mismatch")
            print("  [FAIL] SQLite: read-back mismatch")
    except Exception as e:
        issues.append("SQLite: pool init failed - " + str(e))
        print("  [FAIL] SQLite: " + str(e))

    # 4. Sandbox tool proxy check
    try:
        from gateway import tool_gateway
        if hasattr(tool_gateway, '_sandbox_tool_proxy'):
            issues.append("Sandbox: _sandbox_tool_proxy STILL EXISTS")
            print("  [FAIL] Sandbox: _sandbox_tool_proxy escape vector present")
        else:
            print("  [OK] Sandbox: tool proxy removed, no escape vector")
    except Exception as e:
        print("  [WARN] Sandbox: could not verify - " + str(e))

    # 5. Plugin directory security scan
    try:
        from gateway.plugin_mgr import scan_plugin_safe
        if TOOLS_DIR.exists():
            blocked = []
            for pf in sorted(TOOLS_DIR.glob("*.py")):
                if pf.name.startswith("_"):
                    continue
                if not scan_plugin_safe(pf):
                    blocked.append(pf.name)
            if blocked:
                issues.append("Plugins: " + str(len(blocked)) + " unsafe: " + str(blocked))
                print("  [FAIL] Plugins: " + str(len(blocked)) + " UNSAFE: " + str(blocked))
            else:
                print("  [OK] Plugins: all safe (no dangerous imports)")
        else:
            print("  [OK] Plugins: tools/ directory not present")
    except Exception as e:
        issues.append("Plugins: scan failed - " + str(e))
        print("  [FAIL] Plugins: scan FAILED - " + str(e))

    # 6. File operation entry point
    try:
        from storage.file_secure import cli_secure_path
        blocked_ok = True
        for test_path in [".crypto.key", "users.enc", "keys.enc"]:
            try:
                cli_secure_path(test_path)
                issues.append("File security: allowed " + test_path)
                print("  [FAIL] File security: allowed " + test_path)
                blocked_ok = False
                break
            except PermissionError:
                pass
        if blocked_ok:
            print("  [OK] File security: cli_secure_path blocks sensitive files")
    except Exception as e:
        issues.append("File security: validation failed - " + str(e))
        print("  [FAIL] File security: " + str(e))

    # Summary
    print("-" * 60)
    if issues:
        print("  [WARN] " + str(len(issues)) + " ISSUE(S) FOUND:")
        for i in issues:
            print("     - " + i)
        print("  Service will start but security may be compromised.")
    else:
        print("  [OK] ALL 6 CHECKS PASSED")
    print("=" * 60)
    return len(issues) == 0


# ============================================================
# Data migration: plaintext -> encrypted
# ============================================================
def migrate_plaintext_to_encrypted():
    """One-time migration: convert plaintext JSON files to .enc."""
    migrations = [
        (DATA_DIR / "users.json", USERS_FILE),
        (PROJECT_ROOT / "keys.json", KEYS_FILE),
        (DATA_DIR / "rules.json", DATA_DIR / "rules.enc"),
        (DATA_DIR / "learned_rules.json", DATA_DIR / "learned_rules.enc"),
        (DATA_DIR / "prefs.json", DATA_DIR / "prefs.enc"),
    ]
    
    for old_path, new_path in migrations:
        if old_path.exists() and not new_path.exists():
            try:
                data = json.loads(old_path.read_text(encoding="utf-8"))
                secure_save(new_path, data)
                backup = old_path.with_suffix(old_path.suffix + ".bak")
                shutil.move(str(old_path), str(backup))
                logger.info("Migrated: %s -> %s (backup: %s)", old_path.name, new_path.name, backup.name)
            except Exception as e:
                logger.warning("Migration failed: %s -> %s: %s", old_path.name, new_path.name, e)


# ============================================================
# Config loader (now uses encrypted keys.enc)
# ============================================================
def _load_config() -> dict:
    config = {}
    
    # Try encrypted keys.enc first
    if KEYS_FILE.exists():
        try:
            data = secure_load(KEYS_FILE, force_encrypt=False)
            for k, v in data.items():
                if isinstance(v, dict) and ("key" in v or "api_key" in v):
                    config[k] = v
        except Exception as e:
            logger.warning("keys.enc load failed: %s", e)
    
    # Legacy fallback: plaintext keys.json
    # ★ 2026-10-09 修: 原来这里无条件 `logger.warning("Using legacy plaintext keys.json
    #   -- run --migrate")`。对**新装的收件人**这是双重误导:
    #     ① 明文 keys.json 正是 install.py 生成、README 让他编辑的**正常状态**,
    #        不是"legacy"(那是相对我自己的 .enc 加密流程而言);
    #     ② `--migrate` 是**数据迁移**(明文→.enc)的内部开关, 点名的**计划任务会跑它**——
    #        让收件人手动跑它是错的 (且我们自己有"禁跑 --migrate"的规矩)。
    #   现在改成: 只有**确实存在 keys.enc 但读不出来**时才告警 (那才是真问题);
    #   正常走明文时不出声。
    legacy_keys = PROJECT_ROOT / "keys.json"
    if not config and legacy_keys.exists():
        try:
            data = json.loads(legacy_keys.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, dict) and ("key" in v or "api_key" in v):
                    config[k] = v
            if (PROJECT_ROOT / "keys.enc").exists():
                # 有密文却没用上 —— 这才值得提醒 (去体检那份密文, 别顺手删明文)
                logger.warning("keys.enc 存在但读不出, 本次用了明文 keys.json")
        except Exception:
            pass
    
    # Environment variable overrides
    for ek in ["DEEPSEEK_API_KEY", "MIMO_API_KEY", "OPENAI_API_KEY"]:
        val = os.environ.get(ek)
        if val:
            name = ek.replace("_API_KEY", "").lower()
            if name not in config:
                config[name] = {"key": val, "url": "", "model": name}
    return config


# ============================================================
# App factory
# ============================================================
def create_app(config: dict):
    """Build FastAPI app with all routers and middleware."""
    try:
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:
        logger.error("FastAPI not installed")
        return None

    from config.settings import CORS_ORIGINS
    # ★ 2026-10-09 (分发边界): `api/` 已按"交付版只给 Windows 前端"排出包外。
    #   这一处 import 变成"可选" —— 缺了就明确说清, 而不是抛 ModuleNotFoundError。
    #   交付路径 (Tiger.M.M.bat → tmm_app.py → web_server.py) 不经过这里。
    try:
        from api.routers import health, chat, code, gateway, admin, inbox
    except ImportError:
        logger.error("本包不含 API 服务端 (api/ 已按分发边界排除)；"
                     "请用 Tiger.M.M.bat / tmm_app.py 启动桌面端")
        return None

    app = FastAPI(title="MARY III", version="4.2.0")

    # Request body size limit: 100KB max
    from starlette.middleware.base import BaseHTTPMiddleware
    from fastapi import Request as _FR
    class _BodyLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: _FR, call_next):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > 102400:
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "Request too large (max 100KB)"}, status_code=413)
            return await call_next(request)
    app.add_middleware(_BodyLimitMiddleware)

    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS,
                        allow_methods=["*"], allow_headers=["*"])

    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline

    model_client = ModelClient(config)
    pipeline = Level4Pipeline(model_client, config)

    app.state.pipeline = pipeline
    app.state.auto_guide = pipeline.auto_guide
    app.state.plugins = pipeline.plugins
    app.state.gateway = pipeline.gateway
    app.state.model_client = model_client
    app.state.config = config

    from storage.sqlite_store import MemoryStore
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    app.state.memory = MemoryStore(MEMORY_DB)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(code.router)
    app.include_router(gateway.router)
    app.include_router(admin.router)
    app.include_router(inbox.router)

    @app.get("/")
    async def root():
        from fastapi.responses import HTMLResponse
        import os as _os
        html_paths = [
            _os.path.join(_os.path.dirname(__file__), "..", "super_agent_frontend.html"),
            _os.path.join(_os.path.dirname(__file__), "..", "index.html"),
            _os.path.join(_os.path.dirname(__file__), "mobile_web", "index.html"),
        ]
        for p in html_paths:
            if _os.path.exists(p):
                with open(p, 'r', encoding='utf-8') as fh:
                    return HTMLResponse(fh.read())
        return {"name": "MARY III", "version": "4.2.0", "status": "online"}

    @app.on_event("shutdown")
    async def shutdown():
        """Graceful shutdown: close connections, flush stores."""
        if hasattr(app.state, 'memory') and app.state.memory:
            try:
                app.state.memory.close()
                logger.info("SQLite pool closed")
            except Exception as e:
                logger.error("SQLite close error: %s", e)
        if hasattr(app.state, 'model_client') and app.state.model_client:
            try:
                await model_client.close()
                logger.info("Model client closed")
            except Exception as e:
                logger.error("Model client close error: %s", e)
        try:
            from storage.json_store import cleanup_expired_chat
            cleanup_expired_chat()
        except Exception:
            pass

    logger.info("App created: %d routers", 5)
    # === L0 dispatch 500 exception handler (no-model resilience) ===
    @app.exception_handler(500)
    async def sandbox_500_handler(request, exc):
        from fastapi.responses import JSONResponse
        err_str = str(exc).lower()
        if "permission" in err_str or "access denied" in err_str:
            return JSONResponse(status_code=500, content={"code": 500, "msg": "沙箱权限校验失败：路径/操作不在白名单内"})
        if "sandbox" in err_str or "security" in err_str:
            return JSONResponse(status_code=500, content={"code": 500, "msg": "沙箱安全校验异常，已自动重试"})
        if "timeout" in err_str:
            return JSONResponse(status_code=500, content={"code": 500, "msg": "工具调用超时，无模型链路自动重试中"})
        return JSONResponse(status_code=500, content={"code": 500, "msg": "Agent调度异常，链路已安全降级"})

    return app


# ============================================================
# Server entry
# ============================================================
async def main_server(port: int = DEFAULT_PORT):
    config = _load_config()
    logger.info("Config: %d models loaded", len(config))

    app = create_app(config)
    if app is None:
        logger.error("Cannot start -- FastAPI missing. pip install fastapi uvicorn")
        return

    import uvicorn
    cfg = uvicorn.Config(app, host=DEFAULT_HOST, port=port, log_level="info")
    server = uvicorn.Server(cfg)
    logger.info("MARY III v4.2.0 on http://%s:%d", DEFAULT_HOST, port)
    await server.serve()


async def main_cli():
    """Full Tiger.M.M CLI experience."""
    config = _load_config()

    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.cli import run_cli

    mc = ModelClient(config)
    pipeline = Level4Pipeline(mc, config)

    from core.startup_check import run as startup_check
    _startup_rpt = startup_check(str(PROJECT_ROOT), config)
    # ── Install CLI hooks ──
    from core.cli_hooks import install as hooks_install
    hooks_install(pipeline, config, _startup_rpt)

    # (Remote bridge / TMM Link 已于 2026-10-09 封杀 —— 见 docs/monitor/封杀TMM_Link_20261009.md)

    # ── Tool generation hook ──
    _orig_process = pipeline.process
    pipeline.process = lambda msg, **kw: _hooked_process(msg, _orig_process, **kw)
    async def _hooked_process(msg, orig_fn, **kw):
        # ── FAST PATH: "打开XX" → launch browser ──
        if "打开" in msg:
            _sites = {"百度":"https://www.baidu.com", "谷歌":"https://www.google.com",
                      "B站":"https://www.bilibili.com", "bilibili":"https://www.bilibili.com",
                      "知乎":"https://www.zhihu.com", "京东":"https://www.jd.com"}
            for s, url in _sites.items():
                if s in msg:
                    import subprocess, os as _os
                    for b in [r"C:/Program Files/Google/Chrome/Application/chrome.exe",
                             r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]:
                        if _os.path.exists(b):
                            subprocess.Popen([b, url])
                            return {"response": f"已打开{s}", "intent":"open_browser","model":"local","elapsed":0.05}
                    subprocess.Popen(['start', url], shell=True)
                    return {"response": f"已打开{s}", "intent":"open_browser","model":"local","elapsed":0.05}
        
        from core.tool_gen import parse_tool_request, generate
        parsed = parse_tool_request(msg)
        if parsed:
            name, desc = parsed
            from pathlib import Path
            _mc = getattr(pipeline, "model_client", None) or getattr(pipeline, "mc", None)
            filepath = generate(name, desc, str(Path(__file__).parent / "tools"), model_client=_mc)
            return {"response": f"✅ 工具已生成: {filepath}\n重启TMM后可用。", "intent": "tool_gen", "model": "local", "elapsed": 0.05}
        # Self-evolution: detect corrections
        if msg.startswith("不对") or msg.startswith("应该说") or "不是这样" in msg:
            from core.cli_hooks import on_correction
            # ★ 2026-09-19 修: 旧代码传的是**空串** original_msg → record_correction
            #   立即 return None → 这条自进化链从来没工作过 (corrections.json 一直不存在)。
            #   现在取真实的"上一句用户消息"作为被纠正对象。
            _prev = ""
            try:
                _mem = getattr(pipeline, "memory", None)
                if _mem is not None:
                    for _t in reversed(_mem.recent(6) or []):
                        if _t.get("role") == "user" and (_t.get("text") or "").strip() != msg.strip():
                            _prev = _t.get("text") or ""
                            break
            except Exception:
                _prev = ""
            on_correction(_prev, msg)
        return await orig_fn(msg, **kw)

    # ── TMM Link (手机↔电脑通道) ─────────────────────────────────────────
    # ★ 2026-10-09 **封杀 TMM Link**（证据与回滚见 docs/monitor/封杀TMM_Link_20261009.md）:
    #   ① 交付版**不含这套代码**（relay_client / *_bridge / relay_bot 已进排除表）
    #      → 这里 import 不到, 安静跳过 (不是错误, 是边界);
    #   ② 开发仓里有代码, 但"没配就不连"(relay_configured 为假)。
    #      以前是**无条件**启动 + 重连一个写死的作者地址。
    try:
        from core.relay_client import relay_configured, start_relay_client
    except ImportError:
        relay_configured = None          # 交付版按设计没有这套 → 不报错、不提示

    if relay_configured is not None:
        try:
            if relay_configured():
                # Start HTTP bridge (VPS bridge calls this) — run on main loop to avoid aiohttp cross-loop crash
                def _start_bridge():
                    try:
                        from core.http_bridge import start as http_start
                        import asyncio as _asyncio
                        _main_loop = _asyncio.get_event_loop()
                        # 手机端(TMM-PC通道)固定走 mimo: 包一层强制 ext_model="mimo"
                        _orig_process = pipeline.process
                        async def _bridge_process(msg):
                            return await _orig_process(msg, ext_model="mimo")
                        http_start(_bridge_process, port=19529, main_loop=_main_loop)
                        print("  \033[32m✓\033[0m HTTP桥已启动 :19529 (手机端→mimo)")
                    except Exception:
                        pass
                _start_bridge()

                start_relay_client()
                print("  \033[32m✓\033[0m 中继客户端已启动 (直连 relay)")
            else:
                print("  \033[2m· TMM Link 未配置 —— 已关闭 "
                      "(要开: 设 TMM_RELAY_URL 与 TMM_RELAY_TOKEN)\033[0m")
        except Exception as e:
            print(f"  \033[33m⚠\033[0m TMM Link 未启动: {e}")

    try:
        await run_cli(pipeline, mc, _startup_rpt)
    finally:
        # 退出收口: 关掉 MCP 子进程, 别留孤儿 + 泄漏的 asyncio 传输
        try:
            await pipeline.aclose_resources()
        except Exception as _e:
            logger.debug("shutdown cleanup failed: %s", _e)


# ============================================================
# Headless single-task entry  (--exec)
# ============================================================
def _install_container_relay(container: str) -> None:
    r"""--exec-shell-in: 把 shell_exec 的"执行的机器"从宿主换成容器。

    为什么猴补 PluginManager.call 而不是改 tools/shell_exec.py:
      ① PM 每次调用都用 spec_from_file_location **从文件重新加载**模块 ⇒
         改模块属性会被下一次加载覆盖, 不生效;
      ② 改磁盘上的文件 = 污染用户仓库 (本项目铁律: 探针禁改生产文件);
      ③ 补在 PM 上, 工具自己的安全检查/审计照旧跑, 只是"在哪台机器执行"换了。

    被中继的只有 shell_exec。宿主文件类工具 (file_ops 等) 仍指向宿主 ——
    评测时 TMM 用 shell 就能覆盖绝大多数操作 (实测它会自适应: 见
    scripts/verification/_probe_container_relay.py 的输出)。

    返回形状对齐 tools/shell_exec.py 的契约: {"success", "output", "exit_code"}。
    """
    import subprocess as _sp

    from core.plugin_manager import PluginManager as _PM_a
    from core.pipeline import PluginManager as _PM_b

    def _wrap(orig):
        async def _relay(self, tool_name, **kwargs):
            if tool_name != "shell_exec":
                return await orig(self, tool_name, **kwargs)

            cmd = kwargs.get("command") or kwargs.get("cmd") or ""
            if not cmd:
                return {"success": False, "error": "No command provided"}
            try:
                r = _sp.run(
                    ["docker", "exec", container, "sh", "-lc", cmd],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=600,
                )
                return {
                    "success": r.returncode == 0,
                    "output": ((r.stdout or r.stderr) or "(no output)")[:5000],
                    "exit_code": r.returncode,
                    "_relay": "container:%s" % container,
                }
            except Exception as _e:                  # noqa: BLE001
                return {"success": False,
                        "error": "container relay failed: %r" % (_e,)}
        return _relay

    _PM_a.call = _wrap(_PM_a.call)
    _PM_b.call = _wrap(_PM_b.call)


def _sweep_stale_exec_sandboxes() -> int:
    r"""清掉上次 --exec 留下的沙箱 (tmp/_probe_<pid>/)。

    为什么需要: --exec 用 _probe_env.activate() 建的是一次性沙箱, 但它的收尾删除
    **在进程活着时删不掉** —— sqlite (chat_sessions.db) 还占着句柄, rmtree 静默失败;
    等进程真退出、锁释放了, 已经没人再删了。实测每次 --exec 都留一个 44 KB 目录,
    89 题评测就是 89 个残骸。所以改成**下次启动时扫掉**: 那时上个进程必已结束。

    安全: 只删 **PID 已不存在** 的目录 —— 并发跑评测时不会误删别人正在用的沙箱。
    psutil 不可用就整个跳过 (宁可留残骸, 不可误删)。
    """
    import shutil as _sh
    try:
        import psutil
    except Exception:                                # noqa: BLE001
        return -1
    tmp = PROJECT_ROOT / "tmp"
    if not tmp.is_dir():
        return 0
    n = 0
    for d in tmp.glob("_probe_*"):
        if not d.is_dir():
            continue
        _pid = d.name[len("_probe_"):]
        if not _pid.isdigit():
            continue
        try:
            if psutil.pid_exists(int(_pid)):
                continue                             # 别人还在跑, 别碰
        except Exception:                            # noqa: BLE001
            continue
        _sh.rmtree(d, ignore_errors=True)
        if not d.exists():
            n += 1
    return n


async def main_exec(task: str, model: str | None = None, as_json: bool = False,
                    timeout: float = 0.0, live: bool = False,
                    keep_sandbox: bool = False,
                    shell_in: str | None = None) -> int:
    """非交互模式: 跑一条任务, 打完收工 —— 给脚本 / CI / 外部评测 harness 驱动用。

    交互式 CLI (--cli) 要人敲键盘, 外部程序没法驱动它; 这个入口补上那一半。

    隔离 (默认): 按**探针**跑 —— 复用 scripts/verification/_probe_env.py 那套
      已定稿的隔离契约 (40 个落盘点全部指向 tmp/_probe_<pid>/, 并装写盘闸),
      **不回写用户真实 data/**。要按"真人会话"记账 (真写用户数据) 显式加 --live。
      为什么默认隔离: 本项目已有两次"探针写脏真对话库"和一次"真账本被多记 6 轮"
      的事故, 结论是隔离不能靠调用方记得设变量 —— 见 core/probe_guard.py。

    输出约定: **stdout 只有任务结果** (便于程序消费), 其余一切日志走 stderr。
    """
    real_stdout = sys.stdout
    _sandbox = None

    # ── 1) 隔离 (必须在 import core.pipeline 之前: perception 等模块在 import 时绑定路径) ──
    if not live:
        try:
            _n_swept = _sweep_stale_exec_sandboxes()
            if _n_swept > 0:
                print("[exec] 清掉 %d 个上次残留的沙箱" % _n_swept, file=sys.stderr)
            _sv = str(PROJECT_ROOT / "scripts" / "verification")
            if _sv not in sys.path:
                sys.path.insert(0, _sv)
            from _probe_env import activate          # noqa: PLC0415
            _sandbox = activate()
            print("[exec] 隔离模式: 落盘 -> %s (不写用户真实数据; 要真写加 --live)"
                  % _sandbox, file=sys.stderr)
        except Exception as _e:                      # noqa: BLE001
            print("[exec] 隔离初始化失败, 拒绝启动: %r" % (_e,), file=sys.stderr)
            return 3
    else:
        print("[exec] live 模式: 会写真实 data/ (记忆/台账/会话)", file=sys.stderr)

    # ── 1.5) 容器中继 (--exec-shell-in): shell 的"执行的机器"换成容器 ──
    #    评测用: 题目跑在 Docker 容器里, TMM 跑在宿主上 —— 不中继的话它的命令
    #    落在宿主, 根本看不见题目文件。做法是猴补 PluginManager.call,
    #    **不改磁盘上任何文件**(探针禁改生产文件)。
    if shell_in:
        try:
            _install_container_relay(shell_in)
            print("[exec] shell 已中继到容器 %s (shell_exec -> docker exec)" % shell_in,
                  file=sys.stderr)
            # ★ 环境事实必须一起给: 否则模型以为自己还在 Windows (system prompt 这么写的),
            #   发 `dir C:\app`、`if exist \app` 这类 cmd 命令 ⇒ 中继进容器全部报错 ⇒
            #   它就得出"工具够不到"的结论放弃。实测(2026-09-28): 不给这句, 同一道题它失败;
            #   给了之后它用 sh 语法直接做对。
            task = (
                "【运行环境】你的 shell 命令执行在一个 **Linux 容器**里（Docker），"
                "不是 Windows 本机。请用 sh/bash 语法（如 `ls` `mkdir -p` `echo x > f` `cat`），"
                "不要用 cmd 语法（`dir`/`type`/`if exist`/反斜杠盘符路径）。"
                "容器里的路径就是 Linux 路径（如 /app、/work）。\n\n"
                + task
            )
        except Exception as _e:                      # noqa: BLE001
            print("[exec] 容器中继失败, 拒绝启动: %r" % (_e,), file=sys.stderr)
            return 3

    # ── 2) 引擎 (与 --cli 同一条 pipeline; 只省掉登录/中继/中控台那几件人机交互的) ──
    config = _load_config()
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline

    mc = ModelClient(config)
    pipeline = Level4Pipeline(mc, config)

    try:
        from core.cli_hooks import install as hooks_install
        hooks_install(pipeline, config, "")
    except Exception as _e:                          # noqa: BLE001
        print("[exec] hooks 安装跳过: %r" % (_e,), file=sys.stderr)

    _kw = {}
    if model:
        _kw["ext_model"] = model

    # ── 3) 执行 (执行期 stdout 下到 stderr, 保证 stdout 干净) ──
    # ★ 2026-09-28 (评测实测逼出来的): **非交互入口 = 无人值守**。
    #   实测 terminal-bench: TMM 拿到题后反问用户"请问你的具体需求是什么?"、
    #   或说"要的话说一声我立刻跑" —— 而调用方是程序, 没人能回答, 预算就这么烧掉了
    #   (swe-bench-langcodes 15 秒结束、extract-moves-from-video 空转到 660 秒)。
    #   交互式 CLI 里"先确认再动手"是对的; 这里必须明说没有人。
    #   放 main_exec 里而不是改 system prompt: 交互场景一个字节都不受影响。
    # ★ 2026-09-28: 让引擎知道"这次没有人在" (见 pipeline._skill_dag_exec 的闸门)
    os.environ.setdefault("TMM_UNATTENDED", "1")
    _task = (
        "【无人值守执行 · 由程序调用】\n"
        "正在调用你的是程序而不是人。没有人能回答你的提问:\n"
        "· 不要问用户、不要请求确认、不要问\"你要哪种\" —— 自己判断并直接执行\n"
        "· 不要只给方案/分析: 去动手做完再汇报\n"
        "· 任务没要求就别生成文档/文件, 也不要往桌面写东西 —— 直接执行、直接回话\n"
        "· 汇报要给真实证据(命令输出/文件路径/行数), 做不到就说清卡在哪一步\n\n"
        + task
    )
    sys.stdout = sys.stderr
    try:
        _coro = pipeline.process(_task, **_kw)
        if timeout and timeout > 0:
            result = await asyncio.wait_for(_coro, timeout=timeout)
        else:
            result = await _coro
    except asyncio.TimeoutError:
        result = {"response": "", "error": "timeout after %.0fs" % timeout,
                  "intent": "timeout", "model": "", "elapsed": timeout}
    except Exception as _e:                          # noqa: BLE001
        result = {"response": "", "error": "%s: %s" % (type(_e).__name__, _e),
                  "intent": "error", "model": "", "elapsed": 0.0}
    finally:
        sys.stdout = real_stdout
        try:
            await pipeline.aclose_resources()
        except Exception:                            # noqa: BLE001
            pass

    # ── 3.5) 收沙箱 ──
    # _probe_env 自己也注册了 atexit 清理, 但实测**不生效**: 退出时 sqlite
    # (chat_sessions.db) 还占着, rmtree 静默失败 ⇒ 每次 --exec 都在 tmp/ 留一个
    # 44 KB 的 _probe_<pid>/。89 题评测就是 89 个残骸。所以收工后再删一次。
    if _sandbox is not None and not keep_sandbox:
        import shutil as _sh
        for _try in range(4):
            _sh.rmtree(_sandbox, ignore_errors=True)
            if not Path(_sandbox).exists():
                break
            time.sleep(0.4)

    result = result or {}
    err = result.get("error")

    # ── 4) 交回结果 ──
    if as_json:
        print(json.dumps({
            "ok": not bool(err),
            "response": result.get("response", ""),
            "model": result.get("model"),
            "intent": result.get("intent"),
            "elapsed": result.get("elapsed"),
            "error": err,
        }, ensure_ascii=False))
    else:
        if result.get("response"):
            print(result["response"])
        if err:
            print("[exec error] %s" % err, file=sys.stderr)
    return 1 if err else 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="MARY III v4.2.0")
    p.add_argument("--api", action="store_true", help="Start API server")
    p.add_argument("--cli", action="store_true", help="Start CLI interactive mode")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--migrate", action="store_true", help="Run data migration (plaintext -> .enc)")
    p.add_argument("--no-check", action="store_true", help="Skip startup security self-check")
    # ── 非交互入口 (给脚本/CI/评测 harness 驱动) ──
    p.add_argument("--exec", dest="exec_task", metavar="TASK", default=None,
                   help="Headless: run ONE task and exit. Use '-' to read the task from stdin.")
    p.add_argument("--model", dest="exec_model", default=None,
                   help="--exec: force a model (deepseek / mimo / ollama)")
    p.add_argument("--exec-json", action="store_true",
                   help="--exec: emit one JSON object on stdout instead of plain text")
    p.add_argument("--exec-timeout", type=float, default=0.0,
                   help="--exec: abort after N seconds (0 = no limit)")
    p.add_argument("--live", action="store_true",
                   help="--exec: write real user data (default is an isolated sandbox)")
    p.add_argument("--exec-keep-sandbox", action="store_true",
                   help="--exec: keep the sandbox dir after the run (for diagnosing a failure)")
    p.add_argument("--exec-shell-in", dest="exec_shell_in", metavar="CONTAINER",
                   default=None,
                   help="--exec: run shell commands INSIDE this docker container "
                        "(relay; for benchmarks whose task lives in a container)")
    args = p.parse_args()

    # Data migration
    if args.migrate:
        migrate_plaintext_to_encrypted()
        print("Migration complete. Old files renamed to .json.bak")
        sys.exit(0)

    # ── Headless single task (--exec): 走这条, 不碰 CLI/服务器, 也不跑启动自检
    #    (自检是给人看的启动画面, 会把 stdout 弄脏; --exec 的 stdout 是要给程序读的) ──
    if args.exec_task is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        _task = args.exec_task
        if _task == "-":
            _task = sys.stdin.read()
        if not _task.strip():
            print("[exec] 任务为空", file=sys.stderr)
            sys.exit(2)
        sys.exit(asyncio.run(main_exec(
            _task,
            model=args.exec_model,
            as_json=args.exec_json,
            timeout=args.exec_timeout,
            live=args.live,
            keep_sandbox=args.exec_keep_sandbox,
            shell_in=args.exec_shell_in,
        )))

    # Startup security self-check
    if not args.no_check:
        ok = startup_self_check()
        if not ok:
            logger.warning("Security self-check found issues -- proceeding anyway")

    try:
        if args.cli or (not args.api):
            asyncio.run(main_cli())
        else:
            asyncio.run(main_server(args.port))
    except KeyboardInterrupt:
        logger.info("Shutdown")
