# -*- coding: utf-8 -*-
"""重复-无进展闸 (repeat guard) —— 2026-09-26 立

起因 (真事)
═══════════════════════════════════════════════════════════════
用户为了"往 csv 里加 20 条人员" 连发了 **6 条**消息 (换着说法):
    …你再增加20个人员信息 / "…csv" 增加20条人员信息 / 增加20条人员信息"…csv"
    / 在"…csv"增加20条人员信息 / "…csv" 增加 / "…csv" 增加人员信息
**6 次全部走了同一条路** (intent=file_read, 0.06 秒, 把文件内容回显), 6 次一模一样。

人在第 3 次就该收到的是「我卡住了, 这条路不对」, 而不是第 6 份同样的文件内容。
反复给同一个坏答案, 比第一次答错更伤人 —— 它让用户以为"再说清楚一点就能成",
于是白费 6 轮。

本闸做什么 (纯规则)
───────────────────────────────────────────────────────────────
按 **(目标对象, 动作类别)** 聚合同一会话里的请求:
    · 同一条请求第 2 次出现, 且上次**没有产生成功的写/改动作** ⇒ 标记"卡住了"
    · 第 3 次再出现 ⇒ 强制在回复前面加一段实话, 点明"前面 N 次都是同一条路,
      没成, 别再让你重复了", 并给出换路的选项。
同时把这条卡点落审计 (`data/repeat_guard.jsonl`), 供事后排查。

指纹 = 目标 + 动作 + **事由** (三者缺一不可)
───────────────────────────────────────────────────────────────
只按"目标+动作"会误伤: 往同一个 out.docx 里写会议纪要 / 项目计划书 / 求职简历
是三件不同的事, 但目标和动作一样 ⇒ 被并成一件, 第 N 条正常请求被拦 (真被拦过)。
按原话去重又一条都抓不到 (事故里 6 条换了 6 种说法)。所以取中间: 剥掉路径/动作/
计数/虚词/泛指名词, 剩下的"事由"进指纹 —— 事故那 6 条剥完都是空, 照旧并成一件;
不同事由则分开算。

为什么按"目标+动作"聚合而不是按原话
───────────────────────────────────────────────────────────────
用户会换说法 ("增加20条人员信息" / "在这增加" / "增加人员信息") —— 按原话去重
一条也抓不到。真正不变的是**他在动哪个文件**和**他要干什么**。

边界: 只标记, 不擅自改用户的请求; 第 3 次也只是把话说清楚, 不替用户做决定。
"""
import json
import logging
import re
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger("core.repeat_guard")

# 改类动词 → 统一动作类别 (用于聚合)
_ACT_CLASSES = [
    ("append",  ("增加", "添加", "追加", "填入", "填充", "补充", "增补", "多加")),
    ("write",   ("写入", "创建", "新建", "保存", "写到", "存到", "放到")),
    ("modify",  ("修改", "更新", "改成", "改为", "替换", "修正")),
    ("delete",  ("删除", "删掉", "移除", "清理")),
    ("read",    ("读取", "打开", "看看", "看一下", "内容")),
    ("send",    ("发给", "发送", "邮件", "通知")),
]

_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s，。；;\"']+")


def _norm_target(msg: str) -> str:
    """抽出"在动哪个东西" —— 优先完整路径, 退而求其次用文件名。"""
    m = _PATH_RE.search(msg or "")
    if m:
        p = m.group(0).rstrip("，。；;、")
        # 统一分隔符/大小写, 免得 D:\A\b.csv 与 d:/a/B.CSV 被当成两回事
        return p.replace("/", "\\").lower()
    m2 = re.search(r"([\w\u4e00-\u9fa5\-]+\.(?:csv|txt|xlsx|xls|docx|doc|pptx|pdf|py|md|json))",
                   msg or "", re.I)
    return m2.group(1).lower() if m2 else ""


#: 主题剥离时要去掉的虚词/泛指词 —— 只用于**判定**, 不影响原话
_SUBJ_STOP = (
    "帮我", "请", "麻烦", "你", "我", "再", "又", "一下", "这个", "那个", "这", "那",
    "把", "将", "给", "在", "往", "到", "里", "里面", "面", "上", "下", "中", "的", "了",
    "个", "条", "行", "列", "份", "组", "些", "点", "多", "次",
    "人员信息", "人员", "信息", "数据", "记录", "条目", "资料", "内容", "文本",
    "示例", "东西", "东西", "文件", "文件里", "csv", "excel", "表格", "这份", "那份",
)


def _norm_subject(msg: str) -> str:
    """抽"这条请求到底在说什么事" —— 用来把**不同的事**分开。

    ★ 2026-09-26 真引擎抓出来的误判: 第一版指纹只有 `目标|动作`, 于是
      「帮我写一份生成会议纪要，存到 …out.docx」和
      「帮我写一份项目计划书，存到 …out.docx」
      被当成**同一件事** —— 第 14 条消息时闸门跳出来说"你说了 14 次我都做不成",
      把一条完全正常的请求拦掉了。
    教训: "同一个文件" 不等于 "同一件事"。真正要抓的是「**说法在变、事情没变**」
    (用户原话 6 条换着说法要往同一个 csv 加人员), 所以他说的**事由**必须进指纹。
    判据就是这里: 把路径/动作/计数/虚词/泛指名词全剥掉, 剩下的"事由"。
    剩下空 ⇒ 用户只是在指同一个对象+同一个动作 (事故那种) ⇒ 并成一件;
    剩下有别的内容 ⇒ 是不同的事 ⇒ 分开算, 不许互相计数。
    """
    s_ = msg or ""
    m = _PATH_RE.search(s_)
    if m:
        s_ = s_.replace(m.group(0), " ")
    s_ = re.sub(r"[\w\u4e00-\u9fa5\-]+\.(?:csv|txt|xlsx|xls|docx|doc|pptx|pdf|py|md|json)",
                " ", s_, flags=re.I)
    s_ = s_.lower()
    for _name, words in _ACT_CLASSES:
        for w in words:
            s_ = s_.replace(w, " ")
    s_ = re.sub(r"\d{1,4}\s*(?:条|个|行|列|份|组)", " ", s_)
    for w in _SUBJ_STOP:
        s_ = s_.replace(w, " ")
    s_ = re.sub(r"[\s，。；;、：:,.!！?？\"\'“”‘’()（）\[\]【】|/\\-]+", "", s_)
    return s_[:24]


def _norm_action(msg: str) -> str:
    low = (msg or "").lower()
    for name, words in _ACT_CLASSES:
        if any(w in low for w in words):
            return name
    return ""


class RepeatGuard:
    def __init__(self, data_dir=None, threshold: int = 2, enabled: bool = True):
        self.enabled = enabled
        self.threshold = threshold          # 第 2 次起算"卡住", 第 3 次起强制提示
        self.data_dir = Path(data_dir) if data_dir else None
        import os as _os
        _ovr = _os.environ.get("TMM_REPEAT_GUARD_LOG")
        self.audit_path = (Path(_ovr) if _ovr else
                           ((self.data_dir / "repeat_guard.jsonl") if self.data_dir else None))
        self._seen = {}                     # key -> [次数, 首次时间, 是否成功过]
        self.stats = {"counted": 0, "warned": 0, "noted": 0}

    # ────────── 记一次请求 ──────────
    def note(self, message: str, wrote_ok: bool = False) -> dict:
        """登记一条用户请求。返回 {stuck, times, key, action, target}。

        wrote_ok: 上一轮是否真的产生了成功的写/改动作。
        一旦成功过, 计数清零 —— 我们要抓的是"没成还在重复", 不是"用户爱问同件事"。
        """
        if not self.enabled:
            return {"stuck": False, "times": 0, "key": "", "action": "", "target": "",
                    "subject": ""}
        target, action, subject = _norm_target(message), _norm_action(message), _norm_subject(message)
        if not (target and action):
            return {"stuck": False, "times": 0, "key": "", "action": action,
                    "target": target, "subject": ""}

        # ★ 指纹 = 目标 + 动作 + **事由**。少任何一项都会误判 (见 _norm_subject 注释)
        key = f"{target}|{action}|{subject or '-'}"
        rec = self._seen.get(key)
        if rec is None:
            self._seen[key] = {"n": 1, "first": time.time(), "ok": bool(wrote_ok)}
            self.stats["counted"] += 1
            return {"stuck": False, "times": 1, "key": key, "action": action,
                    "target": target, "subject": subject}

        if wrote_ok:
            rec["n"], rec["ok"] = 1, True       # 成了 → 这条线结束, 重新开始数
            return {"stuck": False, "times": 1, "key": key, "action": action,
                    "target": target, "subject": subject}

        rec["n"] += 1
        self.stats["counted"] += 1
        stuck = rec["n"] >= self.threshold
        if stuck:
            if rec["n"] == self.threshold:
                self.stats["noted"] += 1
            else:
                self.stats["warned"] += 1
            self._audit(key, rec["n"], action, target)
            logger.info("repeat_guard: %s 第 %d 次, 仍未成功", key, rec["n"])
        return {"stuck": stuck, "times": rec["n"], "key": key,
                "action": action, "target": target, "subject": subject}

    # ────────── 给回复加一段实话 ──────────
    def banner(self, info: dict) -> str:
        n = info.get("times") or 0
        act_cn = {"append": "往里面加内容", "write": "写/新建", "modify": "改动",
                  "delete": "删东西", "read": "看内容", "send": "发出去"}.get(
                      info.get("action") or "", "这件事")
        tgt = info.get("target") or "这个对象"
        head = (
            "⚠ **同一件事你已经说了 %d 次，我前 %d 次都没做成。**\n"
            "`%s` → 目标: `%s`\n"
            "别再用同样的说法再问一次了 —— 那不是你没说清，是我这条路走不通。\n"
            % (n, n - 1, act_cn, tgt)
        )
        if n >= 3:
            head += (
                "**我先把话说清楚: 我卡在「%s」这一步。** 请你二选一（回一个字就行）:\n"
                "· 回 **1** —— 让我先把文件读出来给你看，确认现状后再动手；\n"
                "· 回 **2** —— 你直接把要加的内容贴给我，我只负责写进去，写完报行数。\n"
                % act_cn
            )
        else:
            head += "我换个走法：先读出现状确认，再动手。（下面是我这次的答复）\n"
        return head + "\n---\n"

    def _audit(self, key, n, action, target):
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "key": key, "times": n,
                                     "action": action, "target": target},
                                    ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("repeat_guard 审计写入失败: %s", e)

    def forget(self, key: str = ""):
        if key:
            self._seen.pop(key, None)
        else:
            self._seen.clear()


_G = None


def get_repeat_guard(data_dir=None) -> RepeatGuard:
    global _G
    if _G is None:
        if data_dir is None:
            try:
                import config.settings as _st
                data_dir = getattr(_st, "DATA_DIR", None)
            except Exception:
                data_dir = None
        _G = RepeatGuard(data_dir)
    return _G


def reset_repeat_guard():
    global _G
    _G = None
