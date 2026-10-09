# -*- coding: utf-8 -*-
"""填完 Key 之后跑这个 —— 挨个验一遍, 告诉你"哪个能用、哪个不行、该改哪儿"。

用法 (Windows):
    双击  check_keys.bat
    或命令行: python check_keys.py

它做什么:
  读 keys.json 里每个"模型槽" → **真发一次最小请求** (1 个 token) → 用中文报结果。

它不做什么:
  · 不改 keys.json, 不写任何配置 (只读)
  · 不回显你的 Key (出错信息也会先抹掉 Key 痕迹)
  · 不消耗可感知的额度 (每次 1 个 token)

退出码: 0 = 至少一家可用；1 = 一家都没有可用 (那时引擎起不来, 先照提示修)
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

GOLD, GREEN, RED, DIM, RST = "\033[93m", "\033[92m", "\033[91m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        GOLD = GREEN = RED = DIM = RST = ""


def load_config() -> tuple[dict, str]:
    """读配置 —— 与引擎同一优先序: keys.enc → keys.json。返回 (config, 来源说明)。

    ★ 2026-10-09: 来源说明原来写死成 "keys.enc / keys.json" —— 收件人读起来像
      一个叫「keys.enc / keys.json」的文件, 反而更糊涂 (实测: 用户会问这是啥)。
      现在说清**实际用的那一个**。
    """
    def _which() -> str:
        return "keys.enc（加密凭据包）" if (ROOT / "keys.enc").exists() else "keys.json"

    try:
        from main import _load_config
        cfg = _load_config() or {}
        if cfg:
            return cfg, _which()
    except Exception:
        pass
    for name in ("keys.enc", "keys.json"):
        p = ROOT / name
        if not p.exists():
            continue
        if name == "keys.json":
            try:
                return json.loads(p.read_text(encoding="utf-8")), "keys.json"
            except Exception as e:
                return {}, f"keys.json 读不了: {e}"
    return {}, "没找到 keys.json"


def _found_msg() -> str:
    """没读到时给一句能指路的话 (收件人第一次双击就在这)"""
    return (f"没找到配置文件 —— 这个文件夹里既没有 keys.json 也没有 keys.enc。\n"
            f"    先双击 install.py 跑一次向导 (它会从模板生成 keys.json),\n"
            f"    或者起界面后在左栏「模型 · 设置」里配一家。")


MODEL_SLOT_KEYS = ("key", "api_key", "url", "base_url", "model")


def is_model_slot(slot: str, cfg) -> bool:
    from core.provider_catalog import is_config_slot
    if not isinstance(cfg, dict):
        return False
    if not is_config_slot(slot):
        return False
    return any(k in cfg for k in MODEL_SLOT_KEYS)


def main() -> int:
    print(f"{GOLD}Tiger.M.M —— Key 自检{RST}")
    print("=" * 66)
    cfg, src = load_config()
    slots = {k: v for k, v in cfg.items() if is_model_slot(k, v)}

    if not slots:
        if src.startswith("没找到"):
            print(f"{RED}{_found_msg()}{RST}")
        else:
            print(f"{RED}没读到任何模型配置{RST} (来源: {src})")
        print("\n下一步:")
        print("  1. 起界面 → 左栏「模型 · 设置」→ 选一家、粘 Key、点「测一下」当场验（不用改文件、不用重启）")
        print("  2. 或者跑一次向导生成模板: 双击 install.py（也可复制 keys_template.json 为 keys.json）")
        print("  3. 命令行复核: 再跑一次 check_keys.bat")
        print("\n提示: 只想先试试、不想花钱 → 装本地 Ollama 即可，不用填任何 Key。")
        return 1

    from core.key_probe import probe_slot_sync
    from core.provider_catalog import is_placeholder_key

    # ★ 占位符/空值的槽**不发请求** —— 模板里那句示例就是"还没填"的意思。
    #   (踩过: 拿占位符去请求 → 报 'latin-1' codec can't encode characters, 用户看不懂)
    def _k(sc):
        return sc.get("key", sc.get("api_key", ""))

    filled = {k: v for k, v in slots.items() if not is_placeholder_key(_k(v))}
    unfilled = sorted(k for k in slots if k not in filled)
    if not filled:
        print(f"{GOLD}还没填任何真 Key{RST} (来源: {src})")
        print(f"\n模板里这 {len(unfilled)} 个槽还空着: {', '.join(unfilled)}")
        print("\n下一步 (二选一):")
        print("  A. 有钱花: 起界面 → 左栏「模型 · 设置」→ 选一家、粘 Key、点「测一下」当场验、保存即生效")
        print("     (不用改文件、不用重启; 想直接编辑 keys.json 也行 —— 引擎按那一行的 url 认家, 不认 Key 长相)")
        print("  B. 不花钱: 装本地 Ollama, 再在同一个面板里点「刷新」, 挑个模型点「用这个模型」")
        print("  改完再跑一次 check_keys.bat")
        return 1

    print(f"配置来源: {src}   待验: {len(filled)} 段"
          + (f"   (另有 {len(unfilled)} 段没填: {', '.join(unfilled)})" if unfilled else "") + "\n")
    ok_slots, bad_slots = [], []
    for slot, sc in sorted(filled.items()):
        # 统一成 probe 认的字段名
        norm = {"key": sc.get("key", sc.get("api_key", "")),
                "url": sc.get("url", sc.get("base_url", "")),
                "model": sc.get("model", "")}
        r = probe_slot_sync(slot, norm)
        mark = f"{GREEN}✓ 可用{RST}" if r["ok"] else f"{RED}✗ 不行{RST}"
        how = {"url": "按地址认出", "name": "按槽位名认出", None: "认不出"}[r["identified_by"]]
        print(f"  {mark}  {GOLD}{slot}{RST}")
        print(f"         这家: {r['provider_name']}  ({how})")
        print(f"         模型: {r['model'] or '(没填)'}")
        lat = f"  用时 {r['latency']}s" if r.get("latency") else ""
        print(f"         结果: {r['message']}{lat}")
        if not r["ok"] and r.get("doc"):
            print(f"         {DIM}该家的 Key 在这拿: {r['doc']}{RST}")
        (ok_slots if r["ok"] else bad_slots).append(slot)
        print()

    print("=" * 66)
    if ok_slots:
        print(f"{GREEN}可以跑了。{RST}可用的: {', '.join(ok_slots)}")
        if bad_slots:
            print(f"{GOLD}这些还没通 (不影响启动, 只是用不到):{RST} {', '.join(bad_slots)}")
            print("  → 上面每段都写了「该改哪儿」「该去哪拿 Key」。改完再跑一次即可。")
        return 0

    print(f"{RED}一段都没通 —— 引擎还起不来。{RST}")
    print("\n最可能的三种情况, 按顺序对一遍:")
    print("  ① 地址和 Key 不配对 —— 把 Key 粘进了别家的槽 (地址决定厂商, 见上面「这家:」)")
    print("  ② Key 从没开通或已过期 —— 去上面给的「该家的 Key 在这拿」地址重新生成一个")
    print("  ③ 一分钱不想花 —— 装本地 Ollama (地址 http://127.0.0.1:11434/v1) 就能跑")
    return 1


if __name__ == "__main__":
    sys.exit(main())
