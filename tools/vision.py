"""
看图 (Vision) — 让 TMM 真正"看"图片内容。

两条路 (provider=auto 时**本地优先**):
  ① local    : 本机 ollama 上**自报 vision 能力**的模型 (qwen2.5vl / minicpm-v / gemma4 …)
               零成本、离线、不出网。用 /api/show 的 capabilities 判定, 不按名字猜。
  ② dashscope: 阿里百炼 qwen-vl-plus (OpenAI 兼容端点)。需要出网 + 凭证。

与 ocr 的分工: ocr 抽文字 (Tesseract, 离线), 本工具**理解画面**
(场景/物体/颜色/布局/图表含义/情绪…)。

凭证解析顺序 (分布友好):
  1. 环境变量 DASHSCOPE_API_KEY
  2. 环境变量 QWEN_API_KEY
  3. keys.json 的 qwen.key
"""
import os
import re
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

TOOL = {
    "name": "vision",
    "version": "2.0",
    "description": ("Understand an image: describe scene, objects, colors, layout, charts, mood. "
                    "Use when the user asks what is IN a picture ('这张图里有什么' / '看看这张图'). "
                    "For reading TEXT off an image, prefer ocr instead."),
    "requires": [],
    "permission": ["file_read", "network"],
    "category": "multimodal",
    "keywords": ["看图", "识图", "这张图", "图片里", "图里有什么", "vision"],
    "params": [
        {"name": "image", "type": "str", "required": False,
         "description": "Image path, http(s) URL, or data:image/...;base64,..."},
        {"name": "question", "type": "str", "required": False,
         "description": "What to ask about the image (default: describe it)"},
        {"name": "provider", "type": "str", "required": False,
         "description": "'auto' (默认, 本地优先) / 'local' (只用本机 ollama) / 'dashscope' (只用百炼)"},
        {"name": "model", "type": "str", "required": False,
         "description": "指定模型 (provider=dashscope 时默认 qwen-vl-plus)"},
    ],
}

PLUGIN = TOOL

_KEY_CACHE = {}
_DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "qwen-vl-plus"

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}

_IMG_RE = re.compile(
    r'([A-Za-z]:[\\/][^\n"\'<>|?*]+?\.(?:png|jpe?g|gif|webp|bmp))'
    r'|(https?://[^\s"\'<>]+?\.(?:png|jpe?g|gif|webp|bmp))', re.IGNORECASE)


# ────────────────────────────── 通用工具 ──────────────────────────────

def _ollama_endpoint() -> str:
    host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
    if not host.startswith("http"):
        host = "http://" + host
    host = host.rstrip("/")
    return host[:-3].rstrip("/") if host.endswith("/v1") else host


def _extract_image(text: str) -> str:
    """从自然语言里捞出图片路径/URL。
    为什么: plugin.keyword / 技能层都会把**整句**透传 ("看看这张图 D:\\a.png 里有什么") ——
    工具必须自己找图 (与 pipeline._clean_city_name 同一思路: 在边界把脏输入洗干净)。
    """
    if not text:
        return ""
    m = _IMG_RE.search(text)
    if m:
        return (m.group(1) or m.group(2) or "").strip().strip('"\'')
    m2 = re.search(r'([^\s"\'<>|?*]+\.(?:png|jpe?g|gif|webp|bmp))', text, re.IGNORECASE)
    return m2.group(1).strip() if m2 else ""


def _resolve_local_image(src: str) -> str:
    """本地路径/data URL → data URL; 远程 URL 原样返回。"""
    if src.startswith(("http://", "https://", "data:")):
        return src
    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(f"图片不存在: {src}")
    mime = _MIME.get(p.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _raw_b64(src: str):
    """给 ollama /api/chat 用的纯 base64 (它不收 data URL)。远程 URL 返回 None。"""
    if src.startswith(("http://", "https://")):
        return None
    if src.startswith("data:"):
        return src.split(",", 1)[1]
    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(f"图片不存在: {src}")
    return base64.b64encode(p.read_bytes()).decode()


def _resolve_key() -> str:
    if _KEY_CACHE.get("key"):
        return _KEY_CACHE["key"]
    key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY") or ""
    if not key:
        for cand in (Path(__file__).resolve().parent.parent / "keys.json",
                     Path.cwd() / "keys.json"):
            try:
                if cand.exists():
                    d = json.loads(cand.read_text(encoding="utf-8"))
                    q = d.get("qwen") or {}
                    key = (q.get("key") or "") if isinstance(q, dict) else ""
                    if key:
                        _KEY_CACHE["url"] = q.get("url") or _DEFAULT_URL
                        break
            except Exception:
                pass
    _KEY_CACHE["key"] = key
    return key


# ────────────────────────────── 本地 ollama ──────────────────────────────

def _ollama_caps(model: str):
    try:
        req = urllib.request.Request(_ollama_endpoint() + "/api/show",
                                     data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=6).read().decode())
        return set(d.get("capabilities") or [])
    except Exception:
        return None


def _ollama_up(timeout=3) -> bool:
    """ollama **服务**是否在跑。★ 2026-09-22 加: 服务没起与"没装视觉模型"是两回事,
    实测报错把前者说成后者 → 用户照着去下载模型, 白折腾 (库里其实有 qwen2.5vl:7b)。
    判据: /api/version 能应答 (这是"服务活着"的最轻探针)。
    """
    try:
        urllib.request.urlopen(_ollama_endpoint() + "/api/version", timeout=timeout).read()
        return True
    except Exception:
        return False


def _local_vision_probe():
    """挑视觉模型, 并**区分失败原因** (诚实失败原则)。

    返回 (model|None, reason) —— reason 只在挑不到时给, 且必须能指导下一步动作:
      · 服务没跑      → 告诉用户去启动 ollama (不是"没有模型")
      · 服务在但无视觉模型 → 报清"装了 N 个都没 vision 能力" + 给一条 pull 命令
    """
    pinned = (os.environ.get("TMM_VISION_LOCAL_MODEL") or "").strip()
    if pinned:
        return pinned, ""
    if not _ollama_up():
        return None, (f"本机 ollama 服务未启动 ({_ollama_endpoint()} 连不上) —— "
                      f"视觉/本地模型都依赖它。启动 ollama 后重试。")
    try:
        d = json.loads(urllib.request.urlopen(_ollama_endpoint() + "/api/tags", timeout=6).read().decode())
        names = [m["name"] for m in (d.get("models") or [])]
    except Exception as e:
        return None, f"本机 ollama 不可用 ({type(e).__name__}: {str(e)[:80]})"
    cand = []
    for n in names:
        caps = _ollama_caps(n)
        if caps and "vision" in caps:
            score = 0 if re.search(r'(vl|vision)', n, re.I) else 1
            cand.append((score, n))
    if not cand:
        return None, (f"本机 ollama 上没有视觉模型 (已装 {len(names)} 个, 都不自报 vision 能力)。"
                      f"可执行: ollama pull qwen2.5vl:7b")
    cand.sort()
    return cand[0][1], ""


def _local_vision_model():
    """挑一个本机 ollama 上**自报 vision 能力**的模型 (旧签名, 保留兼容)。
    环境变量 TMM_VISION_LOCAL_MODEL 可指定。优先名字含 vl/vision 的专用视觉模型。
    没有 → None (不报错, 交给下一档 provider)。

    ★ 2026-09-22: 这个函数是**可执行路径上的选择点** —— 调用方/测试都拿它当插桩点
    (monkeypatch 它就能模拟"本机没有视觉模型")。所以 `_see_local` 必须**经由它**决策,
    不能绕过它直接调 `_local_vision_probe()` (踩过: 绕过之后桩失效, canonical 立刻红)。
    """
    return _local_vision_probe()[0]


async def _see_local(src: str, ask: str, model: str = "") -> dict:
    # ★ 决策必须**经由 _local_vision_model()** —— 它是可执行路径上的插桩点
    #   (canonical 里有 monkeypatch 它模拟"本机无视觉模型"的用例)。
    #   拿不到模型时**再**去问一次"为什么"(只在失败路径多花一次轻探针), 让报错能指导下一步。
    m = (model or _local_vision_model() or "").strip()
    if not m:
        _, why = _local_vision_probe()
        return {"success": False, "error": why or "本机 ollama 上没有可用的视觉模型", "output": ""}
    try:
        b64 = _raw_b64(src)
    except FileNotFoundError as e:
        return {"success": False, "output": "", "error": str(e)}
    if b64 is None:
        # ollama /api/chat 不收远程 URL → 先下下来
        try:
            with urllib.request.urlopen(src, timeout=60) as r:
                b64 = base64.b64encode(r.read()).decode()
        except Exception as e:
            return {"success": False, "output": "", "error": f"下载图片失败: {type(e).__name__}: {e}"}
    payload = {"model": m, "stream": False, "options": {"num_predict": 2048},
               "messages": [{"role": "user", "content": ask, "images": [b64]}]}
    try:
        req = urllib.request.Request(_ollama_endpoint() + "/api/chat",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
    except Exception as e:
        return {"success": False, "output": "", "error": f"本地视觉调用失败 {type(e).__name__}: {str(e)[:200]}"}
    msg = r.get("message") or {}
    text = (msg.get("content") or "").strip()
    think = (msg.get("thinking") or msg.get("think") or "").strip()
    if not text:
        if think:
            # thinking 模型 (gemma4/qwen3.5) 可能把答案全放在思考里 —— 那是**它自己的输出**,
            # 用它是诚实的 (不是编造); 但要如实标注来源。
            return {"success": True, "output": think, "intent": "vision", "model": m,
                    "provider": "local", "from_thinking": True}
        return {"success": False, "output": "", "error": f"本地模型 {m} 没有返回内容"}
    return {"success": True, "output": text, "intent": "vision", "model": m, "provider": "local"}


# ────────────────────────────── DashScope ──────────────────────────────

async def _see_dashscope(src: str, ask: str, model: str = "") -> dict:
    key = _resolve_key()
    if not key:
        return {"success": False, "output": "",
                "error": "缺少 DashScope/Qwen 凭证 (环境变量 DASHSCOPE_API_KEY 或 keys.json 的 qwen.key)"}
    try:
        url = _resolve_local_image(src)
    except FileNotFoundError as e:
        return {"success": False, "output": "", "error": str(e)}
    mdl = (model or os.environ.get("TMM_VL_MODEL") or _DEFAULT_MODEL).strip()
    base = (_KEY_CACHE.get("url") or _DEFAULT_URL).rstrip("/")
    payload = {"model": mdl, "max_tokens": 1200,
               "messages": [{"role": "user", "content": [
                   {"type": "image_url", "image_url": {"url": url}},
                   {"type": "text", "text": ask}]}]}
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if not text or not str(text).strip():
            return {"success": False, "output": "", "error": "模型没有返回内容", "raw": str(d)[:300]}
        return {"success": True, "output": str(text).strip(), "intent": "vision",
                "model": mdl, "provider": "dashscope", "usage": d.get("usage") or {}}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        return {"success": False, "output": "", "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"success": False, "output": "", "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ────────────────────────────── 入口 ──────────────────────────────

async def run(image: str = "", question: str = "", model: str = "",
              path: str = "", prompt: str = "", query: str = "",
              provider: str = "auto", **kwargs) -> dict:
    """看图。image/path 同义; query=(路由/技能传来的整句) → 从里面提图片路径。"""
    src = image or path or kwargs.get("image_path") or query or kwargs.get("text") or ""
    # ★ 永远清洗: 传进来的可能是整句("看看这张图 D:\\a.png 里有什么")而不是纯路径。
    if src and not src.startswith(("http://", "https://", "data:")) and not Path(src).exists():
        src = _extract_image(src)
    if not src:
        return {"success": False, "output": "",
                "error": "没找到图片。请给出图片路径, 例如: 看看这张图 D:\\a.png"}
    # ★ 本地校验前置: 文件不存在就别进网络 (ad-hoc 验证器抓到 —— 原来会先探一次
    #   ollama /api/tags 才发现图不在)。输入先验, 再花网络/配额。
    if not src.startswith(("http://", "https://", "data:")) and not Path(src).exists():
        return {"success": False, "output": "", "error": f"图片不存在: {src}"}
    ask = (question or prompt or query
           or "详细描述这张图：内容、物体、颜色、布局；如有文字请读出来。").strip()
    prov = (provider or "auto").strip().lower()

    errs = []
    if prov in ("auto", "local"):
        r = await _see_local(src, ask, model if prov == "local" else "")
        if r.get("success"):
            return r
        errs.append("local: " + str(r.get("error"))[:160])
        if prov == "local":
            return {"success": False, "output": "", "error": errs[0]}
    if prov in ("auto", "dashscope"):
        r = await _see_dashscope(src, ask, model)
        if r.get("success"):
            return r
        errs.append("dashscope: " + str(r.get("error"))[:160])
        if prov == "dashscope":
            return {"success": False, "output": "", "error": errs[0]}
    # auto 且两档都失败 → 诚实汇总, 不编造
    return {"success": False, "output": "", "error": "看图失败 —— " + " | ".join(errs)}
