"""技能可达性 —— 装机即验证 (常驻验证器)

**为什么需要它**
`verify_skill_format.py` 只验"文件格式合法"; `verify_skill_dag_layer.py` 只验"执行层能跑"。
两者都**不验**: 一个技能装了之后, **它的 triggers 到底能不能真赢到自己的路由** ——
也就是它会不会被更早的裁决块(`_analysis_kw` / TaskPlanner / SmartRoute / IR chain)劫持,
或者反过来抢走别的技能的句子。

**它做什么**
对 `tmm_skills/` 下**每一个**技能:
  1. 取它的 triggers, 构造一句最该由它接手的消息
  2. 过真 `process()`(打桩模型/网关/子进程, probe=True)  → 断言胜出者**就是它**
  3. ★ 反向: 断言这句**没被别的技能接手**(防抢)
  4. ★ 断言 DAG 真的被执行(有 llm/工具步骤被走到, 或诚实失败带缺参提示)
  5. 断言技能有 **steps**(不是"只有 triggers 的散文")

这样"新装一个技能"这件事本身就被验证了 —— 加技能后跑一遍, 安全是被证明的, 不是假设的。

跑法:
    python scripts/verification/verify_skill_reachability.py
自清理: 打桩 + probe + 快照还原 data/mode.json。
"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

def _fill(s: str) -> str:
    """把探针模板里的 @@PROJ@@ 换成真实项目根。

    ★ 2026-09-21: 探针脚本写在临时目录里, 自己推不出项目根 (`__file__` 指向 %TEMP%)。
      原来是**写死绝对路径** —— 公开/换机就会指向别处。现在用占位符, 写入时填充。
    """
    # ★ 用**正斜杠**注入: 探针里的路径常被拼进中文话术再交给引擎解析, 反斜杠会与 "/"
    #   混用导致解析失败 (实测: 会当成"没给目录"而落到桌面)。正斜杠在 Windows 上同样合法。
    return s.replace("@@PROJ@@", str(ROOT).replace("\\", "/"))


PY = sys.executable
MJ = ROOT / "data" / "mode.json"
SBX = ROOT / "tmp" / "_skill_reach"

P, F = [], []
def chk(n, c, d=""):
    (P if c else F).append(n); print(("  PASS " if c else "  FAIL ") + n + (f"   <- {d}" if d and not c else ""))


PROBE = r'''
import sys, os, json, asyncio, logging, re, yaml
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
from core.knowledge import KnowledgeEngine
from config.settings import DATA_DIR

SBX = r"@@PROJ@@/tmp/_skill_reach"
os.makedirs(SBX, exist_ok=True)

# ── 收集每个技能的 triggers / steps / 是否含生成步骤 ──
SKDIR = r"@@PROJ@@/tmm_skills"
skills = []
for d in sorted(os.listdir(SKDIR)):
    # ★ 2026-09-24: 跳过测试夹具 (下划线开头, 如 _broken_no_fm)
    if d.startswith("_"):
        continue
    f = os.path.join(SKDIR, d, "SKILL.md")
    if not os.path.exists(f):
        continue
    txt = open(f, encoding="utf-8").read()
    m = re.match(r"---\s*\n(.*?)\n---", txt, re.S)
    fm = yaml.safe_load(m.group(1)) if m else {}
    steps = fm.get("steps") or []
    skills.append({
        "dir": d,
        "name": fm.get("name"),
        "triggers": fm.get("triggers") or [],
        "n_steps": len(steps),
        "tools": sorted({str(s.get("tool")) for s in steps if isinstance(s, dict)}),
        "has_llm": any(isinstance(s, dict) and s.get("tool") == "llm" for s in steps),
        "params": fm.get("params") or {},
    })

cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg); pl._ke = KnowledgeEngine(DATA_DIR)
SIDE = []

async def fake_gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
    SIDE.append(["llm", str(model)])
    return {"text": "# 产出\n## 一\n- 要点", "error": None}
pl.model_client.generate = fake_gen

async def fake_call(tool, **kw):
    SIDE.append(["gateway", str(tool)])
    return {"success": True, "output": "[stub]", "path": str(kw.get("path", ""))}
pl.gateway.call = fake_call

import subprocess as _sp
class _FakeP:
    pid = -1
    def __init__(self, *a, **k): SIDE.append(["popen", str(a[0] if a else "")[:40]])
    def wait(self, *a, **k): return 0
_sp.Popen = _FakeP

# 造数据文件 (表格类技能要用)
import openpyxl
wb = openpyxl.Workbook(); ws = wb.active
ws.append(["门店", "城市", "月销量"])
for r in [("A店", "北京", 1200), ("B店", "上海", 860)]:
    ws.append(list(r))
wb.save(os.path.join(SBX, "t.xlsx")); wb.close()

ROUTE_TAIL = "存到 " + SBX + "/out.docx"

async def main():
    rows = []
    for s in skills:
        # ★ 优先中文 trigger (取最长) —— 别拿英文 trigger 造中文句子 (会造出乱句子)
        cn = [x for x in (s["triggers"] or []) if re.search(r"[\u4e00-\u9fff]", x)]
        pool = cn or (s["triggers"] or [])
        trig = sorted(pool, key=len, reverse=True)[0] if pool else ""
        if not trig:
            rows.append({**{k: s[k] for k in ("dir","name","n_steps","tools","has_llm","params")},
                         "trig": "", "msg": "", "intent": None, "got": None, "side": []})
            continue
        # 有生成步骤 → 加"写"动词过 guard_generation; 需要输入表的 → 带上 xlsx
        extra = ""
        if "path" in (s.get("params") or {}):
            extra = " " + os.path.join(SBX, "t.xlsx")
        msg = ("帮我写一份" if s["has_llm"] else "帮我") + trig + extra + "，" + ROUTE_TAIL
        SIDE.clear()
        try:
            r = await pl.process(msg, probe=True, mode_override="craft")
        except Exception as e:
            r = {"intent": "EXC:" + type(e).__name__, "skill": None, "response": str(e)[:100]}
        rows.append({
            "dir": s["dir"], "name": s["name"], "n_steps": s["n_steps"],
            "tools": s["tools"], "has_llm": s["has_llm"], "trig": trig, "msg": msg,
            "intent": r.get("intent"), "got": r.get("skill"),
            "resp": str(r.get("response"))[:100],
            "side": sorted({":".join(map(str, x)) for x in SIDE}),
        })
    print("<<<J>>>" + json.dumps(rows, ensure_ascii=False))

try:
    asyncio.run(main())
finally:
    shutil.rmtree(SBX, ignore_errors=True)
'''


def run_probe():
    fd, tmp = tempfile.mkstemp(suffix=".py"); os.close(fd)
    Path(tmp).write_text(_fill(PROBE), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp], cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=580)
        if "<<<J>>>" not in (r.stdout or ""):
            return None, (r.stdout or "") + (r.stderr or "")
        return json.loads(r.stdout.split("<<<J>>>")[1].strip().splitlines()[0]), ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    snap = MJ.read_bytes() if MJ.exists() else None
    try:
        print("=" * 78)
        print("技能可达性 —— 装机即验证")
        print("=" * 78)
        rows, err = run_probe()
        if rows is None:
            print("  探针失败:", err[-600:]); return 1

        print(f"  {'技能':<24} {'步':<3} {'trig':<14} {'胜出者':<26} {'旁证'}")
        print("  " + "-" * 104)
        for r in rows:
            got = f"{r['intent']}" + (f"/{r['got']}" if r.get("got") else "")
            print(f"  {r['dir']:<24} {r['n_steps']:<3} {r['trig'][:13]:<14} {got:<26} {','.join(r['side'])[:26]}")
        print()

        no_trig = [r["dir"] for r in rows if not r["trig"]]
        chk("每个技能都有 triggers", not no_trig, str(no_trig))

        # ① ★ 必须真有 steps (不是"只有 triggers 的散文")
        prose = [r["dir"] for r in rows if r["n_steps"] == 0]
        chk("★ 每个技能都有 steps (非散文)", not prose, str(prose))

        # ② ★ 可达性: 有生成步骤的技能必须走技能层; 工具类技能走技能层**或**走按设计的兜底层
        #    (纯工具技能被**故意**排除在早路由之外, 防它们抢生成类请求 —— 见 pipeline 注释)
        def _honest_ask(r):
            """★ 工具类技能的合法结局之一: **诚实问你要缺的东西**。

            2026-09-19 加固 send 守卫后, "缺内容就发信" 被改成"问你要收件人/主题/内容" ——
            这比以前**真发一封垃圾邮件**正确。所以"没调工具"不等于"不可达", 只要它
            明确告诉你缺什么。仍然有辨别力: 静默什么都不做、或谎报成功 → 照样判红。
            """
            resp = r.get("resp") or ""
            return any(k in resp for k in ("缺", "请提供", "请告诉", "需要你", "请给", "请补充"))

        def reachable(r):
            if not r["trig"]:
                return False
            if r["intent"] == "skill_dag" and r["got"] == r["name"]:
                return True
            if r["has_llm"]:
                return False        # 生成类必须走技能层
            # 工具类: 走兜底层(IR chain) 但**它声明的工具真被调用** → 也算可达
            if any(any(tl in sd for sd in r["side"]) for tl in r["tools"]):
                return True
            return _honest_ask(r)          # 或: 诚实问你要缺的东西
        unreach = [r["dir"] for r in rows if not reachable(r)]
        chk("★ 每个技能都可达 (生成类走技能层 / 工具类走兜底层且工具真被调用)", not unreach, str(unreach))

        # ③ ★ 不抢: 没有别的技能接手别人的句子
        stolen = [(r["dir"], r["got"]) for r in rows
                  if r["got"] and r["got"] != r["name"]]
        chk("★ 没有技能抢走别人的句子", not stolen, str(stolen))

        # ④ ★ DAG 真被执行: 生成类必须有 llm 步骤被走到
        gen = [r for r in rows if r["has_llm"]]
        gen_ok = [r for r in gen if any(s.startswith("llm") for s in r["side"])]
        chk(f"★ 生成类技能的 llm 步骤真被走到 ({len(gen_ok)}/{len(gen)})",
            len(gen_ok) == len(gen), str([r["dir"] for r in gen if r not in gen_ok]))

        # ⑤ 工具类: 必须有工具被调用或被诚实拒绝
        tool = [r for r in rows if not r["has_llm"]]
        tool_ok = [r for r in tool
                   if any(s.startswith("gateway") for s in r["side"]) or _honest_ask(r)]
        chk(f"工具类技能有工具调用 或 诚实问你要缺的东西 ({len(tool_ok)}/{len(tool)})",
            len(tool_ok) == len(tool), str([r["dir"] for r in tool if r not in tool_ok]))
    finally:
        if snap is not None:
            MJ.write_bytes(snap)
            print(f"\n[cleanup] mode.json 已还原: {MJ.read_bytes() == snap}")
        shutil.rmtree(SBX, ignore_errors=True)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
