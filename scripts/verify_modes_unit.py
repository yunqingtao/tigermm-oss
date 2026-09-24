"""verify_modes_unit.py — 三模式单元验证器 (只读生产文件, 状态写临时目录)"""
import re
import json, os, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(r"str(Path(__file__).resolve().parents[1])")
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))

# ── ★ 2026-09-21 探针隔离 (第三次污染真库后的加固) ──────────────────────────
# 本脚本会**真跑 pipeline**。不隔离的话, 测试对话会写进用户的真实 chat_sessions.db
# (实测踩过: 2026-09-21 01:52 跑本脚本 → 真库 +16 行, 全是 ask/plan_proposal/plan_exec)。
# 隔离变量集中在 scripts/verification/_probe_env.py; 拿不到就**醒目告警**, 不静默继续。
try:
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    from _probe_env import activate as _activate_probe_isolation
    _activate_probe_isolation()
    print("[隔离] 已启用探针沙箱 (不会污染真实数据)")
except Exception as _e:
    print(f"[★ 告警] 探针隔离未生效: {type(_e).__name__}: {_e}")
    print("         继续跑可能把测试对话写进真实库! 建议先修好 _probe_env 再跑。")
# ───────────────────────────────────────────────────────────────────────────


PASS, FAIL = [], []
def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name + (f"  [{extra}]" if extra and not cond else ""))
    print(("  PASS " if cond else "  FAIL ") + name + (f"   {extra}" if extra and not cond else ""))

print("=" * 62)
print("1. core/modes.py 单元行为")
print("=" * 62)
from core.modes import ModeManager, MODES, DEFAULT_MODE

tmp = Path(tempfile.mkdtemp(prefix="modes_"))
m = ModeManager(tmp)

chk("默认模式 = craft", m.get() == "craft", m.get())
chk("三模式齐全", set(MODES) == {"ask", "plan", "craft"})
chk("info() 结构", m.info()["cn"] == "实干" and m.info()["en"] == "Craft")
chk("set(ask) 成功", m.set("ask") and m.get() == "ask")
chk("set(plan) 成功", m.set("plan") and m.get() == "plan")
chk("非法模式被拒", m.set("godmode") is False and m.get() == "plan", m.get())
chk("set_error 文案存在", "ask / plan / craft" in m.set_error())

# 持久化
m2 = ModeManager(tmp)
chk("模式持久化 (重新实例化)", m2.get() == "plan", m2.get())

# 词判定
chk("确认词: 执行", ModeManager.is_confirm("执行"))
chk("确认词: 开始 (带句号)", ModeManager.is_confirm("开始。"))
chk("确认词: y", ModeManager.is_confirm("y"))
chk("确认词: 空白不是确认", not ModeManager.is_confirm("   "))
chk("取消词: 取消", ModeManager.is_cancel("取消"))
chk("取消词: n", ModeManager.is_cancel("N"))
chk("取消词: 执行 不是取消", not ModeManager.is_cancel("执行"))

# pending
steps = [{"step": 1, "tool": "file_ops", "action": "write", "params": {"path": "a.txt"}}]
m2.set_pending(steps, "写个文件", "方案文本")
p = m2.get_pending()
chk("pending 读取", p and len(p["steps"]) == 1 and p["message"] == "写个文件")
m3 = ModeManager(tmp)
chk("pending 持久化", m3.get_pending() and m3.get_pending()["text"] == "方案文本")

m3.clear_pending()
chk("pending 清除", m3.get_pending() is None)

# TTL 过期
m3.set_pending(steps, "写个文件", "x")
m3.state["pending_at"] = time.time() - 4000
m3._save()
chk("pending TTL 过期 (30min)", m3.get_pending() is None)

# 换模式清空 pending
m3.set_pending(steps, "写个文件", "x")
m3.set("craft")
chk("换模式作废 pending", m3.get_pending() is None)

# 空 pending 不误判
m4 = ModeManager(tmp)
m4.set_pending([], "", "")
chk("空 pending 返回 None", m4.get_pending() is None)

shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 62)
print("2. pipeline.py / web_chat.py / ui 接线检查 (源码断言)")
print("=" * 62)
src = (ROOT / "core/pipeline.py").read_text(encoding="utf-8")

chk("import ModeManager", "from core.modes import ModeManager" in src)
chk("__init__ 挂载 self.modes", "self.modes = ModeManager(DATA_DIR)" in src)
chk("process 支持 mode_override + probe",
    "mode_override=None,\n                      probe=False)" in src
    or "mode_override=None, probe=False" in src)
chk("三模式闸门存在", "★ 三模式闸门" in src)
chk("ask 分支调用 _process_ask (带 probe)",
    "return await self._process_ask(message, ext_model, t0, probe=probe)" in src)
# ★ 2026-09-21 修 (断言自身太脆): 原来做**精确单行**匹配, 而该调用后来被改成多行
#   (加了 persist=not probe 参数以支持 probe 不落盘) → 断言失效, 代码其实是对的。
#   改为**多行容错**匹配 (只认关键实参序列)。
chk("plan 分支调用 _plan_propose",
    bool(re.search(r"return await self\._plan_propose\(\s*message,\s*ext_model,\s*t0,\s*prev=_pend", src)))
chk("plan 分支调用 _run_plan (带 probe)",
    "return await self._run_plan(_pend, ext_model, t0, probe=probe)" in src)
for meth in ["_process_ask", "_plan_propose", "_run_plan", "_plan_text_via_model",
             "_format_plan_text", "_ask_messages", "_mode_pick_model", "_fmt_plan_step"]:
    # ★ 2026-09-21 修 (断言自身有 bug): 原来用 src.count("def _run_plan") 做子串计数,
    #   而 "def _run_planner" **以之开头** → 误报 x2 (代码里定义其实只有 1 个)。
    #   改用词边界正则, 只认真正带括号的定义。
    n = len(re.findall(r"\bdef\s+" + re.escape(meth) + r"\s*\(", src))
    chk(f"{meth} 定义唯一", n == 1, f"x{n}")
chk("ask 不挂工具 (无 tools= 传参)", "self._ask_messages(message, self._ASK_SYS)" in src
    and "tools=" not in src.split("def _process_ask")[1].split("def _fmt_plan_step")[0])
chk("CLI /ask /plan /craft", "'/ask', '/plan', '/craft'" in src)
chk("CLI /mode 查看", "('/mode', '/modes')" in src)
chk("CLI 提示符带模式", "_mode_tag = f\" {GOLD}[{_mi['cn']}]{RST}\"" in src)
cli = (ROOT / "core/cli.py").read_text(encoding="utf-8")
chk("CLI(活) /ask /plan /craft", "'/ask', '/plan', '/craft'" in cli)
chk("CLI(活) /mode 查看", "('/mode', '/modes')" in cli)
chk("CLI(活) 提示符带模式", "_mode_tag = f\" {GOLD}[{_mi['cn']}]{RST}\"" in cli)
chk("CLI(活) 方案先展示再确认", "方案待确认 → 先展示方案, 再交互确认" in cli
    and "_ptxt = result.get('response', '')" in cli)
chk("CLI(活) 方案确认提示语", "执行该方案 · 输入新需求 = 重出方案" in cli)
chk("CLI(活) 取消清 pending", "pipeline_obj.modes.clear_pending()" in cli)
chk("CLI(活) 帮助含 /mode", "/mode{RST}       运行模式" in cli)
chk("CLI(死副本) 同步改动", "方案待确认 → 先展示方案, 再交互确认" in src)
chk("帮助含 /mode", "/mode{RST}     查看运行模式" in src)

wc = (ROOT / "web_chat.py").read_text(encoding="utf-8")
chk("web /modes 路由", "@app.route('/modes')" in wc)
chk("web /mode POST 路由", "@app.route('/mode', methods=['POST'])" in wc)
chk("web /chat 接收 mode", "data.get('mode')" in wc)
chk("web done 事件带模式载荷 (流式+非流式)",
    wc.count("_done.update(_mode_payload(result))") == 2
    and "def _mode_payload(result)" in wc,
    f"update x{wc.count('_done.update(_mode_payload(result))')}")
chk("web 优先服务 ui/app.html", 'ui" / "app.html' in wc)

ws = (ROOT / "web_server.py").read_text(encoding="utf-8")
chk("web_server 线上界面优先 app.html", 'UI_FILE = Path(__file__).parent / "ui" / "app.html"' in ws)
chk("web_server /modes 路由", '@app.get("/modes")' in ws)
chk("web_server /mode POST 路由", '@app.post("/mode")' in ws)
chk("web_server /chat 接收 mode", 'data.get("mode")' in ws)
chk("web_server done 事件带模式载荷", ws.count("_done.update(_mode_payload(result))") == 1
    and "_pe.update(_mode_payload(result))" in ws)
chk("web_server 回落 ui/index.html", "UI_FILE_LEGACY" in ws)
chk("MCP 后台线程竞态已修", 'getattr(self, "mcp", None) or getattr' in src)
chk("probe 不写用户记忆", "if not probe:\n            self.memory.add_turn" in src)
chk("web probe 透传 pipeline", "probe=probe" in ws and 'data.get("probe")' in ws)
chk("web 非 probe 才写 assistant 记忆",
    "if not probe:\n                pipeline.memory.add_turn" in ws)
chk("e2e 请求带 probe", '"probe": True' in (ROOT / "scripts/verify_modes_e2e.py").read_text(encoding="utf-8"))
chk("总入口快照 chat_sessions.db", "CHAT_DB" in (ROOT / "scripts/verify_modes.py").read_text(encoding="utf-8"))

ui = ROOT / "ui/app.html"
chk("ui/app.html 存在", ui.exists())
if ui.exists():
    h = ui.read_text(encoding="utf-8")
    chk("界面含三模式切换", 'data-mode="ask"' in h and 'data-mode="plan"' in h and 'data-mode="craft"' in h)
    chk("界面含方案确认卡", "方案待确认" in h and "planPanel" in h)
    chk("界面含执行步骤面板", "tracePanel" in h and "执行步骤" in h)
    chk("界面请求 /modes", "fetch('/modes')" in h)
    chk("界面发送 mode 字段", "mode:mode" in h)
    chk("黑金配色 (无白色)", "#FFAC02" in h and "#0a0502" in h and "color:#fff" not in h.lower())
    chk("无 onclick 内联注入行号串", h.count("onclick=\"resolveInbox") == 0)

print()
print("=" * 62)
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("  -", f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
