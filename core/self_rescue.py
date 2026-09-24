"""SelfRescue — 能力缺口的**本地自救** (「能自己造就不出门」)。

═══════════════════════════════════════════════════════════════════════
为什么要有它 (承接 gap_ledger 第 1 步)
═══════════════════════════════════════════════════════════════════════
第 1 步的账本解决了"看不见缺什么"。但**记账不是目的, 补上才是**。
而补上的第一条路不该是联网找别人的东西 —— 本地往往就够了:

    很多"缺的能力"根本不是缺能力, 而是**本地已有的工具没被组合起来**
    → 技能工厂 (/skill build) 一句话就能编译成 DAG 技能, 零联网零风险。

所以顺序是: 缺口 → **本地自救 (本模块)** → 联网只读检索 → 人工确认装入。
出门前必须先在本地试过, 否则就是"家里有锤子还出门买"。

═══════════════════════════════════════════════════════════════════════
核心设计: 按**卡在哪一环**分流自救策略 (不同环的修法完全不同)
═══════════════════════════════════════════════════════════════════════
    no_tool        本地没能力覆盖      → ★ 试技能工厂 (组已有工具) —— 多数能造就解决
    skill_failed   技能跑了但失败      → ★ 试技能工厂重编 (多半是 DAG 缺步骤/参数)
    missing_dep    缺依赖 (模块/命令)  → 给出**确切装法** (不自动装!) —— 最便宜
    no_entry       工具没可调用入口    → 需改代码补入口 (指给人, 不像话的自动做)
    missing_arg    调用时缺必填参数    → 需改技能/路由的提参逻辑
    unknown_action 工具不认这个动作    → 需给工具补动作 (改代码)
    model_refuse   模型不会            → 知识问题 (交给 learn_loop) 或真需联网

★ 两条硬规矩 (与 TMM 既有风格一致):
  ① **不自动装、不自动生效** —— 依赖只给命令, 技能只出候选, 都要人工确认。
     (四道门: 编译 → 质检 → 自测 → **人工确认**; 本模块只走到"候选就绪")
  ② **救不了的如实说** —— 结果写回账本:
        building       本地自救已启动 (技能候选就绪, 待确认)
        built          已解决 (人工确认生效 / 依赖已装)
        blocked_local  ★ 本地救不了 —— 这才是**该出门找**的 (第 3 步的输入)
     不能把"没救成"粉饰成"已处理", 否则出门的判据就假了。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("core.self_rescue")

# ═══════════════════════════════════════════════════════════════════
# 策略表: 分类 → 本地自救可行性 + 具体做法 (纯数据, 可审计)
# ═══════════════════════════════════════════════════════════════════
#   local: True  = 本地有可能自己解决
#   how:   给「谁能解决/怎么解决」的一句话 (要显示给人看)
#   tries_factory: 是否值得交给技能工厂试一次 (真的会花模型调用)
STRATEGIES: dict[str, dict] = {
    "no_tool": {
        "local": True, "tries_factory": True,
        "how": "用技能工厂把本地已有工具组合成 DAG 技能 (多数情况这就够了)",
    },
    "skill_failed": {
        "local": True, "tries_factory": True,
        "how": "技能跑失败多半是 DAG 缺步骤/参数 → 重新编译一版",
    },
    "missing_dep": {
        "local": True, "tries_factory": False,
        "how": "缺的是依赖, 装上行 —— 最便宜的一条路 (本模块只给命令, 不自动装)",
    },
    "no_entry": {
        "local": False, "tries_factory": False,
        "how": "工具存在但没 run()/execute() 入口 —— 需要改代码补入口 (交给人)",
    },
    "missing_arg": {
        "local": False, "tries_factory": False,
        "how": "技能/路由没把参数提全 —— 需要改进提参逻辑 (交给人)",
    },
    "unknown_action": {
        "local": False, "tries_factory": False,
        "how": "工具不支持这个动作 —— 需要给工具补动作 (交给人)",
    },
    "model_refuse": {
        "local": False, "tries_factory": False,
        "how": "模型答不上来 —— 知识问题交给学习闭环; 若是真缺能力则需联网找",
    },
}

# ── 依赖名提取 + 装法 (只给命令, 绝不自动执行) ──
_MODULE = re.compile(r"No module named ['\"]([\w.\-]+)['\"]")
_BINARY = re.compile(r"(?:找不到|无法找到|缺少|缺失)[^\n]{0,8}?([\w\-\.]+)\s*(?:可执行文件|命令|程序)|"
                     r"['\"]?([\w\-\.]+)['\"]?\s*(?:is not recognized|command not found)", re.I)

# 已知依赖 → 确切的装法 (真实验证过的; 未知的给通用命令)
KNOWN_DEPS = {
    "pytesseract": {"kind": "python", "install": "pip install pytesseract",
                    "note": "另需 tesseract 本体: winget install UB-Mannheim.TesseractOCR"},
    "docx":        {"kind": "python", "install": "pip install python-docx"},
    "pptx":        {"kind": "python", "install": "pip install python-pptx"},
    "openpyxl":    {"kind": "python", "install": "pip install openpyxl"},
    "PIL":         {"kind": "python", "install": "pip install pillow"},
    "cv2":         {"kind": "python", "install": "pip install opencv-python"},
    "tesseract":   {"kind": "binary", "install": "winget install UB-Mannheim.TesseractOCR",
                    "note": "装完把安装目录加进 PATH (或设 TESSERACT_CMD)"},
    "ffmpeg":      {"kind": "binary", "install": "winget install Gyan.FFmpeg",
                    "note": "装完重开终端让 PATH 生效"},
    "curl":        {"kind": "binary", "install": "Windows 10+ 自带; 或 winget install curl.curl"},
}


def _extract_dep(detail: str) -> tuple[str, str]:
    """从失败文本里抽出依赖名。返回 (名字, 种类 python|binary|unknown)。"""
    t = str(detail or "")
    m = _MODULE.search(t)
    if m:
        return m.group(1).split(".")[0], "python"
    m = _BINARY.search(t)
    if m:
        return (m.group(1) or m.group(2) or "").strip(), "binary"
    return "", "unknown"


def dep_hint(detail: str) -> str:
    """缺依赖 → 一条可照抄的装法 (纯函数, 只生成文本)。"""
    name, kind = _extract_dep(detail)
    if not name:
        return "没从报错里认出具体依赖名 —— 请把完整报错看一眼, 手动确认缺什么。"
    k = KNOWN_DEPS.get(name) or KNOWN_DEPS.get(name.lower())
    if k:
        extra = f"  ({k['note']})" if k.get("note") else ""
        return f"缺 {name} ({k['kind']}) → 装法: {k['install']}{extra}"
    if kind == "python":
        return f"缺 Python 模块 {name} → 装法: pip install {name}"
    if kind == "binary":
        return f"缺命令/程序 {name} → 自行安装并确保在 PATH 里 (本机包管理器: winget / choco)"
    return f"疑似缺 {name} —— 请确认是模块还是外部程序。"


# ═══════════════════════════════════════════════════════════════════
# 方案 (纯函数 → 可重复调用, 便于测试与展示)
# ═══════════════════════════════════════════════════════════════════

def plan(gap: dict) -> dict:
    """给一条缺口出**本地自救方案** (纯函数, 不写任何东西)。

    返回 {category, local, tries_factory, how, needs_human, hint}
      local=False + needs_human=True → 本地救不了, 是「该出门找」的候选 (第 3 步输入)
    """
    cat = str((gap or {}).get("category") or "")
    st = STRATEGIES.get(cat) or {
        "local": False, "tries_factory": False,
        "how": "未知分类 —— 先当「需人工判断」处理 (不假装能自救)",
    }
    out = {
        "category": cat,
        "local": bool(st["local"]),
        "tries_factory": bool(st["tries_factory"]),
        "how": st["how"],
        "needs_human": not st["local"],
        "hint": "",
    }
    if cat == "missing_dep":
        out["hint"] = dep_hint((gap or {}).get("detail") or "")
    elif cat == "missing_arg":
        out["hint"] = "看缺的是哪个参数 → 在技能 SKILL.md 的 params/DAG 里补上提取"
    elif cat == "unknown_action":
        out["hint"] = "给该工具加这个 action 的分派 (tools/ 下对应模块)"
    elif cat == "no_entry":
        out["hint"] = "给该工具补 run()/execute() 入口 (照 tiger_office 的约定)"
    elif cat == "model_refuse":
        out["hint"] = ("知识型 → 交给学习闭环 (/learn 教一遍它就会了); "
                       "真缺能力 → 属「该出门找」")
    return out


def render_plan(gap: dict) -> str:
    """把方案渲染成给人看的几行。"""
    p = plan(gap)
    lines = [f"#{gap.get('id')} [{gap.get('category')}] 本地自救方案",
             f"  原话: {str(gap.get('sample') or '')[:120]}",
             f"  能做: {p['how']}"]
    if p["hint"]:
        lines.append(f"  下一步: {p['hint']}")
    if p["local"]:
        lines.append("  → 属**本地可救**; 先别出门。")
    else:
        lines.append("  → 本地救不了 (★ 这是「该出门找」的候选, 归到第 3 步)")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 真试一次 (会调技能工厂 → 花模型调用; 必须人工触发)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RescueResult:
    gap_id: int = 0
    category: str = ""
    attempted: bool = False          # 有没有真试 (missing_dep 之类不试, 只给命令)
    ok: bool = False                 # 本地救成功了没
    kind: str = ""                   # factory_candidate / dep_hint / manual / cannot
    detail: str = ""                 # 给人看的结果
    next_step: str = ""              # 明确的下一步 (含要敲什么命令)
    artifacts: list = field(default_factory=list)   # 产物 (候选技能路径等)
    factory_raw: dict = field(default_factory=dict)  # 工厂原始结果 (供失败细分)


def _factory_needs_msg(gap: dict) -> str:
    """交给技能工厂时用的话 —— 用**用户原话**(最能表达真实需求)。"""
    return str(gap.get("sample") or "").strip()


async def attempt(gap: dict, factory=None) -> RescueResult:
    """对一条缺口**真试一次**本地自救。

    两条硬规矩: 不自动装依赖 / 不自动让技能生效 (只出候选)。
    factory 为 None 时不试工厂 (只出方案) —— 这样调用方可以"先看方案再决定要不要花调用"。
    """
    p = plan(gap)
    r = RescueResult(gap_id=int(gap.get("id") or 0), category=p["category"])

    if p["category"] == "missing_dep":
        r.kind, r.ok, r.attempted = "dep_hint", False, False
        r.detail = p["hint"]
        r.next_step = "照上面的命令装好 (我不自动装 —— 装依赖会改你的环境, 该你点头)"
        return r

    if not p["local"]:
        r.kind = "cannot"
        r.detail = p["how"]
        # ★ 接上第 3 步: 本地救不了 → 出门找 (只读检索 + 安全评估), 或人工改代码
        r.next_step = (f"本地救不了 → 已记入「该出门找」; "
                       f"可 /depot search {str(gap.get('sample') or '')[:40]} 去外面找候选, "
                       f"或人工改代码: " + (p["hint"] or ""))
        return r

    # 本地能救 → 交给技能工厂试 (编译 → 质检 → 自测 → 实跑门)
    if factory is None:
        r.kind = "factory_skipped"
        r.detail = "本地可救 (技能工厂), 但本次没带工厂 → 只出方案, 不花调用"
        r.next_step = f"要试就说一声: /gap rescue {gap.get('id')}"
        return r
    msg = _factory_needs_msg(gap)
    if not msg:
        r.kind, r.detail = "no_input", "这条缺口没记下原话, 没法交给工厂"
        r.next_step = "手工补一句需求再试"
        return r
    r.attempted = True
    try:
        res = await factory.build(msg)
    except Exception as e:
        r.kind = "factory_error"
        r.detail = f"技能工厂调用出错: {type(e).__name__}: {e}"
        r.next_step = "看错误; 若是工厂自身问题该修工厂"
        return r
    if res.get("ok"):
        r.kind, r.ok = "factory_candidate", True
        r.detail = (f"技能工厂编出了候选: {res.get('name')} "
                    f"({(res.get('self_test') or {}).get('steps')} 步)")
        r.artifacts.append(str(res.get("path") or ""))
        r.next_step = f"人工过一眼再确认生效: /skill approve {res.get('name')}"
        return r
    r.kind = "factory_failed"
    r.factory_raw = res
    r.detail = f"工厂在「{res.get('stage')}」没过: " + " / ".join(
        str(e) for e in (res.get("errors") or [])[:3])
    # ★ 工厂自己没过 ≠ 该出门找 —— 细分 (见 classify_factory_failure 的实测来由)
    cls = classify_factory_failure(res)
    r.next_step = cls["hint"]
    r.detail += f"\n  (细分: {cls['reason']})"
    return r


# ═══════════════════════════════════════════════════════════════════
# 工厂失败的**细分** (纯函数) —— ★ 一次真跑实测踩出来的必需品
# ═══════════════════════════════════════════════════════════════════
# 实测: 一条 "把 CSV 转成 Excel" 的缺口交给工厂, 生成成功、质检过、但**实跑门**失败:
#     step 'read_csv': File not found: tmp/_factory_smoke/<name>/out.txt
# 原因: smoke_test 给 path 类参数喂的是 `sbx/out.txt` —— 一个**不存在的文件**,
#       而"读输入"类技能必然读不到 → 必然失败。
# 这是**工厂实跑门的输入局限**, 不是"本地没这能力"。
# ★ 若不细分就一律记 blocked_local, 会把本地**能造**的活误判成"该出门找" ——
#   那样出门找的依据就假了, 而且会把人推向外来代码 (风险更大的那条路)。
# 判定方向: **漏判比误判安全** —— 不确定就说"需人看"(open), 别轻易说"救不了"。
_INPUT_LIMIT = re.compile(
    r"File not found|文件不存在|no such file|找不到.{0,10}文件|未找到.{0,10}文件|"
    r"resolved to|输入文件|缺少输入", re.I)
_NO_LOCAL = re.compile(
    r"未知工具|没有可调用入口|无\s*run/execute|没有入口|不支持|unknown tool|"
    r"未安装|No module named", re.I)


def classify_factory_failure(result: dict) -> dict:
    """工厂没过 → 到底该记什么状态。返回 {status, reason, hint}。

    三种结论:
      input_limited  实跑门受**输入**限制 (沙箱没给输入文件) → **open**
                     (本地可能仍可救; 人要过一眼候选)
      no_local       真缺本地积木 (未知工具/没入口/缺依赖)   → **blocked_local**
      unknown        说不清                                          → **open** (不许瞎判"救不了")
    """
    txt = " ".join(str(x) for x in
                   ((result.get("errors") or []) + [result.get("error") or ""]))
    stage = str(result.get("stage") or "")
    if _INPUT_LIMIT.search(txt):
        return {"status": "open", "reason": "input_limited",
                "hint": ("实跑门没成是**沙箱没给输入文件**(读输入类技能必然如此), "
                         "不代表本地造不出 → 需人工看一眼候选; 或换句话再编一版")}
    if _NO_LOCAL.search(txt):
        return {"status": "blocked_local", "reason": "no_local",
                "hint": "本地确实缺积木 (未知工具/没入口/缺依赖) → 该补本地或该出门找"}
    return {"status": "open", "reason": "unknown",
            "hint": f"工厂在「{stage}」没过但说不清原因 → 需人看 (不轻易判'救不了')"}


# ── 结果写回账本的状态映射 (单一出口, 便于测试) ──
def status_for(result: RescueResult) -> tuple[str, str]:
    """自救结果 → (账本状态, note)。**救不了必须如实标记**, 否则"该出门找"的判据就假了。"""
    if result.ok:
        return "building", f"本地自救: {result.detail}"
    if result.kind == "cannot":
        return "blocked_local", f"本地救不了: {result.detail}"
    if result.kind == "factory_failed":
        cls = classify_factory_failure(result.factory_raw or {})
        return cls["status"], f"工厂没过({cls['reason']}): {result.detail[:160]}"
    if result.kind == "dep_hint":
        return "open", f"待装依赖: {result.detail}"
    return "open", f"未完成: {result.detail or result.kind}"


def local_unsalvageable(ledger, limit: int = 20) -> list[dict]:
    """★ 该出门找的缺口 —— 本地救不了的 (第 3 步联网检索的输入)。"""
    return ledger.list_gaps(status="blocked_local", limit=limit)
