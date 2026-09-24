"""GitHub 巡检技能 (github-issues) — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: 一句"我的 github 有什么动静" → 走技能层 → **真打 GitHub API** → 出巡检。
关键设计: 不用 gh CLI (本机没装), 第一方工具 tools/github_api.py 只依赖标准库 urllib。

覆盖:
  A 技能结构 (加载 / 两步 DAG / answer_mode / requires_tools / 触发词 / tags 无泛词)
  B 自然说法可达性 (实测自然说法, 不拿 triggers 自证)
  C 真 pipeline (process(probe=True) + 打桩网关) —— 看**真的调了** github_api
  D 真 API (有 token 时) —— whoami/repos/digest 真通; 无 token 时**诚实失败**
  E 凭据卫生 —— keys_github.json 被 gitignore / 输出里不回显 token / 零副作用

跑法:
    python -B scripts/verification/verify_github_skill.py

隔离: 自带四变量自我隔离 (只在未设时才设); probe=True 不写用户历史。
网络: D 段需要联网 (打 api.github.com); 断网时该段 SKIP (不假报绿也不假报红)。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable

_ISO = Path(tempfile.gettempdir()) / ("tmm_gh_iso_%d" % os.getpid())
_ISO.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMM_SESSION_DB", str(_ISO / "sessions.db"))
os.environ.setdefault("TMM_GAP_DB", str(_ISO / "gap.db"))
os.environ.setdefault("TMM_PERCEPTION_FILE", str(_ISO / "perception.json"))
os.environ.setdefault("TMM_USAGE_LOG", str(_ISO / "usage.jsonl"))
sys.path.insert(0, str(ROOT))

NATURAL = [
    "我的 github 有什么动静",
    "看看我的仓库",
    "github 有什么新东西",
    "待办 issue 有哪些",
    "我的仓库动态",
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
    if str(tool) == "github_api":
        rec += ["limit=" + str(kw.get("limit", "")), "repo=" + repr(str(kw.get("repo", "")))[:40]]
    SIDE.append(rec)
    return {"success": True, "output": "[stub-tool]", "data": "[stub-digest]", "path": ""}
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


def _gh_reachable(timeout=8):
    """api.github.com 是否可达。HTTPError(403/401 等) 也算可达 —— 那是权限问题不是网络问题。"""
    import urllib.request as _u
    import urllib.error as _ue
    try:
        _u.urlopen("https://api.github.com/", timeout=timeout)
        return True
    except _ue.HTTPError:
        return True
    except Exception:
        return False


def main():
    P, F, SKIP = [], [], []

    def chk(name, cond, detail=""):
        (P if cond else F).append(name)
        print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))

    def skip(name, why):
        SKIP.append(name)
        print(f"  SKIP {name}   <- {why}")

    print("=" * 78)
    print("GitHub 巡检 github-issues —— 真打 API, 不用 gh CLI")
    print("=" * 78)

    # ── A 结构 ──
    print("\n[A] 技能结构")
    from core.skill_loader import get_skill_index
    eng = get_skill_index()
    idx = eng.get_index()
    dag = eng.get_dag("github-issues")
    chk("skill 已加载", "github-issues" in idx and dag is not None)
    if dag is None:
        print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
        return 1
    fm = dag.raw_source or {}
    steps = [(s.get("id"), s.get("tool"), s.get("action")) for s in dag.steps]
    chk("DAG = [github_api:digest → llm]", steps[0] == ("gh", "github_api", "digest")
        and steps[-1][1] == "llm", steps)
    chk("声明 answer_mode (查询类豁免产出守卫)", fm.get("answer_mode") is True, fm.get("answer_mode"))
    chk("steps 工具都声明在 requires_tools",
        {"github_api", "llm"} <= set(fm.get("requires_tools") or []), fm.get("requires_tools"))
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
        chk(f"「{m[:24]}」→ github-issues", "github-issues" in got, got[:50])
    for m in NEGATIVE:
        got = eng.match_context(m, max_skills=1) or ""
        chk(f"★ 不该被抢: 「{m}」", "github-issues" not in got, got[:50])

    # ── C 真 pipeline ──
    print("\n[C] 真 pipeline (process(probe=True) + 打桩网关)")
    sbx = ROOT / "tmp" / "_gh_sandbox"
    sbx.mkdir(parents=True, exist_ok=True)
    rows, err = run_pipeline([NATURAL[0]], sbx)
    if rows is None:
        chk("pipeline 采集成功", False, err[-400:])
    else:
        r = rows[0]
        side = r.get("side", [])
        print(f"      {r['msg'][:34]:<36} route={str(r.get('route')):<20} side={','.join(side)[:70]}")
        chk("★ 真调了 github_api:digest", any("github_api:digest" in s for s in side), side)
        chk("★ 胜出者是 github-issues", r.get("skill") == "github-issues", r.get("skill"))
        chk("★ 走的是技能层 (没被 IR chain / 兜底吞掉)", r.get("route") == "skill.trigger_first", r.get("route"))

    # ── D 真 API ──
    print("\n[D] 真 API (打 api.github.com)")
    import asyncio
    from tools import github_api as G
    chk("工具能读到 token (环境变量或 keys_github.json)", bool(G._token()), "没读到 —— 检查 keys_github.json / GH_TOKEN")
    if not G._token():
        skip("真 API 三连 (whoami/repos/digest)", "没配 token")
    elif not _gh_reachable():
        # 文件头承诺: 断网时 SKIP, 不假报绿也不假报红
        skip("真 API 三连 (whoami/repos/digest)", "api.github.com 网络不可达 —— 非代码问题, 不假报红")
        r_bad = asyncio.run(G.run(action="_nope_"))
        chk("★ 未知动作诚实失败", not r_bad.get("success"), str(r_bad)[:100])
        skip("★ issues 真通 (空列表也算通)", "同上 (需联网)")
    else:
        def _net0(r) -> str:
            """结果是**网络层失败**吗 (HTTP 0 / URLError / 握手超时 / DNS) → 命中词或空串。

            ★ 2026-09-22 加: `_gh_reachable()` 只探了**一次** —— 探活通过之后, 真调用仍可能
            撞上同一类出网抖动 (实测 `URLError: _ssl.c:989: The handshake operation timed out`,
            配额未耗尽 ⇒ 不是限流)。原先这 4 条一律判 FAIL ⇒ 把**网络抖动报成了代码红**。
            判据: 网络层失败 → SKIP 带原因 (诚实); 服务端给了 HTTP 错误码 → 照旧 FAIL
            (那是配置/请求问题, 有可操作性)。
            """
            if r.get("success"):
                return ""
            s = str(r)
            for k in ("HTTP 0", "URLError", "handshake", "timed out", "getaddrinfo",
                      "ConnectionReset", "Connection aborted"):
                if k in s:
                    return k
            return ""

        r_who = asyncio.run(G.run(action="whoami"))
        if _net0(r_who):
            skip("★ whoami 真通 (HTTP 200)", f"出网抖动 ({_net0(r_who)}) —— 非代码问题, 不假报红")
        else:
            chk("★ whoami 真通 (HTTP 200)", bool(r_who.get("success")) and bool(r_who.get("user")),
                str(r_who)[:140])
        r_rp = asyncio.run(G.run(action="repos", limit=5))
        if _net0(r_rp):
            skip("★ repos 真通且非空", f"出网抖动 ({_net0(r_rp)})")
        else:
            chk("★ repos 真通且非空", bool(r_rp.get("success")) and (r_rp.get("count") or 0) >= 1,
                str(r_rp)[:140])
        r_dg = asyncio.run(G.run(action="digest", limit=3))
        ok_dg = bool(r_dg.get("success")) and "身份:" in str(r_dg.get("output"))
        if _net0(r_dg):
            skip("★ digest 聚合真通 (身份+仓库)", f"出网抖动 ({_net0(r_dg)})")
        else:
            chk("★ digest 聚合真通 (身份+仓库)", ok_dg, str(r_dg.get("output"))[:160])
        # 未知动作必须诚实报错, 不能假装成功 (本地分发, 无需联网)
        r_bad = asyncio.run(G.run(action="_nope_"))
        chk("★ 未知动作诚实失败", not r_bad.get("success"), str(r_bad)[:100])
        # issue 列表真通 (可能为空 —— 空也是成功, 不假报红)
        r_is = asyncio.run(G.run(action="issues", limit=3))
        if _net0(r_is):
            skip("★ issues 真通 (空列表也算通)", f"出网抖动 ({_net0(r_is)})")
        else:
            chk("★ issues 真通 (空列表也算通)", bool(r_is.get("success")), str(r_is)[:140])

    # ── E 凭据卫生 ──
    print("\n[E] 凭据卫生 / 零副作用")
    r = subprocess.run(["git", "check-ignore", "-q", "keys_github.json"], cwd=str(ROOT),
                       capture_output=True, text=True)
    chk("★ keys_github.json 被 gitignore (不进仓库)", r.returncode == 0, f"exit={r.returncode} (0=已忽略)")
    src = (ROOT / "tools" / "github_api.py").read_text(encoding="utf-8")
    chk("工具不打印 token (无 print(token) 之类)", "print(tok" not in src and "print(_token" not in src)
    # 输出里不得回显 token 明文 (拿真 token 前缀在 digest 输出里找)
    tok = G._token()
    if tok:
        blob = str(asyncio.run(G.run(action="digest", limit=2)))
        chk("★ 输出/日志不含 token 明文", tok not in blob and tok[:12] not in blob, "输出里出现了 token!")
    chk("工具文件在 tools/ 且已登记 SAFE_PLUGINS",
        "github_api" in (ROOT / "config" / "settings.py").read_text(encoding="utf-8"))
    junk = [str(p) for p in [ROOT / "文档.txt"] if p.exists()]
    chk("项目根无残留", not junk, junk)

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
        shutil.rmtree(ROOT / "tmp" / "_gh_sandbox", ignore_errors=True)
        shutil.rmtree(_ISO, ignore_errors=True)
