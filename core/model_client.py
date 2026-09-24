"""
Multi-model API client — aiohttp-based, supports DeepSeek/OpenAI/Ollama.
v4.3: fresh session per call — no cross-loop, no connector-closed issues.
"""
import time
import aiohttp
from config.settings import MODEL_TIMEOUT
from config.settings import PROJECT_ROOT
import os
import json
import re
import contextvars
import logging
import threading

_logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 模型用量记账 (2026-09-20) —— 让"钱花在哪"看得见
#   为什么要: model_client 每次调用**都拿到了 usage**(token 数), 但**没人存下来**
#     → 用户问"用了多少/哪个模型花得多"只能靠猜。
#   怎么做: 每次调用后往 data/model_usage.jsonl **追加一行** JSON。
#     ★ 追加式 (不是改文件): 崩溃/并发都不容易写坏; 读取端按行解析, 坏行跳过。
#     ★ 全程 try/except 包住: **记账失败绝不许影响对话本身**。
#     ★ probe 流量不记账 (见 set_probe): 验证/门禁跑的调用不该混进真实用量统计,
#       否则数字虚高 —— 与"验证不得污染用户数据"同一条铁律。
# ═══════════════════════════════════════════════════════════════

_probe_ctx: "contextvars.ContextVar[bool]" = contextvars.ContextVar("tmm_probe", default=False)
_usage_lock = threading.Lock()


def usage_log_path() -> str:
    """用量日志位置 (环境变量可覆盖 —— 测试/门禁必须指向沙箱)。"""
    override = os.environ.get("TMM_USAGE_LOG")
    if override:
        return override
    data_dir = os.path.join(PROJECT_ROOT, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(data_dir, "model_usage.jsonl")


def set_probe(flag: bool):
    """标记当前上下文是探针流量 (probe=True → 不记账)。返回 token 供 reset。"""
    return _probe_ctx.set(bool(flag))


def reset_probe(token):
    try:
        _probe_ctx.reset(token)
    except Exception:
        pass


def is_probe() -> bool:
    try:
        return bool(_probe_ctx.get())
    except Exception:
        return False


def record_usage(model_name: str, result: dict, elapsed: float, messages=None) -> bool:
    """把一次调用的用量追加进日志。返回是否记账; 任何异常都吞掉 (只记 warning)。

    ★ probe 守卫放在**函数内部** (不只依赖调用方): 实测发现只在外层 generate
      包装里判断的话, 任何**直接调用**本函数的新代码都会绕开守卫 →
      探针流量混进真实用量。纵深防御, 这里再判一次。
    """
    try:
        if is_probe():
            return False
        if not isinstance(result, dict):
            return False
        usage = result.get("usage") or {}
        try:
            prompt_in = sum(len(str(m.get("content") or "")) for m in (messages or []))
        except Exception:
            prompt_in = 0
        row = {
            "ts": time.time(),
            "model": model_name,
            "model_id": result.get("model") or model_name,
            "ok": not result.get("error"),
            "error": (str(result.get("error"))[:200] if result.get("error") else ""),
            "elapsed": round(float(elapsed or 0), 3),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "chars_in": int(prompt_in),
        }
        line = json.dumps(row, ensure_ascii=False)
        path = usage_log_path()
        with _usage_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception:
        _logger.warning("usage record failed (对话不受影响)", exc_info=True)
        return False


class ModelClient:
    def __init__(self, config):
        self.config = config
        self._pool_size = int(os.environ.get("HTTP_POOL_SIZE", "20"))
        try:
            from mcp import ProviderRegistry
            self._registry = ProviderRegistry()
            self._registry.load_from_config(config)
            self._registry.load_from_env()
        except ImportError:
            self._registry = None

    def _get_model_config(self, model_name):
        if self._registry:
            profile = self._registry.get(model_name)
            if profile:
                return {
                    "key": profile.api_key,
                    "url": profile.base_url,
                    "model": profile.model,
                }
        for k, v in self.config.items():
            if k.lower() == model_name.lower():
                return v
        defaults = {
            "deepseek": {"key": "", "url": "https://api.deepseek.com", "model": "deepseek-chat"},
            "mimo": {"key": "", "url": "https://api.xiaomimimo.com/v1", "model": "mimo-v2.5"},
            "qwen": {"key": "", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.7-flash"},
            "ollama": {"key": "ollama", "url": "http://127.0.0.1:11434/v1", "model": "gemma4-12b-fullgpu:latest"},
        }
        if model_name in defaults:
            cfg = defaults[model_name]
            if model_name in self.config:
                cfg.update(self.config[model_name])
            return cfg
        return {}

    async def generate(self, model_name, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
        """★ 2026-09-20: 记账包装层 —— 原逻辑整体挪到 _generate_impl, 这里只多"记一行"。

        为什么用包装而不是在每个 return 处插一行: 那个函数里有 5 个 return 点,
        插 5 处 = 5 次改错机会, 而且以后新增分支容易漏。
        行为对调用方**完全不变** (同一个返回字典)。
        """
        _t0 = time.time()
        res = await self._generate_impl(model_name, messages, temperature=temperature,
                                        max_tokens=max_tokens, tools=tools, think=think)
        if not is_probe():
            record_usage(model_name, res, time.time() - _t0, messages)
        return res

    async def _generate_impl(self, model_name, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
        t0 = time.time()
        cfg = self._get_model_config(model_name)
        import logging
        _log = logging.getLogger(__name__)
        if not cfg:
            return {"text": "", "think": "", "model": model_name, "elapsed": 0,
                    "error": f"Unknown model: {model_name}", "tool_calls": None}
        api_key = cfg.get("key", cfg.get("api_key", ""))
        base_url = cfg.get("url", cfg.get("base_url", "")).rstrip("/")
        model_id = cfg.get("model", model_name)
        if not api_key and model_name != "ollama":
            return {"text": "", "think": "", "model": model_name, "elapsed": 0,
                    "error": f"No API key for {model_name}", "tool_calls": None}
        
        # ── 视觉检测：有图片且是ollama → 走原生/api/chat ──
        _has_images = False
        _ollama_msgs = []
        _images_all = []
        if model_name == "ollama":
            for _m in messages:
                _c = _m.get("content")
                if isinstance(_c, list):
                    _has_images = True
                    _text = ""
                    _imgs = []
                    for _p in _c:
                        if _p.get("type") == "text":
                            _text = _p.get("text", "")
                        elif _p.get("type") == "image_url":
                            _url = _p.get("image_url", {}).get("url", "")
                            if _url.startswith("data:"):
                                _b64 = _url.split(",", 1)[-1]
                                _imgs.append(_b64)
                    _ollama_msgs.append({"role": _m["role"], "content": _text, "images": _imgs})
                    _images_all.extend(_imgs)
                else:
                    _ollama_msgs.append({"role": _m["role"], "content": _c})
        
        if _has_images and _images_all:
            # 走 Ollama 原生 /api/chat
            url = base_url.replace("/v1", "") + "/api/chat"
            payload = {"model": model_id, "messages": _ollama_msgs, "stream": False}
            if model_name == "ollama":
                payload["think"] = think if think is not None else True  # 推理模型默认开启思考; 调用方可覆盖
            if "options" not in payload:
                payload["options"] = {}
            payload["options"]["temperature"] = temperature
            payload["options"]["num_predict"] = max_tokens
            payload["options"]["num_ctx"] = 8192
        elif model_name == "ollama":
            # 🔴 Ollama 一律走原生 /api/chat (2026-08-19):
            # /v1/chat/completions 的 think 参数不生效 → gemma4 强制输出推理 →
            # text 空 → pipeline L1784 用 think(英文推理) 当回复 → 用户看到英文分析
            # /api/chat 的 think 参数生效 → 简单/预读任务 think=False 真正关闭推理
            url = base_url.replace("/v1", "") + "/api/chat"
            payload = {"model": model_id, "messages": _ollama_msgs, "stream": False,
                       "think": think if think is not None else True}
            # 2026-08-20: 传 tools 给本地模型 → gemma4-12B 支持原生工具调用,
            # 响应 message.tool_calls 解析 (修复 ollama 永远无法调工具的缺陷)
            if tools:
                payload["tools"] = tools
            if "options" not in payload:
                payload["options"] = {}
            payload["options"]["temperature"] = temperature
            payload["options"]["num_predict"] = max_tokens
            payload["options"]["num_ctx"] = 8192
        else:
            # 标准 OpenAI-compatible /v1/chat/completions
            if "/v1" in base_url:
                url = f"{base_url}/chat/completions"
            else:
                url = f"{base_url}/v1/chat/completions"
            payload = {"model": model_id, "messages": messages, "temperature": temperature,
                        "max_tokens": max_tokens}
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            if "mimo" in model_name.lower() or "mimo" in model_id.lower():
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                payload["extra_body"] = {"thinking": {"type": "disabled"}}
        headers = {"Content-Type": "application/json"}
        if api_key != "ollama":
            headers["Authorization"] = f"Bearer {api_key}"
        
        session = None
        try:
            _log.debug(f"API call: {model_name} -> {url} model={model_id}")
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                # ── Ollama 原生 /api/chat 响应 (图片或纯文本 ollama 都走 /api/chat) ──
                if "message" in data:
                    content = data.get("message", {}).get("content", "") or ""
                    think = data.get("message", {}).get("thinking", "") or ""
                    # 2026-08-20: Ollama tool_calls 格式 [{"function": {"name","arguments"(dict)}}]
                    tool_calls = data.get("message", {}).get("tool_calls", None)
                    return {"text": content, "think": think, "model": model_id,
                            "elapsed": round(time.time() - t0, 2), "error": None,
                            "tool_calls": tool_calls,
                            "usage": {"prompt_tokens": data.get("prompt_eval_count", 0),
                                      "completion_tokens": data.get("eval_count", 0),
                                      "total_tokens": (data.get("prompt_eval_count", 0) + data.get("eval_count", 0))}}
                # ── OpenAI-compatible /v1/chat/completions 响应 ──
                if "choices" in data and len(data["choices"]) > 0:
                    msg = data["choices"][0]["message"]
                    content = msg.get("content") or ""
                    think = msg.get("reasoning", "") or msg.get("reasoning_content", "") or ""
                    tool_calls = msg.get("tool_calls", None)
                    if content is None:
                        content = ""
                    usage = data.get("usage", {})
                    return {"text": content, "think": think, "model": model_id,
                            "elapsed": round(time.time() - t0, 2), "error": None,
                            "tool_calls": tool_calls,
                            "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                                      "completion_tokens": usage.get("completion_tokens", 0),
                                      "total_tokens": usage.get("total_tokens", 0)}}
                else:
                    return {"text": "", "think": "", "model": model_id,
                            "elapsed": round(time.time() - t0, 2),
                            "error": f"API error: {data.get('error', data)}",
                            "tool_calls": None}
        except Exception as e:
            return {"text": "", "think": "", "model": model_id,
                    "elapsed": round(time.time() - t0, 2), "error": str(e),
                    "tool_calls": None}
        finally:
            if session and not session.closed:
                await session.close()

    async def close(self):
        pass
