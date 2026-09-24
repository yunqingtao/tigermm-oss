"""路由矩阵 — "谁吃了这句话" 的基线快照与回归门禁

**为什么需要它**
`core/pipeline.py` 的 `process()` 有 1002 行、26 个顶层裁决块、57 个 return 点,
优先级 = **行号**(没有任何显式规则)。想删重复块 / 抽路由表, 必须先有一张
"每句话当前被谁接走"的基线表 —— 否则改了哪句话被谁接走都不知道。

**它做什么**
把 N 句代表消息依次过 `process(probe=True)`, 记录胜出者(intent / skill / 副作用调用序列),
存成基线 JSON。之后任何改动跑一遍, 逐句比对, 胜出者变了就 FAIL。

**零副作用设计**(所以能安全覆盖全部层)
  · probe=True            → 不写用户记忆/历史
  · 打桩 model_client      → 不调真模型(确定性 + 零成本)
  · 打桩 gateway.call      → 不真发邮件/不真控制桌面/不真读表(只记录)
  · 打桩 subprocess.Popen  → 不真开浏览器
  · 快照还原 data/mode.json → 用户模式不被测试改

**跑法**
    # 建立/更新基线 (仅在确认当前行为正确时)
    python scripts/verification/verify_routing_matrix.py --write-baseline
    # 回归比对 (默认)
    python scripts/verification/verify_routing_matrix.py

**注意**
基线是"当前行为"的快照, 不是"正确行为"的定义。若某句当前走错了层,
先修行为、再更新基线 —— 别用基线给错误行为背书。
"""
import argparse
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
BASELINE = Path(__file__).resolve().parent / "routing_matrix_baseline.json"

# ═══════════════════════════════════════════════════════════════════
# 消息语料 — 覆盖 process() 里 26 个裁决块
# 每条注明**意图上的层**(用于人读; 比对只看实际胜出者)
# ═══════════════════════════════════════════════════════════════════
SBX = str(ROOT / "tmp" / "_routing_matrix")
# ★ 2026-09-21: 基线里原来存的是**绝对沙箱路径** —— 换台机器路径不同 → 语料比对必然"变了"。
#   现在存 `{SBX}` 占位, 加载时再替换回本机实际路径 (基线因此可跨机器复用)。
SBX_TOKEN = "{SBX}"


def _to_token(m: str) -> str:
    return m.replace(SBX, SBX_TOKEN)


def _from_token(m: str) -> str:
    return m.replace(SBX_TOKEN, SBX)
CORPUS = [
    # —— 内部命令 (前 5 块) ——
    ("/stats",                                  "命令: /stats"),
    ("/cost",                                   "命令: /cost"),
    ("/stats-old",                              "命令: /stats-old(legacy)"),
    ("/hive 拆解这个任务",                        "命令: /hive"),
    ("/tool system_info",                       "命令: /tool"),

    # —— 技能层 · 强触发词优先 (L1095) ——
    (f"写周报，本周修了 MCP 泄漏，存到 {SBX}/w.docx",  "技能: write-office-doc"),
    (f"做PPT讲项目进展，存到 {SBX}/p.pptx",            "技能: make-slides"),
    ("写个新功能上线的推广方案",                        "技能: write-proposal"),
    ("帮我写份简历，五年测试经验",                       "技能: write-resume"),
    ("写份述职报告，今年做了三个项目",                    "技能: write-self-review"),
    (f"把这个表格做成分析报告 {SBX}/t.xlsx 到 {SBX}/r.docx",
     "技能: table-to-report (三步 DAG)"),
    ("画个架构图",                                    "技能: diagram(缺参)"),

    # —— ★ 2026-09-21 短板1+B: 缺信息自查 / 行动判定 (路由 lookup.prefill, 150) ——
    ("帮我看看这100个Excel怎么归类",                    "缺信息 → 只读自查预填 (lookup.prefill)"),
    ("把这批文件整理一下",                              "要动手 → 指示优先调工具 (lookup.prefill)"),
    ("解释一下什么是闭包",                              "问概念 → 不注入自查块 (model.fallback)"),

    # —— TaskPlanner (L1122) ——
    ("先写公司背景，再写推广方案，最后给出预算",           "TaskPlanner: 2+ 多步词"),
    ("然后接着再并把步骤发给涛哥",                       "TaskPlanner: 堆多步词 ★2026-09-19修: 曾在缺内容时发垃圾邮件 → 现在问你要内容"),

    # —— 分析类 → 预读文件 (L1163) ——
    (f"分析一下 {SBX}/t.xlsx 里有什么",                "分析+路径 → 预读"),
    (f"{SBX}/t.xlsx 有哪些列",                        "分析+路径 → 预读"),

    # —— 时间/空/噪声 预过滤 (L1261) ——
    ("",                                        "空输入"),
    ("。。。",                                    "噪声"),

    # —— 插件关键词 (L1406) ——
    ("北京天气怎么样",                                "插件: openmeteo"),
    ("上海坐标是多少",                                "plugin.geocode ★2026-09-19修: 曾落 general"),
    ("把这句话翻译成英文",                             "插件: 翻译?"),

    # —— 搜索 (L1476) ——
    ("搜索一下 Python 装饰器",                        "web_search ★2026-09-19修: 曾被 brain 硬编码 tech 答案截走"),
    ("百度一下今天的新闻",                             "web_search ★2026-09-19修: 曾被 knowledge 罐头答案截走"),

    # —— 打开站点 (L1503) ——
    ("打开百度",                                     "open_browser ★2026-09-19修: 曾被 search 抢走"),

    # —— 已知缺陷 (标 [待修]): 归属 200/300 带 (brain._route('intent') → intent_router → file_ops list)
    #    实测: 含"建议"但没有目录语义的问句被列了 PROJECT_ROOT 目录 —— 明显错误。
    #    进基线只为**检测变化**; 修好后这里会 FAIL → 说明修复生效, 再更新基线。
    ("这个项目有什么改进建议呢",                        "★2026-09-19修: 曾被 intent_reasoner 判 list 列了 PROJECT_ROOT → 现在走模型"),
    ("分析一下这个项目有什么改进建议",                   "★2026-09-19修: 同上 (len=15 未过分析层门槛, 曾落 IR chain 列目录)"),

    # —— 900 @模型直通 (2026-09-19 提到最高优先) ——
    ("@deepseek 帮我看看这个怎么写",                    "@模型 → 直通", "deepseek"),
    # ★ 这 4 条旧行为是**被更早的块抢走**的 (实测), 修复后应走 model.explicit:
    ("@deepseek 搜索一下 Python 装饰器",                "@模型 > 搜索", "deepseek"),
    ("@deepseek 北京天气怎么样",                        "@模型 > 天气插件", "deepseek"),
    ("@deepseek 打开百度",                             "@模型 > 开站", "deepseek"),
    ("@deepseek 上海坐标是多少",                        "@模型 > 坐标插件", "deepseek"),
    # @ollama 例外: 本地模型不支持 function calling → 仍走本地链路 (技能层可接管)
    ("@ollama 先写背景，再写方案",                       "@ollama 例外 (不被 900 接走)", "ollama"),

    # —— Smart Route (L1533) ——
    ("这是" + "很长的一段话，" * 40 + "请分析",        "SmartRoute: 长文本"),

    # —— IR chain (L1553) ——
    ("写文件",                                       "IR: 写文件(缺参)"),
    ("把桌面文件发给涛哥",                             "IR: 文件发送(列目录)"),
    ("给涛哥写封邮件",                                "★2026-09-19修: 曾被判 check_mail(读收件箱)! 现在 send_email 意图 → 缺正文则要内容"),
    ("给涛哥发封邮件",                                "★2026-09-19修: 同上 (creation 形态)"),
    # ★ 锁死两条真 bug: ①意图层 send_email + delivery email 曾**追加第二步** → 同一封发两次
    #   ②消息里"内容:你好"曾触发 Brain 罐头问候语(子串匹配) → 发信请求被吞
    ("发邮件给 x@y.com 主题:嗨 内容:你好",             "★2026-09-19修: 曾发两封 + 被'你好'罐头劫持 → 现在单步 send_email"),

    # —— 本地兜底层 (L1758 技能 / L1787 fix guard / L1792-1834 L1-L3 / L1834+) ——
    ("帮我看看这份周报",                               "读类 → 不生成"),
    ("这个代码怎么优化",                               "通用 → 模型"),
    ("你好",                                        "问候"),
    ("帮我修一下",                                   "Fix Context Guard"),

    # —— 桌面控制 / FTS (L1868 / L1870) ——
    ("截个屏",                                       "desktop(已打桩)"),
    ("搜一下之前聊过的部署",                            "FTS 历史"),

    # —— 模型兜底 (L1883) ——
    ("说个笑话",                                     "模型兜底"),
    ("解释一下什么是闭包",                             "模型兜底"),
]


PROBE = r'''
import sys, os, json, asyncio, logging, hashlib, shutil
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
from core.knowledge import KnowledgeEngine
from config.settings import DATA_DIR

CASES = json.loads(sys.argv[2])          # [[msg, ext_model], ...]
PIN = sys.argv[4] if len(sys.argv) > 4 else ""   # 采集期间钉死的模式
SBX = sys.argv[3]
MJ = r"@@PROJ@@/data/mode.json"

# ★ 备份用户 mode.json, 结束时还原
_mj_bak = None
if os.path.exists(MJ):
    _mj_bak = open(MJ, "rb").read()

# ★★ 感知引擎改写**沙箱**: core.perception 的持久化目标默认指向用户的
#   data/perception.json —— 本探针跑的是**真 pipeline**, 会往那里写"纠正记录"
#   (实测: 46 句真跑一轮就多出纠正条目; 崩溃/kill 时还原逻辑跑不到, 残留就留下)。
#   根治: 在 import pipeline 之前把 PERCEPTION_FILE 重定向到沙箱 → 无论崩不崩都不脏用户文件。
import core.perception as _perc
_perc.PERCEPTION_FILE = __import__("pathlib").Path(SBX) / "_sandbox_perception.json"

cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg)
pl._ke = KnowledgeEngine(DATA_DIR)

SIDE = []          # 副作用调用记录 (证明"只记录不执行")

# ── 打桩 1: 模型 (确定性 + 零成本) ──
async def fake_gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
    SIDE.append(["llm", str(model)])
    return {"text": "[stub-llm]", "think": "", "model": model, "elapsed": 0.0,
            "error": None, "tool_calls": None}
pl.model_client.generate = fake_gen

# ── 打桩 2: 工具网关 (不真发邮件/不真读表/不真控制桌面) ──
_orig_call = pl.gateway.call
async def fake_call(tool, **kw):
    SIDE.append(["gateway", str(tool) + (":" + str(kw.get("action")) if kw.get("action") else "")])
    return {"success": True, "output": "[stub-tool]", "path": str(kw.get("path", ""))}
pl.gateway.call = fake_call

# ── 打桩 3: 子进程 (不真开浏览器) ──
import subprocess as _sp
class _FakeP:
    pid = -1
    def __init__(self, *a, **k): SIDE.append(["popen", str(a[0] if a else k)[:40]])
    def wait(self, *a, **k): return 0
_sp.Popen = _FakeP

async def main():
    out = []
    for _msg, _ext in CASES:
        msg = _msg
        SIDE.clear()
        try:
            # ★ 显式钉死 craft: 矩阵不得依赖外部可变状态
            #   (首次采集时 mode.json 恰为 plan → 第 6 句起全走提案路径, 基线作废)
            r = await pl.process(msg, ext_model=(_ext or "auto"), probe=True,
                                 mode_override="craft")
        except Exception as e:
            r = {"intent": "EXC:" + type(e).__name__, "response": str(e)[:80]}
        out.append({
            "msg": msg, "ext": _ext,
            "intent": r.get("intent"),
            "skill": r.get("skill"),
            "route": r.get("route"),          # ★ P1: 胜出路由 (仅记录/展示, 不参与比对)
            "model": r.get("model"),
            "resp": str(r.get("response"))[:70],
            "side": sorted({(":".join(map(str, s))) for s in SIDE}),
        })
    print("<<<J>>>" + json.dumps(out, ensure_ascii=False))

try:
    asyncio.run(main())
finally:
    # ★ 还原用户 mode.json
    if _mj_bak is not None:
        open(MJ, "wb").write(_mj_bak)
    # 清沙箱
    shutil.rmtree(SBX, ignore_errors=True)
'''


def run_matrix():
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(tmp).write_text(_fill(PROBE), encoding="utf-8")
    try:
        # 造沙箱 + 语料里的输入文件
        sbx = ROOT / "tmp" / "_routing_matrix"
        sbx.mkdir(parents=True, exist_ok=True)
        try:
            import openpyxl
            wb = openpyxl.Workbook(); ws = wb.active
            ws.append(["门店", "城市", "月销量"])
            for r in [("A店", "北京", 1200), ("B店", "上海", 860)]:
                ws.append(list(r))
            wb.save(sbx / "t.xlsx"); wb.close()
        except Exception as e:
            print(f"  (警告: 造 xlsx 失败 {e})")

            # ★ 传 (msg, ext_model) 对 —— @模型 用例要带模型名
        # ★ 2026-09-21: 新的 lookup.prefill(150) 路由会做"缺信息只读自查" —— 用环境变量把它
        #   收口到本沙箱, 免得每次跑矩阵都去通扫用户真实的桌面/下载 (慢, 且结果不 hermetic)。
        cases = [[c[0], (c[2] if len(c) > 2 else None)] for c in CORPUS]
        _env = dict(os.environ)
        _env["TMM_SELF_LOOKUP_DIRS"] = str(sbx)
        r = subprocess.run([PY, "-B", tmp, "run", json.dumps(cases, ensure_ascii=False), str(sbx), "craft"],
                           cwd=str(ROOT), capture_output=True, text=True, env=_env,
                           encoding="utf-8", errors="replace", timeout=580)
        if "<<<J>>>" not in (r.stdout or ""):
            return None, (r.stdout or "") + (r.stderr or "")
        return json.loads(r.stdout.split("<<<J>>>")[1].strip().splitlines()[0]), ""
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-baseline", action="store_true",
                    help="把当前行为写成基线 (仅在确认当前行为正确时)")
    args = ap.parse_args()

    # 保护: 用户 mode.json 快照
    mj = ROOT / "data" / "mode.json"
    mj_snap = mj.read_bytes() if mj.exists() else None
    # ★ perception.json 也会被真跑的 46 句写 (实测: 跑一轮矩阵就多出纠正记录)
    pj = ROOT / "data" / "perception.json"
    pj_snap = pj.read_bytes() if pj.exists() else None

    P, F = [], []
    def chk(n, c, d=""):
        (P if c else F).append(n)
        print(("  PASS " if c else "  FAIL ") + n + (f"   <- {d}" if d and not c else ""))

    print("=" * 78)
    print("路由矩阵 —— 采集当前行为")
    print("=" * 78)
    rows, err = run_matrix()
    if rows is None:
        print("  采集失败:", err[-500:])
        return 1

    by_msg = {r["msg"]: r for r in rows}
    print(f"  采到 {len(rows)} 句\n")
    print(f"  {'消息':<40} {'胜出层':<22} {'路由表':<16} 副作用")
    print("  " + "-" * 92)
    for _c, r in zip(CORPUS, rows):
        msg, note = _c[0], _c[1]
        mx = (msg[:42] + "…") if len(msg) > 43 else msg
        win = f"{r['intent']}" + (f"/{r['skill']}" if r.get("skill") else "")
        rt = f"[{r.get('route')}]" if r.get("route") else ""
        side = ",".join(r["side"])[:26] or "-"
        print(f"  {mx:<40} {win:<22} {rt:<16} {side}")
        print(f"  {'':<44} ({note})")

    if args.write_baseline:
        for _r in rows:
            _r["msg"] = _to_token(_r["msg"])
        BASELINE.write_text(json.dumps(
            {"note": "路由矩阵基线 — 当前实现下每句消息的胜出者。改行为后需人工确认再更新。",
             "corpus_note": [c[1] for c in CORPUS],
             "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n  基线已写入: {BASELINE.name} ({BASELINE.stat().st_size}B)")
        return 0

    # —— 回归比对 ——
    print("\n" + "=" * 78)
    print("与基线比对")
    print("=" * 78)
    if not BASELINE.exists():
        print("  [x] 基线不存在 — 先跑 --write-baseline")
        return 1
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    bmap = {_from_token(r["msg"]): r for r in base["rows"]}

    changed, added, removed = [], [], []
    for r in rows:
        b = bmap.get(r["msg"])
        if b is None:
            added.append(r["msg"]); continue
        if (b["intent"], b.get("skill")) != (r["intent"], r.get("skill")):
            changed.append((r["msg"], f"{b['intent']}/{b.get('skill')}", f"{r['intent']}/{r.get('skill')}"))
    for m in bmap:
        if _from_token(m) not in by_msg:
            removed.append(m)

    for m, was, now in changed:
        print(f"  ✗ 胜出者变了: {m[:40]!r}")
        print(f"      基线 {was}  →  现在 {now}")
    for m in added:
        print(f"  + 新增消息(基线没有): {m[:40]!r}")
    for m in removed:
        print(f"  - 基线有、本次没跑: {m[:40]!r}")

    chk(f"胜出者与基线一致 ({len(rows)-len(changed)}/{len(rows)})", not changed, f"{len(changed)} 句变了")
    chk("语料未变", not added and not removed, f"+{len(added)} -{len(removed)}")

    # 用户模式还原
    if mj_snap is not None:
        mj.write_bytes(mj_snap)
        ok = hashlib.md5(mj.read_bytes()).hexdigest() == hashlib.md5(mj_snap).hexdigest()
        chk("用户 mode.json 已还原", ok)

    # ★ 还原 perception.json (验证不得留痕 —— 门禁 runner 会比对 md5 兜底)
    if pj_snap is not None:
        pj.write_bytes(pj_snap)
        okp = hashlib.md5(pj.read_bytes()).hexdigest() == hashlib.md5(pj_snap).hexdigest()
        chk("用户 perception.json 已还原", okp)
        print("        mode =", json.loads(mj.read_text(encoding="utf-8")).get("mode"))

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
