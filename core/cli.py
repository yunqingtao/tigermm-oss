"""
Tiger.M.M CLI — interactive terminal loop.
Extracted from pipeline.py for maintainability.
"""
import sys, os, time, logging, asyncio, re, subprocess, getpass

logger = logging.getLogger("core.cli")


def _cli_cred() -> tuple:
    """CLI 登录凭据 —— 从配置读, **不硬编码** (2026-09-20 分发整改)。

    读取顺序: 环境变量 TMM_CLI_USER / TMM_CLI_PASSWORD → keys.json["cli"]
    ★ 原来源码里是 `if user == "TMM" and pwd == "<明文口令>"`, 而本文件被 git 跟踪
      → 一旦发布, 登录口令就是公开的。读不到则**跳过登录**(本地单人工具的仪式门,
      不该硬塞一个通用口令给所有分发对象)。
    """
    import json as _json_cli
    u = os.environ.get("TMM_CLI_USER", "")
    pw = os.environ.get("TMM_CLI_PASSWORD", "")
    if not (u and pw):
        try:
            from config.settings import PROJECT_ROOT as _PR
            with open(_PR / "keys.json", encoding="utf-8") as f:
                d = _json_cli.load(f).get("cli") or {}
            u = u or str(d.get("user", "") or "")
            pw = pw or str(d.get("password", "") or "")
        except Exception:
            pass
    return u, pw


# Re-import colors from pipeline
from core.pipeline import (
    GOLD, DARK, DIM, BOLD, RST, CYAN, GREEN, RED, CLEAR, FRAMEWORK_TEXT,
    _rule
)

async def run_cli(pipeline_obj, model_client_obj, startup_report: str = ""):
    """Tiger.M.M CLI — built into pipeline."""
    if sys.platform == "win32":
        import ctypes
        try: ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception: pass
    
    print(FRAMEWORK_TEXT)
    logger.info("CLI started — pid=%d", os.getpid())

    # Tool list — two columns, CN/EN
    # Auth
    import sys as _sys
    authed = False
    # ★ 2026-09-20 分发整改: 登录凭据**从配置读**, 不再明文写在源码里
    #   (原来 L57 是 `if user == "TMM" and pwd == "<明文>"` —— 而本文件被 git 跟踪,
    #    发布即等于把登录口令公开)。
    #   读取顺序: 环境变量 TMM_CLI_USER/TMM_CLI_PASSWORD → keys.json["cli"]
    #   都没配 → **跳过登录**(本地单人工具, 这个门本来就是仪式感, 不该硬塞一个通用口令)。
    _cu, _cp = _cli_cred()
    if not (_cu and _cp):
        print(f"{GOLD}（未配置登录凭据 —— 已跳过登录。要开启: 在 keys.json 里加 "
              f"\"cli\": {{\"user\": \"TMM\", \"password\": \"你的口令\"}}）{RST}")
        authed = True
    for _ in range(3 if not authed else 0):
        _sys.stdout.write(f"{GOLD}用户名:{RST} ")
        _sys.stdout.flush()
        user = input().strip()
        _sys.stdout.write(f"{GOLD}密码:  {RST}")
        _sys.stdout.flush()
        # Read password with * echo
        pwd = ""
        if _sys.platform == "win32":
            import msvcrt
            while True:
                ch = msvcrt.getch()
                if ch == b'\r' or ch == b'\n':
                    print()
                    break
                elif ch == b'\x08':  # backspace
                    if pwd:
                        pwd = pwd[:-1]
                        _sys.stdout.write('\b \b')
                        _sys.stdout.flush()
                elif ch == b'\x03':  # Ctrl+C
                    raise KeyboardInterrupt
                else:
                    pwd += ch.decode('utf-8', errors='replace')
                    _sys.stdout.write('*')
                    _sys.stdout.flush()
        else:
            pwd = input()
        if user == _cu and pwd == _cp:
            authed = True
            print()
            break
        print(f"      {GOLD}用户名或密码错误{RST}")
    if not authed:
        print(f"      {GOLD}登录失败{RST}")
        return

    _rule("Tiger")
    print()
    # ── Startup hooks: show status + start remote bridge ──
    try:
        from core.cli_hooks import on_startup
        on_startup()
    except Exception:
        pass
    
    # Init bridge protocol with background processor
    try:
        from core.bridge_protocol import init as bp_init, poll as bp_poll, write_response as bp_write
        from config.settings import PROJECT_ROOT
        import threading as _thr, asyncio as _asyncio
        bp_init(str(PROJECT_ROOT))
        
        def _bridge_processor():
            loop = _asyncio.new_event_loop()
            while True:
                try:
                    for sender, text, msg_id in bp_poll():
                        print(f"\n{DIM}[bridge] ← {sender[:12]}: {text[:40]}{RST}")
                        try:
                            result = loop.run_until_complete(pipeline_obj.process(text))
                            resp = result.get("response", str(result)) if isinstance(result, dict) else str(result)
                            bp_write(sender, resp, msg_id)
                            print(f"{DIM}[bridge] → replied{RST}")
                        except Exception as e:
                            bp_write(sender, f"[TMM] 处理失败: {e}", msg_id)
                            print(f"{DIM}[bridge] → error: {e}{RST}")
                except Exception:
                    pass
                time.sleep(1)
        _thr.Thread(target=_bridge_processor, daemon=True).start()
    except Exception:
        pass
    
    current_model = "auto"
    # ★ 2026-09-20: 显式初始化。原来只在分支里赋值, 读的时候靠 `'_pending_inbox' in dir()` 兜
    #   —— 运行时安全但很脆 (静态检查看不到, 后人一挪代码就可能踩 NameError)。显式置 None 等价。
    _pending_inbox = None

    while True:
        model_tag = f" {DIM}@{current_model}{RST}" if current_model != "auto" else ""
        try:
            _mi = pipeline_obj.modes.info()
            _mode_tag = f" {GOLD}[{_mi['cn']}]{RST}"
        except Exception:
            _mode_tag = ""
        prompt = f"{GOLD}Tiger.M.M{_mode_tag}{model_tag} >>>{RST} "
        try:
            # 🔴 input() 放子线程 — 否则阻塞主线程=阻塞 main_loop,
            # 手机端 http_bridge 的 run_coroutine_threadsafe 永不执行 → "没反应"
            raw = (await asyncio.to_thread(input, prompt)).strip()
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
            # Auto backup on exit
            try:
                from core.backup import get_backup
                result = get_backup().create("exit")
                # Save session context
                try:
                    from core.session_recovery import save
                    from pathlib import Path
                    msgs = []
                    if hasattr(pipeline_obj, 'get_last_turns'):
                        msgs = pipeline_obj.get_last_turns(6)
                    save(str(Path(__file__).parent.parent), msgs)
                except Exception:
                    pass
                if result:
                    print(f"{DIM}[backup] {result['name']} {result['size_mb']}MB{RST}")
            except Exception:
                pass
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
  {GOLD}/mode{RST}       运行模式     {GOLD}/ask{RST} /plan /craft  切模式
  {GOLD}/help{RST}       显示帮助     {GOLD}/voice{RST}       语音输入
  {GOLD}/backup{RST}      手动备份     {GOLD}/backups{RST}     备份列表
  {GOLD}/memory{RST}      记忆查询     {GOLD}/remember xxx{RST} 写入记忆
  {GOLD}/changes{RST}     会话变更     {GOLD}/changelog{RST}   外部变更
  {GOLD}/reload{RST}      清缓存       {GOLD}/stats{RST}       运行统计
  {GOLD}/cost{RST}        Token用量    {GOLD}/tasks{RST}       任务列表
  {GOLD}/plugin{RST}   插件注册表管理
  {GOLD}/regex{RST}     正则快捷通道开关
  {GOLD}/state{RST}     执行前铺状态开关 (默认关; /state on|off)
  {GOLD}/stats{RST}     引擎状态统计
  {GOLD}/tasks{RST}     列出所有任务会话
  {GOLD}/delegate{RST}  拆解+并行委派
  {GOLD}/spawn{RST}     后台执行任务
  {GOLD}/inbox{RST}     查看审批队列
  {GOLD}/allow{RST}     批准审批项
  {GOLD}/deny{RST}      拒绝审批项
  {GOLD}/cron{RST}     定时任务管理
  {GOLD}/hive{RST}     蜂巢多模型协作
  {GOLD}/task{RST}      查看任务输出
  {GOLD}/search{RST}    搜索对话历史
  {GOLD}/sessions{RST}  列出历史会话
  {GOLD}/exit{RST}      退出
  {GOLD}/backup{RST}    手动备份
  {GOLD}/backups{RST}   备份列表

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
            try:
                from core.backup import get_backup
                result = get_backup().create("manual")
                if result:
                    print(f"{GREEN}[backup] {result['name']}{RST} {result['size_mb']}MB, {result['file_count']}files, {result['elapsed_s']}s")
                else:
                    print(f"{RED}[backup] 失败{RST}")
            except Exception as e:
                print(f"{RED}[backup] 异常: {e}{RST}")
            continue

        if user_input.strip() == '/backups':
            try:
                from core.backup import get_backup
                items = get_backup().list_backups()
                if items:
                    print(f"\n{GOLD}═══ 备份列表（保留最近10个）═══{RST}")
                    for b in items:
                        print(f"  {GREEN}{b['name']}{RST} {b['size_mb']}MB {DIM}{b['created']}{RST}")
                else:
                    print(f"{DIM}暂无备份{RST}")
            except Exception as e:
                print(f"{RED}[backups] 异常: {e}{RST}")
            continue

        if user_input.startswith('/delegate '):
            goal = user_input[10:].strip()
            if goal:
                mgr = getattr(pipeline_obj, 'task_mgr', None)
                if mgr:
                    print(f"{GOLD}[delegate] 拆解任务...{RST}")
                    result = await mgr.run(goal)
                    print(f"{GREEN}[delegate] {result['task_id']}{RST} "
                          f"{result['completed']}/{result['subtask_count']} done, "
                          f"{result['failed']} failed")
                    print(result['result'][:2000])
                else:
                    print(f"{DIM}TaskManager 未初始化{RST}")
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

        if user_input.strip() == '/cron' or user_input.startswith('/cron '):
            sch = getattr(pipeline_obj, 'scheduler', None)
            if sch:
                sub = user_input.strip()[5:].strip()
                if sub and sub not in ('list',) and not sub.startswith('add ') and not sub.startswith('rm '):
                    print(f"{DIM}用法: /cron add <schedule> <name> <prompt> | /cron list | /cron rm <id>{RST}")
                    continue
                jobs = sch.list_jobs()
                if jobs:
                    print(f"\n{GOLD}═══ 定时任务 — {len(jobs)} jobs ═══{RST}")
                    for j in jobs:
                        icon = "●" if j['enabled'] else "○"
                        print(f"  {icon} {GOLD}{j['id']}{RST} {j['name']}")
                        print(f"    {DIM}schedule={j['schedule']} | runs={j['run_count']} | last={j['last_run'] or 'never'}{RST}")
                else:
                    print(f"{DIM}  暂无定时任务。用法: /cron add 每早8点 搜索AI新闻发给老王{RST}")
            else:
                print(f"{DIM}Scheduler 未初始化{RST}")
            continue

        if user_input.startswith('/cron add '):
            sch = getattr(pipeline_obj, 'scheduler', None)
            if sch:
                args = user_input[10:].strip()
                parts = args.split(None, 2)
                if len(parts) >= 3:
                    sched, name, prompt = parts[0], parts[1], parts[2]
                    job = sch.add(name=name, schedule=sched, prompt=prompt)
                    print(f"{GREEN}[cron] {job.id}{RST} {name} ({sched})")
                elif len(parts) == 2:
                    name, prompt = parts[0], parts[1]
                    job = sch.add(name=name, schedule="every 1h", prompt=prompt)
                    print(f"{GREEN}[cron] {job.id}{RST} {name} (every 1h)")
                else:
                    print(f"{DIM}用法: /cron add <schedule> <name> <prompt>{RST}")
                    print(f"{DIM}  schedule: 'every 30m' | 'every 2h' | '0 9 * * *' | ISO时间戳{RST}")
            else:
                print(f"{DIM}Scheduler 未初始化{RST}")
            continue

        if user_input.startswith('/cron rm '):
            sch = getattr(pipeline_obj, 'scheduler', None)
            if sch:
                jid = user_input[9:].strip()
                if sch.remove(jid):
                    print(f"{GREEN}[cron] removed {jid}{RST}")
                else:
                    print(f"{DIM}  任务 {jid} 不存在{RST}")
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

        if user_input.strip() == '/plugin' or user_input.startswith('/plugin '):
            reg = getattr(pipeline_obj, 'plugin_registry', None)
            if reg:
                sub = user_input.strip()[7:].strip()
                if sub and sub not in ('list', 'reload') and not sub.startswith('install ') and not sub.startswith('remove ') and sub != 'check':
                    print(f"{DIM}用法: /plugin list | reload | install <pkg> | remove <name> | check{RST}")
                    continue
                if sub == 'reload':
                    if hasattr(pipeline_obj, 'plugins'):
                        new = pipeline_obj.plugins.reload()
                        reg._scan()
                        print(f"{GREEN}[plugin] 重新扫描: {new} 个插件{RST}")
                    continue
                plugins = reg.list_plugins()
                print(f"\n{GOLD}═══ 插件注册表 — {len(plugins)} plugins ═══{RST}")
                for p in plugins:
                    icon = "◆" if p['builtin'] else "◇"
                    deps = f" [{', '.join(p['depends'])}]" if p['depends'] else ""
                    print(f"  {icon} {GOLD}{p['name']}{RST} v{p['version']}{deps}")
                print(f"\n  {DIM}/plugin check  — 检查依赖完整性{RST}")
                print(f"  {DIM}/plugin install <pkg>  — 安装插件{RST}")
                print(f"  {DIM}/plugin remove <name>  — 卸载插件{RST}")
            continue

        if user_input.startswith('/plugin install '):
            reg = getattr(pipeline_obj, 'plugin_registry', None)
            if reg:
                pkg = user_input[17:].strip()
                ok, msg = reg.install(pkg)
                print(f"{GREEN if ok else RED}[plugin] {msg}{RST}")
            continue

        if user_input.startswith('/plugin remove '):
            reg = getattr(pipeline_obj, 'plugin_registry', None)
            if reg:
                name = user_input[16:].strip()
                ok, msg = reg.remove(name)
                print(f"{GREEN if ok else RED}[plugin] {msg}{RST}")
            continue

        if user_input.strip() == '/plugin check':
            reg = getattr(pipeline_obj, 'plugin_registry', None)
            if reg:
                issues = reg.check()
                if issues:
                    print(f"\n{RED}  {len(issues)} issue(s):{RST}")
                    for i in issues:
                        print(f"  ✗ {i['plugin']}: {i['issue']}")
                else:
                    print(f"{GREEN}  所有插件依赖完整{RST}")
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

        # ── 执行前铺状态 开关 (默认关: 输出干净, 无"虎哥理解"行) ──
        if _mcmd == '/state' or _mcmd.startswith('/state '):
            _arg = _mcmd[6:].strip()
            if _arg in ('', 'on', 'off'):
                print(pipeline_obj.set_lay_state(_arg))
            else:
                print(f"{DIM}用法: /state | /state on | /state off{RST}")
            continue

        if user_input.startswith('@'):
            parts = user_input[1:].strip().split(None, 1)
            target = parts[0]
            if target in {m['id'] for m in pipeline_obj.list_models()} or target in ('auto', 'voice'):
                current_model = target
                if len(parts) > 1:
                    user_input = parts[1]  # process rest of message
                else:
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
        # Execute pending plan after confirmation
        if hasattr(pipeline_obj, '_pending_plan') and pipeline_obj._pending_plan:
            try:
                result = await pipeline_obj.plan_executor.execute(pipeline_obj._pending_plan)
                if result.get('success'):
                    print(pipeline_obj.plan_executor.format_summary(result))
                else:
                    print(f"  失败: {result}")
                pipeline_obj._pending_plan = None
            except Exception as e:
                print(f"  执行出错: {e}")
                pipeline_obj._pending_plan = None
            continue

        # --- Command gate (intercept /commands before LLM dispatch) ---
        if hasattr(pipeline_obj, 'cmds') and pipeline_obj.cmds:
            # Check slash-commands first
            if pipeline_obj.cmds.is_command(user_input):
                result = await pipeline_obj.cmds.handle(user_input, t0)
                if result is not None:
                    elapsed = time.time() - t0
                    print(f"{result.get('response', '')}")
                    pipeline_obj.memory.add_turn("assistant", result.get('response', ''))
                    continue
            # Check natural-language memory intents
            mem_intent = pipeline_obj.cmds.is_memory_intent(user_input)
            if mem_intent == 'query':
                result = await pipeline_obj.cmds._memory('/memory', t0)
                print(f"{result.get('response', '')}")
                continue
            elif mem_intent == 'store':
                # Extract content after pattern word
                import re as _re_mem
                for pat in ['记住', '记下来', '写入记忆']:
                    if pat in user_input:
                        idx = user_input.index(pat) + len(pat)
                        text = user_input[idx:].strip().lstrip('：:').strip()
                        if text:
                            result = await pipeline_obj.cmds._remember(f'/remember {text}', t0)
                            print(f"{result.get('response', '')}")
                            break
                continue

        # --- Intent gate ---
        try:
            if pipeline_obj._classify_intent(user_input) == 'task':
                result = await pipeline_obj._process_task(user_input, t0)
            else:
                result = await pipeline_obj.process(user_input, ext_model=current_model if current_model not in ("auto", "voice") else None)
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
                    _cf, ext_model=current_model if current_model not in ("auto", "voice") else None)
            except Exception as e:
                print(f"{RED}[规划模式] 执行出错: {e}{RST}")
                continue

        try:
            elapsed = time.time() - t0
            model = result.get('model', 'local')
            response = result.get('response', '')
            print()
            pass
            print(response)
            # Log to file for agent readback
            try:
                import os as _os
                _log_dir = str(PROJECT_ROOT / "data")
                _os.makedirs(_log_dir, exist_ok=True)
                _log_path = _os.path.join(_log_dir, "tmm_output.log")
                with open(_log_path, "w", encoding="utf-8") as _lf:
                    _lf.write(str(response))
            except Exception as _e:
                pass  # non-critical

            _tool_count = result.get('tool_results', 0) or result.get('code_rounds', 0)
            if _tool_count >= 5:
                try:
                    from core.workflow_snapshot import get_workflow_engine
                    from pathlib import Path as _WfPath
                    _data_dir = _WfPath(PROJECT_ROOT / "data")
                    _we = get_workflow_engine(_data_dir)
                    _we.capture(user_input, result, _tool_count)
                except Exception:
                    pass
            
            # -- Auto-extract shell commands from model response -> Inbox --
            _pending_inbox = None
            import re as _re2
            shell_blocks = _re2.findall(r'```(?:shell|bash|cmd|sh)\s*\n(.+?)```', response, _re2.DOTALL)
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
            # ★ 2026-09-19 改: 原来在这里写 chat_sessions.db。但 CLI 的输入也是经
            #   pipeline_obj.process() 处理的, 而 process() 现在**已统一收尾落库**
            #   (core/pipeline.py 的 process/_persist_turn) → 这里再写一次会让每条
            #   消息入库两遍。所以删除, 落库口径统一到 process() 一处。
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
