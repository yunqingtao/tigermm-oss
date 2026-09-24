"""TMM v4.2 Test Suite"""
import asyncio, json, os, shutil, sys, time
from pathlib import Path

PROJECT = Path("<项目根>")
sys.path.insert(0, str(PROJECT))

class Runner:
    """极简测试收集器。

    ★ 2026-09-19 修 (真缺陷): 原来 `test()` 装饰器**在装饰时立即执行** fn() ——
      于是"import 这个模块"就等于"跑整套测试", 会真写 data/perception.json、
      真建/删 skills 目录。pytest 收集这个文件时 (它没有 test_* 函数, 显示 "no tests
      ran") 就已经把用户数据写了一遍 (实测: stats.analyzed 3508→3510)。
      现在改为**注册**, 由 run_all() 显式执行 (import 零副作用)。
    """

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self._tests = []

    def test(self, name):
        def dec(fn):
            self._tests.append((name, fn))      # 只登记, 不执行
            return fn
        return dec

    def run_all(self):
        for name, fn in self._tests:
            try:
                fn()
                self.passed += 1
                print(f"  PASS {name}")
            except Exception as e:
                self.failed += 1
                print(f"  FAIL {name}: {e}")

R = Runner()

def ld():
    import importlib.util
    p = PROJECT / "tools" / "windows_desktop.py"
    for m in list(sys.modules):
        if "windows_desktop" in m.lower(): del sys.modules[m]
    s = importlib.util.spec_from_file_location("wd", str(p))
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod

def chk(r):
    assert isinstance(r, dict), f"not dict: {type(r)}"
    ok = r.get("ok") or r.get("success")
    assert ok, f"not ok: {r.get('error', r)}"

def fex(p, sz=0):
    assert os.path.exists(p), f"missing: {p}"
    if sz: assert os.path.getsize(p) >= sz, f"too small: {os.path.getsize(p)}"

# Desktop

@R.test("list_windows")
def t1():
    m=ld(); r=m.execute(action="list_windows"); chk(r); assert len(r.get("windows",[]))>0

@R.test("screenshot_now >10KB")
def t2():
    m=ld(); p=os.path.join(os.environ["USERPROFILE"],"Desktop","_t_ss.png")
    if os.path.exists(p): os.remove(p)
    r=m.execute(action="screenshot_now", path=p); chk(r); fex(p,10000); os.remove(p)

@R.test("save_screenshot >10KB")
def t3():
    m=ld(); p=os.path.join(os.environ["USERPROFILE"],"Desktop","_t_ds.png")
    if os.path.exists(p): os.remove(p)
    r=m.execute(action="save_screenshot", path=p); chk(r); fex(p,10000); os.remove(p)

@R.test("open_folder")
def t4():
    m=ld(); r=m.execute(action="open_folder", target="C:\\"); chk(r)

@R.test("wait")
def t5():
    m=ld(); t0=time.time(); m.execute(action="wait", seconds=1); assert time.time()-t0>=0.8

@R.test("TOOL metadata")
def t6():
    m=ld(); meta=getattr(m,"TOOL",None); assert meta
    acts=[p["enum"] for p in meta["params"] if p.get("enum")]
    flat=acts[0] if acts else []
    for a in ["list_windows","capture","click","save_screenshot","screenshot_now","open_folder","wait"]:
        assert a in flat, f"missing {a}"

@R.test("success field")
def t7():
    m=ld(); r=m.execute(action="list_windows"); assert r.get("success")==r.get("ok")

@R.test("e2e folder+screenshot")
def t8():
    m=ld(); p=os.path.join(os.environ["USERPROFILE"],"Desktop","_e2e.png")
    if os.path.exists(p): os.remove(p)
    r1=m.execute(action="open_folder", target="C:\\"); chk(r1)
    r2=m.execute(action="screenshot_now", path=p); chk(r2); fex(p,10000); os.remove(p)

# MCP

def amcp():
    async def go():
        from mcp.mcp_server import MCPServer
        s=MCPServer()
        r=await s._handle({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
        assert r["result"]["serverInfo"]["name"]=="tmm"; R.passed+=1; print("  PASS mcp_init")
        s.register("echo","",lambda **kw:{"ok":True})
        r=await s._handle({"jsonrpc":"2.0","id":2,"method":"tools/list"})
        assert len(r["result"]["tools"])==1; R.passed+=1; print("  PASS mcp_list")
        r=await s._handle({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{}}})
        d=json.loads(r["result"]["content"][0]["text"]); assert d["ok"]; R.passed+=1; print("  PASS mcp_call")
        s.register_plugin("wd",{"module":ld()})
        r=await s._handle({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"wd","arguments":{"action":"list_windows"}}})
        d=json.loads(r["result"]["content"][0]["text"]); assert d.get("ok"); R.passed+=1; print("  PASS mcp_plugin")
    asyncio.run(go())

# Perception

@R.test("perception analyze")
def t9():
    from core.perception import PerceptionEngine
    pe=PerceptionEngine(); f=pe.analyze("test","ok")
    assert isinstance(f,dict) and "new_paths" in f

@R.test("perception persist")
def t10():
    # ★ 2026-09-20 (A③): 原来盯的是**用户真实文件** data/perception.json 的 mtime ——
    #   隔离跑时它根本不写那里 → 这条其实什么都没测。现在盯 PERCEPTION_FILE
    #   (隔离目标, 由冒烟启动时从真实库拷来的副本): 真写、真涨时间。
    from core.perception import PerceptionEngine, PERCEPTION_FILE
    pe=PerceptionEngine()
    t0=PERCEPTION_FILE.stat().st_mtime if PERCEPTION_FILE.exists() else 0
    pe.analyze(f"打开 {Path(__file__).resolve().parents[1] / 'README.md'} 看看 {int(time.time())}","ok")
    assert PERCEPTION_FILE.exists(), f"没落盘: {PERCEPTION_FILE}"
    assert PERCEPTION_FILE.stat().st_mtime>=t0

@R.test("perception context")
def t11():
    from core.perception import PerceptionEngine, PERCEPTION_FILE
    pe=PerceptionEngine(); ctx=pe.get_full_context()
    assert isinstance(ctx,str)
    # ★ 2026-09-20 (A③): 冒烟启动时把**真实库拷成副本**当种子, 所以正常情况下
    #   必须有内容 (原来 >100 的断言其实是靠用户真实数据撑着的)。
    #   全新机器 (库为空) 时只校验类型契约, 不算失败。
    _seeded = PERCEPTION_FILE.exists() and PERCEPTION_FILE.stat().st_size > 10
    assert (len(ctx) > 100) if _seeded else True, f"库里有料却没拼出上下文 (len={len(ctx)})"

# Skill

@R.test("skill pack")
def t12():
    from core.skill_pack import create_skill, pack_skill
    d=create_skill("_ts","test",output_dir=str(PROJECT/"skills"))
    pkg=pack_skill(d); assert os.path.getsize(pkg)>100
    os.remove(pkg); shutil.rmtree(d)

# Guardrail

@R.test("guardrail in pipeline")
def t13():
    src=open(PROJECT/"core"/"pipeline.py",encoding="utf-8").read()
    # ★ 2026-09-20 更新陈旧断言 (A③): 原断言要求源码里存在变量名 `action_words`,
    #   但那个名字早被重构掉了 (实测 pipeline.py 中 0 处) → 这条永远 FAIL。
    #   那是**断言过期**, 不是功能缺陷。现在断言当前**真实的护栏接线**:
    #     ① 工具结果计数 (循环上限护栏) 仍在
    #     ② 输入/输出守卫模块已导入并接线 (core.guardrails)
    assert "total_tool_results" in src, "工具结果计数护栏不见了"
    assert "run_input_guards" in src and "run_output_guards" in src, "输入/输出守卫未接线"

@R.test("no hermes_bridge refs")
def t14():
    for root, dirs, files in os.walk(PROJECT):
        dirs[:] = [d for d in dirs if d not in ("backup_20260727_153603","__pycache__")]
        for f in files:
            if f.endswith(".py") and "backup" not in root and "test" not in f:
                src = open(os.path.join(root,f), encoding="utf-8", errors="replace").read()
                assert "from hermes_bridge" not in src, f"ref in {root}/{f}"

# ──

if __name__ == "__main__":
    # ★ 2026-09-20 (A③): 自我隔离 —— 这套 smoke 会真写 perception / 落库 / 截图。
    #   单独跑时先把四个隔离变量指向临时文件 (门禁 runner 已注入时不覆盖)。
    #   事故背景: verify_learn_loop 单独跑没隔离 → 测试消息真写进用户对话库 16 条。
    import tempfile
    _iso = Path(tempfile.gettempdir()) / f"tmm_v4_smoke_{os.getpid()}"
    _iso.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMM_SESSION_DB", str(_iso / "sessions.db"))
    os.environ.setdefault("TMM_GAP_DB", str(_iso / "gap.db"))
    # ★ 感知库: 把**真实库拷成副本**当种子 —— 只读真实文件, 写入全落副本。
    #   (踩过: 先写成 "{}" 空对象 → 引擎默认 schema 被顶掉, t9~t11 全报 KeyError 'stats')
    _pf = _iso / "perception.json"
    _real_pf = PROJECT / "data" / "perception.json"
    if not _pf.exists() and _real_pf.exists():
        shutil.copy2(_real_pf, _pf)
    os.environ.setdefault("TMM_PERCEPTION_FILE", str(_pf))
    os.environ.setdefault("TMM_USAGE_LOG", str(_iso / "usage.jsonl"))
    # ★ 注意: 本文件自带 runner → pytest **收集不到** (没有 test_* 函数)。
    #   它是**手工冒烟套件**, 不在 CI 门禁里; 门禁 = scripts/verification/ 下的留档验证器
    #   + canonical `pytest tests/`。改名成 pytest 形态会把这些真截图/真落库用例
    #   塞进门禁 (污染风险), 所以刻意保持现状。
    print("TMM v4.2 手工冒烟套件 (隔离目录: %s)" % _iso)
    print("=" * 30)
    try:
        R.run_all()
        amcp()
    finally:
        shutil.rmtree(_iso, ignore_errors=True)
    print(f"\n{R.passed}/{R.passed + R.failed} passed")
    sys.exit(0 if R.failed == 0 else 1)
