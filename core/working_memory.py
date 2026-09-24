"""
WorkingMemory -- Tiger.M.M 工作记忆隔离层
==========================================
每个 TaskLoop / Plan 一个独立沙箱内存: 高度上下文敏感, 任务结束即销毁,
防止不同任务间的上下文污染 (Context Pollution)。

特性:
  1. 任务级隔离 -- 每个 task_id 独立 messages 栈, 互不可见
  2. 生命周期 -- create → push → snapshot → destroy, 任务结束强制清理
  3. 崩溃恢复 -- 可选磁盘持久化 (data/working_memory/<task_id>.json), destroy 时删除
  4. 活跃清单 -- active_count / list_active, 供诊断与清理
"""
import json, os, time, logging, threading
from pathlib import Path

logger = logging.getLogger("core.working_memory")

DATA_DIR = Path(__file__).parent.parent / "data"
WM_DIR = DATA_DIR / "working_memory"

# 单任务沙箱上限: 防止失控任务无限膨胀
MAX_MESSAGES_PER_TASK = 60
MAX_ACTIVE_TASKS = 20


class WorkingMemory:
    """工作记忆管理器(进程内单例)。"""

    def __init__(self, data_dir: Path = None, persist: bool = True):
        self._dir = Path(data_dir) if data_dir else WM_DIR
        self._persist = persist
        self._tasks: dict[str, dict] = {}   # task_id -> {context, messages, created_at, updated_at}
        self._lock = threading.Lock()
        if persist:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning("working_memory dir create failed: %s", e)

    # ── 生命周期 ──

    def create(self, task_id: str, context: str = "") -> bool:
        """创建任务沙箱。返回 False 表示已存在或超出上限。"""
        if not task_id:
            return False
        with self._lock:
            if len(self._tasks) >= MAX_ACTIVE_TASKS and task_id not in self._tasks:
                logger.warning("working_memory: task cap reached (%d), reject %s",
                               MAX_ACTIVE_TASKS, task_id)
                return False
            if task_id in self._tasks:
                return False
            now = time.time()
            self._tasks[task_id] = {
                "context": context,
                "messages": [],
                "created_at": now,
                "updated_at": now,
            }
        self._save(task_id)
        logger.info("working_memory: created %s", task_id)
        return True

    def push(self, task_id: str, role: str, content: str) -> bool:
        """向沙箱追加一条消息。超限时滚动丢弃最旧(保底保留最近 MAX_MESSAGES_PER_TASK)。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task["messages"].append({"role": role, "content": str(content)[:4000]})
            if len(task["messages"]) > MAX_MESSAGES_PER_TASK:
                task["messages"] = task["messages"][-MAX_MESSAGES_PER_TASK:]
            task["updated_at"] = time.time()
        self._save(task_id)
        return True

    def get(self, task_id: str) -> dict:
        """读取任务沙箱完整状态(隔离视图: 只返回本任务)。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return {}
            return {
                "task_id": task_id,
                "context": task.get("context", ""),
                "messages": list(task.get("messages", [])),
                "created_at": task.get("created_at", 0),
                "updated_at": task.get("updated_at", 0),
            }

    def snapshot(self, task_id: str) -> str:
        """任务中途快照: 渲染为模型可读文本(工作记忆内容)。"""
        task = self.get(task_id)
        if not task:
            return ""
        lines = [f"[Working Memory: {task_id}]"]
        if task.get("context"):
            lines.append(f"  目标: {task['context'][:200]}")
        msgs = task.get("messages", [])
        for m in msgs[-15:]:  # 最近 15 条
            role = "用户" if m.get("role") == "user" else "虎哥"
            lines.append(f"  {role}: {str(m.get('content', ''))[:150]}")
        return "\n".join(lines)

    def destroy(self, task_id: str) -> bool:
        """任务结束销毁沙箱: 内存清除 + 磁盘文件删除。"""
        with self._lock:
            existed = self._tasks.pop(task_id, None) is not None
        if existed:
            try:
                f = self._dir / f"{self._safe(task_id)}.json"
                if f.exists():
                    f.unlink()
            except Exception:
                pass
            logger.info("working_memory: destroyed %s", task_id)
        return existed

    def active_count(self) -> int:
        with self._lock:
            return len(self._tasks)

    def list_active(self) -> list[str]:
        with self._lock:
            return list(self._tasks.keys())

    # ── 持久化 ──

    def _safe(self, task_id: str) -> str:
        return "".join(c for c in task_id if c.isalnum() or c in "-_")[:64] or "task"

    def _save(self, task_id: str):
        if not self._persist:
            return
        try:
            task = self.get(task_id)
            if not task:
                return
            (self._dir / f"{self._safe(task_id)}.json").write_text(
                json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.debug("working_memory save failed: %s", e)

    def recover(self) -> list[str]:
        """进程重启后从磁盘恢复未销毁的任务(崩溃恢复)。"""
        if not self._persist or not self._dir.exists():
            return []
        recovered = []
        for f in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                tid = data.get("task_id", f.stem)
                self._tasks[tid] = {
                    "context": data.get("context", ""),
                    "messages": data.get("messages", []),
                    "created_at": data.get("created_at", time.time()),
                    "updated_at": data.get("updated_at", time.time()),
                }
                recovered.append(tid)
            except Exception as e:
                logger.warning("working_memory recover skip %s: %s", f.name, e)
        if recovered:
            logger.info("working_memory: recovered %d tasks", len(recovered))
        return recovered

    def gc(self, max_age: float = 86400) -> int:
        """清理超龄沙箱(任务挂起超过 max_age 秒视为泄漏)。"""
        now = time.time()
        dead = []
        with self._lock:
            for tid, task in self._tasks.items():
                if now - task.get("updated_at", now) > max_age:
                    dead.append(tid)
        for tid in dead:
            self.destroy(tid)
        return len(dead)


# ═══ Singleton ═══
_wm: WorkingMemory | None = None


def get_working_memory(data_dir: Path = None, persist: bool = True) -> WorkingMemory:
    global _wm
    if _wm is None:
        _wm = WorkingMemory(data_dir, persist)
    return _wm
