# -*- coding: utf-8 -*-
"""宣称-证据核对闸 (claim guard) —— 2026-09-26 立

起因 (真事, 用户当场发火)
═══════════════════════════════════════════════════════════════
用户说「桌面有个 csv, 里面有人员信息, 你再增加20个人员信息」。
TMM 回:「**已经为您在"…人员信息采集表.csv"中增加了20条新的人员信息。现在文件中
包含总共40条记录（含标题）。**」

事实: 那一轮 **一个工具都没调用** (intent=general, model=ollama, 跑了 82.74 秒),
那个文件**两天没被碰过**, 一直是 20 条。它编了一个具体到可查的数字("40条"),
用户去开文件才发现根本不是。

为什么这比"答错"更坏
───────────────────────────────────────────────────────────────
答错是能力问题; **编造一个可查的完成状态是信用问题**。用户以后没法判断
"它说做完了" 到底等不等于"真做完了" —— 那整套"单机 + 你能查"的价值就没了。

本闸做什么 (纯规则, 不调模型)
───────────────────────────────────────────────────────────────
在最外层唯一接缝 (pipeline._process_with_degrade) 上, 拿**本轮真实跑过的工具单**
去对模型的**完成宣告**:

    回复里有「主动完成宣告」 + 改动了某个文件/数据  且  本轮没有任何"能写"的工具跑过
        ⇒ 判定为**无证据的完成宣告**, 拦掉, 换成实话, 并落审计。

三条刻意的设计
───────────────────────────────────────────────────────────────
① **判官用规则, 不用模型** —— 让模型判"我是不是编的"就是让被闸对象自己发毕业证。
   这里只做字符串匹配 + 工具单查表, 判定过程可以逐行复现。
② **只管"我做了X", 不管"X是那样"** —— 前者是承诺, 后者是陈述。
   `文件里已经有性别/出生日期/籍贯三个字段` 是**陈述**(且当时为真), 不该拦;
   `已为您增加了20条` 是**承诺**, 必须有工具记录兜底。这条边界是本闸不误伤的关键。
③ **只认工具单, 不认模型自述** —— 工具单来自 gateway.turn_tools (谁跑过谁没跑过,
   系统自己记的), 模型说"我调了"不算。

误判方向: 宁可漏(放过一句真话), 不可错杀(把真做完的事说成没做)。
所以触发条件收得很紧: 必须有**第一人称完成宣告** + **写类意图** + **文件/数据目标**,
三者同时成立才拦。
"""
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("core.claim_guard")

# ── 本轮跑过这些工具 = 真的有可能改过文件 ──
WRITE_CAPABLE = {
    "file_ops",          # write/append/delete/move/copy/mkdir/patch
    "tiger_office", "office_cli",   # 中文办公三件套
    "shell_exec",        # 能写文件
    "image_gen", "chart", "artifact" , "artifacts",
    "publish_pack", "kb_import", "kb_delete", "backup",
    "memory_claim", "send_email", "sms_send",
    "whisper", "transcribe",
}

# ── 能改文件的具体动作 (file_ops 只读动作不算) ──
READONLY_ACTIONS = {"read", "list", "walk", "search", "grep", "exists", "stat"}

# ── 主动完成宣告 (第一人称 / 明确"已为您") ──
#    刻意要求第一人称或服务语气 —— 排除"文件里已有…"这种客观陈述。
CLAIM_PATTERNS = [
    r"我已(经)?\s*(为|帮|替)?",
    r"已经(为|帮|替)(您|你)",
    r"已(为您|帮你|帮您|替你|为您)",
    r"已成功",
    # ★★ 2026-09-29 补 (09-28 晚生产原文实测漏网): 「已**经**成功为您将…全部写入到 …txt 文件中」
    #   这一族 09-28 20:05:37 原样发生过 (用户说「没有写入」→ 模型编「已经成功为您将…全部写入」,
    #   本轮零写工具), 而旧词表只认**连写**的「已成功」——"已经成功"里夹了个"经" ⇒ 不命中。
    #   实测 (_probe_claim_guard_dead.py): 旧词表 check() 判 blocked=False。
    #   只加"宣告词"; 三要素 (第一人称宣告 + 改动动词 + 文件目标) 的紧箍咒一个字没松。
    r"已(经)?成功",
    r"已(经)?\s*(帮|为|替)(您|你)",
    r"已(经)?(完成|写好|写完|存好|放好|改好|弄好|搞定|处理完|办妥)",
    r"已按(要求|您的要求)",
    r"操作(已)?完成",
    r"处理(已)?完成",
    r"任务(已)?完成",
    r"已帮你",
    r"已经完成",
    r"完成了(对|这个)?(文件|表格|任务|操作)",
]
# ── 改动类动词 (必须有) ──
MUTATE_VERBS = (
    "增加", "添加", "追加", "填入", "填充", "补充", "增补", "写入", "写到", "保存到",
    "创建", "新建", "修改", "更新", "改成", "改为", "删除", "删掉", "移除", "替换",
    "生成", "导出", "导入", "重命名", "覆盖", "整理",
)
# ── 文件/数据目标 (必须有, 否则可能是在说别的事) ──
FILE_TARGETS = re.compile(
    r"(文件|表格|文档|清单|CSV|CSV|记录|数据|\.csv|\.txt|\.xlsx|\.xls|\.docx|\.doc|"
    r"\.pptx|\.pdf|\.py|\.md|\.json|桌面|路径)", re.I)


class ClaimGuard:
    """规则化的"宣称 vs 证据"核对器。"""

    def __init__(self, data_dir=None, enabled: bool = True):
        self.enabled = enabled
        self.data_dir = Path(data_dir) if data_dir else None
        import os as _os
        _ovr = _os.environ.get("TMM_CLAIM_GUARD_LOG")
        self.audit_path = (Path(_ovr) if _ovr else
                           ((self.data_dir / "claim_guard.jsonl") if self.data_dir else None))
        self.stats = {"checked": 0, "blocked": 0, "passed": 0}

    # ────────── 判定 ──────────
    def check(self, response: str, tools: list, actions: list = None) -> dict:
        """返回 {blocked: bool, reason: str, claim: str}。

        tools   —— 本轮真实跑过的工具名 (来自 gateway.turn_tools)
        actions —— 本轮工具的具体动作 (可选, 用于排除只读的文件操作)
        """
        self.stats["checked"] += 1
        text = str(response or "")
        if not text.strip():
            return self._pass()

        claim = self._find_claim(text)
        if not claim:
            return self._pass()

        if not FILE_TARGETS.search(text):
            return self._pass()          # 不是在说文件, 不拦

        if self._did_write(tools, actions):
            return self._pass()

        # ── 走到这里: 宣告改了文件, 但本轮没有任何能写的东西跑过 ──
        self.stats["blocked"] += 1
        reason = (f"完成宣告「{claim}」但本轮无写操作 "
                  f"(本轮工具: {list(tools) or '空'})")
        self._audit(text, claim, tools, reason)
        logger.warning("claim_guard 拦下无证据的完成宣告: %s", reason)
        return {"blocked": True, "reason": reason, "claim": claim}

    # ────────── 内部 ──────────
    @staticmethod
    def _pass():
        return {"blocked": False, "reason": "", "claim": ""}

    def _find_claim(self, text: str) -> str:
        """找第一处"主动完成宣告 + 改动动词"的邻接片段。"""
        for pat in CLAIM_PATTERNS:
            for m in re.finditer(pat, text):
                # 宣告之后 40 字内必须有改动动词 (宣告与动作常常挨着说)
                win = text[m.start(): m.start() + 60]
                for v in MUTATE_VERBS:
                    if v in win:
                        return (m.group(0) + "…" + v)[:40]
        return ""

    @staticmethod
    def _did_write(tools: list, actions: list) -> bool:
        names = [str(t) for t in (tools or [])]
        if not names:
            return False
        wrote = [n for n in names if n in WRITE_CAPABLE]
        if not wrote:
            return False
        # file_ops 单独看动作 —— 只读的 file_ops 不算写
        acts = [str(a).lower() for a in (actions or [])]
        if wrote == ["file_ops"] and acts and all(a in READONLY_ACTIONS for a in acts):
            return False
        return True

    def _audit(self, text, claim, tools, reason):
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(), "claim": claim, "reason": reason,
                    "tools": list(tools or []),
                    "response_head": str(text)[:400],
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("claim_guard 审计写入失败: %s", e)

    def honest_replacement(self, claim: str, response: str) -> str:
        """把编造的完成宣告换成实话 —— 不删模型的其他内容, 但把结论纠正过来。"""
        return (
            "⚠ 我上一句说「%s」**没有依据** —— 本轮没有真的跑任何写操作。\n"
            "（这一段是系统核对后替你挡下的，原文已记入审计。）\n\n"
            "**请当作「我没做」处理。** 要我继续的话，我先把文件读出来确认现状，"
            "再动手改，改完把行数核对结果给你。\n\n"
            "—— 以下是我原本要说的话，请**只当参考、不要当成已完成** ——\n\n%s"
            % (claim, str(response or "").strip())
        )


_G = None


def get_claim_guard(data_dir=None) -> ClaimGuard:
    global _G
    if _G is None:
        if data_dir is None:
            try:
                import config.settings as _st
                data_dir = getattr(_st, "DATA_DIR", None)
            except Exception:
                data_dir = None
        _G = ClaimGuard(data_dir)
    return _G


def reset_claim_guard():
    global _G
    _G = None
