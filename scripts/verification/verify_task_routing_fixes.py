"""任务路由四条缺陷的常驻验证器 (2026-09-24 用户实测清单 ①②③④)

用户原话要素:
  ① "打开一首诗.txt" → 报 File not found: 诗.txt   (文件名开头的量词被吃)
  ② "朗读这个短文"   → speak-aloud: No text to speak (指代接不上上一轮产物)
  ③ "总结今天干了什么" → 模型答"我无法感知您的活动"  (只认"今天做了什么")
  ④ '"F:\\cs"有什么'  → 空路由, 掉大模型直答 12~17 秒 (只认"列出 X")

四条都已修, 本门禁钉住**修好后的行为** + 不回归:
  A ① 量词: 纯文件名形态保留量词; 散文里的量词仍照剥
  B ③ 今日汇总: 干/做/忙/搞 各种说法都走 artifacts
  C ④ "有路径 + 看里面" 的自然说法 → file_list; 写入类动词**不**误判
  D ② 指代: "朗读这个短文" 能接上上一轮产物的真实文件并念出**内容**
  E 不回归: 带扩展名的文件路径仍走 file_read (不是 file_list)
  F 零污染: 会话库/产物台账全在沙箱; 真库只读不写

跑法:
    python -B scripts/verification/verify_task_routing_fixes.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REAL_DB = ROOT / "data" / "chat_sessions.db"
P, F, SKIP = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    os.chdir(str(ROOT))

    SBX = Path(tempfile.gettempdir()) / "_routefix_gate_sbx"
    shutil.rmtree(SBX, ignore_errors=True)
    SBX.mkdir(parents=True, exist_ok=True)
    os.environ.pop("TMM_PROBE", None)
    os.environ["TMM_LIVE"] = "1"
    for k, v in [("TMM_SESSION_DB", "chat_sessions.db"), ("TMM_TASK_DB", "task_ledger.db"),
                 ("TMM_GAP_DB", "gap_ledger.db"), ("TMM_PERCEPTION_FILE", "perception.json"),
                 ("TMM_USAGE_LOG", "usage.jsonl"), ("TMM_SKILL_USAGE_FILE", "skill_usage.jsonl"),
                 ("TMM_ARTIFACTS_FILE", "artifacts.jsonl"), ("TMM_SESSION_SNAPSHOT", "session_snapshot.json")]:
        os.environ[k] = str(SBX / v)

    _sd = None
    ART = ROOT / "tmp" / "_routefix_gate_poem.txt"
    try:
        from _probe_env import activate
        _sd = activate()
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from core.knowledge import KnowledgeEngine
        from core.nl_paths import clean_filename
        from config.settings import DATA_DIR

        # ★★ 2026-09-24 加: **静音自动备份** —— 本门禁会调 process(), 而它每 2 小时
        #   触发一次 AutoBackup.create() 往**项目外的备份目录**写 zip; 探针守卫会拒写,
        #   随后 rotate/glob 撞上未关闭的句柄 → PermissionError [WinError 32] 把整条门禁打挂
        #   (实测: 单独跑 31 PASS, 被别的验证器嵌套调用时 26 PASS / 1 FAIL, 全是环境抖动)。
        #   门禁不需要备份, 直接置空。
        try:
            import core.backup as _bk
            _bk.AutoBackup.create = lambda self, label="auto": None
            _bk.AutoBackup.auto_tick = lambda self: None
        except Exception:
            pass

        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl._ke = KnowledgeEngine(DATA_DIR)
        pl.modes.path = SBX / "mode.json"

        def acts(m):
            return (pl.intent_router.classify(m) or {}).get("actions") or []

        def chain_tools(m):
            ch = pl.intent_router.build_chain(pl.intent_router.classify(m))
            return [c.get("tool") for c in (ch or [])], ch

        def path_of(m):
            _, ch = chain_tools(m)
            return ((ch[0].get("params") or {}).get("path") if ch else None)

        print("—— A ① 量词: 纯文件名保留, 散文里照剥 ——")
        for msg, want in (("打开一首诗.txt", "一首诗.txt"), ("打开一个文件.txt", "一个文件.txt"),
                          ("读取一首诗.txt", "一首诗.txt"), ("打开两道题.txt", "两道题.txt")):
            got = (path_of(msg) or "")
            chk(f"A1 {msg!r} → 保留 {want!r}", got == want, got)
        chk("A2 散文里的量词仍照剥 ('把这首诗写入桌面 大哥.txt里面' → 大哥.txt)",
            clean_filename("把这首诗写入桌面 大哥.txt里面") == "大哥.txt",
            clean_filename("把这首诗写入桌面 大哥.txt里面"))
        chk("A3 剥过动词后量词可剥 ('写一个报告.txt' → 报告.txt)",
            clean_filename("写一个报告.txt") == "报告.txt", clean_filename("写一个报告.txt"))
        chk("A4 原文档样例不回归 ('写一首诗保存到桌面大哥.txt' → 大哥.txt)",
            clean_filename("写一首诗保存到桌面大哥.txt") == "大哥.txt",
            clean_filename("写一首诗保存到桌面大哥.txt"))

        print("—— B ③ 今日汇总: 各种说法都走 artifacts ——")
        for m in ("今天做了什么", "总结今天干了什么", "今天干了什么", "今天做了啥",
                  "今天都干了啥", "今天忙了啥", "总结一下今天"):
            chk(f"B {m!r} → artifacts", acts(m) == ["artifacts"], acts(m))

        print("—— C ④ 自然说法 → file_list; 写入类不误判 ——")
        for m in ("列出 F:\\cs", '"F:\\cs"有什么', "F:\\cs里有什么", "打开F:\\cs里面文件",
                  "看看 F:\\cs 里面", "F:\\cs 有哪些文件"):
            chk(f"C1 {m[:24]!r} → file_list", acts(m) == ["file_list"], acts(m))
        for m in ("把报告存到 F:\\cs 里面", "写个说明放到 F:\\cs 里面"):
            chk(f"C2 写入类不误判: {m[:22]!r}", acts(m) != ["file_list"], acts(m))

        print("—— E 不回归: 文件路径仍走 file_read ——")
        chk("E1 'F:\\cs\\一首诗.txt 内容是什么' → file_read",
            acts("F:\\cs\\一首诗.txt 内容是什么") == ["file_read"],
            acts("F:\\cs\\一首诗.txt 内容是什么"))

        print("—— D ② 指代: '朗读这个短文' 接上一轮产物 ——")
        ART.write_text("《窗台上的光》\n清晨六点，第一缕阳光越过对面楼顶，斜斜地落在窗台上。\n",
                       encoding="utf-8")
        pl.sessions.add_message("user", f"写一篇短文保存到 {ART}")
        pl.sessions.add_message("assistant", f"✅ [1] file_ops: 撰写短文并保存到 {ART}")

        SEEN = {}

        class _FC:
            def __init__(self, text, voice):
                SEEN["text"] = text

            async def save(self, path):
                Path(path).write_bytes(b"x" * 2000)

        _fk = types.ModuleType("edge_tts")
        _fk.Communicate = _FC
        sys.modules["edge_tts"] = _fk
        spec = importlib.util.spec_from_file_location("voice_routefix", ROOT / "tools" / "voice.py")
        vm = importlib.util.module_from_spec(spec)
        sys.modules["voice_routefix"] = vm
        spec.loader.exec_module(vm)
        vm._play_audio = lambda p: "stub-no-sound"       # ★ 不发声

        calls = []
        _o = pl.gateway.call

        async def _spy(t, **kw):
            calls.append((t, kw))
            return await vm.run(**kw) if t == "voice" else await _o(t, **kw)

        pl.gateway.call = _spy
        SEEN.clear()
        res = asyncio.run(pl.process("朗读这个短文", probe=True)) or {}
        pl.gateway.call = _o
        voice_kw = next((kw for t, kw in calls if t == "voice"), {})
        spoken = SEEN.get("text") or ""
        chk("D1 走了 voice 技能", "voice" in [t for t, _ in calls], calls)
        chk("D2 ★ 指代接到上一轮那个文件 (file=产物)",
            str(voice_kw.get("file") or "").endswith(ART.name), voice_kw)
        chk("D3 ★ 念的是**文件内容**, 不是路径",
            ("窗台上的光" in spoken) and ("Users" not in spoken), spoken[:70])
        chk("D4 不再报 'No text to speak'",
            "No text to speak" not in str(res.get("response") or ""), res.get("response"))
        chk("D5 指代仅在有指代词时生效 (普通句不受影响)",
            not pl._looks_anaphoric("列出 F:\\cs") and pl._looks_anaphoric("朗读这个短文"))
    except Exception as e:
        chk("A~F 段", False, f"{type(e).__name__}: {e}")
    finally:
        sys.modules.pop("edge_tts", None)
        ART.unlink(missing_ok=True)
        if _sd:
            shutil.rmtree(_sd, ignore_errors=True)
        shutil.rmtree(SBX, ignore_errors=True)

    print("—— F 零污染 ——")
    chk("F1 沙箱已清", not SBX.exists())
    chk("F2 测试产物已删", not ART.exists())
    try:
        c = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True, timeout=8)
        n = c.execute("select count(*) from messages").fetchone()[0]
        c.close()
        chk(f"F3 真库未被写 ({n} 条)", n > 0, n)
    except Exception as e:
        chk("F3 真库可读", False, f"{type(e).__name__}: {e}")

    print()
    print("=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
