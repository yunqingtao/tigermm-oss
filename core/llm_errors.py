# -*- coding: utf-8 -*-
"""把模型接口返回的原始错误, 翻成人话, 并**抹掉 Key 痕迹**。

两个必须解决的具体问题 (实测):
  ① 现在用户看到的是: `API error: {'message': 'Authentication Fails, Your api key:
     ****3456 is invalid (request_id: ...)', 'type': 'authentication_error', ...}`
     —— 英文、还**回显了 Key 的尾 4 位**。
  ② 用户不知道该改哪儿: 报错里没有"你填的地址是 X, 该家说 Key 不对"这层信息,
     而"Key 填错槽/地址不是这家的"恰恰是最常见的原因。

本模块只做**文本处理** —— 不联网、不改行为, 因此可以随便被 import。
"""

from __future__ import annotations

import re

# ── Key 抹除 ──────────────────────────────────────────────────────────────
#: 覆盖各家回显 Key 的常见形态: `api key: xxx`, `****3456`, `sk-abcdef…`, `key[abcd…]`
_KEY_PATTERNS = [
    re.compile(r"(api[_\s-]?key[\"'\s:=]*)([A-Za-z0-9_\-\.]{6,})", re.I),
    re.compile(r"(key[\"'\s:=]*)([A-Za-z0-9_\-\.]{8,})", re.I),
    re.compile(r"\*{2,}([A-Za-z0-9]{2,})"),          # ****3456
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b(gsk_|xai-|sk-or-)[A-Za-z0-9_\-]{6,}"),
]


def scrub(text: str) -> str:
    """抹掉任何看起来像 Key 的片段 (含尾号)。宁可多抹, 不可漏。"""
    if not text:
        return ""
    s = str(text)
    s = _KEY_PATTERNS[0].sub(lambda m: f"{m.group(1)}[已隐藏]", s)
    s = _KEY_PATTERNS[1].sub(lambda m: f"{m.group(1)}[已隐藏]", s)
    s = _KEY_PATTERNS[2].sub("[已隐藏]", s)
    s = _KEY_PATTERNS[3].sub("[已隐藏]", s)
    s = _KEY_PATTERNS[4].sub("[已隐藏]", s)
    return s


# ── 人话翻译 ──────────────────────────────────────────────────────────────
_STATUS_HINT = {
    400: "请求被拒 —— 多半是 model 名字不对 (那家没有这个模型)",
    401: "Key 无效 —— 这把 Key 不是这家的, 或已失效/被吊销",
    402: "余额或额度不足 —— 去那家控制台看一下",
    403: "被拒绝 —— Key 没有这个模型的权限, 或所在地区不允许",
    404: "地址不对 —— 这家没有这个接口路径 (检查 url 结尾要不要 /v1)",
    408: "超时 —— 网络慢或那家在抖, 重试一次",
    429: "太快了 —— 频率或额度超限, 等一会儿再试",
    500: "那家服务器报错 (不是你的问题), 过会儿再试",
    502: "网关错误 (那家在维护?)",
    503: "那家暂时不可用",
    504: "那家网关超时",
}

#: ★ "没有这个模型" 的判据 (两种语序都要认: `model X not found` 与 `no such model`)。
#   ⚠ 别用 `[^.]` 排除点号 —— 模型名本来就带点 (`qwen2.5:7b`), 排了点号真报错反而匹配不上。
_MODEL_MISSING = re.compile(
    r"(model|模型)[^\n]{0,60}(not[ _]?found|does not exist|not exist|unknown|不存在|未找到|没有)"
    r"|no such (model|模型)"
    r"|(不存在|未找到|没有)[^\n]{0,20}(这个)?(模型|model)"
    r"|invalid[^\n]{0,20}model"
    , re.I)

_CONN_HINT = [
    (r"name or service not known|nodename nor servname|getaddrinfo|dns",
     "地址解析不了 —— url 里的域名可能写错了"),
    (r"connection refused|拒绝",
     "连不上 —— 地址或端口不对, 或那个服务没在跑 (本地 Ollama 常见)"),
    (r"timed out|timeout",
     "超时 —— 网络不通, 或需要走代理"),
    (r"ssl|certificate",
     "证书/SSL 出错 —— 地址写成了 http/https 不匹配, 或被中间人拦"),
    (r"proxy",
     "代理出错 —— 检查一下系统代理设置"),
]


def explain(raw: str, slot: str = "", url: str = "", status=None) -> str:
    """把原始错误 → 中文一句话 + 该改哪儿。

    raw    : 厂商返回的原始错误文本 (或异常字符串)
    slot   : keys.json 里的槽位名
    url    : 该槽填的地址
    status : HTTP 状态码 (拿不到就 None)
    """
    from core.provider_catalog import display_name  # 延迟 import, 避免环

    where = f"「{slot}」→ {url or '(没填 url)'}"
    who = display_name(slot, url)
    body = scrub(raw or "").strip()
    low = body.lower()

    # ⓿ ★ 2026-10-09: "没有这个模型" 要压过状态码判据。
    #   实测 (本地 Ollama 未 pull 模型): 接口返 404 + 正文 `model 'qwen2.5:7b' not found`,
    #   而 404 的通用话术是"地址不对 —— 检查 url 结尾要不要 /v1" ⇒ **把用户指去改本来
    #   就对的地址**。正文比状态码更具体, 谁具体听谁的。
    #   ⚠ 判据踩过: 最初写成 `(model|模型)[^.\n]{0,48}(not found|…)` —— `[^.]` 里那个点
    #   把 `qwen2.5` 里的点当句号挡住 ⇒ 真报错反而匹配不上。模型名**本来就带点**, 别排点。
    if _MODEL_MISSING.search(low):
        # 本地 Ollama 的家名本身就带 "本地 Ollama", 别再重复一遍括号 (读着像结巴)。
        _tail = "还没 pull 过这个模型" if "ollama" in who.lower() else "或(本地 Ollama)还没 pull 过"
        return (f"{who} 那边**没有这个模型** —— 模型名写错, {_tail}。"
                f"位置: {where}｜那家原话: {body[:200]}")

    # ① HTTP 状态码优先 (最确定)
    if status and status in _STATUS_HINT:
        head = _STATUS_HINT[status]
        if status == 401:
            head += f"；这一行填的地址是 {who} 的"
        return f"{head}。位置: {where}｜那家原话: {body[:200]}"

    # ② 连接类异常
    low = body.lower()
    for pat, hint in _CONN_HINT:
        if re.search(pat, low):
            return f"{hint}。位置: {where}｜原文: {body[:200]}"

    # ③ 文本里能看出的语义
    if re.search(r"authentication|unauthorized|invalid.*(api|key)|api.?key.*(invalid|incorrect)", low):
        return (f"Key 无效 —— 这把 Key 在 {who} 那边没通过。"
                f"确认它是哪家的、有没有填错槽。位置: {where}｜那家原话: {body[:200]}")
    if re.search(r"insufficient|balance|quota|额度|余额", low):
        return f"余额/额度不足。位置: {where}｜那家原话: {body[:200]}"
    if re.search(r"model.*(not.*(found|exist)|invalid)|does not exist", low):
        return f"model 名字不对 —— 这家没有这个模型名。位置: {where}｜那家原话: {body[:200]}"
    if re.search(r"rate.?limit|too many requests", low):
        return f"频率超限, 稍后再试。位置: {where}｜那家原话: {body[:200]}"

    return f"请求失败。位置: {where}｜那家原话: {body[:240]}"


def no_key_message(slot: str, url: str = "") -> str:
    """槽里没填 Key —— 说清是哪个槽、去哪儿填。"""
    from core.provider_catalog import display_name
    who = display_name(slot, url)
    return (f"「{slot}」这一段没填 API Key。位置: keys.json → \"{slot}\" → \"key\""
            f"（这一行填的地址是 {who} 的；把该家的 Key 粘进去即可）")


def unknown_slot_message(slot: str) -> str:
    return (f"keys.json 里没有「{slot}」这一段 —— 要么写错了名字, 要么还没配。"
            f"在 keys.json 加一段 {{\"{slot}\": {{\"key\": \"...\", \"url\": \"...\", \"model\": \"...\"}}}}")
