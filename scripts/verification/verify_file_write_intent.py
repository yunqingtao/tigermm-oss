"""file_write 意图判定 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: **"存到 X" 不等于 "写文件"** —— 没有文件名、也没说写什么内容时,
绝不能凭空生成一个名叫 `文档.txt`、内容是**那句话本身**的垃圾文件。

背景 (A① 真缺陷, 2026-09-20 修):
  `pipeline._match_ir_chain()` 有一条 Auto-fill: `delivery == "save"` 且无 actions
  → 强行补 `actions = ["file_write"]`。于是任何带"存到/保存到"的句子都走到
  `intent_router.build_chain()` 的 file_write 分支:
      fname 默认 = "文档.txt"; content = 剥完指令词后**剩下的整句**。
  实测证据 (修前): "服务还活着吗 存到 tmp/_batch2/" → 真写出 28B 的 文档.txt,
  内容 "服务还活着 tmp/_batch2/"; "写入文件" → 写出 0B 的 文档.txt;
  "把这段话存到桌面" → 桌面多一个 文档.txt。

修法 (能区分, 不是禁掉): 写文件请求必须满足**至少一条**显式判据 ——
  ① 显式文件名 (file_path 槽位 / 消息里带扩展名)   ← "把你好世界写入a.txt"
  ② 显式内容标记 (内容: / 正文: / 一行内容:)        ← "创建b.txt 内容:hello"
  ③ "把X写入Y" 句式
三条都不满足 → 不生成写步骤 → 消息交给技能层/模型层 (技能自带落盘)。

验证分三层:
  A 纯意图层 (classify + pipeline 同款 Auto-fill + build_chain) —— 零副作用
  B 真 pipeline (`process(probe=True)` + 打桩模型/网关) —— 看**副作用序列**
  C 磁盘卫生 —— 项目根 / 桌面 / 沙箱里没有 文档.txt

跑法:
    python -B scripts/verification/verify_file_write_intent.py

隔离: 自带四变量自我隔离 (只在未设时才设, 不覆盖门禁值); probe=True 不写用户历史。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
SANDBOX = ROOT / "tmp" / "_a1_write_sandbox"
DESKTOP = Path(os.path.expanduser("~")) / "Desktop"

# ── 自我隔离 (只在未设时才设; 门禁 runner 已注入时不覆盖) ──
_ISO = Path(tempfile.gettempdir()) / ("tmm_a1_iso_%d" % os.getpid())
_ISO.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMM_SESSION_DB", str(_ISO / "sessions.db"))
os.environ.setdefault("TMM_GAP_DB", str(_ISO / "gap.db"))
os.environ.setdefault("TMM_PERCEPTION_FILE", str(_ISO / "perception.json"))
os.environ.setdefault("TMM_USAGE_LOG", str(_ISO / "usage.jsonl"))
sys.path.insert(0, str(ROOT))

# ── 语料 ──
# 假写文件请求: 没有文件名 / 没说写什么 → 一步都不能生成
NEG = [
    "服务还活着吗 存到 D:/x/",
    "服务还活着吗 存到 tmp/_batch2/",
    "服务还活着吗，存到 tmp/_batch2/",
    "给电脑做个体检 存到 D:/x/",
    "写入文件",
    "写入文件 服务还活着",
    "把这段话存到桌面",
    "保存到桌面",
]
# 真写文件请求: 必须照样写对 (★ 加性改动不得回退既有能力)
POS = [
    ("把你好世界写入agent_test.txt", "agent_test.txt", "你好世界"),
    ("创建test.md 内容:hello", "test.md", "hello"),
    ("在桌面新建一个报告.txt 内容: 今天天气不错", "报告.txt", "今天天气不错"),
]


def auto_fill(cl):
    """复刻 pipeline._match_ir_chain / _ir_chain_exec 的 Auto-fill (逐字一致)。"""
    if cl.get("delivery") and not cl.get("actions"):
        if cl["delivery"] == "email":
            cl["actions"] = ["send_email"]
        elif cl["delivery"] == "save":
            cl["actions"] = ["file_write"]
    return cl


def writes(chain):
    return [s for s in (chain or []) if s.get("tool") == "file_ops" and s.get("action") == "write"]


# ══════════════════════════════════════════════════════════════════════
PROBE = r'''
import sys, os, json, asyncio, logging, shutil
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

CASES = json.loads(sys.argv[2])
SBX = sys.argv[3]
_perc.PERCEPTION_FILE = _P(SBX) / "_sbx_perception.json"

cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg)
pl._ke = KnowledgeEngine(DATA_DIR)

SIDE = []

async def fake_gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
    SIDE.append(["llm", str(model)])
    return {"text": "[stub-llm]", "think": "", "model": model, "elapsed": 0.0,
            "error": None, "tool_calls": None}
pl.model_client.generate = fake_gen

async def fake_call(tool, **kw):
    rec = ["gateway", str(tool) + (":" + str(kw.get("action")) if kw.get("action") else "")]
    if str(kw.get("action")) == "write":
        rec += ["path=" + str(kw.get("path", "")), "content=" + repr(str(kw.get("content", "")))[:80]]
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
                    "route": r.get("route"),
                    "resp": str(r.get("response"))[:90],
                    "side": ["|".join(map(str, s)) for s in SIDE]})
    print("<<<J>>>" + json.dumps(out, ensure_ascii=False))

asyncio.run(main())
'''


def run_pipeline(cases):
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(tmp).write_text(PROBE.replace("__ROOT__", str(ROOT).replace("\\", "\\\\")), encoding="utf-8")
    try:
        env = os.environ.copy()
        env["TMM_SESSION_DB"] = os.environ["TMM_SESSION_DB"]
        env["TMM_GAP_DB"] = os.environ["TMM_GAP_DB"]
        env["TMM_PERCEPTION_FILE"] = os.environ["TMM_PERCEPTION_FILE"]
        env["TMM_USAGE_LOG"] = os.environ["TMM_USAGE_LOG"]
        r = subprocess.run([PY, "-B", tmp, "run", json.dumps(cases, ensure_ascii=False), str(SANDBOX)],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=580, env=env)
        if "<<<J>>>" not in (r.stdout or ""):
            return None, (r.stdout or "") + (r.stderr or "")
        return json.loads(r.stdout.split("<<<J>>>")[1].strip().splitlines()[0]), ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    P, F = [], []

    def chk(name, cond, detail=""):
        (P if cond else F).append(name)
        print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))

    def path_of(side_entry):
        """从 'gateway|file_ops:write|path=文档.txt|content=...' 取 path。"""
        for part in str(side_entry).split("|"):
            if part.startswith("path="):
                return part[5:]
        return ""
    print("=" * 78)
    print("A① file_write 意图判定 —— \"存到 X\" / \"写入文件\" 不得凭空造 文档.txt")
    print("=" * 78)

    # ── A. 纯意图层 ──
    print("\n[A] 意图层 (classify + Auto-fill + build_chain) —— 零副作用")
    from core.intent_router import get_router
    rt = get_router()
    junk = []
    for m in NEG:
        cl = auto_fill(rt.classify(m))
        # ★ 只算"带路径"的写: params={} (缺参 → 反问用户) 是既有正确行为, 不算垃圾文件
        w = [s for s in writes(rt.build_chain(cl)) if (s.get("params") or {}).get("path")]
        if w:
            junk.append((m, w[0].get("params")))
    chk(f"假写文件请求不落盘 (无路径写步骤) ({len(NEG)} 句)", not junk, junk)
    kept = []
    for m, fname, content in POS:
        cl = auto_fill(rt.classify(m))
        w = writes(rt.build_chain(cl))
        ok = bool(w) and str(w[0]["params"].get("path", "")).endswith(fname) \
            and str(w[0]["params"].get("content", "")) == content
        if not ok:
            kept.append((m, w[0].get("params") if w else None))
    chk(f"真写文件请求照样写对 ({len(POS)} 句)", not kept, kept)

    # ── B. 真 pipeline (打桩) ──
    print("\n[B] 真 pipeline (process(probe=True) + 打桩网关) —— 看副作用序列")
    cases = NEG[:5] + [POS[0][0], POS[1][0]]
    rows, err = run_pipeline(cases)
    if rows is None:
        chk("pipeline 采集成功", False, err[-400:])
    else:
        by = {r["msg"]: r for r in rows}
        bad = []
        for m in cases[:5]:
            r = by.get(m, {})
            w = [s for s in r.get("side", []) if "file_ops:write" in s and path_of(s)]
            if w:
                bad.append((m, w, r.get("route")))
            print(f"      {m[:34]:<36} route={str(r.get('route')):<18} side={','.join(r.get('side', []))[:60]}")
        chk("真链路上也不落盘垃圾文件 (5 句)", not bad, bad)
        misses = []
        for m, fname, content in [POS[0], POS[1]]:
            r = by.get(m, {})
            w = [s for s in r.get("side", []) if "file_ops:write" in s]
            if not w or fname not in w[0] or content not in w[0]:
                misses.append((m, w))
        chk("真链路上真写文件请求仍落盘 (2 句)", not misses, misses)

    # ── C. 磁盘卫生 ──
    print("\n[C] 磁盘卫生")
    junk_files = [str(p) for p in [ROOT / "文档.txt", DESKTOP / "文档.txt"] if p.exists()]
    if SANDBOX.exists():
        junk_files += [str(p) for p in SANDBOX.rglob("文档.txt")]
    chk("项目根 / 桌面 / 沙箱 无 文档.txt", not junk_files, junk_files)

    # ── D. ★ 口语落盘说法 (2026-09-23 用户实测驱动) ──
    print("\n[D] 口语落盘说法: \"写一首诗放桌面\" 不许写出指令碎片的垃圾文件")
    print("    起因 (用户原话实测): \"写一首诗放桌面\" → l4.knowledge → 桌面多一个")
    print("    doc_<时间戳>.txt, 内容只有 6 字 \"一首诗放桌面\" (指令的尾巴当正文)。")
    print("    对照: 同一句话写\"保存到桌面\"是好的 ⇒ 只认书面式(存到/写到…)会漏口语形态。")
    COLLOQ = ["写一首诗放桌面", "写一首诗放到桌面", "写首诗存桌面", "写一首诗搁桌面"]
    # D 段全是**纯判据** (`_step_evidence` / `_match_planner` / `_ke.parse`) —— 不跑 process()。
    # 用 `object.__new__` **绕过 __init__** 拿一个真实例: 既有类属性 (_QACT/_WACT…),
    # 又不构造网关/会话库 (那会打出 "Using legacy plaintext keys.json" 之类的无关噪声)。
    # ★ 第一版用 SimpleNamespace 调未绑定方法 → AttributeError: 没有 _QACT (类是属性载体, 不能省)。
    from core.knowledge import KnowledgeEngine as _KE2
    from core.pipeline import Level4Pipeline as _L4
    from config.settings import DATA_DIR as _DD
    pl = object.__new__(_L4)
    pl._ke = _KE2(_DD)
    # ★ 两层分工 (别混测):
    #   · `_step_evidence` 的 target 只认**书面式**落盘词 (存到/保存到/写到…) —— 它同时喂
    #     `(n_q>=1 and target)` 那条分支, 保持窄口径才不会把读请求判成落盘;
    #   · 口语形态 ("…放桌面/搁桌面") 由 `_match_planner` 的**生成型分支**用"以目录词收尾"兜住。
    #   ⇒ 断言只该测**行为结果** (_match_planner), 不该要求 target 也认识口语 (第一版就是这么白报 FAIL 的)。
    _narrow_ok = (pl._step_evidence("写一首诗保存到桌面")[2] is True
                  and pl._step_evidence("写一首诗搁桌面")[2] is False)
    chk("target 保持窄口径 (书面式认/口语不认, 交给 planner 分支兜)", _narrow_ok)
    _miss_p = [m for m in COLLOQ if not pl._match_planner(m, {})]
    chk(f"★ 这些句子走规划链 (生成+落盘), 不落给知识层 ({len(COLLOQ)} 句)",
        not _miss_p, _miss_p)
    # ★ 反向 (别把读请求也判成落盘): "看看桌面" 这类 n_q>=1 但**无生成动词**的句子,
    #   不许因为"以目录词收尾"就被判成多步落盘。
    _read_like = ["看看桌面", "列出桌面文件", "桌面有什么"]
    _bad_read = [m for m in _read_like if pl._match_planner(m, {})]
    chk(f"读类请求不被误判成落盘 ({len(_read_like)} 句)", not _bad_read, _bad_read)
    # 知识层直判 (零副作用, 不依赖模型): 指令尾巴不许被当成"正文"
    _DIR_TAIL = ("桌面", "文档", "下载", "desktop", "documents", "downloads")
    _bad_kb = []
    for m in COLLOQ:
        try:
            r = pl._ke.parse(m)
        except Exception as e:
            _bad_kb.append((m, "parse 异常 " + type(e).__name__))
            continue
        sk = (r.get("skill") or [None])[0]
        content = str((r.get("params") or {}).get("content") or "").strip()
        if sk and content and content.lower().endswith(_DIR_TAIL):
            _bad_kb.append((m, sk, content))
    chk(f"★ 知识层不把指令尾巴当正文写入 ({len(COLLOQ)} 句)", not _bad_kb, _bad_kb)
    # 真写文件请求不受影响 (加性): 带文件名的写请求走 **IR 链**, 内容照样对。
    # ★ 判据必须测**写盘实际发生的那条路** (IR 链) —— 第一版拿 `_ke.parse` 的内容当期望值,
    #   测错了层 ⇒ 白报 FAIL。
    _rt2 = get_router()
    _ok_pos = []
    for _m, _fname, _content in POS:
        _w = writes(_rt2.build_chain(auto_fill(_rt2.classify(_m))))
        if _w and str(_w[0]["params"].get("content", "")) == _content:
            _ok_pos.append(_m)
    chk(f"带文件名的真写请求内容未被新守卫误伤 ({len(POS)} 句)", len(_ok_pos) == len(POS),
        [m for m, _f, _c in POS if m not in _ok_pos])
    # 磁盘卫生扩展: 这类句子真跑过的话不许留 doc_ 垃圾
    import datetime as _dt
    _today = _dt.datetime.now().strftime("%Y%m%d")
    _d = [str(p) for p in DESKTOP.glob(f"doc_{_today}*.txt")]
    chk("桌面无 doc_<今天> 自动命名垃圾", not _d, _d)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil as _sh
        _sh.rmtree(SANDBOX, ignore_errors=True)
        _sh.rmtree(_ISO, ignore_errors=True)
