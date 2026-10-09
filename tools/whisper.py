"""
Whisper TTS - audio-to-text transcription via OpenAI Whisper.
Local, free, no API key.
"""
import subprocess, os, sys, shutil, logging
logger = logging.getLogger("tools.whisper")

PLUGIN = {
    "name": "whisper",
    "version": "1.0",
    "description": "Transcribe audio (mp3/wav/m4a) to text using local Whisper. Chinese + English.",
    "requires": ["openai-whisper"],
    "permission": ["file_read"],
    "category": "automation",
    "keywords": ["whisper", "转录", "语音转文字", "音频转文字", "导出文字"],
}

async def run(path: str = "", lang: str = "zh", model: str = "base", **kwargs):
    """Transcribe audio to text using Whisper."""
    if not path:
        path = kwargs.get("audio", "") or kwargs.get("file", "") or kwargs.get("query", "")
    if not path:
        return {"success": False, "error": "请提供音频文件路径"}

    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not os.path.exists(path):
        return {"success": False, "error": f"文件不存在: {path}"}

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4"):
        return {"success": False, "error": f"不支持格式: {ext}"}

    lang_map = {"zh": "Chinese", "cn": "Chinese", "en": "English"}
    lang_arg = lang_map.get(lang.lower(), "")
    model = model if model in ("tiny","base","small","medium","large") else "base"

    # ★ 2026-09-20 加: 解析 whisper 可执行文件。
    #   实测问题: 原来硬用裸 "whisper" → 靠 PATH。而引擎按**桌面快捷方式**启动时 PATH
    #   来自用户环境, 通常**不含** Hermes venv 的 Scripts → 报 "Whisper 未安装",
    #   但机器上其实装了 (在 Hermes venv 里, 实测 whisper.exe --help 正常)。
    #   顺序: PATH → 已知候选路径 → python -m whisper。全不行才诚实报未装。
    _exe = shutil.which("whisper")
    if not _exe:
        _cands = [
            os.path.expandvars(r"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\whisper.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python311\Scripts\whisper.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\Scripts\whisper.exe"),
        ]
        # 同目录下解释器的 Scripts (若用户把 whisper 装在当前解释器)
        _cands.insert(0, os.path.join(os.path.dirname(sys.executable), "Scripts", "whisper.exe"))
        for _c in _cands:
            if _c and os.path.isfile(_c):
                _exe = _c
                break
    if not _exe:
        # 退一步: 用当前解释器跑 -m whisper (装了 whisper 模块但没 console_scripts 时)
        try:
            _probe = subprocess.run([sys.executable, "-m", "whisper", "--help"],
                                    capture_output=True, text=True, timeout=60,
                                    encoding="utf-8", errors="replace")
            if _probe.returncode == 0:
                _exe = sys.executable
        except Exception:
            pass

    # ★ 2026-09-20 修: 副产物别落在用户目录。
    #   实测缺陷: whisper CLI 默认把 .txt/.srt/.vtt/.tsv/.json 五件套写在**输入文件旁边**,
    #   旧代码 cwd=dirname(path) → 用户给一个视频, 他的文件夹立刻多出 5 个文件。
    #   读取用户素材不该污染他的目录 → 统一落到临时目录, 用完删。
    import tempfile as _tf
    _outdir = _tf.mkdtemp(prefix="tmm_whisper_")
    try:
        cmd = [_exe, path, "--model", model, "--task", "transcribe",
               "--output_dir", _outdir] if _exe \
            else [sys.executable, "-m", "whisper", path, "--model", model,
                  "--task", "transcribe", "--output_dir", _outdir]
        if lang_arg:
            cmd += ["--language", lang_arg]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, encoding='utf-8', errors='replace',
                             cwd=_outdir)

        if proc.returncode != 0:
            err = proc.stderr.strip()[:300]
            if "not found" in err.lower():
                return {"success": False, "error": "Whisper 未安装。pip install openai-whisper"}
            return {"success": False, "error": err}

        output = proc.stdout.strip() or proc.stderr.strip()

        # Check for output file too (★ 现在在临时目录里找, 不再看用户目录)
        txt_file = os.path.join(_outdir, os.path.splitext(os.path.basename(path))[0] + ".txt")
        if os.path.exists(txt_file):
            with open(txt_file, "r", encoding="utf-8") as f:
                file_out = f.read().strip()
            if len(file_out) > len(output):
                output = file_out

        if not output:
            return {"success": False, "error": "转录无文字输出"}

        return {"success": True, "output": output[:5000], "text": output[:5000],
                "model": model, "language": lang_arg or "auto"}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "转录超时，尝试 --model tiny"}
    except FileNotFoundError:
        return {"success": False, "error": "Whisper 未安装"}
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}
    finally:
        # 收尾: 删临时副产物目录, 不给用户留垃圾
        try:
            import shutil as _sh
            _sh.rmtree(_outdir, ignore_errors=True)
        except Exception:
            pass