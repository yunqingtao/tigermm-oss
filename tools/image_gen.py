"""
生图 (Image generation) — 文生图, 走阿里百炼 (DashScope) 通义万相。

默认模型 wan2.2-t2i-flash (快、便宜; 实测可用)。
异步任务式: 提交 → 轮询 task → 下载图片到本地。

凭证解析顺序 (分布友好):
  1. 环境变量 DASHSCOPE_API_KEY
  2. 环境变量 QWEN_API_KEY
  3. keys.json 的 qwen.key
"""
import os
import re
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

TOOL = {
    "name": "image_gen",
    "version": "1.0",
    "description": ("Generate an image from a text prompt (text-to-image). "
                    "Use when the user asks to draw/create/make a picture "
                    "('画一张…' / '生成图片' / '做张海报')."),
    "requires": [],
    "permission": ["file_write", "network"],
    "category": "multimodal",
    "keywords": ["生成图片", "画一张", "画个图", "生图", "做张图片", "文生图"],
    "params": [
        {"name": "prompt", "type": "str", "required": True, "description": "图片描述"},
        {"name": "path", "type": "str", "required": False,
         "description": "保存路径 (默认桌面, 文件名自动)"},
        {"name": "size", "type": "str", "required": False,
         "description": "尺寸, 如 1024*1024 (默认)"},
        {"name": "model", "type": "str", "required": False,
         "description": "模型 (默认 wan2.2-t2i-flash)"},
    ],
}

PLUGIN = TOOL

_BASE = "https://dashscope.aliyuncs.com/api/v1"
_DEFAULT_MODEL = "wan2.2-t2i-flash"
_CACHE = {}


def _resolve_key() -> str:
    if _CACHE.get("key"):
        return _CACHE["key"]
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
                        break
            except Exception:
                pass
    _CACHE["key"] = key
    return key


def _default_path() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    desk = Path(os.path.expanduser("~")) / "Desktop"
    base = desk if desk.is_dir() else Path.cwd()
    return str(base / f"TMM_图_{ts}.png")


def _download(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "TigerMM/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    dest.write_bytes(data)
    return len(data)


# ★ 分两类 (2026-09-19, 由 ad-hoc 验证器抓出):
#   ① 客套词 (请/帮忙/帮我/麻烦) —— 可**反复**剥 ("请帮我画一张…")
#   ② 引导动词 (画一张/生成图片/…) —— **只剥一次**。
#      早期把两类混在一起循环剥 3 次 → 裸"画"会把描述开头的字吃掉:
#      "画一张画中有画的画" → "中有画的画" (错), 应为 "画中有画的画"。
_FLUFF_RE = re.compile(r'^\s*(?:请|帮忙|帮我|麻烦)\s*')
_VERB_RE = re.compile(
    r'^\s*(?:画一张|画一个|画个|画幅|生成一张|生成一个|生成图片|生成图|'
    r'做一张|做一个|做个|来一张|来张|来个|生图|文生图|画)\s*[:：,，]?\s*')


def _extract_prompt(text: str) -> str:
    """从自然语言里剥出图片描述。
    为什么: plugin.keyword / 技能层都会把整句透传 ("画一张赛博朋克龙") ——
    直接当 prompt 会画出奇怪的东西 (与 _clean_city_name 同一思路)。
    """
    out = (text or "").strip()
    for _ in range(2):                       # 客套词可叠加: "请帮我…"
        new = _FLUFF_RE.sub("", out, count=1).strip()
        if new == out:
            break
        out = new
    out = _VERB_RE.sub("", out, count=1).strip()   # ★ 动词短语只剥一次
    return out.strip(' 的第"\'“”')


async def run(prompt: str = "", path: str = "", size: str = "", model: str = "",
              out: str = "", query: str = "", **kwargs) -> dict:
    """文生图。返回保存路径。"""
    p = (prompt or query or kwargs.get("text") or kwargs.get("description") or "").strip()
    # ★ 永远剥引导词: 技能/关键词路由会把整句("画一张赛博朋克龙")透传过来
    p = _extract_prompt(p)
    if not p:
        return {"success": False, "error": "缺少 prompt (图片描述)", "output": ""}
    key = _resolve_key()
    if not key:
        return {"success": False, "output": "",
                "error": "缺少 DashScope/Qwen 凭证 (环境变量 DASHSCOPE_API_KEY 或 keys.json 的 qwen.key)"}

    mdl = (model or os.environ.get("TMM_T2I_MODEL") or _DEFAULT_MODEL).strip()
    payload = {"model": mdl, "input": {"prompt": p},
               "parameters": {"size": (size or "1024*1024"), "n": 1}}
    req = urllib.request.Request(
        _BASE + "/services/aigc/text2image/image-synthesis",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key,
                 "X-DashScope-Async": "enable"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=90).read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:400]
        except Exception:
            pass
        return {"success": False, "output": "", "error": f"提交失败 HTTP {e.code}: {body}"}
    except Exception as e:
        return {"success": False, "output": "", "error": f"提交异常 {type(e).__name__}: {e}"}

    task = (r.get("output") or {}).get("task_id")
    if not task:
        return {"success": False, "output": "", "error": f"未拿到 task_id: {str(r)[:300]}"}

    # 轮询 (异步任务) —— 最多约 3 分钟
    status, img_url, msg = "", "", ""
    for _ in range(36):
        time.sleep(5)
        try:
            g = json.loads(urllib.request.urlopen(urllib.request.Request(
                _BASE + "/tasks/" + str(task),
                headers={"Authorization": "Bearer " + key}), timeout=60).read().decode())
        except Exception:
            continue
        o = g.get("output") or {}
        status = o.get("task_status") or ""
        msg = o.get("message") or ""
        if status == "SUCCEEDED":
            img_url = ((o.get("results") or [{}])[0]).get("url") or ""
            break
        if status == "FAILED":
            break

    if status != "SUCCEEDED" or not img_url:
        return {"success": False, "output": "",
                "error": f"生成未成功 (status={status or '超时'}{(': ' + msg) if msg else ''})",
                "task_id": task}

    # ★ 用户给的是**目录** → 补默认文件名 (深测: "存到 D:/图/" 曾被当成文件名)
    _raw_dest = str(path or out or "").strip().strip('"\'“”')
    if _raw_dest and (_raw_dest.endswith(("/", "\\")) or os.path.isdir(_raw_dest)):
        dest = Path(_raw_dest) / (f"TMM_图_{time.strftime('%Y%m%d_%H%M%S')}.png")
    else:
        dest = Path(_raw_dest or _default_path())
    try:
        n = _download(img_url, dest)
    except Exception as e:
        # 图片已生成但下载失败 —— 把 URL 给用户, 不谎报成功
        return {"success": False, "output": f"图片已生成但下载失败: {img_url}",
                "error": f"{type(e).__name__}: {e}", "url": img_url}
    return {"success": True, "path": str(dest), "output": f"已生成图片: {dest}",
            "intent": "image_gen", "model": mdl, "bytes": n, "url": img_url}
