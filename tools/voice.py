"""
Voice I/O — record microphone, transcribe via whisper, speak via TTS.
========================================================================
Provides voice_recorder for audio capture and voice_pipeline for full loop.

Requires: pip install sounddevice soundfile
"""
import os, tempfile, time, logging

logger = logging.getLogger("tools.voice")

PLUGIN = {
    "name": "voice",
    "description": "Voice I/O: record audio, transcribe with whisper, speak via TTS",
    "version": "1.0",
    "trigger": ["语音", "voice", "说话", "录音"],
    "permission": ["system"],
    "category": "multimodal",
}


async def run(action: str = "", text: str = "", duration: int = 5, lang: str = "zh",
              file: str = "", **kwargs):
    """
    Voice I/O gateway.
    action="" (auto): speak if text provided, record if not
    action="speak": speak text via TTS
    action="record": record from mic, transcribe, return text
    action="transcribe": transcribe existing audio file
    """
    # Auto-detect: if text given and no explicit action, speak it
    if not action:
        if text:
            action = "speak"
        else:
            action = "record"
    
    if action == "speak":
        return await _speak(text, file)
    elif action == "transcribe":
        return await _transcribe_file(file, lang)
    elif action == "record":
        return await _record_and_transcribe(duration, lang)
    elif action == "full":
        # Record, transcribe, then return text (caller pipes to TMM and speaks response)
        rec_result = await _record_and_transcribe(duration, lang)
        if rec_result.get("success"):
            return rec_result
        return rec_result
    else:
        return {"success": False, "output": f"未知语音操作: {action}", "error": f"Unknown action: {action}"}


async def _record_and_transcribe(duration: int, lang: str) -> dict:
    """Record audio from default mic, save to temp file, transcribe."""
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError:
        return {"success": False, "output": "录音依赖未安装", "error": "sounddevice/soundfile not installed"}

    tmp_path = os.path.join(tempfile.gettempdir(), f"tmm_voice_{int(time.time())}.wav")
    
    try:
        # Record
        sample_rate = 16000
        logger.info("Recording %ds at %dHz...", duration, sample_rate)
        audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='float32')
        sd.wait()
        
        # Save
        sf.write(tmp_path, audio, sample_rate)
        logger.info("Saved: %s (%d bytes)", tmp_path, os.path.getsize(tmp_path))
        
    except Exception as e:
        return {"success": False, "output": f"录音失败: {e}", "error": f"Recording failed: {e}"}

    # Transcribe
    return await _transcribe_file(tmp_path, lang)


async def _transcribe_file(file_path: str, lang: str) -> dict:
    """Transcribe audio file with whisper."""
    if not os.path.exists(file_path):
        return {"success": False, "output": f"音频文件不存在: {file_path}", "error": f"File not found: {file_path}"}

    try:
        import whisper
        model = whisper.load_model("medium")
        result = model.transcribe(file_path, language=lang)
        text = result.get("text", "").strip()
        
        return {
            "success": True,
            "text": text,
            "file": file_path,
            "output": text,
        }
    except ImportError:
        return {"success": False, "output": "Whisper未安装", "error": "whisper not installed"}
    except Exception as e:
        return {"success": False, "output": f"语音识别失败: {e}", "error": f"Whisper error: {e}"}


def _play_audio(path: str) -> str:
    """播放音频文件 —— 二级兜底, 返回实际用的方式。

    ★ 2026-09-20 加: 原来写死 `from playsound import playsound` —— 本机没装 playsound
      (TMM 的 Python311 里只有 pygame/edge-tts/pyttsx3) → 整个 Edge TTS 分支直接
      抛 ImportError → 悄悄退化成 SAPI 机械音 (用户听起来"音色变差了", 却没人知道为什么)。
      现在: playsound (装了就用) → pygame.mixer (本机已装) —— 缺哪个都不影响音色。
    """
    try:
        from playsound import playsound
        playsound(path)
        return "playsound"
    except Exception:
        pass
    try:
        import pygame
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.1)
        pygame.mixer.music.unload()
        return "pygame"
    except Exception as e:
        raise RuntimeError(f"没有可用的音频播放器 (playsound/pygame): {e}")


async def _speak(text: str, file: str = "") -> dict:
    """Speak text via Edge TTS (natural neural voice)."""
    # ★★ 2026-09-24 修 (用户实测发火的那句): 调用方常把**文件路径同时填进 text**,
    #   如说话技能 `朗读"C:\\...\\窗台上的光.txt"` → text/file 都是那个路径。
    #   老逻辑只在 `not text` 时才读文件 ⇒ 这里 text 非空 ⇒ **把路径字符串念出来**
    #   (用户听到 "C 冒号 反斜杠 Users …"), 而正文一个字没念。
    #   修法: text 若是"真实存在的文件路径"并且内容不是多行正文 → 当文件读。
    #   (判据: 存在 + 单行 + 末段像文件名 ⇒ 极不可能是用户想听的一句话)
    if text:
        # ★ 修 (2026-09-24): 调用方常把**文件路径同时填进 text** (说话技能就是这样)。
        #   老逻辑 `if not text and file` 只在 text 为空时读文件 ⇒ 这里 text 非空
        #   ⇒ **把路径字符串念出来** (用户听到 "C 冒号 反斜杠 Users …"), 正文没念。
        #   现在: text 只要"像路径"就当文件读, 有 file 用 file。
        #   ⚠ 坑: 本函数下面有 `import edge_tts, ..., re, ...` ⇒ `re` 是**局部名**,
        #     这里必须用别名 `import re as _re`, 否则 UnboundLocalError 被 except 吞掉
        #     而**静默失效** (门禁 B2/B3 就是这么抓出来的)。
        try:
            import re as _re
            _tp = text.strip().strip('"').strip("'")
            _pathish = bool(
                _re.match(r"^[A-Za-z]:[\\/]", _tp)
                or ("\\" in _tp) or ("/" in _tp)
                or _re.search(r"\.(?:txt|docx?|pptx?|pdf|py|md|xlsx?|csv|json|png|jpe?g|"
                              r"zip|mp[34]|wav|m4a|flac|ogg|aac|mov|mkv|avi|webm|"
                              r"gif|bmp|webp|srt|ass|lrc)$", _tp, _re.I))
            if _pathish and ("\n" not in _tp) and ("\r" not in _tp) and len(_tp) <= 260:
                file, text = (file or _tp), ""
                logger.info("speak: text 是文件路径 → 改读文件内容: %s", file)
        except Exception as _e:
            logger.debug("speak: 路径判定跳过 (%s)", _e)
    # ★ 2026-09-20: 只给文件不给文字 → 念文件内容 (本地播报: "念一下 D:/x.txt")
    if not text and file:
        try:
            with open(file, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            return {"success": False, "output": f"读不到要念的文件: {e}", "error": str(e)}
    if not text:
        return {"success": False, "output": "没有要朗读的文字", "error": "No text to speak"}

    # Primary: Edge TTS (zh-CN-YunxiNeural, natural male voice)
    try:
        import edge_tts, tempfile, os, re, asyncio as _asyncio
        
        # Clean text
        clean = text[:800]
        clean = re.sub(r'[\U0001F300-\U0001F9FF\u2600-\u27BF]', '', clean)
        clean = re.sub(r'[*_#>`|]', '', clean)
        clean = re.sub(r'\n{2,}', '。', clean)
        clean = re.sub(r'\n', '，', clean)
        clean = clean.strip()
        if not clean:
            return {"success": False, "output": "没有可朗读的内容 (清洗后为空)", "error": "empty after clean"}
        
        voice = "zh-CN-YunxiNeural"
        tmp = os.path.join(tempfile.gettempdir(), f"tmm_tts_{int(time.time())}.mp3")
        
        # Retry up to 3 times
        for attempt in range(3):
            try:
                comm = edge_tts.Communicate(clean, voice)
                await comm.save(tmp)
                if os.path.getsize(tmp) > 1000:
                    break
                os.remove(tmp)
            except Exception:
                if attempt < 2:
                    await _asyncio.sleep(1)
                else:
                    raise
        
        how = _play_audio(tmp)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return {"success": True, "output": f"已播报 ({how} · {len(clean)}字)",
                "voice": voice, "chars": len(clean), "player": how}
    except Exception as e:
        logger.warning("Edge TTS failed: %s, trying SAPI", e)

    # Fallback: Windows SAPI
    try:
        import win32com.client
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        speaker.Speak(text[:800])
        return {"success": True, "output": "已播报 (SAPI 兜底)"}
    except ImportError:
        pass
    except Exception as e:
        logger.warning("SAPI also failed: %s", e)

    return {"success": False, "output": "语音合成不可用", "error": "No TTS available"}
