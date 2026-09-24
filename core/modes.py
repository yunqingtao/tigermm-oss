"""
core/modes.py — 三模式执行闸门 (Ask / Plan / Craft)

对标 WorkBuddy(OpenClaw) 的三种运行模式:

  ask   问答  — 只回答, 不碰工具、不碰文件系统、不执行任何操作
  plan  规划  — 先出可执行方案, 用户确认后才动手
  craft 实干  — 直接执行文件/系统操作 (默认, 即原有行为)

状态持久化到 data/mode.json, CLI 与 Web 共用同一份状态。
"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("core.modes")

MODES = {
    "ask":   {"cn": "问答", "en": "Ask",   "desc": "只回答，不执行任何操作"},
    "plan":  {"cn": "规划", "en": "Plan",  "desc": "先出方案，确认后再执行"},
    "craft": {"cn": "实干", "en": "Craft", "desc": "直接执行文件/系统操作"},
}
DEFAULT_MODE = "craft"
MODE_ORDER = ["ask", "plan", "craft"]

CONFIRM_WORDS = {
    "执行", "开始", "确认", "可以", "好的", "好", "行", "干", "动手", "跑",
    "ok", "go", "run", "y", "yes", "do it", "继续",
}
CANCEL_WORDS = {
    "取消", "不", "不用", "算了", "停", "别", "cancel", "abort", "n", "no", "q",
}
PENDING_TTL = 1800  # 方案 30 分钟未确认自动失效


def _norm(msg: str) -> str:
    return (msg or "").strip().lower().rstrip("。！!，,~ ")


class ModeManager:
    """三模式状态机 + 规划方案待确认队列。"""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "mode.json"
        self.state = {
            "mode": DEFAULT_MODE,
            "updated": 0,
            "pending": None,          # 可执行步骤 (task_planner 出)
            "pending_message": "",    # 触发方案的原话
            "pending_text": "",       # 展示给用户的方案文本
            "pending_at": 0,
        }
        self._load()

    # ── 持久化 ──
    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    if d.get("mode") in MODES:
                        self.state["mode"] = d["mode"]
                    for k in ("updated", "pending", "pending_message",
                              "pending_text", "pending_at"):
                        if k in d:
                            self.state[k] = d[k]
        except Exception as e:
            logger.warning("mode.json 读取失败(用默认 craft): %s", e)

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            logger.warning("mode.json 写入失败: %s", e)

    # ── 模式读写 ──
    def get(self) -> str:
        m = self.state.get("mode")
        return m if m in MODES else DEFAULT_MODE

    def set(self, mode: str) -> bool:
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            return False
        if mode != self.state.get("mode"):
            # ★ 2026-09-23 加: 模式切换留痕。
            #   起因: mode.json 漂移成 ask 两次 (2026-09-21 一次未复现, 2026-09-23 一次),
            #   每次都只能看到"结果"看不到"谁改的"。加了调用栈后, 下次当场可查。
            try:
                import traceback as _tb
                _old = self.state.get("mode")
                logger.info("模式切换: %s → %s (调用者见下)", _old, mode)
                for _ln in _tb.format_stack()[-8:-1]:
                    logger.info("    %s", _ln.strip().replace("\n", " ")[:160])
            except Exception:
                pass
            self.state["mode"] = mode
            self.state["updated"] = time.time()
            # 换模式 → 作废未确认的方案, 避免跨模式误执行
            self._reset_pending()
            # ★ 2026-09-19: 只在**真的变化**时写盘。原来无条件 _save() →
            #   前端每次请求都带 mode 字段时每次都写磁盘 (无谓 IO + 与读方竞态)。
            self._save()
        return True

    def info(self, mode: str = None) -> dict:
        m = mode or self.get()
        d = MODES.get(m, MODES[DEFAULT_MODE])
        return {"mode": m, "cn": d["cn"], "en": d["en"], "desc": d["desc"],
                "order": MODE_ORDER}

    def all(self) -> dict:
        return {k: dict(v, mode=k) for k, v in MODES.items()}

    def set_error(self) -> str:
        return "模式只能是 ask / plan / craft（问答 / 规划 / 实干）"

    # ── 规划方案待确认 ──
    def set_pending(self, steps: list, message: str, text: str = ""):
        self.state["pending"] = steps or []
        self.state["pending_message"] = message or ""
        self.state["pending_text"] = text or ""
        self.state["pending_at"] = time.time()
        self._save()

    def get_pending(self):
        """返回 {steps, message, text} 或 None（含 TTL 过期检查）。"""
        if not self.state.get("pending_at"):
            return None
        if time.time() - float(self.state.get("pending_at") or 0) > PENDING_TTL:
            self.clear_pending()
            return None
        if not (self.state.get("pending") or self.state.get("pending_text")):
            return None
        return {
            "steps": self.state.get("pending") or [],
            "message": self.state.get("pending_message") or "",
            "text": self.state.get("pending_text") or "",
        }

    def _reset_pending(self):
        self.state["pending"] = None
        self.state["pending_message"] = ""
        self.state["pending_text"] = ""
        self.state["pending_at"] = 0

    def clear_pending(self, save: bool = True):
        self._reset_pending()
        if save:
            self._save()

    # ── 词判定 ──
    @staticmethod
    def is_confirm(msg: str) -> bool:
        return _norm(msg) in CONFIRM_WORDS

    @staticmethod
    def is_cancel(msg: str) -> bool:
        return _norm(msg) in CANCEL_WORDS
