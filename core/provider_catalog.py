# -*- coding: utf-8 -*-
"""厂商目录 —— **按地址认家**, 不猜 Key 前缀。

为什么按地址不按前缀:
  · `sk-` 前缀几乎所有 OpenAI 兼容厂商都在用 (DeepSeek / Kimi / 智谱 / 硅基流动 /
    OpenRouter …), 按前缀猜必错。
  · 唯一**确定**的事实是"这个 Key 被送去哪个地址、那个地址怎么回它" —— 代码里
    决定厂商的就是 keys.json 那一行的 `url`, 不是 Key 长什么样。
  · 所以本地能做的只有两件: ① 由 `url` 反查"这是哪家"(帮人确认识别);
    ② 真发一次请求, 让那家自己说"这把 Key 能不能用"。

用途:
  · `check_keys.py` / `core/key_probe.py` 验 Key 时报"这是哪家、该配哪个地址"。
  · `core/model_router.py` 把"用户自配的家"当成一等公民参与路由 (不再只认识
    写死的 4 个名字)。

约定: 纯标准库、无第三方依赖 (分发包里要能直接跑)。
"""

from __future__ import annotations

import re

# ── 厂商表 ────────────────────────────────────────────────────────────────
# urls: 地址片段 (小写子串匹配 —— 只比 host, 见 _host())
# protocol: "openai" = /v1/chat/completions 兼容; "ollama" = 本地 Ollama; "anthropic" = 非兼容协议
# key_hint: 仅用于**提示**, 绝不作为判据 (很多家共用 sk-)
PROVIDERS = [
    {
        "id": "deepseek", "name": "DeepSeek 深度求索",
        "hosts": ["api.deepseek.com"],
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "doc": "platform.deepseek.com → API Keys",
        "protocol": "openai", "key_hint": "sk-",
    },
    {
        "id": "mimo", "name": "MiMo 小米",
        "hosts": ["api.xiaomimimo.com", "xiaomimimo.com"],
        "models": ["mimo-v2.5"],
        "doc": "api.xiaomimimo.com → 控制台",
        "protocol": "openai", "key_hint": "",
    },
    {
        "id": "qwen", "name": "通义千问 Qwen (阿里云百炼)",
        "hosts": ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"],
        "models": ["qwen3.7-flash", "qwen-plus", "qwen-max"],
        "doc": "bailian.console.aliyun.com → API-KEY",
        "protocol": "openai", "key_hint": "sk-",
    },
    {
        "id": "openai", "name": "OpenAI",
        "hosts": ["api.openai.com"],
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1"],
        "doc": "platform.openai.com → API keys",
        "protocol": "openai", "key_hint": "sk-",
    },
    {
        "id": "moonshot", "name": "月之暗面 Kimi",
        "hosts": ["api.moonshot.cn", "api.moonshot.ai"],
        "models": ["kimi-k2-0905-preview", "moonshot-v1-8k"],
        "doc": "platform.moonshot.cn → API Key 管理",
        "protocol": "openai", "key_hint": "sk-",
    },
    {
        "id": "zhipu", "name": "智谱 GLM",
        "hosts": ["open.bigmodel.cn", "api.z.ai"],
        "models": ["glm-4.6", "glm-4-flash"],
        "doc": "open.bigmodel.cn → API Keys",
        "protocol": "openai", "key_hint": "",
    },
    {
        "id": "siliconflow", "name": "硅基流动 SiliconFlow",
        "hosts": ["api.siliconflow.cn", "api.siliconflow.com"],
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-8B"],
        "doc": "cloud.siliconflow.cn → API 密钥",
        "protocol": "openai", "key_hint": "sk-",
    },
    {
        "id": "openrouter", "name": "OpenRouter",
        "hosts": ["openrouter.ai", "api.openrouter.ai"],
        "models": ["deepseek/deepseek-chat", "openai/gpt-4o-mini"],
        "doc": "openrouter.ai/keys",
        "protocol": "openai", "key_hint": "sk-or-",
    },
    {
        "id": "groq", "name": "Groq",
        "hosts": ["api.groq.com"],
        "models": ["llama-3.3-70b-versatile"],
        "doc": "console.groq.com → API Keys",
        "protocol": "openai", "key_hint": "gsk_",
    },
    {
        "id": "xai", "name": "xAI Grok",
        "hosts": ["api.x.ai"],
        "models": ["grok-4", "grok-3-mini"],
        "doc": "console.x.ai → API Keys",
        "protocol": "openai", "key_hint": "xai-",
    },
    {
        "id": "together", "name": "Together AI",
        "hosts": ["api.together.xyz", "api.together.ai"],
        "models": ["deepseek-ai/DeepSeek-V3"],
        "doc": "api.together.xyz → Settings → API Keys",
        "protocol": "openai", "key_hint": "",
    },
    {
        "id": "minimax", "name": "MiniMax",
        "hosts": ["api.minimax.chat", "api.minimaxi.com"],
        "models": ["abab6.5s-chat"],
        "doc": "platform.minimaxi.com → 接口密钥",
        "protocol": "openai", "key_hint": "",
    },
    {
        "id": "ollama", "name": "本地 Ollama",
        "hosts": ["127.0.0.1:11434", "localhost:11434"],
        "models": ["qwen2.5:7b", "gemma4-12b-fullgpu:latest"],
        "doc": "本地装 Ollama 即可，无需 Key (零成本)",
        "protocol": "ollama", "key_hint": "",
    },
]

SHELL_SLOT_PREFIXES = ("_", "cli", "mail", "sms", "notify", "github", "web", "push")

def _host(url: str) -> str:
    """从 url 取 host[:port] (小写)。空/畸形返回 ''。"""
    s = (url or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/", 1)[0]
    return s


def by_url(url: str):
    """按地址认家 —— 唯一可靠的判据是它。命中返回厂商 dict, 否则 None。"""
    h = _host(url)
    if not h:
        return None
    for p in PROVIDERS:
        for frag in p["hosts"]:
            if frag in h:
                return p
    return None


def by_slot_name(slot: str):
    """槽位名恰好等于厂商 id 时也算认出来 (弱判据, 只在地址认不出时兜底)。"""
    s = (slot or "").strip().lower()
    for p in PROVIDERS:
        if s == p["id"]:
            return p
    return None


def is_config_slot(slot: str) -> bool:
    """这个槽位名是不是"模型槽"(而不是 cli/mail/sms 之类配置段)。"""
    s = (slot or "").strip().lower()
    if not s or s.startswith(SHELL_SLOT_PREFIXES):
        return False
    return True


def identify(slot: str, url: str):
    """认家主入口 → (provider|None, how)  how ∈ {"url","name",None}"""
    p = by_url(url)
    if p:
        return p, "url"
    p = by_slot_name(slot)
    if p:
        return p, "name"
    return None, None


def display_name(slot: str, url: str) -> str:
    """给人看的家名: 认得出就用厂商名, 认不出老实说"不认识这家"。"""
    p, how = identify(slot, url)
    if p:
        return p["name"]
    h = _host(url)
    return f"不认识的地址({h})" if h else "没填地址"


def suggest_slots_for_hint(url: str) -> str:
    """地址认不出时, 给一句"这看着像哪家"的提示 (只做子串提示, 不下结论)。"""
    h = _host(url)
    if not h:
        return "还没填 url —— 每家厂商的地址不同, 必须填对"
    tail = h.split(".")[-2] if h.count(".") >= 2 else h
    for p in PROVIDERS:
        if tail and tail in p["id"]:
            return f"地址里含「{tail}」, 像是 {p['name']} —— 请核对"
    return "这个地址不在内置目录里。若它是 OpenAI 兼容的 (/v1/chat/completions), 可直接用, 只是引擎不会自动为它排任务"


def all_ids():
    return [p["id"] for p in PROVIDERS]


# ── 占位符判据（**唯一一处**，install.py / check_keys.py 共用）────────────────
#: 模板里的示例 Key / 用户没删干净的说明文字 —— 这些**不算已配置**, 更不能拿去发请求。
#: ★ 踩过: 曾经把空串放进 startswith 元组 → `startswith("")` 恒真 → 已配好的槽
#:   全被判成"还没填"(向导骗自己)。别把空串塞进来。
_PLACEHOLDER_HINTS = ("sk-把你的", "sk-your", "your-key", "placeholder",
                      "填这里", "填你的", "xxxx", "<")


def is_placeholder_key(val) -> bool:
    """这把"Key"是不是占位符/说明文字(而非真凭据)。空值也算。"""
    s = str(val or "").strip()
    if not s:
        return True
    low = s.lower()
    return any(h.lower() in low for h in _PLACEHOLDER_HINTS)


def is_ascii_key(val) -> bool:
    """HTTP 头只能放 ASCII —— 含中文/全角字符的 Key 一定不是真 Key, 直接拦下,
    免得抛 `'latin-1' codec can't encode characters` 这种看不懂的错。"""
    s = str(val or "")
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def usable_providers(config) -> list:
    """配置里**真能用**的家 (模型槽 + Key 非空非占位) —— 单一判据。

    ★ 2026-10-09 加: 收件人刚下载、**一个模型都没配**时, 引擎原来一律回
      "网络异常，已尝试ollama。" —— 那句话会把人带去查网络/WiFi, 而真因是"一家都没配"。
      判据必须只有这一处 (与 install.py / check_keys.py 同源), 否则三处各判一套必走岔。

    注意: 本函数只回答"**看着**配了没" (Key 在场且不像占位符), 不回答"这家通不通" ——
    通不通要真发一次请求才知道 (那是 core/key_probe.py 的活)。
    """
    out = []
    for slot, cfg in (config or {}).items():
        if not isinstance(cfg, dict):
            continue
        val = cfg.get("key", cfg.get("api_key", ""))
        if is_config_slot(slot) and str(val or "").strip() and not is_placeholder_key(val):
            out.append(slot)
    return out

