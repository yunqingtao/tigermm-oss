"""
StartupCheck — TMM启动自检: 工具可用、模型在线、降级清单。
"""
import sys, os, time, logging
from pathlib import Path

logger = logging.getLogger("core.startup_check")

GREEN = '\033[32m'
RED = '\033[31m'
YELLOW = '\033[33m'
DIM = '\033[2m'
RST = '\033[0m'

# 工具 → 外部依赖检查 (2026-08-18 治理: 缺依赖的工具标提醒而非静默)
# 每项: (依赖名, 检查函数) — 函数返回 True=就绪, False=缺失
def _check_module(mod: str):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _check_exe(exe: str):
    import shutil
    return shutil.which(exe) is not None


TOOL_DEPS = {
    "ocr": [("tesseract.exe", lambda: _check_exe("tesseract"))],
    "whisper": [("whisper 包", lambda: _check_module("whisper")),
                ("sounddevice 包", lambda: _check_module("sounddevice"))],
    "voice": [("edge_tts 包", lambda: _check_module("edge_tts")),
              ("playsound 包", lambda: _check_module("playsound"))],
    "browser": [("playwright 包", lambda: _check_module("playwright"))],
}


def run(project_root: str, config: dict) -> str:
    """Run startup checks. Returns formatted report string."""
    lines = []
    ok = 0
    warn = 0
    fail = 0

    # ═══ 1. Tools ═══
    lines.append(f"{DIM}── 工具 ──{RST}")
    tools_dir = Path(project_root) / "tools"
    if tools_dir.exists():
        for f in sorted(tools_dir.glob("*.py")):
            if f.name.startswith("_") or f.name in ("hermes_bridge.py",):
                continue
            try:
                # Quick syntax check
                with open(f, 'r', encoding='utf-8') as fh:
                    compile(fh.read(), str(f), 'exec')
                # 依赖检查: 缺外部依赖的工具标提醒
                _missing = []
                for _dep_name, _dep_fn in TOOL_DEPS.get(f.stem, []):
                    try:
                        if not _dep_fn():
                            _missing.append(_dep_name)
                    except Exception:
                        _missing.append(_dep_name)
                if _missing:
                    lines.append(f"  {YELLOW}~{RST} {f.stem} (缺: {'/'.join(_missing)})")
                    warn += 1
                else:
                    lines.append(f"  {GREEN}✓{RST} {f.stem}")
                    ok += 1
            except SyntaxError:
                lines.append(f"  {RED}✗{RST} {f.stem} (语法错误)")
                fail += 1
            except Exception:
                lines.append(f"  {YELLOW}~{RST} {f.stem}")
                warn += 1

    # ═══ 2. Models ═══
    lines.append(f"\n{DIM}── 模型 ──{RST}")
    models = config.get("models", config)
    try:
        import urllib.request, json
        for name in ("deepseek", "mimo", "openai"):
            cfg = models.get(name, {})
            if not cfg.get("key") or "your-key" in cfg.get("key", "") or "sk-your" in cfg.get("key", ""):
                lines.append(f"  {YELLOW}~{RST} {name}: 未配置key")
                warn += 1
                continue
            url = cfg.get("url", "").rstrip("/")
            if "/v1" not in url:
                url += "/v1"
            try:
                req = urllib.request.Request(
                    url + "/models",
                    headers={"Authorization": f"Bearer {cfg['key']}"}
                )
                urllib.request.urlopen(req, timeout=5)
                lines.append(f"  {GREEN}✓{RST} {name} ({cfg.get('model', name)})")
                ok += 1
            except Exception as e:
                err = str(e)[:60]
                lines.append(f"  {RED}✗{RST} {name}: {err}")
                fail += 1
    except Exception:
        pass

    # Ollama
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        urllib.request.urlopen(req, timeout=2)
        lines.append(f"  {GREEN}✓{RST} ollama (本地)")
        ok += 1
    except Exception:
        lines.append(f"  {DIM}-{RST} ollama: 未运行")
        warn += 1

    # ═══ 3. Config ═══
    lines.append(f"\n{DIM}── 配置 ──{RST}")
    for f in ("keys.json", "keys.enc"):
        p = Path(project_root) / f
        if p.exists():
            lines.append(f"  {GREEN}✓{RST} {f}")
            ok += 1
        else:
            lines.append(f"  {YELLOW}~{RST} {f} (缺失)")
            warn += 1

    data = Path(project_root) / "data"
    if data.exists():
        lines.append(f"  {GREEN}✓{RST} data/")
        ok += 1
    else:
        lines.append(f"  {DIM}-{RST} data/ (首次运行自动创建)")


    # ═══ First-run detection ═══
    _has_real_key = False
    try:
        for _name in ("deepseek", "mimo"):
            _v = config.get(_name, {}).get("key", "")
            if _v and "your-key" not in str(_v) and "sk-your" not in str(_v) and len(str(_v)) > 10:
                _has_real_key = True
                break
    except Exception:
        pass

    if not _has_real_key:
        lines.append(f"\n{YELLOW}⚠ 首次运行 — 尚未配置API Key{RST}")
        lines.append(f"  {DIM}1. 编辑 keys.json 填入你的API Key{RST}")
        lines.append(f"  {DIM}2. 或复制 keys_template.json 为 keys.json 再编辑{RST}")
        lines.append(f"  {DIM}3. 至少配置 deepseek 或 mimo 其中一个{RST}")
        lines.append(f"  {DIM}4. 保存后重启TMM{RST}")

    # ═══ Summary ═══
    total = ok + warn + fail
    status = f"{GREEN}✓{RST}" if fail == 0 else f"{YELLOW}⚠{RST}"
    lines.insert(0, f"{status} 启动自检: {ok}通过 {warn}提醒 {fail}失败 ({total}项)")

    return "\n".join(lines)
