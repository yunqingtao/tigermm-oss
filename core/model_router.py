"""
ModelRouter — 智能模型调度
==========================
不依赖 Ollama。纯云端模型间按任务类型+历史成功率+成本自动选。

三层决策:
  1. 用户显式 @model → 直接使用
  2. intent_router 已处理 → 不调模型（零成本）
  3. 自动路由: 任务分类 → 模型匹配 → 成本优化 → 回退链

模型注册表 (cost: 相对成本, cap: 能力评分 1-10):
  deepseek — cost=1, cap=10, tool_calling=yes (主力推理)
  mimo     — cost=0, cap=6,  tool_calling=yes (免费备用)

感知驱动:
  perception.suggest() 说某工具不稳定 → 优先选工具调用强的模型
  perception 记录模型成功率 → 历史差的降权
"""
import logging, os, time, json
from typing import Optional
from pathlib import Path

from core.provider_catalog import is_config_slot, display_name

logger = logging.getLogger("core.model_router")

# ── 健康探测（懒加载，避免循环导入）──
_health_probe = None

def _get_probe():
    global _health_probe
    if _health_probe is None:
        from core.health_probe import get_health_probe
        _health_probe = get_health_probe()
    return _health_probe


# ── 用户自配的家 (2026-10-09 包容) ──────────────────────────────────────────
#: 原来只认识写死的 4 个名字 (deepseek/mimo/qwen/ollama) —— 用户拿一把
#: 别家的 Key (OpenAI / Kimi / 智谱 / 硅基流动 / OpenRouter …) 填进 keys.json,
#: 传输层能通, 但**路由层不认识这个名字 → 永远不会派活给它**, 等于白配。
#: 现在: keys.json 里任何"填了 key 的模型槽"都算一等候选 (默认 cap=8)。
_EXTRA_CACHE = {"path": None, "mtime": None, "data": None}


def _read_keys_json() -> dict:
    """读 keys.json (带 mtime 缓存)。读不到就返回 {} —— 那时行为与改动前完全一致。"""
    try:
        from config.settings import PROJECT_ROOT
        p = Path(PROJECT_ROOT) / "keys.json"
        if not p.exists():
            return {}
        mt = p.stat().st_mtime
        if _EXTRA_CACHE["path"] == str(p) and _EXTRA_CACHE["mtime"] == mt:
            return _EXTRA_CACHE["data"] or {}
        data = json.loads(p.read_text(encoding="utf-8"))
        _EXTRA_CACHE.update({"path": str(p), "mtime": mt, "data": data})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _configured_slots() -> dict:
    """keys.json 里**真配好了**的模型槽 → {槽位名: cfg}。

    判据: 非配置段 (is_config_slot) 且 (有非空**非占位符** key 或 是本地 ollama)。
    读不到 keys.json 时返回 {} —— 调用方据此退回"不过滤"的老行为。
    """
    from core.provider_catalog import is_placeholder_key
    out = {}
    for slot, cfg in _read_keys_json().items():
        if not isinstance(cfg, dict) or not is_config_slot(slot):
            continue
        key = str(cfg.get("key") or cfg.get("api_key") or "").strip()
        url = str(cfg.get("url") or cfg.get("base_url") or "").lower()
        if (key and not is_placeholder_key(key)) or "11434" in url or "ollama" in slot.lower():
            out[slot] = cfg
    return out


def _extra_models() -> dict:
    """已配置但不在 MODEL_REGISTRY 里的槽 → 默认档位 (cap=8, cost=1.0, 可调工具)。"""
    extra = {}
    for slot, cfg in _configured_slots().items():
        if slot in MODEL_REGISTRY:
            continue
        extra[slot] = {
            "cost": 1.0, "capability": 8, "tool_calling": True, "max_tokens": 4096,
            "description": f"用户自配: {display_name(slot, cfg.get('url', ''))}",
            "user_configured": True,
        }
    return extra

# ── 模型注册表 ──
MODEL_REGISTRY = {
    "deepseek": {
        "cost": 1.0,
        "capability": 10,
        "tool_calling": True,
        "max_tokens": 4096,
        "description": "DeepSeek - 强推理+工具调用, 主力模型",
    },
    "mimo": {
        "cost": 0.0,
        "capability": 6,
        "tool_calling": True,
        "max_tokens": 4096,
        "description": "Mimo - 免费, 轻量任务够用",
    },
    "qwen": {
        "cost": 0.1,
        "capability": 7,
        "tool_calling": True,
        "max_tokens": 4096,
        "description": "Qwen(通义千问) - 便宜, 中等任务",
    },
    "ollama": {
        "cost": 0.0,
        "capability": 4,
        "tool_calling": False,
        "max_tokens": 4096,
        "description": "Ollama - 本地免费, 闲聊/简单任务(无工具调用)",
    },
}

# ── 任务分类关键词 ──
TASK_PATTERNS = {
    "code": [
        "写代码", "脚本", "编程", "bug", "报错", "traceback",
        "def ", "import ", "class ", "函数", "算法",
        "实现", "修复", "重构", "调试", "debug",
        "pip", "npm", "git ", "docker",
        "api", "endpoint", "路由", "中间件",
        "Python", "Flask", "Django", "FastAPI",
        "数据库", "SQL", "编译", "构建", "部署",
    ],
    "search": [
        "搜索", "查", "最新", "新闻", "今天", "实时",
        "天气", "股价", "汇率", "比赛",
        "是什么", "怎么用", "如何",
        "有哪些", "推荐", "排名",
    ],
    "tool": [
        "截图", "鼠标", "键盘", "点击",
        "发邮件", "发送", "通知", "消息",
        "打开浏览器", "打开网站", "浏览",
    ],
    "file": [
        "读文件", "写文件", "保存", "创建文件",
        "列出", "目录", "删除", "移动", "复制", "重命名",
        "桌面", "文档", "下载",
    ],
    "chat": [
        "你好", "再见", "谢谢", "哈哈", "嗯", "哦", "好的",
        "你是谁", "在吗", "怎么样", "如何", "介绍", "自己",
        "心情", "感觉", "想法", "帮忙", "帮", "能",
    ],
}

TASK_CAPABILITY_MIN = {
    "code": 7,
    "search": 4,
    "tool": 5,
    "file": 3,
    "chat": 1,
    "complex": 8,
    "general": 4,
}

# Cost sensitivity: higher = prefer cheaper model more aggressively
COST_WEIGHT = {
    "code": 1,      # 代码不强省钱, 要能力
    "search": 3,    # 搜索中等, 便宜模型够用
    "tool": 2,      # 工具调用需要一定能力
    "file": 5,      # 文件操作简单, 省钱优先
    "chat": 5,      # 闲聊无所谓, 省钱
    "complex": 1,   # 复杂任务要最强模型
    "general": 3,
}


class ModelRouter:
    """智能模型路由: 任务分类 + 成功率追踪 + 成本优化 + 回退链。"""

    def __init__(self, data_dir: Optional[Path] = None):
        self._data_dir = data_dir or Path(__file__).parent.parent / "data"
        self._stats_file = self._data_dir / "model_router_stats.json"
        self._stats = self._load_stats()
        self._session_failures = {}

    def _load_stats(self) -> dict:
        if self._stats_file.exists():
            try:
                return json.loads(self._stats_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"models": {}, "tasks": {}, "total": 0}

    def _save_stats(self):
        self._stats["updated_at"] = time.time()
        self._stats_file.write_text(
            json.dumps(self._stats, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def record(self, model: str, task_type: str, success: bool, latency: float):
        ms = self._stats["models"].setdefault(model, {
            "ok": 0, "fail": 0, "total_latency": 0.0, "by_task": {},
        })
        ms["ok" if success else "fail"] += 1
        ms["total_latency"] += latency
        ts = ms["by_task"].setdefault(task_type, {"ok": 0, "fail": 0})
        ts["ok" if success else "fail"] += 1
        gts = self._stats["tasks"].setdefault(task_type, {"ok": 0, "fail": 0})
        gts["ok" if success else "fail"] += 1
        self._stats["total"] += 1
        if not success:
            self._session_failures[model] = time.time()
        if self._stats["total"] % 10 == 0:
            self._save_stats()

    def success_rate(self, model: str, task_type: str = "") -> float:
        ms = self._stats["models"].get(model, {})
        if task_type:
            ts = ms.get("by_task", {}).get(task_type, {})
            ok = ts.get("ok", 0)
            fail = ts.get("fail", 0)
        else:
            ok = ms.get("ok", 0)
            fail = ms.get("fail", 0)
        total = ok + fail
        return ok / total if total > 0 else 0.8

    def classify_task(self, message: str, intent_result: dict = None) -> str:
        msg = message.lower().strip()

        if intent_result and intent_result.get("actions"):
            action = intent_result["actions"][0]
            router_actions = {
                "search", "file_read", "file_list", "file_find", "file_rename",
                "file_delete", "file_copy", "file_move", "file_organize",
                "screenshot", "system_info", "disk_info", "check_mail", "weather",
            }
            if action in router_actions and intent_result.get("delivery") in (None, "save", "show"):
                return "local"

        if len(msg) <= 3:
            return "chat"

        scores = {}
        for category, patterns in TASK_PATTERNS.items():
            score = sum(1 for p in patterns if p in msg)
            if score > 0:
                scores[category] = score

        if not scores:
            if len(msg) > 40:
                return "complex"
            return "general"

        for boost in ("code", "tool"):
            if boost in scores:
                scores[boost] *= 2

        best = max(scores, key=lambda k: (scores[k], 1 if k == "chat" else 0))
        return best

    def select_model(self, task_type: str, perception_ctx: dict = None) -> str:
        min_cap = TASK_CAPABILITY_MIN.get(task_type, 4)
        # ★ 2026-10-09 包容: 把"用户自配的家"并进候选 —— 不再只认写死的 4 个名字。
        registry = dict(MODEL_REGISTRY)
        registry.update(_extra_models())
        candidates = {
            name: info for name, info in registry.items()
            if info["capability"] >= min_cap
        }
        # ★ 只给**真配了 Key 的家**派活 (否则会路由到一个空槽 → 用户看到报错)。
        #   读不到 keys.json 时 configured 为空 → 不过滤, 行为与改动前完全一致。
        configured = _configured_slots()
        if configured:
            narrowed = {n: i for n, i in candidates.items() if n in configured}
            if narrowed:
                candidates = narrowed
            else:
                logger.info("router: 没有满足 %s 的已配置模型, 退到已配置里最强的一个", task_type)

        # 健康探测过滤：跳过已知不健康的模型
        try:
            probe = _get_probe()
            unhealthy = probe.get_unhealthy()
            if unhealthy:
                candidates = {n: i for n, i in candidates.items() if n not in unhealthy}
                if unhealthy:
                    logger.info("router: skipping unhealthy models: %s", unhealthy)
        except Exception:
            pass  # 探测不可用时正常路由
        if not candidates:
            # ★ 2026-10-09 包容: 原来无条件返回 "deepseek" —— 用户可能根本没配 DeepSeek,
            #   于是"没有满足能力的模型"会变成"派给一个空槽"。现在先在他配好的家里挑最强的。
            if configured:
                have = {n: registry[n] for n in configured if n in registry}
                if have:
                    pick = max(have, key=lambda n: have[n]["capability"])
                    logger.info("router: 无满足能力的已配模型, 用 %s 兜底", pick)
                    return pick
                return sorted(configured)[0]
            return "deepseek"

        scored = []
        for name, info in candidates.items():
            rate = self.success_rate(name, task_type)
            cost = info["cost"]
            cap = info["capability"]
            # For cost-sensitive tasks, cap effective capability (no overkill)
            if COST_WEIGHT.get(task_type, 3) >= 4:
                min_cap_req = TASK_CAPABILITY_MIN.get(task_type, 1)
                cap = min(cap, min_cap_req + 3)  # enough is enough
            perception_bonus = 0
            if perception_ctx:
                unstable_tools = perception_ctx.get("unstable_tools", [])
                if unstable_tools and info.get("tool_calling"):
                    perception_bonus = min(len(unstable_tools) * 0.5, 2.0)
            fail_penalty = 0
            last_fail = self._session_failures.get(name, 0)
            if last_fail and time.time() - last_fail < 30:
                fail_penalty = 5
            cost_weight = COST_WEIGHT.get(task_type, 3)
            score = (
                cap * 2 + rate * 10 - cost * cost_weight + perception_bonus - fail_penalty
            )
            scored.append((score, name))

        scored.sort(reverse=True)
        best = scored[0][1]
        logger.info("router: task=%s -> %s (score=%.1f)", task_type, best, scored[0][0])
        return best

    def get_fallback(self, model: str, task_type: str) -> Optional[str]:
        chain = {"deepseek": "mimo", "mimo": "deepseek"}
        fb = chain.get(model)
        if fb and MODEL_REGISTRY[fb]["capability"] >= TASK_CAPABILITY_MIN.get(task_type, 1):
            return fb
        return None

    def route(self, message: str, intent_result: dict = None,
              perception=None, ext_model: str = None) -> dict:
        if ext_model and ext_model != "auto":
            return {
                "model": ext_model, "task_type": "user_override",
                "reason": f"user @{ext_model}", "fallback_model": self.get_fallback(ext_model, "general"),
            }

        task_type = self.classify_task(message, intent_result)
        if task_type == "local":
            return {
                "model": "local", "task_type": "local",
                "reason": "intent_router handled", "fallback_model": None,
            }

        perception_ctx = {}
        if perception:
            try:
                sug = perception.suggest()
                unstable = [s["data"]["tool"] for s in sug
                           if s["type"] == "fix" and "tool" in s.get("data", {})]
                if unstable:
                    perception_ctx["unstable_tools"] = unstable
            except Exception:
                pass

        # 异步触发健康探测（后台刷新缓存，不阻塞路由）
        try:
            probe = _get_probe()
            cached = probe._cached("deepseek")
            if not cached:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(probe.probe_all())
                except RuntimeError:
                    pass  # 无事件循环时跳过
        except Exception:
            pass

        model = self.select_model(task_type, perception_ctx)
        return {
            "model": model, "task_type": task_type,
            "reason": f"auto: {task_type} -> {model}", "fallback_model": self.get_fallback(model, task_type),
        }

    def get_stats(self) -> dict:
        return {
            "total": self._stats["total"],
            "models": {
                m: {"ok": d["ok"], "fail": d["fail"]}
                for m, d in self._stats.get("models", {}).items()
            },
        }


_router: Optional[ModelRouter] = None

def get_model_router(data_dir: Path = None) -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter(data_dir)
    return _router
