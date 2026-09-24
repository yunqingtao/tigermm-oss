"""路由表 (core/routes.py) — 常驻验证器 (留档, 不是 pytest 套件)

跑法:
    python -B scripts/verification/verify_route_registry.py

**为什么要有它**
P1 把 process() 的"行号优先级"换成显式路由表。表本身是新的关键基础设施:
裁决错了 → 整条 pipeline 的胜出者都错。所以表的行为必须有独立验证。

**重点验的是两个不变量**(这是本模块存在的意义):
  probe_safe    该路由在 probe 流量下零副作用
  writes_state  该路由会写用户状态
  → dispatch 必须**强制执行**: probe 流量遇到 writes_state 且非 probe_safe → 跳过并记账
  这一条防的是实测踩过的坑: 验证流量把 data/mode.json 写成 plan, 之后所有请求走提案路径。

覆盖:
  A. 注册语义: 重名拒绝 / 优先级排序 / 稳定同优先级
  B. 裁决: 第一个 match 胜出 / 都不中返回 None / 结果带 route / trace 准确
  C. ★ 不变量: probe 流量跳过 writes_state 路由 (且记账); 非 probe 正常执行
  D. 健壮性: match 抛异常不拖垮链条 / requires 缺失自动跳过 / run 支持 async
  E. 真 pipeline 接线: 5 个命令句路由正确 + route 字段 + audit 干净
  F. ★ 辨别力: 去掉不变量守卫 → 必须被捕获 (无效验证不算)

改动历史:
  2026-09-19  P1 建立 (随 core/routes.py 一起)
  踩坑: 第一版把 ad-hoc 脚本的"跑完自删"抄了进来 —— **常驻验证器绝不能自删**,
        它第一次运行就把自己删了。已去掉 SELF.unlink()。
"""
import json
import os
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

P, F = [], []
def chk(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS " if c else "  FAIL ") + n + (f"   <- {d}" if d and not c else ""))


PROBE = r'''
import sys, os, json, asyncio, logging
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from core.routes import RouteRegistry, Route

out = {}
STATEFUL_RAN = []

async def main():
    # ── A. 注册语义 ──
    reg = RouteRegistry()
    reg.register(Route(name="low", priority=100, match=lambda m, c: True, run=lambda m, c: {"intent": "low"}))
    reg.register(Route(name="high", priority=900, match=lambda m, c: True, run=lambda m, c: {"intent": "high"}))
    reg.register(Route(name="mid_a", priority=500, match=lambda m, c: False, run=lambda m, c: {"intent": "a"}))
    reg.register(Route(name="mid_b", priority=500, match=lambda m, c: False, run=lambda m, c: {"intent": "b"}))
    out["order"] = reg.names()
    dup = False
    try:
        reg.register(Route(name="low", priority=1, match=lambda m, c: True, run=lambda m, c: {}))
    except ValueError:
        dup = True
    out["dup_rejected"] = dup

    # ── B. 裁决 ──
    r1 = await reg.dispatch("x", {"probe": False})
    out["winner"] = r1.get("intent")
    out["has_route_key"] = "route" in r1
    out["trace_winner"] = reg.last_trace.winner
    out["trace_tested"] = [e.route for e in reg.last_trace.entries]

    reg2 = RouteRegistry()
    reg2.register(Route(name="never", priority=1, match=lambda m, c: False, run=lambda m, c: {"intent": "x"}))
    out["none_when_no_match"] = (await reg2.dispatch("x", {"probe": False})) is None

    # ── C. ★ 不变量: probe 流量必须跳过 writes_state 路由 ──
    reg3 = RouteRegistry()
    async def run_stateful(m, c):
        STATEFUL_RAN.append(c.get("probe"))
        return {"intent": "stateful"}
    reg3.register(Route(name="stateful", priority=100, match=lambda m, c: True,
                        run=run_stateful, probe_safe=False, writes_state=True))
    reg3.register(Route(name="safe", priority=50, match=lambda m, c: True, run=lambda m, c: {"intent": "safe"}))
    r_probe = await reg3.dispatch("x", {"probe": True})
    _probe_trace = list(reg3.last_trace.entries)     # ★ 立刻取: live 那次会覆盖 last_trace
    r_live = await reg3.dispatch("x", {"probe": False})
    out["inv"] = {
        "probe_winner": r_probe.get("intent"),       # 期望 safe (stateful 被跳过)
        "probe_skipped_recorded": any(e.route == "stateful" and e.skipped for e in _probe_trace),
        "live_winner": r_live.get("intent"),          # 期望 stateful (非 probe 正常)
        "stateful_ran": STATEFUL_RAN,                 # 只应在 probe=False 时跑过一次
    }

    # ── C2. ★ 命令类 stateful 路由: probe 下必须"明确跳过", 不能掉到无关路由 ──
    #   事故背景 (2026-09-19 深测): `/learn stats` 在 probe 下被正确跳过, 但继续 fall-through
    #   到 brain 的罐头技能, 答出 "OK 文件信息: size=40960B…" —— 一个**看似正常其实无关**的答案,
    #   会误导任何用 probe 做验证的人。修法: 命令类 (cmd.*) 路由被 probe 跳过时, 明确回
    #   route="probe.skip"; 通用 stateful 路由仍保持 fall-through (见 C 段契约)。
    reg5 = RouteRegistry()
    reg5.register(Route(name="cmd.fake", priority=1000, match=lambda m, c: True,
                        run=lambda m, c: {"intent": "cmd_ran"}, probe_safe=False, writes_state=True))
    reg5.register(Route(name="safe_late", priority=50, match=lambda m, c: True,
                        run=lambda m, c: {"intent": "safe"}))
    r_cmd_probe = await reg5.dispatch("x", {"probe": True})
    out["cmd_probe_skip"] = {
        "route": r_cmd_probe.get("route"),
        "skipped_route": r_cmd_probe.get("skipped_route"),
        "not_fell_through": r_cmd_probe.get("route") != "safe",
    }

    # ── D. 健壮性 ──
    reg4 = RouteRegistry()
    def boom(m, c): raise RuntimeError("match 炸了")
    reg4.register(Route(name="boom", priority=900, match=boom, run=lambda m, c: {"intent": "boom"}))
    reg4.register(Route(name="ok", priority=100, match=lambda m, c: True, run=lambda m, c: {"intent": "ok"}))
    out["match_exc_survived"] = (await reg4.dispatch("x", {"probe": False})).get("intent") == "ok"
    out["match_exc_recorded"] = any("异常" in e.skipped for e in reg4.last_trace.entries)

    class Owner: pass
    o = Owner(); o.present = object()                # 只有 present, 没有 missing
    reg5 = RouteRegistry()
    reg5.register(Route(name="needs", priority=900, match=lambda m, c: True,
                        run=lambda m, c: {"intent": "needs"}, requires=("missing",)))
    reg5.register(Route(name="no_need", priority=10, match=lambda m, c: True, run=lambda m, c: {"intent": "no_need"}))
    out["requires_gated"] = (await reg5.dispatch("x", {"probe": False}, owner=o)).get("intent") == "no_need"

    # ── E. 真 pipeline 接线 ──
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.intent_router import ACTION_INTENTS as _ACTION_INTENTS
    from core.knowledge import KnowledgeEngine
    from config.settings import DATA_DIR
    cfg = _load_config()
    pl = Level4Pipeline(ModelClient(cfg), cfg); pl._ke = KnowledgeEngine(DATA_DIR)
    async def fake_gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
        return {"text": "[stub]", "error": None}
    pl.model_client.generate = fake_gen
    async def fake_call(tool, **kw):
        return {"success": True, "output": "[stub]"}
    pl.gateway.call = fake_call
    out["real_routes"] = pl.routes.names()
    out["audit"] = pl.routes.audit()
    # ★ 合成注册表: 验 audit 对 probe_gated 的分辨力
    #   (声明了 probe_gated → 不报; 只 writes_state 没声明 → 必须报)
    try:
        from core.routes import RouteRegistry as _RR, Route as _R2
        _rr = _RR()
        _rr.register(_R2(name="z.undeclared", priority=1, match=lambda m, c: True,
                         run=lambda m, c: {}, writes_state=True, probe_safe=False))
        _rr.register(_R2(name="z.gated", priority=1, match=lambda m, c: True,
                         run=lambda m, c: {}, writes_state=True, probe_safe=False,
                         probe_gated=True))
        out["audit_synth"] = _rr.audit()["issues"]
    except Exception as _e:
        out["audit_synth"] = ["EXC " + repr(_e)]
    # ★ 关系型: 从 _build_routes 源码里解析出**声明了哪些 route name**,
    #   断言注册表与之完全一致 (别写死数量 —— 加路由就得改验证器)
    import re as _re, inspect as _insp
    src_build = _insp.getsource(type(pl)._build_routes)
    out["declared"] = sorted(set(_re.findall(r'name="([^"]+)"', src_build)))
    out["by_priority"] = [(r.name, r.priority) for r in pl.routes.routes()]
    # ★ requires 的每个名字必须是 Level4Pipeline 的真实属性 ——
    #   写错(如把 _ke 写成 ke)会让该路由**静默跳过**, 行为悄悄退化:
    #   实测踩过: l4.knowledge 因 requires=("ke",) 被跳过 → 垃圾输入落到模型 → 模型谎报"已写入"
    # ★ 工具参数契约: 每个 intent 声明的 (tool, action) 必须**真能调用** ——
    #   action 只有工具签名接受(action 参数)或工具有 **kwargs 时才可以注入。
    #   实测踩过: send_email 是严格签名(无 action/**kwargs), 而 IR chain 注入 action="send"
    #   → "run() got an unexpected keyword argument 'action'" → 邮件根本没发出去。
    import inspect as _ins, importlib as _il
    _rows, _sigs = [], {}
    for _n, _info in _ACTION_INTENTS.items():
        _tool, _act = _info.get("tool"), _info.get("action")
        if not _act or not _tool:
            continue
        if _tool not in _sigs:
            _mod = None
            for _c in (f"tools.{_tool}", f"plugins.{_tool}", _tool):
                try:
                    _mod = _il.import_module(_c); break
                except Exception:
                    pass
            if _mod is None:
                # 模块导入失败(缺第三方依赖/环境问题, 如 windows_desktop 缺 comtypes.gen)
                # → 无法判断, 记 None **不算失败** (否则门禁会因环境问题长期红着而失去意义)
                _sigs[_tool] = "UNINSPECTABLE"
            else:
                _fn = getattr(_mod, "run", None)
                _sigs[_tool] = (list(_ins.signature(_fn).parameters) if _fn else "NO_RUN")
        _ps = _sigs[_tool]
        if not isinstance(_ps, list):
            continue                      # 跳过不可检查的
        _ok = ("action" in _ps) or ("kwargs" in _ps)
        _rows.append((_n, _tool, _act, _ps, _ok))
    out["tool_contract"] = [(n, tl, ac, ps) for n, tl, ac, ps, ok in _rows if not ok]

    out["reqs"] = [(r.name, list(r.requires),
                    [x for x in r.requires if getattr(pl, x, None) is None])
                   for r in pl.routes.routes() if r.requires]
    cases = [("/stats", "stats", "cmd.stats"), ("/cost", "cost", "cmd.cost"),
             ("/stats-old", "stats", "cmd.stats_old"), ("/hive", "system", "cmd.hive"),
             ("/tool system_info", "tool", "cmd.tool")]
    got = []
    for m, wi, wr in cases:
        r = await pl.process(m, probe=True, mode_override="craft")
        got.append({"msg": m, "want_intent": wi, "want_route": wr,
                    "intent": r.get("intent"), "route": r.get("route")})
    out["real_cases"] = got
    print("<<<J>>>" + json.dumps(out, ensure_ascii=False, default=str))

asyncio.run(main())
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
    mj = ROOT / "data/mode.json"
    snap = mj.read_bytes() if mj.exists() else None
    try:
        d, err = run_probe()
        chk("探针跑通", d is not None, (err or "")[-400:])
        if not d:
            return 1

        print("\nA. 注册语义")
        chk("按 priority 降序 (首=最高, 尾=最低)",
            d["order"][0] == "high" and d["order"][-1] == "low", str(d["order"]))
        chk("顺序严格正确 (high900 → mid500 保序 → low100)",
            d["order"] == ["high", "mid_a", "mid_b", "low"], str(d["order"]))
        chk("★ 重名拒绝 (不静默覆盖)", d["dup_rejected"] is True)

        print("\nB. 裁决 + trace")
        chk("第一个 match 胜出", d["winner"] == "high", str(d["winner"]))
        chk("结果带 route 字段", d["has_route_key"] is True)
        chk("trace 记录胜出者", d["trace_winner"] == "high")
        chk("trace 记录考察过谁", d["trace_tested"] == ["high"], str(d["trace_tested"]))
        chk("全不中返回 None (交给兜底链路)", d["none_when_no_match"] is True)

        print("\nC. ★ 不变量: probe 流量跳过 writes_state 路由")
        inv = d["inv"]
        chk("★ 命令类 stateful: probe 下明确跳过 (route=probe.skip, 不掉到 safe)",
            d["cmd_probe_skip"]["route"] == "probe.skip"
            and d["cmd_probe_skip"]["skipped_route"] == "cmd.fake"
            and d["cmd_probe_skip"]["not_fell_through"], str(d["cmd_probe_skip"]))
        chk("★ probe 流量: stateful 被跳过, safe 胜出", inv["probe_winner"] == "safe", str(inv))
        chk("★ 跳过被记账 (trace 有原因)", inv["probe_skipped_recorded"] is True, str(inv))
        chk("非 probe 流量: stateful 正常胜出", inv["live_winner"] == "stateful", str(inv))
        chk("★ 有副作用的 run 只跑过一次 (probe 那次没跑)",
            inv["stateful_ran"] == [False], str(inv["stateful_ran"]))

        print("\nD. 健壮性")
        chk("match 抛异常不拖垮链条", d["match_exc_survived"] is True)
        chk("异常被记进 trace", d["match_exc_recorded"] is True)
        chk("requires 缺失自动跳过", d["requires_gated"] is True)

        print("\nE. 真 pipeline 接线")
        declared, regged = d["declared"], d["real_routes"]
        _bad_tc = d.get("tool_contract") or []
        chk("★ intent 的 (tool, action) 全部可调用 (无 TypeError 风险)",
            not _bad_tc, f"会崩的: {[(n, tl, ac) for n, tl, ac, _ps in _bad_tc]}")

        _bad_req = [(n, missing) for n, _rq, missing in d["reqs"] if missing]
        chk(f"★ requires 全部指向真实属性 ({len(d['reqs'])} 条有依赖的路由)",
            not _bad_req, f"名字不存在(会被静默跳过): {_bad_req}")

        chk(f"声明与注册完全一致 ({len(declared)} 条)", sorted(regged) == declared,
            f"缺={sorted(set(declared)-set(regged))} 多={sorted(set(regged)-set(declared))}")
        chk("5 个内部命令都在", all(x in regged for x in
            ("cmd.stats", "cmd.cost", "cmd.stats_old", "cmd.tool", "cmd.hive")), str(regged))
        # ★ 关系型 (加路由不用改验证器): 断"分带顺序"而不是"精确名单"
        bp = [n for n, _ in d["by_priority"]]
        pr = dict(d["by_priority"])
        chk("★ 排序生效 (优先级非递增)",
            all(pr[bp[i]] >= pr[bp[i + 1]] for i in range(len(bp) - 1)), str(d["by_priority"]))
        def _first(pref):
            hits = [n for n in bp if n.startswith(pref)]
            return bp.index(hits[0]) if hits else -1
        # ★ 分带锚点 (含"显式模式高于模糊追问"这条设计):
        #   cmd > @模型 > 技能 > planner > plugin.geocode(显式模式610) > analysis(600) > plugin.keyword(500)
        #   geocode 高于 analysis 是**有意的**: "上海坐标是多少" 含"多少", 会被分析预读的
        #   追问匹配误吞 → 显式"城市+坐标"模式必须优先。
        # 完整分带 (12 段) —— 顺序即设计, 改动必须显式且被这里拦住:
        #   命令 > @模型 > 技能 > planner > geocode显式 > 分析 > 插件关键词
        #   > 开站 > 搜索 > Brain > SmartRoute > IR chain
        #   ★ Brain 高于 SmartRoute/IR chain 是**原顺序**(Brain 在文件里最前);
        #     搜索高于 Brain 是 2026-09-19 的**有意修复**(Brain 的硬编码/罐头答案曾劫持搜索)。
        _bands = [("cmd.", "命令"), ("model.explicit", "@模型"), ("skill.trigger_first", "技能"),
                  ("planner.multi_step", "planner"), ("plugin.geocode", "geocode显式"),
                  ("analysis.", "分析"), ("plugin.keyword", "插件关键词"),
                  ("open.site", "开站"), ("search.web", "搜索"),
                  ("brain.route", "Brain"), ("route.smart", "SmartRoute"), ("ir.chain", "IRchain"),
                  ("l4.knowledge", "L4"), ("skill.dag_catchall", "技能兜底"),
                  ("autoguide.route", "AutoGuide"), ("fixguard.context", "FixGuard"),
                  ("l1.precheck", "L1"), ("l2.reasoner", "L2"), ("l3.workflow", "L3"),
                  ("email.regex_send", "邮件"), ("url.open", "URL"), ("fts.search", "FTS"),
                  ("model.fallback", "模型兜底")]
        _pos = [_first(p) for p, _ in _bands]
        chk(f"★ 分带顺序正确 ({' > '.join(n for _, n in _bands)})",
            all(x >= 0 for x in _pos) and _pos == sorted(_pos),
            f"{[(n, x) for (_, n), x in zip(_bands, _pos)]}")
        chk("audit 无问题", not d["audit"]["issues"], str(d["audit"]["issues"]))
        # ★ audit 必须有分辨力: 未声明 probe_gated 的要报, 声明了的不能报
        _syn = d.get("audit_synth") or []
        chk("★ audit 抓得到'漏标 probe_gated'",
            any("z.undeclared" in s for s in _syn), str(_syn))
        chk("★ audit 不误报'已声明 probe_gated'的",
            not any("z.gated" in s for s in _syn), str(_syn))
        bad = [c for c in d["real_cases"]
               if c["intent"] != c["want_intent"] or c["route"] != c["want_route"]]
        for c in d["real_cases"]:
            ok = c["intent"] == c["want_intent"] and c["route"] == c["want_route"]
            print(f"        {'✓' if ok else '✗'} {c['msg']:<18} intent={c['intent']:<8} route={c['route']}")
        chk("★ 命令句全部经路由表裁决 (route 字段吻合)", not bad, str(bad))

        print("\nF. ★ 辨别力 (故意破坏 → 必须被捕获)")
        rp = ROOT / "core/routes.py"
        src = rp.read_text(encoding="utf-8")
        broken = src.replace("if probe and r.writes_state and not r.probe_safe:", "if False:")
        chk("变异点可定位 (不变量守卫)", broken != src)
        if broken != src:
            bak = rp.read_bytes()
            try:
                rp.write_text(broken, encoding="utf-8")
                d2, _ = run_probe()
                caught = d2 is not None and d2["inv"]["probe_winner"] != "safe"
                print(f"        变异后 probe_winner = {d2['inv']['probe_winner'] if d2 else '?'} (期望非 safe)")
                chk("★ 去掉守卫 → 不变量断言会 FAIL (有辨别力)", caught)
            finally:
                rp.write_bytes(bak)
            chk("还原后字节级一致", rp.read_bytes() == bak)
    finally:
        if snap is not None:
            mj.write_bytes(snap)
            print(f"\n[cleanup] mode.json 已还原: {mj.read_bytes() == snap}")

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
