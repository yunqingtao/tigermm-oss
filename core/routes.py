"""显式路由表 —— 把 process() 里"按行号决定优先级"的顺序 if, 变成可审计的声明式表。

═══════════════════════════════════════════════════════════════════
为什么要有它 (代价是实测出来的, 不是洁癖)
═══════════════════════════════════════════════════════════════════
`process()` 曾有 1002 行 / 26 个顶层裁决块 / 57 个 return, **优先级 = 行号**。

实测代价:
  · 技能层为不被 4 个更早的分支劫持, 位置被搬了**三次** (L1520 → L1332 → L1091)
  · 同一套命令处理被复制了两遍 (L1282~L1405 陈旧副本, 已删 -124 行)
  · "这句话被谁吃了" 只能读 1000 行才看得出

加能力的正确姿势应该是"表里加一行", 不是"赌行号"。

═══════════════════════════════════════════════════════════════════
两个机器可校验的不变量 (这是本模块最重要的部分)
═══════════════════════════════════════════════════════════════════
  probe_safe   该路由在 probe(验证/探针) 流量下零副作用
  writes_state 该路由会写**用户状态**(磁盘)

实测踩过的坑: 验证流量 `probe=True` 仍把 `data/mode.json` 写成了 plan + pending_text
→ 之后所有请求都走提案路径, 技能层永远轮不到。
现在 dispatch 会**强制执行**: probe 流量遇到 writes_state 且非 probe_safe 的路由 → 跳过并记账。
从根上防住这一类 bug, 而不是靠每个验证器自己记得快照还原。

═══════════════════════════════════════════════════════════════════
优先级分带 (数字越大越先裁决; 用整带留出插入空间)
═══════════════════════════════════════════════════════════════════
  1000  内部命令          /stats /cost /tool /hive … (斜杠命令最高)
   900  用户显式指定       @模型
   800  技能强触发词       生成/产出类技能 (比启发式更具体, 实测该赢)
   700  任务规划          TaskPlanner
   600  分析/预读         带路径的分析请求
   500  插件关键词        天气/坐标/翻译
   420  打开站点           open_browser (高于搜索: '打开百度' 含'百度')
   400  搜索               web_search
   300  IR chain          写文件/发文件/发邮件
   200  本地兜底          L1-L3 / AutoGuide
   100  模型兜底          交给模型
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# ★ 2026-09-20 补: L257 的 post-route hook 异常分支用了 logger.debug, 但本模块从未定义 logger
#   → 那个分支一执行就 NameError, 反而破坏了"观察失败绝不影响主流程"的承诺。
logger = logging.getLogger("core.routes")

# ── 优先级分带 ──
P_COMMAND = 1000
P_REGULATOR = 990   # 危险操作拦截 (原位置: 一切之前)
P_GATE = 950        # 三模式闸门 ask/plan (须高于 @模型900? 不 —— 闸门自身排除 /@! 前缀)
P_EXPLICIT_MODEL = 900
P_SKILL = 800
P_PLANNER = 700
P_ANALYSIS = 600
P_GEOCODE = 610     # 显式'城市+坐标'模式 —— **高于 600 分析预读**:
                    # 追问匹配(含'多少')会误吞'上海坐标是多少', 而显式模式更具体
# ── 200 带 (迁移顺序即原内联顺序, 从高到低) ──
P_GUARD = 590        # 输入守卫/预过滤 (原位置: analysis 之后, plugin 之前) —— 空/纯符号/危险
P_L4 = 290           # L4 knowledge (实体学习/查询/技能执行)
P_SKILL_CATCH = 285  # 技能 DAG 兜底层 (只认 tags 弱线索; 800 已试过强触发词)
P_LEARN_TEACH = 370  # 用户在**教**系统 (以后/默认/总是/永远/记住…) —— 高于 brain/IR chain:
                     #   教学句绝不能被执行成动作 (实测: "以后默认发给涛哥" 真发了封垃圾邮件)
P_LEARNED = 292        # 学到的东西 (用户教过) — 压过静态罐头, 但让位给真工具
P_AUTOGUIDE = 280    # identity/help/greeting 罐头
P_FIXGUARD = 275     # 模糊修复请求 → 给出上文选项
P_L1 = 270           # L1: 实体+动作 → 任务分解执行
P_L2 = 265           # L2: Intent Reasoner
P_L3 = 260           # L3: Workflow Engine
P_EMAIL = 255        # 邮件正则直发 (regex passthrough)
P_URLOPEN = 250      # URL → 打开浏览器 (regex passthrough)
P_FTS = 245          # 历史对话全文检索
P_LOOKUP = 150       # 缺信息自查 / 行动判定 (★ 2026-09-21): 只接"本要落给模型"的消息,
                     #   低于所有工具/技能路由 → 不抢既有能力; 高于 model.fallback(100)
P_SAVE_GEN = 110     # ★ 2026-09-23: 「把<指代>存到<目标>」而**没有可指的现成内容**时, 自己写一份再落盘。
                     #   位置: 高于 model.fallback(100)、低于 lookup.prefill(150) —— 只接"本来要落给模型"的句子。
P_MODEL = 100        # 模型兜底 (终止路由, 永远匹配)
P_UNDERSTAND = 145   # ★ 2026-09-23: 自然话「理解兜底」—— 所有判据都认不出的句子,
                     #   交给大模型读懂再动手, 而不是让关键词层闷头执行。
                     #   位置: 低于 lookup.prefill(150) 与一切工具/技能路由,
                     #   高于 model.fallback(100) —— 只接"本来要落给模型"的句子。
                     #   起因: 用户实测 "写一首诗放桌面" 被知识层词表撞上 → 写出

P_PLUGIN = 500
P_OPEN = 420        # 打开站点 (须高于 search: '打开百度' 含'百度', 否则被搜索抢走)
P_BRAIN = 360      # Brain 决策树 (须高于 SmartRoute/IR chain —— 原顺序 Brain 在前;
                   # 实测: Brain 产 file_send_list(歧义→列目录), 被 IR chain 抢会变成'已收到'误报)
P_SMART = 350      # 复杂消息(长/多步/代码) → 直通模型 (高于 IR chain, 保持旧顺序)
P_SEARCH = 400
P_IR_CHAIN = 300
P_LOCAL_FALLBACK = 200
P_MODEL = 100


@dataclass
class Route:
    """一条裁决规则。match 决定要不要处理, run 负责处理。

    match(message, ctx) -> bool
    run(message, ctx)   -> dict (或 awaitable)  必须返回 process() 那套结果 dict

    ctx 至少含: ext_model / probe / t0 / eff_mode / mode_override
    """
    name: str
    priority: int
    match: Callable[[str, dict], bool]
    run: Callable[[str, dict], Any]
    probe_safe: bool = True          # probe 流量下零副作用
    # ★ 有意让 probe 跳过 —— 用于"会改**系统能力**"的命令 (如 /skill 装技能):
    #   验证流量绝不该装技能/改配置。声明它就与"漏标 probe_safe"区分开, 免得审计误报。
    probe_gated: bool = False
    writes_state: bool = False       # 会写用户状态 (磁盘)
    requires: tuple = ()             # 需要的 self 属性名, 缺任一 → 该路由自动跳过
    note: str = ""                   # 人读说明 (矩阵/审计用)

    def available(self, owner) -> bool:
        """依赖是否齐备 (如 hive_mind / intent_router 未初始化时自动跳过)。"""
        return all(getattr(owner, r, None) for r in self.requires)


@dataclass
class TraceEntry:
    route: str
    matched: bool
    skipped: str = ""                # 非空 = 跳过的原因 (不变量/依赖缺失)


@dataclass
class RouteTrace:
    """一次 dispatch 的考察记录 —— 一行回答"谁吃了我这句话"。"""
    entries: list = field(default_factory=list)
    winner: str = ""
    elapsed: float = 0.0

    def summary(self, chars: int = 200) -> str:
        tested = [e.route for e in self.entries]
        skipped = [f"{e.route}({e.skipped})" for e in self.entries if e.skipped]
        s = f"胜出: {self.winner or '(无 → 落到兜底链路)'} | 考察 {len(tested)} 条"
        if skipped:
            s += f" | 跳过: {', '.join(skipped)}"
        return s[:chars]

    def to_list(self) -> list:
        return [{"route": e.route, "matched": e.matched, "skipped": e.skipped}
                for e in self.entries]


class RouteRegistry:
    """路由表。按 priority 降序裁决, 第一个 match 的胜出。"""

    def __init__(self):
        self._routes: list[Route] = []
        self._by_name: dict[str, Route] = {}
        self.last_trace: RouteTrace = RouteTrace()

    # ── 注册 ──
    def register(self, route: Route) -> Route:
        if route.name in self._by_name:
            raise ValueError(f"route '{route.name}' 已注册 —— 拒绝静默覆盖")
        self._routes.append(route)
        self._by_name[route.name] = route
        return route

    def get(self, name: str) -> Route | None:
        return self._by_name.get(name)

    def routes(self) -> list[Route]:
        """按 priority 降序 (同优先级保持注册顺序 = 稳定排序)。"""
        return sorted(self._routes, key=lambda r: -r.priority)

    def names(self) -> list[str]:
        return [r.name for r in self.routes()]

    # ── 裁决 ──
    async def dispatch(self, message: str, ctx: dict, owner=None) -> dict | None:
        """依次考察路由。第一个 match 的胜出并执行。

        返回 None = 没有路由接手 (或全部放弃) → 调用方继续走原有兜底链路 (加性, 不破坏旧行为)。
        单条路由的 run 返回 None = "命中但放弃" → 继续试下一条 (保留 fall-through 语义)。
        """
        t0 = time.time()
        trace = RouteTrace()
        probe = bool(ctx.get("probe"))

        for r in self.routes():
            # 不变量 1: 依赖缺失 → 跳过
            if owner is not None and not r.available(owner):
                trace.entries.append(TraceEntry(r.name, False, "依赖缺失"))
                continue
            # 不变量 2: probe 流量不得写用户状态
            if probe and r.writes_state and not r.probe_safe:
                trace.entries.append(TraceEntry(r.name, False, "probe 流量禁写状态"))
                # ★ 2026-09-19 深测修: 原来这里直接 continue → 该路由**放弃**后,
                #   probe 流量会继续落到后面的无关链路, 产生**看似正常其实错误的答案**
                #   (实测: `/learn stats` 在 probe 下没走 cmd.learn, 却掉到 brain 的罐头技能
                #    答出 "OK 文件信息: size=40960B…" —— 验证者会被这个答案误导)。
                #   ★ 但**只对斜杠命令**做"明确跳过": 命令的意图是确定的, 让无关路由去接
                #     一定是错的; 而通用 stateful 路由仍保持旧的 fall-through 语义
                #     (留档契约明确要求: probe 下 stateful 跳过、后面的 safe 路由照常胜出)。
                #     match 契约是**纯函数**, 这里放在 try 里调用以兼容异常。
                try:
                    if not r.name.startswith("cmd."):
                        raise LookupError("非命令路由: 保持 fall-through 语义")
                    if r.match(message, ctx):
                        trace.winner = "probe.skip"
                        trace.elapsed = round(time.time() - t0, 4)
                        self.last_trace = trace
                        out = {"response": f"[probe] 已跳过需写状态的命令: {r.name} —— 验证流量不执行它"
                                            f" (正式对话不受影响)",
                               "intent": "probe_skip", "route": "probe.skip",
                               "skipped_route": r.name, "model": "local", "trace": []}
                        self._notify_owner(owner, message, ctx, out, "probe.skip")
                        return out
                except Exception as e:
                    trace.entries.append(TraceEntry(r.name, False, f"match 异常 {type(e).__name__}"))
                continue
            try:
                hit = r.match(message, ctx)
            except Exception as e:                      # match 自身出错不该拖垮整条链
                trace.entries.append(TraceEntry(r.name, False, f"match 异常 {type(e).__name__}"))
                continue
            if not hit:
                trace.entries.append(TraceEntry(r.name, False))
                continue

            trace.entries.append(TraceEntry(r.name, True))
            out = r.run(message, ctx)
            if hasattr(out, "__await__"):                # 支持 async run
                out = await out
            # ★ run 返回 None = "我命中但放弃" → **继续试下一条路由** (保留旧代码的 fall-through)
            #   例: planner 出了空方案 → 该让 600 分析/兜底继续接; 插件调用异常 → 让 geo 接。
            #   若不做这一步, 匹配即终结, 后面的路由永远轮不到。
            if out is None:
                trace.entries.append(TraceEntry(r.name, False, "命中但放弃(返回 None)"))
                continue
            trace.winner = r.name
            trace.elapsed = round(time.time() - t0, 4)
            self.last_trace = trace
            if isinstance(out, dict):
                out.setdefault("route", r.name)
            self._notify_owner(owner, message, ctx, out, r.name)
            return out

        trace.elapsed = round(time.time() - t0, 4)
        self.last_trace = trace
        self._notify_owner(owner, message, ctx, None, None)
        return None

    def _notify_owner(self, owner, message, ctx, out, route_name):
        """路由执行后的**统一观察点** (基础设施)。

        存在的理由: 学习闭环 (learn_loop) 需要"每轮对话后看一眼学到了什么", 而这件事
        必须对**所有**路由一视同仁 —— 若塞进 process() 加 if, 就违反"新能力只准在
        routes.py 声明"的铁律; 若散在各路由的 run 里, 又会漏掉没接手的情况。
        所以作为 dispatch 的收尾钩子, 一处覆盖全部 (含"没人接手"的兜底路径)。

        owner 没实现该方法时静默跳过 (旧端兼容: dispatch 可独立使用)。
        probe 流量不学习 —— 由 owner 侧再判一次 (双保险)。
        """
        fn = getattr(owner, "_on_route_done", None)
        if not callable(fn):
            return
        try:
            fn(message, ctx, out, route_name)
        except Exception as e:                          # 观察失败绝不影响主流程
            logger.debug("post-route hook failed: %s", e)

    # ── 自检 (给验证器用) ──
    def audit(self) -> dict:
        """静态审计: 重名/优先级冲突/缺依赖声明/不变量可疑项。"""
        issues = []
        seen = {}
        for r in self._routes:
            if r.name in seen:
                issues.append(f"重名: {r.name}")
            seen[r.name] = r
            if not callable(r.match) or not callable(r.run):
                issues.append(f"{r.name}: match/run 不可调用")
            if r.writes_state and not r.probe_safe and not r.probe_gated:
                issues.append(f"{r.name}: writes_state 但非 probe_safe —— probe 流量会被跳过(可能是漏标); "
                              f"若是有意为之请标 probe_gated=True")
        prio = {}
        for r in self.routes():
            prio.setdefault(r.priority, []).append(r.name)
        return {"count": len(self._routes), "priorities": prio, "issues": issues}
