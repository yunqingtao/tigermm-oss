# -*- coding: utf-8 -*-
"""Key 探针 —— 真发一次**最小**请求, 让那家自己说"这把 Key 能不能用"。

设计原则:
  · 只发 1 个 token, 不产生可读内容 —— 成本≈0, 不消耗额度感知。
  · 不猜、不自证: 结论来自对方服务器, 本地只负责把地址填对、把话翻成人话。
  · **绝不回显 Key**: 出错信息统一过 core.llm_errors.scrub。
  · 只读: 不改 keys.json、不写任何配置。

两个入口:
  probe_slot_sync(slot, cfg)   给 check_keys.py / install.py (无事件循环)
  probe_slot(slot, cfg)        给引擎内部 (async)
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from core import llm_errors
from core.provider_catalog import display_name, identify

TIMEOUT = 25


def _chat_url(url: str, protocol: str) -> str:
    base = (url or "").rstrip("/")
    if protocol == "ollama":
        return base.replace("/v1", "") + "/api/chat"
    if "/v1" in base:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _tags_url(url: str) -> str:
    return (url or "").rstrip("/").replace("/v1", "") + "/api/tags"


def _result(slot, cfg, ok, *, status=None, raw="", msg="", extra=None):
    prov, how = identify(slot, cfg.get("url", ""))
    r = {
        "slot": slot,
        "ok": bool(ok),
        "url": cfg.get("url", ""),
        "model": cfg.get("model", ""),
        "provider_id": prov["id"] if prov else None,
        "provider_name": prov["name"] if prov else display_name(slot, cfg.get("url", "")),
        "identified_by": how,
        "doc": prov.get("doc", "") if prov else "",
        "protocol": prov.get("protocol", "openai") if prov else "openai",
        "status": status,
        "message": msg,
        "raw": llm_errors.scrub(raw or ""),
    }
    if extra:
        r.update(extra)
    return r


def probe_slot_sync(slot: str, cfg: dict) -> dict:
    """同步探一次。cfg = {"key","url","model"}。返回 dict, 永不抛。"""
    cfg = cfg or {}
    url = str(cfg.get("url") or "").strip()
    key = str(cfg.get("key") or "").strip()
    model = str(cfg.get("model") or "").strip()
    prov, _ = identify(slot, url)
    protocol = prov.get("protocol", "openai") if prov else "openai"

    if not url:
        return _result(slot, cfg, False, msg=llm_errors.explain("没填 url", slot, url))
    from core.provider_catalog import is_placeholder_key, is_ascii_key
    if protocol != "ollama" and is_placeholder_key(key):
        return _result(slot, cfg, False, msg=llm_errors.no_key_message(slot, url))
    if protocol != "ollama" and not is_ascii_key(key):
        # ★ 踩过: 模板里的占位符/复制时带上的中文说明 → HTTP 头塞不进非 ASCII,
        #   原来报的是 `'latin-1' codec can't encode characters` (用户看不懂)。
        return _result(slot, cfg, False,
                       msg=(f"「{slot}」的 key 里混入了中文/全角字符 —— 那不是真的 API Key。"
                            f"位置: keys.json → \"{slot}\" → \"key\"（只粘那家控制台给的那一串字符）"))

    t0 = time.time()

    # ── 本地 Ollama: 不验 Key, 验"这个模型在不在本机" ──
    if protocol == "ollama":
        try:
            with urllib.request.urlopen(_tags_url(url), timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            names = [m.get("name", "") for m in data.get("models", [])]
            short = [n.split(":")[0] for n in names]
            hit = (model in names) or (model.split(":")[0] in short)
            if not model:
                return _result(slot, cfg, True, msg=f"本地 Ollama 在跑 (已装 {len(names)} 个模型), 但这段没填 model")
            if hit:
                return _result(slot, cfg, True, msg=f"本地模型在: {model}",
                               extra={"latency": round(time.time() - t0, 2),
                                      "available_models": names[:20]})
            return _result(slot, cfg, False,
                           msg=(f"本地 Ollama 在跑, 但**没有** {model} 这个模型。"
                                f"已装: {', '.join(names[:8]) or '(空)'}"
                                f"｜装一个: ollama pull {model or 'qwen2.5:7b'}"),
                           extra={"available_models": names[:20]})
        except Exception as e:
            return _result(slot, cfg, False, raw=str(e),
                           msg=("连不上本地 Ollama。位置: " + url +
                                " —— ① Ollama 没启动 ② 端口不是 11434。"
                                "启动后重试（本地模型不花钱, 适合先试通再填别家的 Key）。"))

    # ── OpenAI 兼容 (绝大多数) ──
    target = _chat_url(url, protocol)
    payload = {"model": model or "gpt-4o-mini",
               "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 1, "stream": False}
    if "mimo" in (model or "").lower() or (prov and prov["id"] == "mimo"):
        payload.pop("max_tokens", None)
        payload["max_completion_tokens"] = 1
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    req = urllib.request.Request(target, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = {}
        try:
            data = json.loads(body)
        except Exception:
            pass
        ok = bool(data.get("choices")) or bool(data.get("message"))
        model_back = data.get("model", model)
        return _result(slot, cfg, ok,
                       status=resp.status,
                       raw="" if ok else body,
                       msg=(f"可用 (那家认了这把 Key, 回的模型名: {model_back})" if ok
                            else "对方回了 200 但内容看不懂 —— 可能不是 OpenAI 兼容接口"),
                       extra={"latency": round(time.time() - t0, 2)})
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return _result(slot, cfg, False, status=e.code, raw=body,
                       msg=llm_errors.explain(body, slot, url, status=e.code),
                       extra={"latency": round(time.time() - t0, 2)})
    except urllib.error.URLError as e:
        return _result(slot, cfg, False, raw=str(e.reason or e),
                       msg=llm_errors.explain(str(e.reason or e), slot, url),
                       extra={"latency": round(time.time() - t0, 2)})
    except (socket.timeout, TimeoutError):
        return _result(slot, cfg, False, msg=llm_errors.explain("timed out", slot, url),
                       extra={"latency": round(time.time() - t0, 2)})
    except Exception as e:
        return _result(slot, cfg, False, raw=str(e),
                       msg=llm_errors.explain(str(e), slot, url),
                       extra={"latency": round(time.time() - t0, 2)})


async def probe_slot(slot: str, cfg: dict) -> dict:
    """异步包装 (引擎里用) —— 实现是同一份, 丢线程池跑。"""
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: probe_slot_sync(slot, cfg))


def probe_all(config: dict, only_slots=None) -> list:
    """把 config (keys.json 解析后的 dict) 里每个**模型槽**探一遍。

    只探"像模型槽"的段 (跳过 cli/mail/sms/notify/github/_说明 之类)。
    """
    from core.provider_catalog import is_config_slot
    out = []
    for slot, cfg in (config or {}).items():
        if not isinstance(cfg, dict) or "key" not in cfg and "url" not in cfg:
            continue
        if not is_config_slot(slot):
            continue
        if only_slots and slot not in only_slots:
            continue
        out.append(probe_slot_sync(slot, cfg))
    return out
