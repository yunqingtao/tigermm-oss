"""朗读文件「念成路径」缺陷的常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景 (2026-09-24, 用户当场发火的原话):
    用户说: 朗读"<桌面>\<文件名>.txt"        (真实路径含个人家目录, 此处略去)
    现象:   不念文件, 反而把**路径字符串**念出来 ("C 冒号 反斜杠 Users …");
            他还重试了别的说法, 全都不行 —— "我也不知道怎么表达他会明白"。

真因 (两处, 都在"路径混进 text 参数"):
    ① core/pipeline.py::_skill_params —— text 参数从**引号内**取内容,
       而 `朗读"<路径>"` 的引号里就是路径 ⇒ text = 路径。
    ② tools/voice.py::_speak —— 老逻辑 `if not text and file:` 才读文件,
       这里 text 非空 ⇒ 跳过读文件 ⇒ 直接 TTS 那段路径文本。

修法:
    ① text 抽取时**排除像路径的值** (含分隔符/盘符/已知扩展名, 且
       "磁盘上真存在" 或 "技能同时声明了 path 参数")。
    ② _speak 防御: text 若是**真实存在的文件路径**且非多行 → 当文件读。

本门禁钉住:
  A _skill_params: 路径不再进 text, 而是进 src
  B _speak 防御: 只给 text=路径 也能读成文件内容
  C 端到端: 真引擎路由 skill_dag → voice, 念的是**文件内容**
  D 不回归: 引号里的**真文字**仍然照念 (「你好」类)
  E 零污染: 全程沙箱 + 桩播放 (不发声、不写用户数据)

跑法:
    python -B scripts/verification/verify_read_aloud.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
P, F, SKIP = [], [], []
TARGET = Path.home() / "Desktop" / "窗台上的光.txt"


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def _norm(s: str) -> str:
    """归一化: 去空白与标点 —— 断言两边必须用**同一套**归一, 否则假红
    (踩过: 只清洗一边 → B3 误报)。"""
    return re.sub(r"[\s，。、；：！？,.!?;:\"'「」【】()（）\-—…]", "", s or "")


def load(path: Path, alias: str):
    spec = importlib.util.spec_from_file_location(alias, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[alias] = m
    spec.loader.exec_module(m)
    return m


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    os.chdir(str(ROOT))

    if not TARGET.exists():
        SKIP.append("全部 (桌面没有 窗台上的光.txt)")
        print(f"SKIP 找不到 {TARGET} —— 本门禁需要那个真实文件")
        print(f"结果: 0 PASS / 0 FAIL / 1 SKIP")
        return 0

    body = TARGET.read_text(encoding="utf-8", errors="replace")
    fp = str(TARGET)

    print("—— A _skill_params: 路径不再进 text ——")
    SBX = Path(tempfile.gettempdir()) / "_aloud_gate_sbx"
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
    try:
        from _probe_env import activate
        _sd = activate()
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from core.knowledge import KnowledgeEngine
        from config.settings import DATA_DIR

        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl._ke = KnowledgeEngine(DATA_DIR)
        pl.modes.path = SBX / "mode.json"

        from core.skill_loader import get_skill_engine
        dag = get_skill_engine().get_dag("speak-aloud")
        chk("A0 speak-aloud 技能可载入", bool(dag and getattr(dag, "steps", None)))

        for msg in (f'朗读"{fp}"', f'念一下"{fp}"', f'把"{fp}"念出来', f'朗读「{fp}」'):
            prm = pl._skill_params(msg, dag)
            txt = prm.get("text")
            src = prm.get("src")
            chk(f"A 路径不进 text [{msg[:26]}…]",
                (not txt) and (not txt or "Desktop" not in str(txt)), f"text={txt!r}")
            chk(f"A 路径进 src [{msg[:26]}…]",
                bool(src) and "窗台上的光" in str(src), f"src={src!r}")

        print("—— A2 ★ 贴正文 + 念的动作 (用户实测: 整首诗直接贴过来 + '朗读。') ——")
        _POEM = "《夜思》\n月落寒江静，\n风来竹影斜。\n一盏孤灯下，\n诗成已半宵。"
        for _label, _msg in (("贴诗+朗读。", _POEM + " 朗读。"),
                             ("贴诗+朗读(无标点)", _POEM + " 朗读"),
                             ("贴诗+念给我听", _POEM + "\n念给我听"),
                             ("贴诗+读出来", _POEM + " 读出来"),
                             ("单行诗+朗读", "《夜思》月落寒江静，风来竹影斜。一盏孤灯下，诗成已半宵。朗读"),
                             ("朗读：正文", "朗读：月落寒江静，风来竹影斜。")):
            _p = pl._skill_params(_msg, dag)
            _t = _p.get("text") or ""
            chk(f"A2 {_label} → 取到正文 ({len(_t)} 字)", "月落寒江静" in _t and "朗读" not in _t, _t[:60])
        # 反向: 贴的是路径 → 仍该走 src, 不是 text
        _p = pl._skill_params(f'朗读"{fp}"', dag)
        chk("A2 路径仍走 src (没被正文兜底抢走)", bool(_p.get("src")) and not _p.get("text"), _p)
        # 反向: 写文档类技能**不该**被正文兜底误伤
        import core.skill_loader as _sl
        _wd = get_skill_engine().get_dag("write-office-doc")
        if _wd:
            _p2 = pl._skill_params("写周报保存到桌面", _wd)
            chk("A2 写文档类技能不受影响 (兜底只对 voice 类)", "写周报" not in str(_p2.get("text") or ""), _p2)

        print("—— A3 ★ 歧义矩阵 (按**路由层**): 动词定方向, 宾语定来源 ——")
        # 出声类 → 必须落到 voice 技能 (而不是被 file_read 抢走)
        # ⚠ `_match_skill_trigger` 只返回 bool (门面); 要拿技能名用 `_skill_match`
        #   (踩过: 拿 bool 当元组取 [0] → 全假红)
        def _skill_name(m):
            h = pl._skill_match(m, trigger_only=True)
            return (h[0] if isinstance(h, (tuple, list)) and h else "")
        for _lbl, _m in (("朗读+路径", f'朗读"{fp}"'),
                         ("朗读+贴正文", _POEM + " 朗读。"),
                         ("朗读+代词", "朗读这个短文"),
                         ("念一下+路径", f'念一下"{fp}"'),
                         ("读出来+正文", "月落寒江静，风来竹影斜。读出来")):
            _n = _skill_name(_m)
            chk(f"A3 出声类 {_lbl} → voice 技能", "speak" in _n, _n or "(未命中技能)")
        # 取内容类 → **不该**被 voice 抢走 (要显示内容, 不是念)
        for _lbl, _m in (("打开+路径", "打开一首诗.txt"), ("读+路径", f'"{fp}" 读')):
            _n = _skill_name(_m)
            chk(f"A3 取内容类 {_lbl} 不走 voice", "speak" not in _n, _n)

        print("—— D 不回归: 引号里的真文字仍然照念 ——")
        prm = pl._skill_params('念给我听：「今天天气不错，适合出门。」', dag)
        chk("D 引号内真文字 → text", (prm.get("text") or "").startswith("今天天气不错"), prm)
        prm = pl._skill_params("说出来给我听：这个方案下周一开始执行", dag)
        chk("D '说：…' 形式仍能抽到", "方案" in str(prm.get("text") or ""), prm)

        print("—— B _speak 防御: 只给 text=路径 也能读成文件 ——")
        SEEN = {}

        class _FakeComm:
            def __init__(self, text, voice):
                SEEN["text"] = text

            async def save(self, path):
                Path(path).write_bytes(b"x" * 2000)

        _fake = types.ModuleType("edge_tts")
        _fake.Communicate = _FakeComm
        sys.modules["edge_tts"] = _fake
        vm = load(ROOT / "tools" / "voice.py", "voice_gate_mod")
        vm._play_audio = lambda p: "stub-no-sound"          # ★ 不发声

        SEEN.clear()
        r = asyncio.run(vm._speak(text=fp, file=fp))
        spoken = SEEN.get("text") or ""
        chk("B1 播放成功", bool(r.get("success")), r)
        chk("B2 ★ text=路径 → 读成文件内容 (不是念路径)",
            ("Users" not in spoken) and ("Desktop" not in spoken) and len(spoken) > 30, spoken[:80])
        chk("B3 念的确实是那个文件的内容",
            _norm(spoken)[:12] in _norm(body), spoken[:60])

        print("—— C 端到端: 真引擎路由 + 念的是内容 ——")
        SEEN.clear()
        calls = []
        _orig = pl.gateway.call

        async def _spy(t, **kw):
            calls.append(t)
            if t == "voice":
                return await vm.run(**kw)
            return await _orig(t, **kw)

        pl.gateway.call = _spy
        res = asyncio.run(pl.process(f'朗读"{fp}"', probe=True)) or {}
        pl.gateway.call = _orig
        spoken = SEEN.get("text") or ""
        chk("C1 路由走技能 (intent=skill_dag)", (res.get("intent") or "") == "skill_dag", res.get("intent"))
        chk("C2 调了 voice 工具", "voice" in calls, calls)
        chk("C3 ★ 念的是文件内容, 不是路径",
            ("Users" not in spoken) and len(spoken) > 30, spoken[:70])
    except Exception as e:
        chk("A/B/C 段", False, f"{type(e).__name__}: {e}")
    finally:
        if _sd:
            shutil.rmtree(_sd, ignore_errors=True)
        shutil.rmtree(SBX, ignore_errors=True)
        sys.modules.pop("edge_tts", None)

    print("—— E 零污染 ——")
    chk("E1 沙箱已清", not SBX.exists())

    print()
    print("=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
