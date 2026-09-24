"""GapLedger — 能力缺口台账 (「我这次没这能力」的账)。

═══════════════════════════════════════════════════════════════════════
为什么要有它 (与 learn_loop.open_questions 的分工, 不重叠)
═══════════════════════════════════════════════════════════════════════
    learn_loop.open_questions  = **知识**缺口 —— 模型答不上来的问题 (待学池)
    gap_ledger                 = **能力**缺口 —— 工具/技能干不了的活 (待造池)

没有它的时候: "缺什么能力"只能靠感觉猜 —— 出门找工具(联网/市场)没有关键词,
评估没有标准, 装完也不知道值不值。**先有账, 才知道该找什么。**

═══════════════════════════════════════════════════════════════════════
三条设计原则 (与 learn_loop / skill_factory 同源)
═══════════════════════════════════════════════════════════════════════
  ① 缺口存**数据** (SQLite), 绝不改源码 —— 运行时读写。
  ② 唯一入口: pipeline._on_route_done (dispatch 收尾的统一观察点) ——
     不往 process() 插 if, 也不散在各路由里 (会漏)。
  ③ 唯一出口: /gap 命令 (cmd.gap)。

═══════════════════════════════════════════════════════════════════════
分类 (按"卡在哪一环"分, 因为不同环对应不同修法)
═══════════════════════════════════════════════════════════════════════
    no_tool        本地没有工具/技能覆盖这件事        → 该造新能力 (走技能工厂/市场)
    missing_dep    有工具但依赖缺失 (模块/二进制)     → 该装依赖 (最便宜)
    no_entry       工具存在但没有可调用入口           → 该补入口 (改代码)
    missing_arg    工具被调用但缺必填参数             → 该改路由/技能提参
    unknown_action 工具不认识这个动作                 → 该补动作或改技能 DAG
    skill_failed   技能/工具跑了但失败                → 该修技能 (缺步骤等)
    model_refuse   模型兜底且明确说干不了             → 该造能力或该诚实告知

聚合 (这台账最值钱的部分):
    同一"签名" (归一化后) 重复出现 → 累加 count。
    count >= REPEAT_THRESHOLD → 标 `worth_building` —— **出现 3 次的是真需求,
    出现 1 次的是偶然**。这一步把"零散抱怨"变成"该造什么"的排序依据。

★ probe 流量绝不记账 (硬不变量): 验证流量不得改用户数据。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("core.gap_ledger")

try:
    from config.settings import DATA_DIR
except Exception:                                            # 独立跑测试时的兜底
    DATA_DIR = Path(__file__).parent.parent / "data"

DB_NAME = "gap_ledger.db"
REPEAT_THRESHOLD = 3        # 出现 >= 3 次 → 值得造 (真需求 vs 偶然)
SIG_MAX = 80                # 签名长度上限

# ═══════════════════════════════════════════════════════════════════
# 检测信号 —— 全部取自**代码里真实产生的文案** (不是猜的)
#   每条信号: (分类, 正则)。顺序即优先级 (前面的更具体)。
# ═══════════════════════════════════════════════════════════════════
SIGNALS: list[tuple[str, re.Pattern]] = [
    # 依赖缺失 (最便宜可修) —— ★ 必须带"模块/命令/依赖"语境:
    #   裸的"找不到/不存在"是**用户给错路径** (用户问题), 不是能力缺口
    #   (实测踩过: `文件不存在: ...v.mp3` 被误记成缺依赖)。
    ("missing_dep", re.compile(
        r"No module named|未安装|没有安装|请先安装|"
        # ★ 实测补: 工厂实跑门失败常见 `step 'x': [WinError 2] 系统找不到指定的文件`
        #   (调外部命令但命令没装) —— 这就是缺依赖, 该给装法而不是记成"技能失败"。
        r"WinError 2|系统找不到指定的文件|"
        r"The system cannot find the file specified|"
        r"is not recognized as an internal or external command|"
        r"不是内部或外部命令|is not recognized|command not found|"
        r"(?:找不到|无法找到|缺失|缺少)[^\n]{0,12}?(?:可执行文件|命令|程序|模块|库|依赖)|"
        r"(?:可执行文件|命令|模块|库|依赖)[^\n]{0,10}?(?:不存在|未找到|缺失)", re.I)),
    # 工具没有可调用入口 (需 run()/execute())
    ("no_entry", re.compile(r"没有可调用入口|无\s*run/execute|没有入口")),
    # 缺必填参数 (技能 DAG 提参没提全)
    ("missing_arg", re.compile(r"缺必填参数|缺少必填|必填参数")),
    # 工具不认识这个动作
    ("unknown_action", re.compile(r"Unknown action|未知工具|未知动作|不支持的动作")),
    # 技能/工具跑了但失败
    ("skill_failed", re.compile(r"没执行成功|执行失败|步骤\(.*\)\s*失败|第\s*\d+\s*步.*失败")),
]

# ★ "系统在正常追问用户要输入" —— 这**不是**能力缺口 (是健康交互)。
#   实测: `✗ look-at-image 第 1 步(look) 失败: 没找到图片。请给出图片路径, 例如: ...`
#   技能干得对 (用户没给图, 它该问)。若不排除, 台账会被"正常追问"灌满。
#   只压制**有歧义**的分类 (缺参数/技能失败); 依赖缺失/无入口/未知动作是硬证据, 不受影响。
_ASK_INPUT = re.compile(
    r"请(?:给出|提供|告诉|给|补充|指明)|例如[:：]|比如[:：]|补上再试|"
    r"需要(?:你|您)(?:提供|给出|说明)|方便(?:的话)?(?:告诉|说)"
)

# 模型兜底时说"干不了"的措辞 → 能力缺口 + 归类
_REFUSE = re.compile(
    r"我(?:无法|不能|没法|做不到|没有(?:这个)?能力)|"
    r"无法帮(?:你|您)|做不到|没法(?:帮|做)|"
    r"抱歉[，,]?\s*(?:我)?(?:无法|不能|没法)|"
    r"暂时(?:没法|无法|不能)|没有(?:相关|对应)的?(?:工具|功能|能力)|"
    r"不支持(?:这个|该)?(?:功能|操作|能力)",
)
# 任务型请求 (判定 no_tool 还是纯知识缺口)
_TASK_HINT = re.compile(
    r"帮(?:我|忙)?|做(?:一|个|份)|生成|制作|写(?:一|个|份|封)|"
    r"转(?:换|成)|下载|爬|抓取|批(?:量|处理)|自动(?:化|做)|"
    r"建(?:立|个)|搭(?:建|个)|导出|扫描|识图|截图|录(?:制|屏)|配音|剪辑",
)
# 归一化: 抹掉具体路径/数字/引号 → 同类缺口聚成一个签名
_NOISE = [
    (re.compile(r"[A-Za-z]:[\\/][^\s，,。；;、'\"]*"), "<路径>"),        # Windows 路径
    (re.compile(r"(?<![\w.])/(?:[\w.\-]+/)*[\w.\-]+"), "<路径>"),        # POSIX 路径
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"['\"“”‘’]"), ""),
    (re.compile(r"\s+"), " "),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS gaps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,     -- 分类 (no_tool / missing_dep / ...)
    signature    TEXT NOT NULL,     -- 归一化签名 (聚合用)
    sample       TEXT,              -- 触发它的**原话** (第一条)
    detail       TEXT,              -- 原始失败文本 (第一条)
    route        TEXT,              -- 当时谁接手的 (含 model.fallback)
    count        INTEGER DEFAULT 1, -- 出现次数 (聚合)
    status       TEXT DEFAULT 'open',   -- open / building / built / dropped
    note         TEXT,
    created_at   REAL,
    updated_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gaps_sig ON gaps(category, signature);
CREATE INDEX IF NOT EXISTS idx_gaps_status ON gaps(status);
"""


# ═══════════════════════════════════════════════════════════════════
# 库路径解析 (验证/门禁隔离) —— 与 storage/session_store.py 同一套规则
# ═══════════════════════════════════════════════════════════════════
_REAL_DB = Path(__file__).parent.parent / "data" / DB_NAME


def resolve_gap_db(db_path=None) -> Path:
    """决定用哪个台账文件。

    ★ 隔离规则 (与 session_store.resolve_db_path 同源, 同一套坑踩过一遍就够了):
      命中条件 = "这个文件就是用户的真实台账" —— 默认取值 **或** 显式传入
      (pipeline 用的是 get_gap_ledger(DATA_DIR) → 显式)。
      显式传**别的**路径 (测试临时库) 一律不动。
      TMM_GAP_DB 由门禁 runner 注入 (scripts/hermes_verify.py), 使
      "跑真实路径的验证器"也不会把缺口写进用户的账。
    """
    import os as _os
    if db_path is None:
        db_path = _REAL_DB
    _env = _os.environ.get("TMM_GAP_DB")
    if _env:
        try:
            if Path(db_path).resolve() == _REAL_DB.resolve():
                return Path(_env)
        except Exception:
            pass
    return Path(db_path)


# ═══════════════════════════════════════════════════════════════════
# 纯函数: 检测 + 归一化 (无副作用 → 可重复调用, 便于测试)
# ═══════════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    """把一句话归一成"签名" —— 抹掉具体路径/数字, 让同类缺口聚到一起。

    例: "写个报告存到 D:/a/b.docx" 与 "写个报告存到 C:/x.docx"
        → 同一个签名 (才看得出"这类需求出现 5 次")。
    """
    s = str(text or "")
    for pat, rep in _NOISE:
        s = pat.sub(rep, s)
    return s.strip()[:SIG_MAX]


def classify(message: str, response: str, route: str = "") -> dict | None:
    """判定这一轮是不是"没能力"。是 → 返回缺口 dict; 否 → None。

    **纯函数**: 只看输入, 不写任何东西 (可自由重复调用)。

    判定顺序 (从严到宽, 避免把正常问答误记成缺口):
      1) 回复里有**具体失败信号** (缺依赖/无入口/缺参数/未知动作/执行失败)
         → 按信号分类 (这是最硬的证据: 工具真的跑了并失败了)
      2) model.fallback/error 兜底 + 模型明确说"干不了"
         → 任务型请求记 no_tool, 否则记 model_refuse (纯知识缺口, 交给 learn_loop)
      3) 其余一律不算缺口 (普通问答不记账 —— 否则账本全是噪音)
    """
    resp = str(response or "")
    msg = str(message or "").strip()
    if not msg:
        return None
    # 斜杠命令的内部提示不算能力缺口 (那是命令用法问题)
    if msg.startswith("/"):
        return None

    asking = bool(_ASK_INPUT.search(resp))          # 系统在追问用户要输入
    for cat, pat in SIGNALS:
        m = pat.search(resp)
        if m:
            # 有歧义的分类 (缺参数/技能失败) 遇上"追问用户" → 是健康交互, 不记账
            if asking and cat in ("missing_arg", "skill_failed"):
                return None
            return {
                "category": cat,
                "signature": normalize(msg),
                "sample": msg[:300],
                "detail": resp[:400],
                "route": route or "",
                "matched": m.group(0)[:40],
            }

    # 模型兜底且自己承认干不了
    is_fallback = (route or "") in ("model.fallback", "error", "")
    if _REFUSE.search(resp) and (is_fallback or not route):
        cat = "no_tool" if _TASK_HINT.search(msg) else "model_refuse"
        return {
            "category": cat,
            "signature": normalize(msg),
            "sample": msg[:300],
            "detail": resp[:400],
            "route": route or "",
            "matched": "",
        }
    return None


# ═══════════════════════════════════════════════════════════════════
# 台账 (存储 + 聚合)
# ═══════════════════════════════════════════════════════════════════

class GapLedger:
    """能力缺口台账。SQLite, 跨会话存活。"""

    def __init__(self, data_dir: Path = None, db_path: Path = None,
                 repeat_threshold: int = None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.db_path = resolve_gap_db(db_path if db_path is not None
                                      else (self.data_dir / DB_NAME))
        self.repeat_threshold = int(
            repeat_threshold if repeat_threshold is not None else REPEAT_THRESHOLD)
        self._init_db()

    # ── 存储 ──
    def _conn(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        c = self._conn()
        try:
            c.executescript(SCHEMA)
            c.commit()
        finally:
            c.close()

    # ── 记录 (唯一入口被 pipeline._on_route_done 调用) ──
    def record(self, message: str, response: str, route: str = "") -> dict | None:
        """检测并记账。不是缺口 → None; 是 → {action, id, gap}。

        同签名重复出现 → count+1 (聚合), 而不是插新行 —— 这样 /gap 一眼看出
        "哪类需求反复出现"。
        """
        gap = classify(message, response, route)
        if not gap:
            return None
        now = time.time()
        c = self._conn()
        try:
            row = c.execute(
                "SELECT id, count, status FROM gaps WHERE category=? AND signature=?",
                (gap["category"], gap["signature"])).fetchone()
            if row:
                c.execute("UPDATE gaps SET count=count+1, updated_at=?, route=? WHERE id=?",
                          (now, gap["route"], row["id"]))
                c.commit()
                return {"action": "repeated", "id": row["id"],
                        "count": row["count"] + 1, "category": gap["category"],
                        "signature": gap["signature"]}
            cur = c.execute(
                """INSERT INTO gaps (category, signature, sample, detail, route, count,
                                     status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, 'open', ?, ?)""",
                (gap["category"], gap["signature"], gap["sample"], gap["detail"],
                 gap["route"], now, now))
            c.commit()
            return {"action": "created", "id": cur.lastrowid, "count": 1,
                    "category": gap["category"], "signature": gap["signature"]}
        except Exception as e:
            logger.debug("gap record failed: %s", e)
            return None
        finally:
            c.close()

    # ── 读取 (唯一出口 cmd.gap 用) ──
    def list_gaps(self, status: str = None, category: str = None,
                  limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM gaps"
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if category:
            where.append("category=?"); args.append(category)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY count DESC, updated_at DESC LIMIT ?"
        args.append(int(limit))
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def get(self, gap_id: int) -> dict | None:
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM gaps WHERE id=?", (int(gap_id),)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def worth_building(self, limit: int = 10) -> list[dict]:
        """值得造的能力 —— 台账的核心产出。

        两条进法 (2026-09-19 补第二条):
          ① 重复出现达阈值 (count >= threshold): "出现 3 次的是真需求"
          ② status='building': **人已经明确决定要造它了** —— 这时不该还卡次数门槛
             (实测踩到: 一条缺口被 /gap rescue 启动, 因为 count=1 就从"值得造"里消失了,
              人造到一半的活从清单上掉队)
        blocked_local / built / dropped 都不算 (前者是"该出门找", 后者已了结)。
        """
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(
                """SELECT * FROM gaps
                   WHERE (count >= ? OR status = 'building')
                     AND status IN ('open', 'building')
                   ORDER BY count DESC, updated_at DESC LIMIT ?""",
                (self.repeat_threshold, int(limit)))]
        finally:
            c.close()

    def set_status(self, gap_id: int, status: str, note: str = "") -> bool:
        """allowed 状态。★ blocked_local 是「本地救不了 → 该出门找」——
        第 3 步(联网检索)的输入; 与「造出来了(built)」严格分开, 不许混。"""
        if status not in ("open", "building", "built", "dropped", "blocked_local"):
            raise ValueError(f"未知状态: {status}")
        c = self._conn()
        try:
            cur = c.execute("UPDATE gaps SET status=?, note=?, updated_at=? WHERE id=?",
                            (status, note, time.time(), int(gap_id)))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()

    def forget(self, gap_id: int) -> bool:
        c = self._conn()
        try:
            cur = c.execute("DELETE FROM gaps WHERE id=?", (int(gap_id),))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()

    def stats(self) -> dict:
        c = self._conn()
        try:
            total = c.execute("SELECT COUNT(*) FROM gaps").fetchone()[0]
            by_cat = {r[0]: r[1] for r in c.execute(
                "SELECT category, COUNT(*) FROM gaps GROUP BY category")}
            by_status = {r[0]: r[1] for r in c.execute(
                "SELECT status, COUNT(*) FROM gaps GROUP BY status")}
            total_hits = c.execute("SELECT COALESCE(SUM(count),0) FROM gaps").fetchone()[0]
            worth = c.execute(
                "SELECT COUNT(*) FROM gaps WHERE count>=? AND status IN ('open','building')",
                (self.repeat_threshold,)).fetchone()[0]
            return {"total": total, "by_category": by_cat, "by_status": by_status,
                    "total_hits": total_hits, "worth_building": worth,
                    "repeat_threshold": self.repeat_threshold}
        finally:
            c.close()

    # ── 给 /gap 的文本渲染 (纯展示, 好读优先) ──
    CAT_CN = {
        "no_tool": "没这能力",
        "missing_dep": "缺依赖",
        "no_entry": "工具没入口",
        "missing_arg": "缺参数",
        "unknown_action": "动作不认",
        "skill_failed": "技能失败",
        "model_refuse": "模型不会",
    }

    def render(self, limit: int = 12) -> str:
        st = self.stats()
        n_out = st["by_status"].get("blocked_local", 0)
        lines = [f"[能力缺口台账] 共 {st['total']} 类 · 累计 {st['total_hits']} 次"
                 f" · 值得造 {st['worth_building']} 项 (出现>={st['repeat_threshold']}次)"
                 + (f" · 该出门找 {n_out} 项" if n_out else "")]
        if not st["total"]:
            lines.append("  (还是空的 —— 本地能接住的活不会记账)")
            return "\n".join(lines)
        if st["by_category"]:
            parts = [f"{self.CAT_CN.get(k, k)} {v}" for k, v in
                     sorted(st["by_category"].items(), key=lambda x: -x[1])]
            lines.append("  分类: " + " · ".join(parts))
        worth = self.worth_building(5)
        if worth:
            lines.append("  ★ 值得造 (反复出现):")
            for g in worth:
                lines.append(f"    #{g['id']} 出现{g['count']}次 [{self.CAT_CN.get(g['category'], g['category'])}]"
                             f" {str(g['sample'])[:44]}")
        lines.append("  最近:")
        for g in self.list_gaps(limit=limit):
            flag = "★" if g["count"] >= self.repeat_threshold else " "
            lines.append(f"   {flag} #{g['id']} ×{g['count']} [{self.CAT_CN.get(g['category'], g['category'])}]"
                         f" {str(g['sample'])[:50]}")
        lines.append("  (/gap why <id> 看细节 · /gap drop <id> 丢弃 · /gap done <id> 标记已造)")
        return "\n".join(lines)


# ── 单例 ──
_ledger: GapLedger | None = None


def get_gap_ledger(data_dir: Path = None) -> GapLedger:
    global _ledger
    if _ledger is None:
        _ledger = GapLedger(data_dir)
    return _ledger


def reset_gap_ledger():
    """给测试用: 丢掉单例 (下次重新按新 data_dir 建)。"""
    global _ledger
    _ledger = None
