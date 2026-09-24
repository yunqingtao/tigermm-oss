"""
OCR plugin — extract text from images using Tesseract.
Supports Chinese, English, and 100+ languages.
"""
import os, tempfile, base64

TOOL = {
    "name": "ocr",
    "version": "1.0",
    "description": "Extract text from images (screenshots, photos, scanned docs). Chinese + English. Use this to read text from any image file.",
    "requires": ["pytesseract", "Pillow"],
    "permission": ["file_read"],
    "category": "automation",
    "keywords": ["ocr"],
    "params": [
        {"name": "path", "type": "str", "required": True, "description": "Path to the image file (e.g., C:\\path\\to\\image.png)"},
        {"name": "lang", "type": "str", "required": False, "description": "Language: 'ch' for Chinese+English, 'en' for English only"},
    ],
}

PLUGIN = TOOL


def _Path_home():
    """当前用户主目录 (替代硬编码 C:\\Users\\<某个人>)。"""
    from pathlib import Path as _P
    return _P.home()

# tesseract 二进制探测 (★ 2026-09-19 深测修): 原来是**硬编码**一个个人机的 conda 路径,
#   换台机器/换 conda 版本就找不到 → OCR 整块不可用。
#   改成按优先级探测 + 环境变量可覆盖。
_TESSDATA_DIR = str(_Path_home() / "miniconda3" / "pkgs" / "tesseract-5.5.2-hfa586c3_0" / "share" / "tessdata")

if os.path.exists(_TESSDATA_DIR):
    os.environ.setdefault("TESSDATA_PREFIX", _TESSDATA_DIR)


def _find_tesseract() -> str:
    """按优先级找 tesseract 可执行文件, 找不到返回空串。"""
    import shutil as _sh
    cands = [os.environ.get("TESSERACT_CMD", ""),
             str(_Path_home() / "miniconda3" / "Library" / "bin" / "tesseract.exe"),
             _sh.which("tesseract") or ""]
    for root in (os.environ.get("CONDA_PREFIX", ""), str(_Path_home() / "miniconda3"),
                 r"C:\ProgramData\miniconda3", r"C:\Program Files\Tesseract-OCR"):
        if root:
            cands.append(os.path.join(root, "Library", "bin", "tesseract.exe"))
            cands.append(os.path.join(root, "tesseract.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


_TESSERACT_CMD = _find_tesseract()


def _decode_image(data: str) -> str:
    if data.startswith("data:image") or (len(data) > 100 and "/" not in data[:10] and "\\" not in data[:10] and not os.path.exists(data)):
        if data.startswith("data:"):
            data = data.split(",", 1)[1]
        img_bytes = base64.b64decode(data)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(img_bytes)
        tmp.close()
        return tmp.name
    if data.startswith("http://") or data.startswith("https://"):
        import urllib.request
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            resp = urllib.request.urlopen(data, timeout=30)
            tmp.write(resp.read())
            tmp.close()
            return tmp.name
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise
    return data


async def execute(path: str = "", image: str = "", lang: str = "chi_sim+eng", **kwargs):
    image_data = image or path or kwargs.get("data", "")
    if not image_data:
        return {"success": False, "output": "OCR失败", "error": "缺少图片。用法: ocr(path=截图.png)"}

    lang_map = {
        "ch": "chi_sim+eng", "cn": "chi_sim+eng", "zh": "chi_sim+eng",
        "en": "eng",
    }
    tesseract_lang = lang_map.get(lang.lower(), lang)

    img_path = None
    error = None
    try:
        img_path = _decode_image(image_data)
    except Exception as e:
        return {"success": False, "output": "OCR失败", "error": f"图片解码失败: {e}"}

    text = ""
    engine = "tesseract"
    try:
        import pytesseract
        from PIL import Image
        if _TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD
        img = Image.open(img_path)
        text = pytesseract.image_to_string(img, lang=tesseract_lang).strip()
    except ImportError as e:
        error = (f"{type(e).__name__}: {e} —— 未安装 OCR 依赖 "
                 f"(pip install pytesseract, 并需 tesseract 二进制)")
    except Exception as e:
        text = ""
        error = str(e)

    # ★ 降级链 (2026-09-19 深测): tesseract 不可用或没读出字 → 用**本地视觉模型**读图。
    #   不是猜: 走 tools/vision 的本地 ollama 优先链; 读不出来照实报。
    if not text:
        try:
            from tools import vision as _vis
            _vr = await _vis.run(path=img_path,
                                 prompt="把这张图片里出现的所有文字原样抄出来，只输出文字本身。")
            if _vr.get("success") and str(_vr.get("output") or "").strip():
                text = str(_vr["output"]).strip()
                engine = f"vision({_vr.get('model', '?')})"
        except Exception as _ve:
            error = (error or "") + f" | vision 兜底失败: {type(_ve).__name__}"

    if img_path and img_path != image_data and img_path != path:
        try:
            os.unlink(img_path)
        except Exception:
            pass

    if not text:
        return {"success": False, "output": "OCR失败",
                "error": f"OCR失败: {error or '图片无文字'}"}

    return {"success": True, "output": text, "result": text, "text": text, "engine": engine}


run = execute
