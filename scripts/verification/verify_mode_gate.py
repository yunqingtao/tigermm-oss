# -*- coding: utf-8 -*-
r"""三模式闸门 —— 常驻验证器 (2026-09-23 立)

命题 (2026-09-23 实测): TMM 被卡在 ask(问答) 模式下 —— 什么都不执行, 但用户看不出来,
以为"不灵"。界面上那句"只回答, 不执行任何操作"没人会去读。
本门禁钉住三件事:
  ① craft(实干): 要干活的请求必须到达**真实路由** (不是全被 mode.gate 拦下) 且真落盘
  ② ask(问答): 要干活的请求**当场说清不执行** (带切回实干的指引) —— 静默失效不可接受
  ③ ask: 纯问答**不加**提示 (输出保持干净, 不制造噪音)

★ 环境适应 (三态探针, 2026-09-23 补 —— 交付包内复核实测):
  ①/②/③ 都要真调模型。交付包**没有凭据** ⇒ 依赖不在位 ⇒ 这四段必须 **SKIP**,
  不许报红 (否则用户拿到的包一跑就红, 分不清"缺密钥"和"功能坏")。
  依赖**在位却调不通** ⇒ 那才是红 (静默跳过 = 假绿, 铁律)。
"""
import asyncio
import glob
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))
os.chdir(ROOT)
from _probe_env import activate          # noqa: E402
activate()
import logging                           # noqa: E402
logging.disable(logging.CRITICAL)

DESK = Path(os.path.expanduser("~/Desktop"))
P, F, S = [], [], []


def chk(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS " if c else "  FAIL ") + n + (("   <- " + str(d)) if d and not c else ""))


def skip(n, why):
    S.append(n)
    print(f"  SKIP {n}   <- {why}")


def _model_probe(PL):
    """三态: (可用, 原因, 依赖是否在位)。"""
    kf = ROOT / "keys.json"
    dep = any((kf.is_file(), os.environ.get("DASHSCOPE_API_KEY"),
               os.environ.get("QWEN_API_KEY"), os.environ.get("DEEPSEEK_API_KEY"),
               os.environ.get("OPENAI_API_KEY"), os.environ.get("MIMO_API_KEY")))
    last = ""
    for _ in range(2):
        try:
            r = asyncio.run(asyncio.wait_for(
                PL.model_client.generate(PL._mode_pick_model(None),
                                         [{"role": "user", "content": "ok"}], max_tokens=4),
                timeout=60))
            if isinstance(r, dict) and not r.get("error"):
                return True, "", bool(dep)
            last = str((r or {}).get("error"))[:90]
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:90]
        time.sleep(1)
    return False, last or "未知", bool(dep)


def main() -> int:
    print("=" * 88)
    print("三模式闸门 (ask / plan / craft)")
    print("=" * 88)

    MJ = ROOT / "data" / "mode.json"
    _bak = MJ.read_bytes() if MJ.exists() else None
    d0 = set(glob.glob(str(DESK / "*")))
    made = []
    try:
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        cfg = _load_config()
        PL = Level4Pipeline(ModelClient(cfg), cfg)
        # ★ 钉住实干 (与每处 mode_override 双保险) —— 用户态若漂到 ask 则全部题目"不执行"
        try:
            PL.modes.set("craft")
        except Exception:
            pass

        ok_m, err, dep = _model_probe(PL)
        if not ok_m:
            if dep:
                chk("★ 依赖在位却调不通模型 → 不许静默跳过真跑段", False, err)
                return 1
            for n in ("craft 不被 mode.gate 拦下", "craft 真执行并落盘",
                      "ask 要干活必带'问答模式'提示", "ask 带切回实干指引",
                      "ask 一个字都不落盘", "ask 纯问答不带提示"):
                skip(n, "本机无可用模型 (交付包无凭据/未配密钥) —— 非功能缺陷")

        # ── A. craft: 真实路由 + 真落盘 ──────────────────────
        if ok_m:
            print("\n[A.craft] 实干模式: 要干活的请求到达真实路由 + 真落盘")
            r = asyncio.run(PL.process("写一首诗放桌面", probe=True, mode_override="craft")) or {}
            chk("★ craft 下不被 mode.gate 拦下", r.get("route") != "mode.gate", r.get("route"))
            _after = sorted(set(glob.glob(str(DESK / "*"))) - d0)
            chk("★ craft 真的执行了 (桌面上出现产物)", bool(_after), _after)
            for q in _after:
                try:
                    Path(q).unlink(missing_ok=True)
                except Exception:
                    pass
        d1 = set(glob.glob(str(DESK / "*")))

        # ── B. ask: 要干活 → 当场说清 ────────────────────────
        if ok_m:
            print("\n[B.ask] 问答模式: 要干活的请求必须说清'不执行'")
            r2 = asyncio.run(PL.process("写一首诗放桌面", probe=True, mode_override="ask")) or {}
            _resp = str(r2.get("response") or "")
            chk("ask 下由 mode.gate 接手", r2.get("route") == "mode.gate", r2.get("route"))
            # ★ 看**提示语本身** ("只回话、不执行") —— "问答模式"四个字也出现在模型报错前缀里,
            #   用它判会误判 (交付包无凭据时实测踩到)。
            chk("★★ 带'只回话、不执行'提示 (静默失效不可接受)", "只回话、不执行" in _resp,
                _resp[-100:])
            chk("★★ 带切回实干的指引 (/mode craft)", "/mode craft" in _resp, _resp[-100:])
            chk("★★ ask 模式一个字都不落盘", not sorted(set(glob.glob(str(DESK / "*"))) - d1),
                sorted(set(glob.glob(str(DESK / "*"))) - d1))

            # ── C. ask: 纯问答不加提示 ───────────────────────
            print("\n[C.ask] 纯问答不制造噪音")
            r3 = asyncio.run(PL.process("什么是诗", probe=True, mode_override="ask")) or {}
            chk("纯问答**不带**模式提示", "只回话、不执行" not in str(r3.get("response") or ""))

        # ── D. 状态零副作用 ─────────────────────────────────
        print("\n[D.状态] 本门禁不许改用户状态")
        _left = sorted(set(glob.glob(str(DESK / "*"))) - d1)
        made.extend(_left)
        chk("★ 桌面零残留 (ask 段没落盘)", not _left, _left)
    finally:
        for q in made:
            try:
                Path(q).unlink(missing_ok=True)
            except Exception:
                pass
        if _bak is not None:
            MJ.write_bytes(_bak)
    chk("★ mode.json 已还原", (MJ.read_bytes() == _bak) if _bak else True)

    print("\n" + "=" * 88)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for x in F:
        print("  -", x)
    print("=" * 88)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
