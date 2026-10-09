# -*- coding: utf-8 -*-
"""模型配置的读写与清单 —— 给网页「设置模型」页用。

为什么单独成模块（而不是写进 web_server.py）:
  · 网页设置页、`check_keys.py`、`install.py` 必须共用**同一套判据**；
    三处各判一套必然走岔（踩过：占位符判据分散两套，同一样东西两种结论）。
    本模块的判据全部来自 `core/provider_catalog.py`（唯一一处）。
  · 收件人**不该手改 keys.json**。网页上点选、粘贴、测试、保存即可；
    但 keys.json 仍是最终真相 —— 本模块只负责"安全地改那一个字段"。

写盘规矩（踩过才写）:
  · 改前先备份 → `keys.json.bak-<时间戳>`；改坏了能回退。
  · 只动指定槽位，其余原样保留（含 `_说明` 这类注释式键，别把它们洗掉）。
  · **永不回显 Key 全文** —— 界面只拿掩码（`mask_key`）。
  · 不猜厂商：认家只按 url（见 provider_catalog 的 by_url / identify）。

约定: 纯标准库 + 本仓 core/provider_catalog —— 分发包里要能直接跑。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.request
from pathlib import Path

from core.provider_catalog import (
    PROVIDERS,
    display_name,
    identify,
    is_config_slot,
    is_placeholder_key,
)

#: 仓库根（本文件在 <root>/core/ 下）
ROOT = Path(__file__).resolve().parent.parent
KEYS_NAME = "keys.json"


# ── 路径与读取 ────────────────────────────────────────────────────────────

def keys_path() -> Path:
    """收件人编辑的那份 keys.json（仓库根）。"""
    return ROOT / KEYS_NAME


def template_path() -> Path:
    """首次运行向导用来生成 keys.json 的模板。"""
    return ROOT / "keys_template.json"


def load_keys(path: Path | None = None) -> dict:
    """读配置。读不了/坏了回落到**模板**（**不抛** —— 界面要能照常显示各家让人填）。

    ★ 2026-10-09 修 (实测缺陷): 原来没有 keys.json 时直接返回 {} ⇒ 设置面板
      **一片空白**(0 家) ⇒ 收件人第一天打开面板没东西可填, 得先猜着去跑一次向导。
      现在回落到 keys_template.json: 面板照常列出各家(都标"还没填"),
      填一家点保存时再真正生成 keys.json (见 save_slot)。
    """
    p = Path(path) if path else keys_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
    except Exception:
        pass
    # 没有(或读坏了) → 借模板的骨架, Key 一律清空(模板本来就空, 这里再保险一道)
    try:
        t = template_path()
        if t.exists():
            tpl = json.loads(t.read_text(encoding="utf-8"))
            if isinstance(tpl, dict):
                for _v in tpl.values():
                    if isinstance(_v, dict) and "key" in _v:
                        _v["key"] = ""
                return tpl
    except Exception:
        pass
    return {}


def mask_key(val) -> str:
    """给人看的掩码 —— 前 5 后 4，中间打点。绝不返回全文。"""
    s = str(val or "").strip()
    if not s:
        return ""
    if len(s) <= 10:
        return "•" * len(s)
    return f"{s[:5]}…{s[-4:]}"


def is_model_slot(slot: str, cfg) -> bool:
    """是不是"模型槽"（而不是 mail / sms / cli / notify 这类配置段）。

    两个条件都要满足，与 web_server.py 读配置的过滤同源:
      ① 槽位名不像 shell 段（provider_catalog.is_config_slot）
      ② 那段里有 key/api_key 字段（这才是模型槽的形态）
    """
    if not isinstance(cfg, dict):
        return False
    return bool(is_config_slot(slot)) and ("key" in cfg or "api_key" in cfg)


# ── 清单（给网页渲染）────────────────────────────────────────────────────

def rows(config: dict | None = None) -> list:
    """模型槽清单 —— 每个槽一条，够网页直接画卡片。

    字段:
      slot/name/known/provider_id/protocol/doc/models  → 认家信息
      url/model                                        → 现值
      key_mask/placeholder/key_set/usable              → Key 状态（只给掩码）
    """
    cfg = load_keys() if config is None else (config or {})
    # ★ ollama 是特例: 模板里它的 key 写的是占位词 "ollama"（本就不需要真 Key），
    #   所以"key 在场"不代表能用 —— 得看本机 11434 真回不回话。
    #   不看的话，一台**没装 Ollama** 的机器上 ollama 槽会被判成"已配好"，
    #   顶部"还没配模型"提示条就不弹了，收件人点进去发消息只会拿到失败。
    try:
        _ol = ollama_status()
    except Exception:
        _ol = {"running": False}
    out = []
    for slot, v in cfg.items():
        if not is_model_slot(slot, v):
            continue
        url = str(v.get("url") or v.get("base_url") or "").strip()
        keyval = v.get("key", v.get("api_key", "")) or ""
        has = bool(str(keyval).strip())
        prov, _how = identify(slot, url)
        proto = (prov or {}).get("protocol", "openai")
        ok_key = has and not is_placeholder_key(keyval)
        note = ""
        if proto == "ollama" and ok_key and not _ol.get("running"):
            ok_key = False
            note = "本机 Ollama 没在跑（装了才有这一家）"
        out.append({
            "slot": slot,
            "name": prov["name"] if prov else display_name(slot, url),
            "known": bool(prov),
            "provider_id": prov["id"] if prov else None,
            "protocol": proto,
            "doc": (prov or {}).get("doc", ""),
            "models": list((prov or {}).get("models", []) or []),
            "url": url,
            "model": str(v.get("model") or "").strip(),
            "key_mask": mask_key(keyval),
            "key_set": has and not is_placeholder_key(keyval),
            #: 有值但看着是模板里的示例/说明文字 —— 界面要提示"这行还没换成你自己的"
            "placeholder": has and is_placeholder_key(keyval),
            "usable": ok_key,
            #: 为什么"看着有 Key 却不能用"（如 ollama 没跑）—— 界面直接显示这句
            "note": note,
        })
    # 能用的排前面；其次按槽位名稳定排序（界面顺序别乱跳）
    out.sort(key=lambda r: (not r["usable"], r["protocol"] != "ollama", r["slot"]))
    return out


def summary(config: dict | None = None) -> dict:
    """一句话状态 —— 界面顶部那条。"""
    r = rows(config)
    usable = [x["slot"] for x in r if x["usable"]]
    return {
        "total": len(r),
        "usable": usable,
        "usable_count": len(usable),
        "configured": bool(usable),
    }


# ── 写盘 ──────────────────────────────────────────────────────────────────

def _backup(p: Path) -> Path | None:
    """改前留一份。返回备份路径（没有文件可备份时 None）。"""
    if not p.exists():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = p.with_name(f"{p.name}.bak-{stamp}")
    try:
        shutil.copy2(p, dst)
        return dst
    except Exception:
        return None


def save_slot(slot: str, key=None, url=None, model=None) -> dict:
    """只改一个槽的那几个字段，其余原样。

    · key=None   → 不动；key="" → 清空这个槽的 Key
    · url/model=None → 不动；空串 → 不动（地址/模型名不该被空串覆盖成空）
    · 返回 {"ok","slot","backup"}；槽位不存在则抛 KeyError（界面据此提示）

    ★ 2026-10-09: 还没有 keys.json 时(裸奔第一次配), load_keys() 会回落模板 ⇒
      这里第一次保存就把**整份 keys.json** 写出来(骨架来自模板, 只有这一家带 Key)。
      等于替收件人顺手跑了向导 —— 不用先猜着去双击 install.py。
    """
    slot = (slot or "").strip()
    if not slot:
        raise ValueError("槽位名为空")
    p = keys_path()
    cfg = load_keys()
    if slot not in cfg:
        raise KeyError(f"配置里没有「{slot}」这一段")
    if not isinstance(cfg[slot], dict):
        raise KeyError(f"「{slot}」不是一段配置")

    cur = cfg[slot]
    if key is not None:
        cur["key"] = str(key).strip()
    if url is not None and str(url).strip():
        cur["url"] = str(url).strip()
    if model is not None and str(model).strip():
        cur["model"] = str(model).strip()

    backup = _backup(p)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "slot": slot, "backup": backup.name if backup else None}


# ── 真探测（复用 check_keys 那一套）──────────────────────────────────────

def test_slot(slot: str, cfg: dict | None = None) -> dict:
    """真发一次最小请求，问那家"这把 Key 能不能用"。

    复用 `core/key_probe.probe_slot_sync` —— 与 check_keys.bat 完全同一条路，
    所以界面上的结论和命令行验出来的**必然一致**。永不抛。
    """
    slot = (slot or "").strip()
    full = load_keys()
    one = cfg if isinstance(cfg, dict) else full.get(slot) or {}
    if not isinstance(one, dict) or not one:
        return {"slot": slot, "ok": False, "message": f"配置里没有「{slot}」这一段"}
    t0 = time.time()
    try:
        from core.key_probe import probe_slot_sync

        r = probe_slot_sync(slot, one)
    except Exception as e:  # 探测层自己崩了也要给人话
        r = {"slot": slot, "ok": False, "message": f"探测失败（{type(e).__name__}）: {e}"}
    r["elapsed"] = round(time.time() - t0, 2)
    return r


# ── 本地 Ollama（零成本那条路）──────────────────────────────────────────

OLLAMA_BASE = "http://127.0.0.1:11434"


def ollama_status() -> dict:
    """本机 Ollama 在不在、装了哪些模型、有没有 ollama 命令。

    "在不在"只认 11434 端口真回话（不是"装了"就算）—— 装了没起也是没起。
    """
    st = {"running": False, "base": OLLAMA_BASE, "models": [], "cli": False, "error": ""}
    try:
        import shutil as _sh

        st["cli"] = bool(_sh.which("ollama"))
    except Exception:
        pass
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=4) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        st["running"] = True
        st["models"] = sorted(
            str(m.get("name") or "") for m in (data.get("models") or []) if m.get("name")
        )
    except Exception as e:
        st["error"] = f"{type(e).__name__}"
    return st


#: 拉模型的进度（后台线程写，界面轮询读）—— 模型几个 GB，不能卡住 HTTP 请求
PULL = {"running": False, "model": "", "done": False, "ok": None, "log": [], "started": 0.0}


def pull_state() -> dict:
    """给界面轮询的拉取状态（只有最后 20 行输出）。"""
    return {
        "running": PULL["running"],
        "model": PULL["model"],
        "done": PULL["done"],
        "ok": PULL["ok"],
        "log": PULL["log"][-20:],
        "elapsed": round(time.time() - PULL["started"], 1) if PULL["started"] else 0,
    }


def start_pull(model: str) -> dict:
    """后台 `ollama pull <model>`。返回是否已开跑。"""
    model = (model or "").strip()
    if not model:
        return {"ok": False, "message": "没给模型名"}
    if PULL["running"]:
        return {"ok": False, "message": f"正在拉 {PULL['model']}，等它跑完"}
    import shutil as _sh

    exe = _sh.which("ollama")
    if not exe:
        return {"ok": False, "message": "本机没有 ollama 命令 —— 先去 ollama.com 装 Ollama"}

    PULL.update({"running": True, "model": model, "done": False, "ok": None,
                 "log": [f"$ ollama pull {model}"], "started": time.time()})

    def _run():
        import subprocess

        try:
            pr = subprocess.Popen([exe, "pull", model], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                  errors="replace", bufsize=1)
            for line in iter(pr.stdout.readline, ""):
                PULL["log"].append(line.rstrip())
                if len(PULL["log"]) > 200:
                    del PULL["log"][:100]
            pr.wait(timeout=3600)
            PULL["ok"] = pr.returncode == 0
        except Exception as e:
            PULL["log"].append(f"✗ {type(e).__name__}: {e}")
            PULL["ok"] = False
        finally:
            PULL["running"] = False
            PULL["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "model": model}


# ── 热加载（改完不用重启）───────────────────────────────────────────────

def reload_engine(pipeline=None) -> dict:
    """把新的 keys.json 就地灌进运行中的引擎，省掉"改完要重启"。

    ModelClient 没有 reload 方法（配置在 __init__ 读一次），但它每次取配置是
    现读 `self.config` —— 所以就地换掉那个字典即可。换不掉就老实说"重启后生效"，
    绝不假装成功。
    """
    if pipeline is None:
        try:
            import web_server as _ws  # web_server.py 里是模块级全局

            pipeline = getattr(_ws, "pipeline", None)
        except Exception:
            pipeline = None
    if pipeline is None:
        return {"reloaded": False, "note": "引擎还没起 —— 下次启动就会读到新配置"}
    cfg = load_keys()
    try:
        pipeline.config = cfg
        mc = getattr(pipeline, "model_client", None)
        if mc is not None:
            mc.config = cfg
        return {"reloaded": True, "note": "已热加载，不用重启"}
    except Exception as e:
        return {"reloaded": False, "note": f"热加载失败，重启后生效（{type(e).__name__}）"}
