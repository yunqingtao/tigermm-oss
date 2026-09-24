"""
CommandHandler — fast-path commands extracted from pipeline.py.
Handles /voice, /stats, /cost, /hive, /tool before the main routing chain.
"""
import asyncio, json, logging, os, re, time, tempfile
from pathlib import Path

logger = logging.getLogger("core.command_handler")

GOLD = '\033[33m'

def _fmt(ts):
    """Format timestamp to readable string."""
    if not ts: return "?"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return str(ts)[:16]
RST = '\033[0m'


class CommandHandler:
    """Handles slash-commands and fast-path intercepts."""

    def __init__(self, pipeline):
        self.p = pipeline  # reference back to Level4Pipeline
        self._wm = None    # whisper model cache

    def is_command(self, message: str) -> bool:
        """Check if message is a handled command."""
        msg = message.strip()
        return msg in ('/voice', '/stats', '/cost', '/stats-old') or \
               msg.startswith('/tool ') or msg.startswith('/hive') or \
               msg.startswith('/remember') or msg.startswith('/memory') or msg.startswith('/reload') or msg.startswith('/changelog') or msg.startswith('/changes')

    def is_memory_intent(self, msg: str) -> str | None:
        """Detect natural-language memory queries. Returns 'query' or 'store' or None."""
        msg_lower = msg.lower().strip()
        query_patterns = ['长期记忆', '记忆里有什么', '记住的东西', '查记忆', '我记了什么', '有什么记忆', '我的记忆', '列出记忆']
        store_patterns = ['记住', '记下来', '写入记忆']
        if any(p in msg_lower for p in query_patterns):
            return 'query'
        if any(p in msg_lower for p in store_patterns):
            return 'store'
        return None

    def is_nl_intent(self, msg: str) -> str | None:
        """Detect natural-language intent → map to slash command.
        Returns cmd name or None."""
        m = msg.strip()
        # Backups list (checked first: "备份列表" must not be eaten by bare "备份")
        if any(p in m for p in ['备份列表', '有哪些备份', '列出备份', '看看备份', '显示备份']):
            return '/backups'
        # Backup create (bare "备份" now matches too)
        if any(p in m for p in ['备份', '备份一下', '备份项目', '做个备份', '创建备份', '搞个备份', '先备份', '备份当前']):
            return '/backup'
        # Changelog
        if any(p in m for p in ['有什么变更', '改了什么东西', '变更列表', '改了什么', '刚才改了什么', '有什么改动']):
            return '/changelog'
        # Reload
        if any(p in m for p in ['清缓存', '重新加载', '刷新代码', '清一下缓存']):
            return '/reload'
        return None

    async def handle(self, message: str, t0: float, ext_model=None) -> dict | None:
        """Handle command. Returns result dict or None if not a command."""
        msg = message.strip()

        if msg == '/voice':
            return await self._voice(t0, ext_model)
        if msg in ('/stats', '/stats-old'):
            return self._stats(t0)
        if msg == '/cost':
            return self._cost(t0)
        if msg.startswith('/tool '):
            return await self._tool(msg, t0)
        if msg.startswith('/hive'):
            return await self._hive(msg, t0)
        if msg.startswith('/remember'):
            return await self._remember(msg, t0)
        if msg.startswith('/memory'):
            return await self._memory(msg, t0)
        if msg.startswith('/reload'):
            return await self._reload(t0)
        if msg.startswith('/changelog'):
            return await self._changelog(t0)

        if msg == '/changes':
            return await self._changes(t0)
        return None

    # ── /voice ──
    async def _voice(self, t0: float, ext_model=None) -> dict:
        t0v = time.time()
        try:
            import sounddevice as sd, soundfile as sf, numpy as np, whisper as wh
        except ImportError as e:
            return {"response": f"缺依赖:{e}", "intent": "voice", "model": "local", "elapsed": 0.1}

        try:
            print("🎤 录音 5s", flush=True)
            audio = sd.rec(int(5 * 16000), samplerate=16000, channels=1, dtype='float32')
            for i in range(4, 0, -1):
                time.sleep(1)
                print(f"🎤 录音中 {i}", flush=True)
            sd.wait()
            print("🎤 识别中...", flush=True)
            fp = os.path.join(tempfile.gettempdir(), f"tmm_v_{int(time.time())}.wav")
            sf.write(fp, audio, 16000)
            if self._wm is None:
                self._wm = wh.load_model("small")
            r = self._wm.transcribe(fp, language="zh")
            t = (r.get("text", "") or "").strip()
            if not t:
                return {"response": "没听到声音", "intent": "voice", "model": "local",
                        "elapsed": round(time.time() - t0v, 2)}
            # Show what was recognized
            print(f"🎤 你说: {t}", flush=True)
            # Quick intent check for tool annotation
            try:
                if hasattr(self.p, 'intent_router') and self.p.intent_router:
                    cl = self.p.intent_router.classify(t)
                    actions = cl.get('actions', [])
                    if actions:
                        action_names = {'search':'搜索','check_mail':'查邮件','send_email':'发邮件',
                                       'file_read':'读文件','file_write':'写文件','screenshot':'截图',
                                       'weather':'查天气','system_info':'系统状态'}
                        cn = action_names.get(actions[0], actions[0])
                        print(f"  → {cn}", flush=True)
            except Exception:
                pass
            # Recurse into process() with transcribed text
            inner = await self.p.process(t, ext_model=ext_model)
            rt = inner.get("response", str(inner))
            # TTS
            try:
                import edge_tts
                from playsound import playsound
                cl = re.sub(r'[\U0001F300-\U0001F9FF]', '', rt[:800])
                cl = re.sub(r'[*_#>`|]', '', cl)
                cl = re.sub(r'\n{2,}', '\u3002', cl).replace('\n', '\uff0c').strip()
                t2 = os.path.join(tempfile.gettempdir(), f"tmm_tts_{int(time.time())}.mp3")
                for attempt in range(3):
                    try:
                        await edge_tts.Communicate(cl, "zh-CN-YunxiNeural").save(t2)
                        if os.path.getsize(t2) > 1000:
                            break
                        os.remove(t2)
                    except Exception:
                        if attempt < 2:
                            await asyncio.sleep(1)
                        else:
                            raise
                playsound(t2)
                os.remove(t2)
            except Exception as e:
                logger.warning("TTS: %s", e)
            return {"response": f"你说: {t}\n\n{rt}", "intent": "voice", "model": "local",
                    "elapsed": round(time.time() - t0v, 2)}
        except Exception as e:
            logger.error("Voice: %s", e)
            return {"response": f"语音失败: {e}", "intent": "voice", "model": "local",
                    "elapsed": round(time.time() - t0v, 2)}

    # ── /stats ──
    def _stats(self, t0: float) -> dict:
        total = self.p.stats.get('total', 0)
        errors = self.p.stats.get('errors', 0)
        uptime = time.time() - self.p._startup
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)
        return {"response": f"运行: {h}h{m}m{s}s | 请求: {total} | 错误: {errors}",
                "intent": "stats", "model": "local", "elapsed": round(time.time() - t0, 3)}

    # ── /cost ──



    # ── /changelog ──
    async def _changelog(self, t0: float) -> dict:
        """List .py files modified since TMM startup (external changes)."""
        import os as _os, time as _time
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        # Read or create startup timestamp
        ts_file = _os.path.join(root, 'data', 'last_startup.txt')
        if _os.path.exists(ts_file):
            startup_ts = float(open(ts_file).read().strip())
        else:
            startup_ts = _time.time()
            _os.makedirs(_os.path.dirname(ts_file), exist_ok=True)
            with open(ts_file, 'w') as f:
                f.write(str(startup_ts))
        # Find modified .py files
        changed = []
        for r, ds, fs in _os.walk(root):
            ds[:] = [d for d in ds if not any(x in d.lower() for x in ('__pycache__', 'backup', '.git', 'data', 'node_modules'))]
            for f in fs:
                if f.endswith('.py'):
                    fp = _os.path.join(r, f)
                    mt = _os.path.getmtime(fp)
                    if mt > startup_ts:
                        rel = _os.path.relpath(fp, root)
                        size = _os.path.getsize(fp)
                        ts = _time.strftime("%H:%M", _time.localtime(mt))
                        changed.append((rel, ts, size))
        if not changed:
            return {"response": "启动后无外部 .py 文件变更", "intent": "changelog", "model": "local", "elapsed": round(_time.time() - t0, 3)}
        changed.sort()
        lines = [f"启动后变更 ({len(changed)}个文件):"]
        for rel, ts, size in changed[:30]:
            lines.append(f"  {ts}  {rel}  ({size/1024:.1f}KB)")
        if len(changed) > 30:
            lines.append(f"  ... 等共{len(changed)}个文件")
        return {"response": "\n".join(lines), "intent": "changelog", "model": "local", "elapsed": round(_time.time() - t0, 3)}


    # ── /changes (session diff) ──
    async def _changes(self, t0: float) -> dict:
        """Show files changed this session vs startup snapshot."""
        import os as _os, time as _time
        from core.session_tracker import get_session_changes, format_session_report
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        changes = get_session_changes(root)
        report = format_session_report(changes)
        return {"response": report, "intent": "changes",
                "model": "local", "elapsed": round(_time.time() - t0, 3)}
    # ── /reload ──
    async def _reload(self, t0: float) -> dict:
        """Clear pycache and report changed files since last reload."""
        import os as _os, shutil, time as _time
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        count = 0
        for r, ds, fs in _os.walk(root):
            if '__pycache__' in ds:
                try:
                    shutil.rmtree(_os.path.join(r, '__pycache__'))
                    count += 1
                except Exception: pass
        # Find recently modified .py files
        recent = []
        cutoff = _time.time() - 3600  # last hour
        for r, ds, fs in _os.walk(root):
            ds[:] = [d for d in ds if 'backup' not in d and '__pycache__' not in d and not d.startswith('.')]
            for f in fs:
                if f.endswith('.py'):
                    fp = _os.path.join(r, f)
                    try:
                        mt = _os.path.getmtime(fp)
                        if mt > cutoff:
                            recent.append(_os.path.relpath(fp, root))
                    except Exception: pass
        lines = [f"已清除 {count} 个 __pycache__ 目录"]
        if recent:
            lines.append(f"最近1小时变更({len(recent)}个文件):")
            for f in sorted(recent)[:15]:
                lines.append(f"  {f}")
        lines.append("\n⚠ 需退出重启才能加载新代码。")
        return {"response": "\n".join(lines), "intent": "reload", "model": "local", "elapsed": round(_time.time() - t0, 3)}

    # ── /memory ──
    async def _memory(self, msg: str, t0: float) -> dict:
        """List/search/stats long-term memory. Usage: /memory [keyword|stats]"""
        keyword = msg[len('/memory'):].strip()
        try:
            import sqlite3 as _sql
            from config.settings import DATA_DIR
            db_path = Path(str(DATA_DIR)) / "long_term.db"
            conn = _sql.connect(str(db_path))
            
            if keyword == "stats":
                row = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT category), "
                    "MIN(first_seen), MAX(first_seen) FROM facts WHERE active=1"
                ).fetchone()
                conn.close()
                return {"response": f"记忆统计: {row[0]}条活跃, {row[1]}个分类 | "
                    f"最早:{_fmt(row[2])} 最新:{_fmt(row[3])}",
                    "intent": "memory", "model": "local",
                    "elapsed": round(time.time() - t0, 3)}
            
            if keyword:
                rows = conn.execute(
                    "SELECT category, content, first_seen FROM facts WHERE content LIKE ? AND active=1 ORDER BY id DESC LIMIT 10",
                    (f"%{keyword}%",)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT category, content, first_seen FROM facts WHERE active=1 ORDER BY id DESC LIMIT 10"
                ).fetchall()
            conn.close()
            if not rows:
                return {"response": "记忆为空" if not keyword else f"未找到包含'{keyword}'的记忆", "intent": "memory", "model": "local", "elapsed": round(time.time() - t0, 3)}
            lines = [f"{'='*40}"]
            for cat, content, ts in rows:
                ts_str = _fmt(ts) if ts else "?"
                lines.append(f"[{cat}] {ts_str} | {content[:120]}")
            return {"response": "\n".join(lines), "intent": "memory", "model": "local", "elapsed": round(time.time() - t0, 3)}
        except Exception as e:
            return {"response": f"记忆查询失败: {e}", "intent": "memory_err", "model": "local", "elapsed": round(time.time() - t0, 3)}

    # ── /remember ──
    async def _remember(self, msg: str, t0: float) -> dict:
        """Write a fact to long-term memory. Usage: /remember <内容>"""
        text = msg[len('/remember'):].strip()
        if not text:
            return {"response": "用法: /remember 要记住的内容\n示例: /remember pipeline.py 需要拆分", "intent": "remember", "model": "local", "elapsed": 0}
        try:
            import sqlite3 as _sql
            from config.settings import DATA_DIR
            db_path = Path(str(DATA_DIR)) / "long_term.db"
            conn = _sql.connect(str(db_path))
            conn.execute(
                "INSERT INTO facts (category, content, source_session, confidence, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                ("manual", text, "cli", 1.0, time.time(), time.time())
            )
            conn.commit()
            conn.close()
            # ── 同步提取关系进知识图谱 (perception relations, 走单例防竞态覆盖) ──
            try:
                from core.perception import get_perception
                _pe = get_perception()
                _rels = _pe._extract_relations(text)
                for _r in _rels:
                    _pe._merge_relation(_r)
            except Exception:
                pass
            return {"response": f"已记住: {text[:100]}", "intent": "remember", "model": "local", "elapsed": round(time.time() - t0, 3)}
        except Exception as e:
            return {"response": f"记忆写入失败: {e}", "intent": "remember_err", "model": "local", "elapsed": round(time.time() - t0, 3)}

    def _cost(self, t0: float) -> dict:
        from core.token_tracker import get_tracker
        summary = get_tracker().summary()
        return {"response": f"Token用量:\n{summary}",
                "intent": "cost", "model": "local", "elapsed": round(time.time() - t0, 3)}

    # ── /tool ──
    async def _tool(self, message: str, t0: float) -> dict:
        parts = message.strip().split(None, 2)
        if len(parts) < 2:
            return {"response": "用法: /tool <工具名> [参数]", "intent": "system",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}
        tool_name = parts[1]
        tool_input = parts[2] if len(parts) > 2 else ""
        try:
            if tool_name in ("run", "exec", "shell_exec"):
                result = await self.p.gateway.call("shell_exec", cmd=tool_input)
            else:
                call_kwargs = {}
                if "=" in tool_input:
                    for m in re.finditer(r'(\w+)=("([^"]+)"|\'([^\']+)\'|(\S+))', tool_input):
                        k = m.group(1)
                        v = m.group(3) or m.group(4) or m.group(5)
                        call_kwargs[k.strip()] = v
                if not call_kwargs:
                    call_kwargs["input"] = tool_input
                result = await self.p.gateway.call(tool_name, **call_kwargs)
            return {"response": str(result)[:5000], "intent": "tool", "model": "local",
                    "elapsed": round(time.time() - t0, 2), "code_rounds": 0}
        except Exception as e:
            return {"response": f"[Err] /tool {tool_name}: {e}", "intent": "error",
                    "model": "local", "elapsed": round(time.time() - t0, 2), "code_rounds": 0}

    # ── /hive ──
    async def _hive(self, message: str, t0: float) -> dict:
        parts = message.strip().split(None, 1)
        if len(parts) < 2:
            return {"response": "用法: /hive <复杂任务>", "intent": "system",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}
        try:
            sub = await self.p.hive_mind.decompose(parts[1])
            result = await self.p.hive_mind.execute(sub.get("sub_tasks", []))
            return {"response": self.p.hive_mind.format_summary(result), "intent": "hive",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}
        except Exception as e:
            return {"response": f"Hive: {e}", "intent": "error", "model": "local",
                    "elapsed": round(time.time() - t0, 2)}
