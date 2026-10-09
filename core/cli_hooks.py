"""
CLI Hooks — 挂载新功能到 CLI，不改 cli.py 源码。
在 main.py 中调用 install() 注入回调。
"""
import time as _time

_hooks = {}


def install(pipeline_obj, config: dict, startup_report: str = ""):
    """Install all hooks. Called from main.py after pipeline init."""
    _hooks["pipeline"] = pipeline_obj
    _hooks["config"] = config
    _hooks["round_count"] = 0
    _hooks["startup_report"] = startup_report


def on_startup():
    """Call after login, before first prompt."""
    rpt = _hooks.get("startup_report", "")
    if rpt:
        GREEN = '\033[32m'; RST = '\033[0m'; DIM = '\033[2m'
        _ok = sum(1 for l in rpt.split(chr(10)) if "✓" in l and "deepseek" not in l and "mimo" not in l)
        _ds = "✓" if any("deepseek" in l and "✓" in l for l in rpt.split(chr(10))) else "✗"
        _mi = "✓" if any("mimo" in l and "✓" in l for l in rpt.split(chr(10))) else "✗"
        _ol = "✓" if any("ollama" in l and "✓" in l for l in rpt.split(chr(10))) else "✗"
        try:
            print(f"  {GREEN}✓{RST} {_ok}工具 | ds={_ds} mi={_mi} auto=✓ ol={_ol} voice=✓")
        except Exception:
            pass

    # Session recovery
    try:
        from core.session_recovery import load as load_session
        from pathlib import Path
        ctx = load_session(str(Path(__file__).parent.parent))
        if ctx:
            print(ctx)
    except Exception:
        pass


def on_round_end():
    """Call after each processed round."""
    _hooks["round_count"] = _hooks.get("round_count", 0) + 1
    pipeline = _hooks.get("pipeline")
    if not pipeline:
        return

    # Auto-mail: every 10 rounds
    if _hooks["round_count"] % 10 == 0:
        try:
            from core.auto_mail import check_and_notify
            from core.desktop_notify import notify_async
            summary = check_and_notify(pipeline.gateway.plugin_mgr, notify_async)
            if summary:
                DIM = '\033[2m'; RST = '\033[0m'
                print(f"\n{DIM}{summary}{RST}")
        except Exception:
            pass


def on_exit():
    """Call on /exit."""
    pipeline = _hooks.get("pipeline")
    if not pipeline:
        return

    # Save session
    try:
        from core.session_recovery import save
        from pathlib import Path
        msgs = []
        if hasattr(pipeline, "get_last_turns"):
            msgs = pipeline.get_last_turns(6)
        save(str(Path(__file__).parent.parent), msgs)
    except Exception:
        pass


def on_correction(original_msg: str, correction_msg: str):
    """用户纠正了 TMM —— 交给**统一学习闭环** (learn_loop)。

    ★ 2026-09-19: 旧实现依赖调用方传 original_msg, 而 main.py 传的是空串 → 整条链是死的。
    现在即便 original_msg 为空也不报假成功 (record_correction 会返回 None),
    并且真正的学习由 pipeline._on_route_done 带**上一轮上下文**统一完成 ——
    这里只是 CLI 的即时回执, 不再承担学习职责。
    """
    try:
        from core.self_evolve import record_correction
        from pathlib import Path
        rule = record_correction(
            str(Path(__file__).parent.parent),
            original_msg,
            correction_msg,
        )
        if rule:
            GREEN = '\033[32m'; RST = '\033[0m'
            print(f"  {GREEN}📝 已学会: {(rule.get('key') or '')[:30]} → {(rule.get('value') or '')[:30]}{RST}")
    except Exception:
        pass
