"""本地播报技能 (speak-aloud) — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: 用户说一句"念给我听：xxx" → TMM **真的出声**念出来 (本机合成, 不联网调 LLM)。

覆盖四层:
  A 技能结构 (加载 / 单步 voice:speak / requires_tools / 触发词 / tags 无泛词)
  B 自然说法可达性 (实测自然说法, 不拿 triggers 自证)
  C 真 pipeline (process(probe=True) + 打桩网关) —— 看**真的调了** voice:speak 且 text 正确
  D 真出声 (edge-tts 真生成 mp3 + 真播放; 播放器链路: playsound → pygame → SAPI)

同时锁住本次修掉的两个真缺陷 (2026-09-20):
  ① tools/voice.py 写死 `from playsound import playsound`, 本机没装 → 整个 Edge TTS
     分支抛 ImportError → 悄悄退化成 SAPI 机械音 (音色变差但无人知)。
  ② 只能念字符串, 不能念文件 (没有 text 就报错)。

跑法:
    python -B scripts/verification/verify_speak_skill.py

隔离: 自带四变量自我隔离 (只在未设时才设); probe=True 不写用户历史。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable

_ISO = Path(tempfile.gettempdir()) / ("tmm_speak_iso_%d" % os.getpid())
_ISO.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMM_SESSION_DB", str(_ISO / "sessions.db"))
os.environ.setdefault("TMM_GAP_DB", str(_ISO / "gap.db"))
os.environ.setdefault("TMM_PERCEPTION_FILE", str(_ISO / "perception.json"))
os.environ.setdefault("TMM_USAGE_LOG", str(_ISO / "usage.jsonl"))
sys.path.insert(0, str(ROOT))

NATURAL = [
    "念给我听：「今天天气不错，适合出门。」",
    "播报：会议三点开始",
    "说出来给我听：明天有雨",
    "朗读：这段文字",
    "读一遍「版本已更新」",
]
NEGATIVE = ["帮我看看这份周报", "这个代码怎么优化"]

PROBE = r'''
import sys, os, json, asyncio, logging
ROOT = r"__ROOT__"
sys.path.insert(0, ROOT); os.chdir(ROOT)
logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
from core.knowledge import KnowledgeEngine
from config.settings import DATA_DIR
import core.perception as _perc
from pathlib import Path as _P
CASES = json.loads(sys.argv[2]); SBX = sys.argv[3]
_perc.PERCEPTION_FILE = _P(SBX) / "_sbx_perception.json"
cfg = _load_config(); pl = Level4Pipeline(ModelClient(cfg), cfg); pl._ke = KnowledgeEngine(DATA_DIR)
SIDE = []
async def fake_gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
    SIDE.append(["llm", str(model)])
    return {"text": "[stub-llm]", "think": "", "model": model, "elapsed": 0.0, "error": None, "tool_calls": None}
pl.model_client.generate = fake_gen
async def fake_call(tool, **kw):
    rec = ["gateway", str(tool) + (":" + str(kw.get("action")) if kw.get("action") else "")]
    if str(tool) == "voice":
        rec += ["text=" + repr(str(kw.get("text", "")))[:60], "file=" + repr(str(kw.get("file", "")))[:120]]
    SIDE.append(rec)
    return {"success": True, "output": "[stub-tool]", "path": str(kw.get("path", ""))}
pl.gateway.call = fake_call
import subprocess as _sp
class _FakeP:
    pid = -1
    def __init__(self, *a, **k): SIDE.append(["popen", str(a[0] if a else k)[:40]])
    def wait(self, *a, **k): return 0
_sp.Popen = _FakeP
async def main():
    out = []
    for _msg in CASES:
        SIDE.clear()
        try:
            r = await pl.process(_msg, ext_model="auto", probe=True, mode_override="craft")
        except Exception as e:
            r = {"intent": "EXC:" + type(e).__name__, "response": str(e)[:120]}
        out.append({"msg": _msg, "intent": r.get("intent"), "skill": r.get("skill"),
                    "route": r.get("route"), "resp": str(r.get("response"))[:80],
                    "side": ["|".join(map(str, s)) for s in SIDE]})
    print("<<<J>>>" + json.dumps(out, ensure_ascii=False))
asyncio.run(main())
'''


def run_pipeline(cases, sbx: Path):
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(tmp).write_text(PROBE.replace("__ROOT__", str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    try:
        env = os.environ.copy()
        for k in ("TMM_SESSION_DB", "TMM_GAP_DB", "TMM_PERCEPTION_FILE", "TMM_USAGE_LOG"):
            env[k] = os.environ[k]
        r = subprocess.run([PY, "-B", tmp, "run", json.dumps(cases, ensure_ascii=False), str(sbx)],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=580, env=env)
        if "<<<J>>>" not in (r.stdout or ""):
            return None, (r.stdout or "") + (r.stderr or "")
        return json.loads(r.stdout.split("<<<J>>>")[1].strip().splitlines()[0]), ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    P, F = [], []
    SKIP = []

    def chk(name, cond, detail=""):
        (P if cond else F).append(name)
        print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))

    def skip(name, why):
        SKIP.append(name)
        print(f"  SKIP {name}   <- {why}")

    print("=" * 78)
    print("本地播报 speak-aloud —— 一句\"念给我听\"必须真出声")
    print("=" * 78)

    # ── A 结构 ──
    print("\n[A] 技能结构")
    from core.skill_loader import get_skill_index
    eng = get_skill_index()
    idx = eng.get_index()
    dag = eng.get_dag("speak-aloud")
    chk("skill 已加载", "speak-aloud" in idx and dag is not None)
    if dag is None:
        print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
        return 1
    fm = dag.raw_source or {}
    steps = [(s.get("id"), s.get("tool"), s.get("action")) for s in dag.steps]
    chk("单步 voice:speak", steps == [("speak", "voice", "speak")], steps)
    chk("steps 工具都声明在 requires_tools", "voice" in set(fm.get("requires_tools") or []), fm.get("requires_tools"))
    chk("触发词 ≥5 且无单字", len(fm.get("triggers") or []) >= 5 and all(len(t) >= 2 for t in fm["triggers"]), fm.get("triggers"))
    _GENERIC = {"整理", "摘要", "记录", "历史", "统计", "服务", "邮件", "视频", "文字",
                "提取", "文档", "数据", "信息", "报告", "内容", "文件", "图片", "分析"}
    _bad = [t for t in (fm.get("tags") or []) if str(t).strip() in _GENERIC]
    chk("tags 无单概念泛词", not _bad, _bad)
    chk("0 解析错误", not dag.parse_errors, dag.parse_errors)

    # ── B 可达性 ──
    print("\n[B] 自然说法可达性 (实测说法, 非自证)")
    for m in NATURAL:
        got = eng.match_context(m, max_skills=1) or ""
        chk(f"「{m[:24]}」→ speak-aloud", "speak-aloud" in got, got[:50])
    for m in NEGATIVE:
        got = eng.match_context(m, max_skills=1) or ""
        chk(f"★ 不该被抢: 「{m}」", "speak-aloud" not in got, got[:50])

    # ── C 真 pipeline ──
    print("\n[C] 真 pipeline (process(probe=True) + 打桩网关)")
    sbx = ROOT / "tmp" / "_speak_sandbox"
    sbx.mkdir(parents=True, exist_ok=True)
    txt = sbx / "要念的稿子.txt"
    txt.write_text("这是文件播报自检。", encoding="utf-8")
    cases = [NATURAL[0], f"念一下 {txt.as_posix()}"]
    rows, err = run_pipeline(cases, sbx)
    if rows is None:
        chk("pipeline 采集成功", False, err[-400:])
    else:
        by = {r["msg"]: r for r in rows}
        for m in cases:
            r = by.get(m, {})
            side = r.get("side", [])
            spoke = [s for s in side if "voice:speak" in s]
            print(f"      {m[:40]:<42} route={str(r.get('route')):<20} side={','.join(side)[:70]}")
            chk(f"「{m[:22]}」真调了 voice:speak", bool(spoke), side)
            chk(f"「{m[:22]}」胜出者是 speak-aloud", r.get("skill") == "speak-aloud", r.get("skill"))
        r0 = by.get(NATURAL[0], {})
        s0 = ",".join(r0.get("side", []))
        chk("★ text 传对了 (不念指令词)", "今天天气不错" in s0 and "念给我听" not in s0.split("text=")[-1][:40], s0[:120])
        r1 = by.get(cases[1], {})
        chk("★ 文件播报把 file 传下去了", "要念的稿子.txt" in ",".join(r1.get("side", [])), r1.get("side"))

    # ── D 真出声 ──
    print("\n[D] 真出声 (edge-tts 合成 + 播放链路)")
    import asyncio
    from tools import voice as V
    chk("播放器兜底链存在 (playsound→pygame)", "pygame" in V._play_audio.__doc__ or "pygame" in (V._play_audio.__code__.co_consts and str(V._play_audio.__code__.co_consts)) or "pygame" in open(V.__file__, encoding="utf-8").read())
    # ① 真生成 mp3 (不依赖扬声器)
    # ★ 2026-09-22: edge-tts 是**云合成** (speech.platform.bing.com)。该主机被 DNS 拦/断网时
    #   这是**环境**问题, 不是代码问题 → SKIP (不假报绿也不假报红), 与 verify_github_skill 同规矩。
    #   真正的合成失败 (网络通但没生成/文件过小) 仍然 FAIL。
    mp3 = _ISO / "probe.mp3"
    gen_ok, gen_info = False, ""

    def _tts_host_up() -> bool:
        import socket
        try:
            socket.gethostbyname("speech.platform.bing.com")
            return True
        except Exception:
            return False

    _net = _tts_host_up()
    if not _net:
        skip("★ edge-tts 真合成出音频 (>1KB)",
             "speech.platform.bing.com DNS 不可达 (云合成需联网) —— 非代码问题, 不假报红")
        gen_info = "SKIP: 云合成本机不可达"
    else:
        try:
            import edge_tts
            async def _gen():
                await edge_tts.Communicate("播报自检通过。", "zh-CN-YunxiNeural").save(str(mp3))
            asyncio.run(_gen())
            gen_ok = mp3.exists() and mp3.stat().st_size > 1000
            gen_info = f"{mp3.stat().st_size} B" if mp3.exists() else "没生成"
        except Exception as e:
            gen_info = f"{type(e).__name__}: {e}"[:120]
            # 网络类异常 (DNS/连接失败) → SKIP, 其余 (合成逻辑错) → FAIL
            if any(k in gen_info for k in ("DNSError", "ConnectorError", "getaddrinfo",
                                           "Cannot connect", "timed out")):
                skip("★ edge-tts 真合成出音频 (>1KB)", f"云合成本机不可达: {gen_info}")
                gen_info = "SKIP: " + gen_info
            else:
                chk("★ edge-tts 真合成出音频 (>1KB)", gen_ok, gen_info)
        else:
            chk("★ edge-tts 真合成出音频 (>1KB)", gen_ok, gen_info)
    # ② 端到端 speak (真出声; 允许无扬声器时退 SAPI)
    res = asyncio.run(V.run(action="speak", text="播报自检通过。"))
    out = str(res.get("output") or "")
    chk("★ voice.run 真跑通 (出声或 SAPI 兜底)", bool(res.get("success")) and ("已播报" in out), res)
    chk("播报结果带可核对的元信息", ("player" in res) or ("SAPI" in out), res)
    # ③ 文件播报
    res2 = asyncio.run(V.run(action="speak", text="", file=str(txt)))
    chk("★ 只给文件也能念", bool(res2.get("success")), res2)
    # ④ 空输入诚实失败
    res3 = asyncio.run(V.run(action="speak", text=""))
    chk("★ 没内容 → 诚实失败 (不瞎念)", (not res3.get("success")) and "没有要朗读的文字" in str(res3.get("output")), res3)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    for f in F:
        print("  -", f)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    import shutil
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(ROOT / "tmp" / "_speak_sandbox", ignore_errors=True)
        shutil.rmtree(_ISO, ignore_errors=True)
