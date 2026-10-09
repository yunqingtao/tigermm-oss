# -*- coding: utf-8 -*-
"""目标硬闸 —— 「没做完不许收尾」, 判官是**规则**不是模型 (2026-09-26 立, 「八件」第 3 条)

为什么有
═══════════════════════════════════════════════════════════════
    Claude Code: 判官逐轮核对, 没做完不许收尾, 预算耗尽**挂起等人**
    TMM: 只有观察者"提醒", 没有闸。

今天补的两个 (claim_guard / repeat_guard) 是**事后抓**: 抓"编的话"、抓"同一件事反复无进展"。
本条是**正主**: 目标没达成就不许挂"完成", 预算烧完就**挂起等人**。

判官为什么必须是规则
───────────────────────────────────────────────────────────────
让模型判"我做完了吗"= 自己给自己发毕业证 (用户定的底线: 不许审批裁判用模型)。
所以**只有可机检的终点**才配装这个闸:
    · 文件真存在 / 行数够 / 数据行够 / 内容含某串 / 哈希对上
    · 某道门禁真跑过 (解析 "结果: N PASS / M FAIL")
开放式目标 (写得好不好、用户满不满意) **不许**硬塞进这里 —— 那会变成橡皮图章。

三种结局, 只有一种是"完成"
───────────────────────────────────────────────────────────────
    done       所有判据都真通过 (带证据)
    continue   没过, 预算还有 → 说清"还差什么", 继续
    suspended  预算烧完 → **挂起等人**, 附"试了几轮/还差什么/试过哪些路"
  ★ 永远没有第四种"我说我完成了"。suspended 的文案里不许出现"完成/已搞定"。

触发面 (诚实: 这里就是它的入口)
───────────────────────────────────────────────────────────────
    data/goal_specs.json    写一个目标 + 判据 → 下一轮起生效, 直到判据全过或被撤下
主 agent 与子 agent 都会在**收尾那一刻**被它拦一次。没有目标 = 行为与从前完全一致
(加性, 旧路径一行不改)。

    data/goal_specs.json 例子:
    {"id":"加20条","max_turns":5,
     "specs":[{"kind":"file_data_rows_min","path":"C:/x/人员.csv","n":40}],
     "note":"用户要 20 条现有 20 条"}
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("core.goal_guard")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 允许的判据类型 (每一条都是**可机检**的; 新加类型必须同时给出反面用例)
SPEC_KINDS = (
    "file_exists",         # {"path"}
    "file_lines_min",      # {"path","n"}  物理行数 (含表头)
    "file_data_rows_min",  # {"path","n"}  数据行数 (不含表头; csv/tsv)
    "text_contains",       # {"path","text"}
    "file_sha",            # {"path","sha"}  精确指纹
    "gate_pass",           # {"script"}  跑 scripts/verification/<script> 并解析结果行
    "dir_file_count_min",  # {"path","n"}
)


def _rows(path: Path) -> int:
    try:
        txt = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return -1
    lines = [l for l in txt.splitlines() if l.strip()]
    return max(0, len(lines) - 1)          # 减表头


def check_one(spec: dict) -> tuple[bool, str]:
    """判一条。返回 (达成?, 人话说明)。**纯规则, 不调模型不联网。**"""
    kind = str(spec.get("kind") or "")
    if kind not in SPEC_KINDS:
        return False, f"判据类型不认识: {kind} (可用: {', '.join(SPEC_KINDS)})"
    try:
        if kind == "file_exists":
            p = Path(str(spec.get("path")))
            return p.exists(), f"{p.name} {'在' if p.exists() else '不在'}"
        if kind == "file_lines_min":
            p = Path(str(spec.get("path")))
            n = int(spec.get("n") or 0)
            got = len(p.read_text(encoding="utf-8-sig", errors="replace").splitlines()) if p.exists() else -1
            return got >= n, f"行数 {got} (要 ≥{n})"
        if kind == "file_data_rows_min":
            p = Path(str(spec.get("path")))
            n = int(spec.get("n") or 0)
            got = _rows(p) if p.exists() else -1
            return got >= n, f"数据行 {got} (要 ≥{n})"
        if kind == "text_contains":
            p = Path(str(spec.get("path")))
            want = str(spec.get("text") or "")
            got = want in p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else False
            return got, f"含「{want[:20]}」={got}"
        if kind == "file_sha":
            p = Path(str(spec.get("path")))
            if not p.exists():
                return False, "文件不在"
            h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            return h == str(spec.get("sha")), f"sha {h}"
        if kind == "dir_file_count_min":
            p = Path(str(spec.get("path")))
            n = int(spec.get("n") or 0)
            got = len([x for x in p.glob("*") if x.is_file()]) if p.exists() else -1
            return got >= n, f"文件数 {got} (要 ≥{n})"
        if kind == "gate_pass":
            sc = PROJECT_ROOT / "scripts" / "verification" / str(spec.get("script"))
            if not sc.exists():
                return False, f"门禁脚本不在: {sc.name}"
            r = subprocess.run([sys.executable, "-B", str(sc)], cwd=str(PROJECT_ROOT),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=580)
            m = re.search(r"结果:\s*(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL", (r.stdout or ""))
            if not m:
                return False, f"{sc.name} 没跑出结果行"
            fail = int(m.group(2))
            return fail == 0, f"{sc.name}: {m.group(1)} PASS / {fail} FAIL"
    except Exception as e:                                  # noqa: BLE001
        return False, f"判据执行出错 ({type(e).__name__}: {e})"
    return False, "判据没写全"


class GoalGuard:
    """目标账本 + 判官。只做判定与"该不该收尾", 不替 agent 干活。"""

    def __init__(self, specs_file: Path | str | None = None, log: Path | str | None = None):
        ovr = os.environ.get("TMM_GOAL_SPECS")
        self.specs_file = Path(ovr) if ovr else (Path(specs_file) if specs_file
                                                 else PROJECT_ROOT / "data" / "goal_specs.json")
        lovr = os.environ.get("TMM_GOAL_LOG")
        self.log = Path(lovr) if lovr else (PROJECT_ROOT / "data" / "goal_loop.jsonl")
        self.stats = {"checked": 0, "done": 0, "continue": 0, "suspended": 0}

    # ────────── 读目标 ──────────
    def goals(self) -> list[dict]:
        if not self.specs_file.exists():
            return []
        try:
            d = json.loads(self.specs_file.read_text(encoding="utf-8"))
        except Exception as e:                              # noqa: BLE001
            logger.debug("goal_specs 读不出来: %s", e)
            return []
        if isinstance(d, dict) and "specs" in d:
            d = [d]
        return [g for g in (d or []) if isinstance(g, dict) and g.get("specs")]

    def set_goal(self, goal: dict) -> dict:
        gs = [g for g in self.goals() if g.get("id") != goal.get("id")]
        gs.append(goal)
        self.specs_file.parent.mkdir(parents=True, exist_ok=True)
        self.specs_file.write_text(json.dumps(gs, ensure_ascii=False, indent=1), encoding="utf-8")
        return goal

    def clear(self, goal_id: str = "") -> int:
        gs = self.goals()
        keep = [g for g in gs if g.get("id") != goal_id] if goal_id else []
        self.specs_file.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        return len(gs) - len(keep)

    # ────────── 判一个目标 ──────────
    def evaluate(self, goal: dict) -> dict:
        """返回 {id, done, unmet:[人话], met:[人话], turns, max_turns, status}"""
        specs = goal.get("specs") or []
        met, unmet = [], []
        for sp in specs:
            ok, why = check_one(sp)
            (met if ok else unmet).append(why)
        done = not unmet and bool(specs)
        turns = int(goal.get("turns") or 0)
        max_turns = int(goal.get("max_turns") or 5)
        status = "done" if done else ("suspended" if turns >= max_turns else "continue")
        self.stats["checked"] += 1
        self.stats[status] = self.stats.get(status, 0) + 1
        return {"id": goal.get("id") or "未命名", "done": done, "met": met, "unmet": unmet,
                "turns": turns, "max_turns": max_turns, "status": status,
                "tried": list(goal.get("tried") or [])[-5:]}

    def check_all(self) -> list[dict]:
        return [self.evaluate(g) for g in self.goals()]

    # ────────── 收尾那一刻的裁决 ──────────
    def verdict_for_turn(self, response: str) -> tuple[bool, str, dict]:
        """主链收尾前问一句: 这一轮能不能就这么交出去?

        返回 (要不要改回复, 新回复, 明细)。
        规则:
          · 没有目标                    → 不动 (加性, 旧行为)
          · 目标全过                    → 不动 (做完了随便说)
          · 没过 + 预算还有             → **追加**一段"还没做到: …", 不许出现完成宣告
          · 没过 + 预算烧完             → **替换**成"挂起等人"文案 (仍不许说完成)
        """
        goals = self.goals()
        if not goals:
            return False, response, {}
        evaluated = [self.evaluate(g) for g in goals]
        open_ones = [e for e in evaluated if not e["done"]]
        if not open_ones:
            return False, response, {"done": [e["id"] for e in evaluated]}

        hard = [e for e in open_ones if e["status"] == "suspended"]
        lines = []
        for e in open_ones:
            lines.append(f"· 「{e['id']}」还差: {'; '.join(e['unmet'])}")
        if hard:
            self.stats["suspended"] = self.stats.get("suspended", 0) + 1
            head = ("⚠ **这件事我没做完, 挂起等你。** (不许我自己宣布完成)\n"
                    f"试了 {hard[0]['turns']}/{hard[0]['max_turns']} 轮, 判据还是没过:\n")
            tail = ("\n要我接着跑就回「继续」(会再加预算); 想换路子就直接说要怎么改。\n"
                    "_下面是我这一轮说过的话 —— **未经核实**, 判据没过, 别当结果看。_\n---\n")
            body = _strip_done_claims(str(response or ""))
            return True, head + "\n".join(lines) + tail + body, {"suspended": [e["id"] for e in hard]}
        self.stats["continue"] = self.stats.get("continue", 0) + 1
        note = ("ℹ 这一轮**还没做到**目标 (不是完成):\n" + "\n".join(lines)
                + "\n(预算还没烧完, 下一轮接着来。)\n"
                + "_下面是我这一轮说过的话 —— **未经核实**, 判据没过, 别当结果看。_\n---\n")
        return True, note + _strip_done_claims(str(response or "")), {"continue": [e["id"] for e in open_ones]}

    def record_turn(self, goal_id: str, note: str = "") -> None:
        """给目标记一轮 + 落审计 (与其它账本同一套做法: 失败只是少一条凭据)。"""
        try:
            gs = self.goals()
            for g in gs:
                if g.get("id") == goal_id:
                    g["turns"] = int(g.get("turns") or 0) + 1
                    if note:
                        g.setdefault("tried", []).append(_trunc(note, 80))
            self.specs_file.write_text(json.dumps(gs, ensure_ascii=False, indent=1), encoding="utf-8")
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": round(time.time(), 3), "goal": goal_id,
                                     "note": _trunc(note, 200)}, ensure_ascii=False) + "\n")
        except Exception as e:                              # noqa: BLE001
            logger.debug("goal_guard 记账失败: %s", e)


def _trunc(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


#: "宣称完成"的措辞 —— 目标没过的时候一律不许出现。
#   ★ 这条表是**门禁 D2 逼出来的**: 第一版只列了"已经为您/已完成"这种抬头,
#     结果 "…增加了20条…" 原样留着 —— 而 D2 要的是"这条回复里不许有完成宣告"。
#     教训: 剥"完成宣告"要剥到**结果断言**那一层 (增加了/写好了/已写入), 不是剥抬头词。
_DONE_WORDS = ("已完成", "已经完成", "已搞定", "搞定了", "已完成全部", "全部完成",
               "已经为您", "已经帮你", "成功增加", "已成功", "已添加", "已写入并核对无误",
               "增加了", "增加了20条", "已经增加", "写好了", "已写好", "已写入",
               "已保存", "搞定", "已经改好", "已修复", "已处理完")


def _strip_done_claims(text: str) -> str:
    """把回复里"我完成了"的措辞换掉 —— 目标没过就没有完成可言。"""
    out = text
    hits = [w for w in _DONE_WORDS if w in out]
    for w in hits:
        out = out.replace(w, "〔**未完成**, 目标判据还没过〕")
    return out


_G = None


def get_goal_guard() -> GoalGuard:
    global _G
    if _G is None:
        _G = GoalGuard()
    return _G


def reset_goal_guard():
    global _G
    _G = None
