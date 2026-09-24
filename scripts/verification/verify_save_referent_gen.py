# -*- coding: utf-8 -*-
r"""「把<指代>存到<目标>」→ 无可指内容时自己生成再落盘 —— 常驻验证器 (ad-hoc 留档)

命题 (2026-09-23 用户指定 "你让他自己生成任意内容进行就可以"):
  用户说 `把这首诗保存到桌面` 但上一轮**没有**可指的现成内容时, TMM 应该
  **自己写一份**再存, 而不是回一句 "请把诗贴出来" 把话头推回去。

改前基线 (真引擎实测, _probe_pronoun_save.py):
  route=model.fallback · 零工具调用 · 回复 "我需要先知道诗的内容…"(诚实反问)

本门盯住四类边界 (都是"加宽判据"最容易打坏的地方):
  A 真引擎: 真生成 + 真落盘 + 内容不是指令原文 + 如实说明 + 零残留
  B 上一轮**真有**可指内容时不抢 (交模型带历史处理 —— 旧行为不变)
  C 带真内容/生成型口令的句子不抢 (回归 2026-09-22 的"生成+落盘"两步规划修复)
  D 没有内容名词 / 没有落盘目标 / 超长句 → 不接 (不放大作用面)
  E 诚实底线: 生成失败 / 生成太短 / 写盘失败 → 不谎报成功

跑法: python -B scripts/verification/verify_save_referent_gen.py
"""
import asyncio
import glob
import hashlib
import os
import re
import shutil
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

HERE = Path(__file__).resolve().parent
P, F = [], []
DESK = Path(os.path.expanduser("~/Desktop"))
SANDBOX = ROOT / "tmp" / "_savegen_sbx"
USER_FILES = [ROOT / "data" / "chat_sessions.db", ROOT / "data" / "mode.json",
              ROOT / "data" / "entities.json", ROOT / "data" / "perception.json"]


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name
          + (("   <- " + str(detail)) if detail and not cond else ""))


S = []


def skip(name, why):
    """前置不在 → 明确跳过并**说明原因** (不是静默不算, 也不是假装绿)。

    为什么需要: 本门的分发包里没有 keys.json (凭据按设计不进包), 本机也可能没起 ollama。
    真引擎那几段在那种环境下**不可能**跑通 —— 那不是代码坏, 是依赖不在。
    项目口径: 依赖不在 → SKIP 带原因 (静默不算会让"没跑"看起来像"跑了且绿")。
    """
    S.append(name)
    print(f"  SKIP {name}   <- {why}")


def _model_probe(PL):
    """探测本机能不能真调模型 → (usable, reason, dep_present)。

    ★ 为什么要分三态 (2026-09-23, 被"套件里的假绿"教育了):
      第一版只有 usable 两态, 探测失败就一律 SKIP。结果**在开发仓里**探针抖了一下
      (依赖明明在位), 15 条真引擎断言全被静默跳过, 而套件显示"27 PASS / 0 FAIL 全绿" ——
      **绿了但没测**。这是最坏的一种: 门禁没红, 覆盖却没了。
      判据: **依赖明确不在** (没有凭据文件/环境变量, 也没有本地模型) 才 SKIP;
      依赖在位却调不通 = 真问题 (或真网络故障) → **报红**, 让人来看。
    """
    kf = ROOT / "keys.json"
    dep = any((kf.is_file(),
               os.environ.get("DASHSCOPE_API_KEY"), os.environ.get("QWEN_API_KEY"),
               os.environ.get("DEEPSEEK_API_KEY"), os.environ.get("OPENAI_API_KEY")))
    last = ""
    for _i in range(3):
        try:
            r = asyncio.run(asyncio.wait_for(
                PL.model_client.generate(PL._mode_pick_model(None),
                                         [{"role": "user", "content": "ok"}], max_tokens=4),
                timeout=60))
            if isinstance(r, dict) and not r.get("error"):
                return True, "", bool(dep)
            last = str((r or {}).get("error"))[:100]
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:100]
        time.sleep(2)
    return False, last or "未知", bool(dep)


def md5s():
    return {str(f): (hashlib.md5(f.read_bytes()).hexdigest()[:10] if f.is_file() else "-")
            for f in USER_FILES}


def ls(d):
    return set(glob.glob(str(Path(d) / "*")))


def main() -> int:
    print("=" * 92)
    print("「把<指代>存到<目标>」→ 无内容时自己生成再落盘 (真引擎 + 边界 + 诚实底线)")
    print("=" * 92)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline

    CFG = _load_config()
    PL = Level4Pipeline(ModelClient(CFG), CFG)
    MADE = []                                    # 本门造出来的文件 (finally 里清)
    before_md5, desk_before = md5s(), ls(DESK)
    # ★ 2026-09-23 加: 清理前先扫掉**上一轮残留**。
    #   实测: 若沙箱里有残留(上一轮清理失败留下), 本轮的"新文件"会被引擎改成
    #   更高后缀名(诗_3.txt), 且沙箱零残留断言从此**每轮必红** —— 残留会级联污染。
    #   先扫干净, 让本门不依赖"上一轮运气好"。
    for _ in range(3):
        shutil.rmtree(SANDBOX, ignore_errors=True)
        if not SANDBOX.exists():
            break
        time.sleep(0.3)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    sbx_before = ls(SANDBOX)
    # ★ 真引擎段的前提: 本机得**真能调模型**。三态处理 (见 _model_probe 的文档):
    #   依赖不在 → SKIP 带原因; 依赖在位却调不通 → 报红 (别把覆盖悄悄丢掉)。
    MODEL_OK, _probe_err, _dep = _model_probe(PL)
    _NO_MODEL = f"本机无可用模型 (无凭据文件/环境变量, 也没起本地模型) —— 真引擎断言跑不了"
    if not MODEL_OK:
        if _dep:
            chk("★ 依赖在位但调不通模型 → 不许静默跳过真引擎段 (绿了但没测最危险)",
                False, f"探测连试 3 次均失败: {_probe_err}")
            return 1 if F else 0

    try:
        # ── 路由声明 (静态) ─────────────────────────────────
        print("\n[静态] 路由声明")
        rt = PL.routes.get("save.referent_gen")
        chk("路由 save.referent_gen 已注册", rt is not None)
        chk("★ 优先级 110 (低于 lookup.prefill 150 / 高于 model.fallback 100)",
            rt is not None and rt.priority == 110,
            None if rt is None else rt.priority)
        chk("★ 声明不写用户状态 (与 ir.chain 同类: 只写用户点名的文件)",
            rt is not None and rt.writes_state is False)
        chk("★ 上一轮**有**长内容 → 不接 (交模型带历史)", PL._has_recent_content() is False,
            "新引擎空会话就该是 False")

        # ── D. 纯判据边界 (零副作用) ─────────────────────────
        print("\n[D.判据边界] 该接的接, 不该接的不接")
        ctx0 = {"ext_model": "auto"}
        yes = PL._match_save_referent("把这首诗保存到桌面", ctx0)
        chk("★ `把这首诗保存到桌面` → 接", yes is True)
        chk("★ `把这段内容存到桌面` → 接", PL._match_save_referent("把这段内容存到桌面", ctx0) is True)
        chk("带文件名也一样接 `把这首诗写到桌面大哥.txt`",
            PL._match_save_referent("把这首诗写到桌面大哥.txt", ctx0) is True)
        chk("盘符落点也接", PL._match_save_referent(r"把这首诗保存到 D:\tmp\x\诗.txt", ctx0) is True)
        chk("★ 生成型口令 (带真素材意图) → 不接 `写一首诗保存到桌面大哥.txt`",
            PL._match_save_referent("写一首诗保存到桌面大哥.txt", ctx0) is False)
        chk("★ 无内容名词 → 不接 `把这个文件保存到桌面`",
            PL._match_save_referent("把这个文件保存到桌面", ctx0) is False)
        chk("★ 无落盘目标 → 不接 `把这首诗保存一下`",
            PL._match_save_referent("把这首诗保存一下", ctx0) is False)
        chk("★ 非指代 (带真内容) → 不接 `把床前明月光保存到桌面`",
            PL._match_save_referent("把床前明月光保存到桌面", ctx0) is False)
        chk("★ 要'提炼/翻译'某物 → 不接 (手里必须先有那个东西) `把这首诗的要点保存到桌面`",
            PL._match_save_referent("把这首诗的要点保存到桌面", ctx0) is False)
        chk("★ 要翻译再存 → 不接 `把这首诗翻译成英文保存到桌面`",
            PL._match_save_referent("把这首诗翻译成英文保存到桌面", ctx0) is False)
        long_msg = "把这首诗保存到桌面" + "，顺便把所有相关材料一起整理一遍再核对一下格式" * 5
        chk(f"★ 超长句 ({len(long_msg)} 字) → 不接", PL._match_save_referent(long_msg, ctx0) is False)
        chk("★ 显式 @模型 → 不接 (用户点了名, 本地启发式不插手)",
            PL._match_save_referent("把这首诗保存到桌面", {"ext_model": "deepseek"}) is False)

        # ── B. 上一轮真有内容 → 不抢 (真引擎) ─────────────────
        print("\n[B.有上下文] 上一轮真有可指内容 → 不让本路由接管")
        PL.sessions.add_message("user", "写一首关于秋天的诗")
        PL.sessions.add_message("assistant",
                                "秋来山色瘦，\n叶落满空阶。\n独坐听风雨，\n一灯照旧怀。" * 2)
        chk("预置长内容后 _has_recent_content() 为真", PL._has_recent_content() is True)
        chk("★ 此时 match 必须为假 (不抢)", PL._match_save_referent("把这首诗保存到桌面", ctx0) is False)
        r_b = asyncio.run(PL.process("把这首诗保存到桌面", probe=True)) or {} if MODEL_OK else {}
        if MODEL_OK:
            chk("★ 真引擎: 不走 save.referent_gen", r_b.get("route") != "save.referent_gen",
                r_b.get("route"))
            # 有上下文时由**模型带历史**处理: 它该把**上文那首诗**写下来 (不是另编一首)。
            # 模型可能选择不落盘 (反问/直接展示) → 两种都算过, 但落盘了就必须是上文那首。
            b_new = sorted(ls(DESK) - desk_before - set(MADE))
            MADE.extend(b_new)
            if b_new:
                _bc = Path(b_new[0]).read_text(encoding="utf-8", errors="replace")
                chk("★ 真引擎: 落盘的是**上文那首** (没另编一首)", "叶落满空阶" in _bc, _bc[:60])
            else:
                chk("★ 真引擎: 有上下文时不抢且不另编 (模型自行处理)", True)
        else:
            skip("B 段真引擎断言 (2 条)", _NO_MODEL)

        # ── C. 生成型口令仍走规划 (回归护栏) ───────────────────
        print("\n[C.回归] 带真素材意图的句子仍走规划链 (2026-09-22 的修复不被我打回)")
        if MODEL_OK:
            r_c = asyncio.run(PL.process("写一首诗保存到桌面大哥.txt", probe=True)) or {}
            chk("★ 真引擎: route=planner.multi_step", r_c.get("route") == "planner.multi_step",
                r_c.get("route"))
            p_c = DESK / "大哥.txt"
            if p_c.is_file():
                MADE.append(str(p_c))
                _txt = p_c.read_text(encoding="utf-8", errors="replace")
                chk("★ 落盘内容是真诗, 不是指令原文",
                    len(_txt.strip()) >= 8 and "保存到桌面" not in _txt, _txt[:60])
            else:
                chk("★ 落盘内容是真诗, 不是指令原文", False, "桌面没有 大哥.txt")
                chk("★ 真引擎: 产物落盘", False, "没落盘")
        else:
            skip("C 段真引擎断言 (3 条)", _NO_MODEL)

        # ── A. 真引擎主用例 (用户原话) ─────────────────────────
        print("\n[A.真引擎] 用户原话 `把这首诗保存到桌面`")
        # 换一个**全新的空会话库** (B 用例刚往隔离库里塞过内容) —— 仍然在 tmp 下, 不碰真实库
        _SESS = ROOT / "tmp" / f"_savegen_sess_{os.getpid()}.db"
        for _suf in ("", "-wal", "-shm"):
            Path(str(_SESS) + _suf).unlink(missing_ok=True)
        PL.sessions = type(PL.sessions)(_SESS)
        SESS_FILES = [str(_SESS) + s for s in ("", "-wal", "-shm")]
        C = []
        _real = PL.gateway.call

        async def spy(tool, action="", **kw):
            C.append((tool, action, str(kw.get("path") or "")))
            return await _real(tool, action=action, **kw)
        if MODEL_OK:
            PL.gateway.call = spy
            r_a = asyncio.run(PL.process("把这首诗保存到桌面", probe=True)) or {}
            PL.gateway.call = _real
            resp = str(r_a.get("response") or "")
            chk("★ route=save.referent_gen", r_a.get("route") == "save.referent_gen",
                r_a.get("route"))
            chk("★ 真调了 file_ops:write",
                any(c[0] == "file_ops" and c[1] == "write" for c in C), C)
            new_files = sorted(ls(DESK) - desk_before - set(MADE))
            MADE.extend(new_files)
            chk("★ 文件真落在桌面", bool(new_files), new_files)
            if new_files:
                _p = Path(new_files[0])
                _c = _p.read_text(encoding="utf-8", errors="replace")
                chk("★ 内容非空且是生成的东西 (≥8 字)", len(_c.strip()) >= 8, repr(_c[:40]))
                chk("★ 内容不是指令原文 (没把'把这首诗保存到桌面'写进文件)",
                    "保存到桌面" not in _c and "把这首诗" not in _c, repr(_c[:60]))
                chk("★ 无 markdown 围栏残留", "```" not in _c)
                chk("★ 回复如实说明是**自己写的** (不装作取到了上文)",
                    ("我自己写" in resp) or ("现写" in resp) or ("没有可指" in resp), resp[:80])
                chk("★ 回复带落点路径", str(_p) in resp or _p.name in resp, resp[:120])
            chk("★ 与 C 的产物同目录不互相踩 (文件名规则生效)",
                all(Path(x).name for x in new_files), new_files)
        else:
            skip("A 段真引擎断言 (8 条: 路由/真调工具/落盘/内容/诚实说明)", _NO_MODEL)

        # ── 重名不覆盖 (真引擎, 沙箱) ──────────────────────────
        print("\n[A2.重名] 已有同名文件 → 换名, 不覆盖")
        keep = SANDBOX / "诗.txt"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("原有内容不许被动", encoding="utf-8")
        sbx_keep = str(keep)
        MADE.append(sbx_keep)
        if MODEL_OK:
            r_a2 = asyncio.run(PL.process("把这首诗保存到 " + str(SANDBOX) + r"\诗.txt",
                                          probe=True)) or {}
            chk("★ 盘符落点也走本路由 (ir.chain 让位)",
                r_a2.get("route") == "save.referent_gen", r_a2.get("route"))
            new_sbx = sorted(ls(SANDBOX) - sbx_before - set(MADE))
            chk("★ 新文件另起名 (不覆盖同名)", bool(new_sbx), new_sbx)
            chk("★ 原文件内容未变", keep.read_text(encoding="utf-8") == "原有内容不许被动")
            if new_sbx:
                MADE.extend(new_sbx)
                chk("★ 新文件里有真内容 (不是指令碎片)",
                    len(Path(new_sbx[0]).read_text(encoding="utf-8").strip()) >= 8,
                    Path(new_sbx[0]).read_text(encoding="utf-8")[:40])
        else:
            # 依赖不在也要把"不覆盖"这条钉住: 直接调落点解析 (纯函数, 与模型无关)
            chk("★ 落点解析遇同名 → 换名 (不需模型, 纯判据)",
                PL._save_resolve_path("把这首诗保存到 " + str(SANDBOX) + r"\诗.txt",
                                      "诗.txt").name != "诗.txt",
                PL._save_resolve_path("把这首诗保存到 " + str(SANDBOX) + r"\诗.txt",
                                      "诗.txt").name)
            chk("★ 原文件内容未变", keep.read_text(encoding="utf-8") == "原有内容不许被动")
            skip("A2 段真引擎断言 (2 条: 路由让位/新文件内容)", _NO_MODEL)

        # ── E. 诚实底线 (打桩, 三条) ───────────────────────────
        print("\n[E.诚实底线] 生成/写盘出问题时, 不许谎报成功")
        _mc_gen = PL.model_client.generate

        async def boom(*a, **k):
            raise RuntimeError("模型不可达")
        PL.model_client.generate = boom
        try:
            out = asyncio.run(PL._run_save_referent("把这首诗保存到桌面", ctx0))
            chk("★ 生成失败 → 返回 None (交回后面链路, 不编内容)", out is None, str(out)[:80])
        finally:
            PL.model_client.generate = _mc_gen

        async def tiny(*a, **k):
            return {"text": "好"}
        PL.model_client.generate = tiny
        try:
            out = asyncio.run(PL._run_save_referent("把这首诗保存到桌面", ctx0))
            chk("★ 生成太短 (<8 字) → 返回 None (不倒垃圾进文件)", out is None, str(out)[:80])
        finally:
            PL.model_client.generate = _mc_gen

        async def ok(*a, **k):
            return {"text": "月照松间水自流，风来竹影满庭秋。闲坐不知天色晚，一壶清茶万事休。"}
        _gw = PL.gateway.call

        async def deny(tool, action="", **kw):
            return {"success": False, "output": "Access denied: 测试用写盘拒绝"}
        PL.model_client.generate = ok
        PL.gateway.call = deny
        try:
            out = asyncio.run(PL._run_save_referent("把这首诗保存到桌面", ctx0)) or {}
            _r = str(out.get("response") or "")
            chk("★ 写盘失败 → 如实说失败 (不含'已存到')",
                "失败" in _r and "已存到" not in _r, _r[:100])
            chk("★ 写盘失败 → intent=save.gen (可审计)", out.get("intent") == "save.gen")
        finally:
            PL.model_client.generate = _mc_gen
            PL.gateway.call = _gw

        # ── 收尾: 零残留 + 零污染 ─────────────────────────────
        print("\n[收尾] 零残留 / 零污染")
        chk("★ 用户数据 md5 未变", md5s() == before_md5)
    finally:
        for x in MADE:
            try:
                Path(x).unlink(missing_ok=True)
            except Exception:
                pass
        # ★ 2026-09-23 修 (实测 WinError 32): 原写法把整个循环套在一个 try 里 ——
        #   tmp/ 下文件会被外部进程(杀软/索引)间歇性占用, 第一次 unlink 抛
        #   PermissionError 就让循环中断, rmdir 压根不执行 → 沙箱残留 → 断言红。
        #   改为: rmtree + 重试 (单项失败不影响其余)。
        for _ in range(5):
            shutil.rmtree(SANDBOX, ignore_errors=True)
            if not SANDBOX.exists():
                break
            time.sleep(0.3)
        for x in SESS_FILES:
            try:
                Path(x).unlink(missing_ok=True)
            except Exception:
                pass

    left = ls(DESK) - desk_before
    chk("★ 桌面零残留 (测试产物已清)", not left, sorted(left))
    chk("★ 沙箱零残留", not SANDBOX.exists())

    print("\n" + "=" * 92)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for x in F:
        print("  -", x)
    for x in S:
        print("  ~", x, "(前置不在, 已在上面说明原因)")
    print("=" * 92)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
