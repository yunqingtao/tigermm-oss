"""
Tiger.M.M Web UI — 独立后端 (FastAPI + uvicorn)

前端: ui/index.html  (纯静态, 不再 import 引擎)
后端: 本文件 (Level4Pipeline 引擎 + HTTP API)

Start: python web_server.py
Open:  http://localhost:8800

为什么从 web_chat.py 迁过来:
  web_chat.py 用 Flask+waitress, waitress 在 Windows 上用 select,
  与重量级引擎(40组件 + numpy/torch)并发时偶发 SIGSEGV (exit 139)。
  uvicorn 走 asyncio ProactorEventLoop, 避开 select 崩溃点。
"""
import asyncio, json, logging, os, sys
from pathlib import Path

logger = logging.getLogger("tmm.web")

sys.path.insert(0, str(Path(__file__).parent))
os.chdir(str(Path(__file__).parent))

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from starlette.responses import Response

from config.settings import DATA_DIR, PROJECT_ROOT
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline

app = FastAPI(title="Tiger.M.M Web UI", version="4.2.0")
pipeline = None
model_client = None

UI_FILE = Path(__file__).parent / "ui" / "app.html"
DATA_FILE = Path(__file__).parent / "ui" / "data.html"   # ★ 2026-09-21: 只读数据面
UI_FILE_LEGACY = Path(__file__).parent / "ui" / "index.html"


def _ui_path():
    """优先三模式新界面 ui/app.html, 缺省回落到 ui/index.html。"""
    return UI_FILE if UI_FILE.exists() else UI_FILE_LEGACY


@app.get("/")
def index():
    html = _ui_path().read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


def _mode_payload(result: dict) -> dict:
    """三模式字段 — 拼进 SSE done 事件。"""
    try:
        return {
            "mode": result.get("mode") or (pipeline.modes.get() if pipeline else "craft"),
            "intent": result.get("intent", ""),
            "pending_confirm": bool(result.get("pending_confirm")),
            "plan": result.get("plan") or [],
            "plan_message": result.get("plan_message", ""),
            # ★ P1: 胜出路由 —— 一行回答"谁吃了我这句话" (见 core/routes.py)
            "route": result.get("route", ""),
        }
    except Exception:
        return {"mode": "craft"}


@app.get("/dash")
def dash_page():
    """只读**数据面**页面 (产物 / 专家 / 审计)。

    ★ 2026-09-21 加: 界面按用户的黑金标准做 (背景 #0a0502 / 主色 #FFAC02 / 无白底),
      与既有 ui/app.html **互不影响** (那条链一个字没动)。
      数据全部来自本机引擎自己的只读接口, 页面本身不写任何状态。
    """
    try:
        html = DATA_FILE.read_text(encoding="utf-8")
    except Exception as e:
        return HTMLResponse(f"<pre>data.html 读取失败: {e}</pre>", status_code=500)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/modes")
def modes_list():
    """三模式清单 + 当前模式。"""
    try:
        cur = pipeline.modes.info()
        return {"current": cur["mode"], "info": cur, "modes": pipeline.modes.all(),
                "order": cur.get("order", ["ask", "plan", "craft"]),
                "pending": bool(pipeline.modes.get_pending())}
    except Exception as e:
        return {"current": "craft", "modes": {}, "error": str(e)}


@app.post("/mode")
async def mode_set(request: Request):
    """切换运行模式: {"mode": "ask"|"plan"|"craft"}"""
    try:
        data = await request.json() or {}
        m = (data.get("mode") or "").strip().lower()
        if not pipeline.modes.set(m):
            return {"ok": False, "error": pipeline.modes.set_error()}
        info = pipeline.modes.info()
        return {"ok": True, "current": info["mode"], "info": info}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/chat")
async def chat(request: Request):
    """Async endpoint — direct await on pipeline.process()."""
    data = await request.json()
    msg = data.get("message", "")
    ext_model = data.get("model", "auto")
    if ext_model == "auto":
        ext_model = None

    # ★ probe: 验证/探针流量 —— 不写任何用户历史, 避免测试污染界面
    # 注意: **不要**在这里写 user 记忆 —— pipeline.process() 已经写了 (覆盖 CLI/Web/relay 全部入口),
    # 这里再写一遍会让每条用户消息在历史里出现两次 (实测: 3 条消息 → 6 条 user 记录)。
    probe = bool(data.get("probe"))

    # ★ 三模式: 前端选中的模式直接生效 (ask / plan / craft)
    # 🔴 2026-09-19 修 (真缺陷): 原来**不看 probe** → 探针流量带 mode 字段就把
    #   data/mode.json 改了(实测: probe+mode=ask → 落盘 ask → 之后所有请求都走问答模式;
    #   这正是"验证污染用户状态"那类 bug 在 web 层的漏网)。probe 必须零副作用。
    _m = (data.get("mode") or "").strip().lower()
    if _m in ("ask", "plan", "craft") and not probe:
        try:
            pipeline.modes.set(_m)
        except Exception:
            pass

    async def sse_gen():
        q = asyncio.Queue()

        def on_progress(ev):
            try:
                q.put_nowait(ev)
            except Exception:
                pass

        pipeline.gateway.set_progress_cb(on_progress)
        task = asyncio.create_task(pipeline.process(msg, ext_model=ext_model, probe=probe))
        try:
            # 先实时推送工具调用进度，直到 process 完成
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=0.2)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    if task.done():
                        break
            try:
                result = await task
            except Exception as e:
                result = {"response": f"处理出错: {e}", "error": str(e), "model": "local", "elapsed": 0, "trace": []}
        finally:
            pipeline.gateway.set_progress_cb(None)

        try:
            if not probe:
                pipeline.memory.add_turn("assistant", result.get("response", ""))
        except Exception:
            pass

        model = result.get("model", "local")
        elapsed = result.get("elapsed", 0)
        trace = result.get("trace", [])
        text = result.get("response", "")
        error = result.get("error", "")

        if error:
            _pe = {"error": error}
            _pe.update(_mode_payload(result))
            yield f"data: {json.dumps(_pe, ensure_ascii=False)}\n\n"
            return

        _done = {"text": text, "done": True, "model": model, "elapsed": elapsed, "trace": trace}
        _done.update(_mode_payload(result))
        yield f"data: {json.dumps(_done, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


@app.get("/history")
def history():
    """界面"恢复历史"—— ★ 2026-09-19 改: 读**数据库**, 不再读内存。

    原实现读 pipeline.memory.recent(50) (纯内存 deque), 于是关掉引擎历史就没了
    (实测: 桌面端聊了 10 条, 重启后 /history 返回 0)。现在从 chat_sessions.db
    取最近 50 条 (跨会话), 并映射成前端要的 {role, text}。
    库读失败时退回内存 (降级可用, 并记日志)。
    """
    try:
        turns = pipeline.sessions.recent_all(50) if pipeline else []
        return {"history": [{"role": t.get("role", ""), "text": t.get("content", "")}
                            for t in turns]}
    except Exception as e:
        logger.warning("history from db failed, fallback to memory: %s", e)
        try:
            return {"history": pipeline.memory.recent(50) if pipeline else []}
        except Exception:
            return {"history": []}


@app.get("/inbox")
def inbox_list():
    try:
        from core.inbox import get_inbox

        items = get_inbox().list_pending()
        return {"count": len(items), "items": [i.to_dict() for i in items]}
    except Exception as e:
        return {"count": 0, "items": [], "error": str(e)}


@app.post("/inbox/resolve")
async def inbox_resolve(request: Request):
    try:
        from core.inbox import (
            RESOLVE_ALLOW,
            RESOLVE_DENY,
            RESOLVE_ALWAYS,
            get_inbox,
        )

        data = await request.json() or {}
        item_id = data.get("item_id", "")
        resolution = data.get("resolution", "allow")
        if resolution not in (RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS):
            return {"resolved": False, "error": "invalid resolution"}
        inbox = get_inbox()
        item = inbox.get(item_id)
        if item is None:
            return {"resolved": False, "error": "item not found"}
        ok = inbox.resolve(item_id, resolution)
        if not ok:
            return {"resolved": False, "error": "resolve failed (already resolved?)"}

        executed = []
        # 允许 → 真执行命令 + 结果回喂 memory（CLI y/n 执行逻辑搬到 web）
        if resolution == RESOLVE_ALLOW and item.cmds:
            import subprocess
            all_out = ""
            for ci, cmd in enumerate(item.cmds, 1):
                try:
                    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                       timeout=60, encoding='gbk', errors='replace')
                    out = (r.stdout.strip() or r.stderr.strip())
                    executed.append({"cmd": cmd[:120], "ok": r.returncode == 0, "output": out[:500]})
                    all_out += f"[{ci}] {'OK' if r.returncode == 0 else 'FAIL'} {out[:500]}\n"
                except Exception as e:
                    executed.append({"cmd": cmd[:120], "ok": False, "output": str(e)[:200]})
                    all_out += f"[{ci}] FAIL {e}\n"
            try:
                pipeline.memory.add_turn("assistant", f"[Inbox 执行结果]\n{all_out}")
            except Exception:
                pass
        return {"resolved": True, "executed": executed}
    except Exception as e:
        return {"resolved": False, "error": str(e)}


@app.get("/image")
def image(path: str = ""):
    try:
        from storage.file_secure import safe_path

        p = path
        if not p.lower().endswith(("png", "jpg", "jpeg", "gif", "bmp", "webp")):
            return JSONResponse({"error": "not an image"}, status_code=400)
        target = safe_path(p, PROJECT_ROOT)
        if not target.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(target))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/stats")
def stats():
    try:
        import time as _t

        uptime = int(_t.time() - pipeline._startup) if pipeline and hasattr(pipeline, "_startup") else 0
        total = pipeline.stats.get("total", 0) if pipeline else 0
        errors = pipeline.stats.get("errors", 0) if pipeline else 0
        sessions = pipeline.sm.stats().get("total", 0) if pipeline else 0
        from core.inbox import get_inbox

        pending = get_inbox().stats().get("pending", 0)
        return {
            "uptime": uptime,
            "total": total,
            "errors": errors,
            "pending": pending,
            "sessions": sessions,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/artifacts")
def artifacts_list(limit: int = 30, kind: str = "", scan: int = 0, days: int = 1):
    """产物/交付清单 (只读) —— 借鉴通用 Agent 平台的 "Files 产物面"。

    2026-09-20 加。回答"今天产出了什么 / 成品在哪": 默认返回**登记过**的产物;
    `?scan=1` 时附带**目录盘点**(桌面/项目 reports,tmp, 最近 days 天), 未登记也能看到。

    ★ 只读: 不移动/不删除任何文件。已不在盘上的记录会被过滤 (不给过期结论)。
    """
    try:
        from core import artifacts as A
        recs = [A.public_rec(r) for r in A.list_recent(limit=max(1, min(int(limit or 30), 200)),
                                                       kind=kind or "")]
        out = {"count": len(recs), "items": recs, "stats": A.stats()}
        if scan:
            try:
                out["scanned"] = A.scan_dirs(days=max(0, int(days or 1)), limit=30)
            except Exception as e:
                out["scanned_error"] = f"{type(e).__name__}: {e}"
        return out
    except Exception as e:
        logger.warning("/artifacts failed: %s", e)
        return {"error": f"{type(e).__name__}: {e}", "count": 0, "items": []}


@app.get("/experts")
def experts_list(name: str = ""):
    """角色专家清单 (只读) —— 借鉴通用 Agent 平台的 "Experts 面"。

    2026-09-20 加。`?name=数据分析` 时返回该专家**会注入什么**(便于核对人设/输出规范)。
    ★ 只读: 专家是 experts/*.md 纯文本配置, 本接口不改任何状态。
    """
    try:
        from core import experts as E
        allx = E.load_all()
        if name:
            rec = allx.get(name.lstrip("@"))
            if not rec:
                return {"error": f"没有叫 '{name}' 的专家", "available": sorted(allx),
                        "count": len(allx)}
            return {"name": name, "block": E.system_block(rec),
                    "persona": rec.get("persona", ""), "output": rec.get("output", ""),
                    "skills": rec.get("skills", []), "tools": rec.get("tools", [])}
        return {"count": len(allx),
                "items": [{"name": n, "aliases": allx[n].get("aliases", []),
                           "persona": str(allx[n].get("persona", ""))[:200],
                           "tools": allx[n].get("tools", [])} for n in sorted(allx)]}
    except Exception as e:
        logger.warning("/experts failed: %s", e)
        return {"error": f"{type(e).__name__}: {e}", "count": 0, "items": []}


@app.get("/audit")
def audit_tail(n: int = 20):
    """工具调用审计尾巴 (只读) —— 借鉴架构图的 "Permission / Task" 可视面。

    2026-09-20 加。数据来自 ToolGateway 的审计环形缓冲(最近 500 条)+ 汇总统计:
    "刚才引擎调了哪些工具、成没成、耗时多少、有没被限流".
    ★ 只读: 只读内存里的审计记录, 不改状态、不触发任何工具。
    """
    try:
        n = max(1, min(int(n or 20), 200))
        gw = getattr(pipeline, "gateway", None) if pipeline else None
        if gw is None:
            return {"error": "引擎未就绪 (没有 gateway)", "entries": []}
        out = {"entries": gw.audit_tail(n)}
        try:
            out["stats"] = gw.stats()
        except Exception:
            pass
        return out
    except Exception as e:
        logger.warning("/audit failed: %s", e)
        return {"error": f"{type(e).__name__}: {e}", "entries": []}


def main():
    global pipeline, model_client
    config = {}
    keys_file = DATA_DIR / "keys.json"
    leg_keys = Path(os.getcwd()) / "keys.json"
    for kf in [keys_file, leg_keys]:
        if kf.exists():
            try:
                data = json.loads(kf.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if isinstance(v, dict) and ("key" in v or "api_key" in v):
                        config[k] = v
            except Exception:
                pass

    model_client = ModelClient(config)
    pipeline = Level4Pipeline(model_client, config)

    import uvicorn

    print("Tiger.M.M Web UI -> http://localhost:8800 (uvicorn)")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8800, log_level="warning")
    finally:
        # 退出收口: 关掉 MCP 子进程 (uvicorn 停时 shutdown 事件也会兜一次, 这里再保一次)
        try:
            if pipeline is not None:
                asyncio.run(pipeline.aclose_resources())
        except Exception:
            pass


if __name__ == "__main__":
    main()
