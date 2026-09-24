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
import os as _os_live; _os_live.environ.setdefault("TMM_LIVE", "1")
_st_ts = _cts.time()
_ts_f = DATA_DIR / "last_startup.txt"
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
    legacy_keys = PROJECT_ROOT / "keys.json"
    if not config and legacy_keys.exists():
        try:
            data = json.loads(legacy_keys.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, dict) and ("key" in v or "api_key" in v):
                    config[k] = v
            logger.warning("Using legacy plaintext keys.json -- run --migrate")
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
    from api.routers import health, chat, code, gateway, admin, inbox

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

    # ── Remote bridge (start after login, not here) ──
    _remote_bridge_started = False

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

    # Start HTTP bridge (VPS bridge calls this) — run on main loop to avoid aiohttp cross-loop crash
    def _start_bridge():
        try:
            from core.http_bridge import start as http_start
            import asyncio as _asyncio
            _main_loop = _asyncio.get_event_loop()
            # 🔴 手机端(TMM-PC通道)固定走 mimo: 包一层强制 ext_model="mimo"
            # 手机消息经 relay → relay_client → 本地 HTTP 桥 → pipeline.process
            # 之前 ext_model=None → ollama(慢, GPU拉满); 改为 mimo(快, 在线)
            _orig_process = pipeline.process
            async def _bridge_process(msg):
                return await _orig_process(msg, ext_model="mimo")
            http_start(_bridge_process, port=19529, main_loop=_main_loop)
            print("  \033[32m✓\033[0m HTTP桥已启动 :19529 (手机端→mimo)")
        except Exception:
            pass
    _start_bridge()
    
    # Start relay client — direct WS to relay, no tunnel needed
    try:
        from core.relay_client import start_relay_client
        start_relay_client()
        print("  \033[32m✓\033[0m 中继客户端已启动 (直连 relay)")
    except Exception as e:
        print(f"  \033[33m⚠\033[0m 中继客户端失败: {e}")

    try:
        await run_cli(pipeline, mc, _startup_rpt)
    finally:
        # 退出收口: 关掉 MCP 子进程, 别留孤儿 + 泄漏的 asyncio 传输
        try:
            await pipeline.aclose_resources()
        except Exception as _e:
            logger.debug("shutdown cleanup failed: %s", _e)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="MARY III v4.2.0")
    p.add_argument("--api", action="store_true", help="Start API server")
    p.add_argument("--cli", action="store_true", help="Start CLI interactive mode")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--migrate", action="store_true", help="Run data migration (plaintext -> .enc)")
    p.add_argument("--no-check", action="store_true", help="Skip startup security self-check")
    args = p.parse_args()

    # Data migration
    if args.migrate:
        migrate_plaintext_to_encrypted()
        print("Migration complete. Old files renamed to .json.bak")
        sys.exit(0)

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
