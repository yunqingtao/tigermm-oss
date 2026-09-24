"""能力缺口台账 (core/gap_ledger.py) 的测试。

守四组:
  A 纯函数 classify —— 真实失败文案必须认出来; 正常问答**不得**误记 (账本不能全是噪音)
  B normalize —— 同类缺口要聚成一个签名 (路径/数字不同也算同一类)
  C 台账 —— 记录/聚合(count)/值得造(阈值)/状态流转/删除
  D 硬不变量 —— probe 不记账 (由 pipeline 侧保证, 这里守 classify 的纯性 + 斜杠命令不记)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.gap_ledger import (GapLedger, classify, normalize, REPEAT_THRESHOLD)


# ───────────────────── A. classify: 该认的认, 不该认的不认 ─────────────────────

class TestClassifyPositive:
    """信号全部取自**代码里真实产生的文案** (实测样本)。"""

    def test_missing_dep(self):
        g = classify("帮我看看这张图里的字", "✗ ocr: No module named 'pytesseract'", "tool")
        assert g and g["category"] == "missing_dep", g

    def test_missing_dep_binary(self):
        g = classify("识别图片", "找不到 tesseract 可执行文件", "tool")
        assert g and g["category"] == "missing_dep", g

    def test_missing_dep_command_not_recognized(self):
        g = classify("跑个脚本", "'ffmpeg' is not recognized as an internal command", "tool")
        assert g and g["category"] == "missing_dep", g

    def test_no_entry(self):
        g = classify("调用 office_cli", "工具 'office_cli' 没有可调用入口 (需要 run()/execute()), 未执行", "skill")
        assert g and g["category"] == "no_entry", g

    def test_missing_arg(self):
        """技能该提到的参数没提到 (且**没有**在追问用户) → 真缺口。"""
        g = classify("发个文件给涛哥", "✗ send-file-to-contact 缺必填参数: file", "skill")
        assert g and g["category"] == "missing_arg", g

    def test_unknown_action(self):
        g = classify("生成表格", "Unknown action: write_excel", "tool")
        assert g and g["category"] == "unknown_action", g

    def test_unknown_tool(self):
        g = classify("记住这事", "✗ 记忆管理 没执行成功: 未知工具: memory", "brain")
        assert g and g["category"] == "unknown_action", g

    def test_skill_failed(self):
        g = classify("画个架构图", "✗ diagram 没执行成功: 第 1 步(dsl) 缺输入", "brain")
        assert g and g["category"] == "skill_failed", g

    def test_model_refuse_task(self):
        g = classify("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert g and g["category"] == "no_tool", g

    def test_model_refuse_question(self):
        """纯知识缺口归 model_refuse (这类交给 learn_loop 的待学池, 但也要有账)。"""
        g = classify("量子纠缠的最新进展是什么", "我无法回答这个问题。", "model.fallback")
        assert g and g["category"] == "model_refuse", g

    def test_matched_reason_kept(self):
        g = classify("识别图片", "No module named 'pytesseract'", "tool")
        assert g["matched"], "要留下命中的原因 (便于人看)" 
        assert "No module named" in g["matched"]


class TestAskInputNotGap:
    """★ 实测挖出的 over-eager: 技能**正常追问用户要输入**不是能力缺口。
    (第一次接完线真跑, 就把 `✗ look-at-image ... 没找到图片。请给出图片路径, 例如: ...`
     记成了缺口 —— 那是技能干得对。台账要是被这种灌满, 等于没有。)
    """

    def test_skill_asking_for_image_path(self):
        resp = ("✗ look-at-image 第 1 步(look) 失败: 没找到图片。"
                "请给出图片路径, 例如: 看看这张图 D:\\a.png")
        assert classify("帮我识别这张图里的文字", resp, "skill.trigger_first") is None

    def test_skill_asking_via_bu_shang_zai_shi(self):
        resp = "✗ send-file-to-contact 缺必填参数: file、recipient。补上再试。"
        assert classify("把文件发给涛哥", resp, "skill.trigger_first") is None

    def test_bare_file_not_found_is_user_error(self):
        """★ 裸'文件不存在'是用户给错路径 (用户问题), 不是能力缺口。"""
        assert classify("转写这个音频", "文件不存在: D:\\tmp\\v.mp3", "tool") is None

    def test_hard_evidence_survives_ask_filter(self):
        """硬证据 (缺依赖) 即使句子里带'请给出'也照记 —— 它确实是干不了。"""
        g = classify("识别图片", "No module named 'pytesseract'。请先安装再试。", "tool")
        assert g and g["category"] == "missing_dep", g

    def test_unknown_action_survives_ask_filter(self):
        g = classify("生成表格", "Unknown action: write_excel。请给出正确的 action", "tool")
        assert g and g["category"] == "unknown_action", g


class TestClassifyNegative:
    """★ 关键: 正常对话**不得**记账 —— 否则台账全是噪音, 等于没有。"""

    def test_normal_answer(self):
        assert classify("1+1等于几", "1+1=2。", "model.fallback") is None

    def test_greeting(self):
        assert classify("你好", "你好！虎哥在此，有事儿你说。", "brain.route") is None

    def test_success_response(self):
        assert classify("写份周报", "已写入 D:/r.docx", "skill") is None

    def test_slash_command_not_recorded(self):
        """斜杠命令的内部提示 (用法问题) 不算能力缺口。"""
        assert classify("/gap", "用法: /gap [why|drop|done] <id>", "cmd.gap") is None

    def test_empty(self):
        assert classify("", "随便什么", "model.fallback") is None

    def test_refusal_but_not_fallback_route(self):
        """非兜底路由说"无法"多半是业务话术 (比如 ask 模式的解释), 不记账。"""
        assert classify("帮我删库", "抱歉，我无法执行该操作。", "regulator.dangerous") is None

    def test_pure_function_purity(self):
        """classify 必须纯 —— 连调三次结果一致 (不依赖任何状态)。"""
        args = ("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        a, b, c = classify(*args), classify(*args), classify(*args)
        assert a == b == c


# ───────────────────── B. normalize: 同类聚成一个签名 ─────────────────────

class TestNormalize:
    def test_paths_collapse(self):
        a = normalize("写个报告存到 D:/工作/2026/周报.docx")
        b = normalize("写个报告存到 C:/另一处/月报.docx")
        assert a == b, (a, b)

    def test_numbers_collapse(self):
        assert normalize("下载第 3 集") == normalize("下载第 12 集")

    def test_posix_path_collapse(self):
        assert normalize("存到 /home/u/a/b.txt") == normalize("存到 /var/tmp/c.txt")

    def test_quote_stripped(self):
        assert normalize('保存"报告"') == normalize("保存报告")

    def test_length_capped(self):
        assert len(normalize("很长的需求" * 50)) <= 80

    def test_different_needs_stay_different(self):
        assert normalize("帮我做个视频") != normalize("帮我写份周报")


# ───────────────────── C. 台账: 记录 / 聚合 / 状态 ─────────────────────

@pytest.fixture
def led(tmp_path):
    return GapLedger(db_path=tmp_path / "gap.db")


class TestLedger:
    def test_record_creates(self, led):
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert r and r["action"] == "created" and r["count"] == 1, r
        assert led.stats()["total"] == 1

    def test_same_gap_aggregates(self, led):
        """★ 同一类缺口重复出现 → count 累加, 不插新行 (台账的核心价值)。"""
        for _ in range(3):
            led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        st = led.stats()
        assert st["total"] == 1, f"同类应聚成 1 行: {led.list_gaps()}"
        assert st["total_hits"] == 3
        assert led.list_gaps()[0]["count"] == 3

    def test_different_path_same_gap(self, led):
        """路径不同但需求同类 → 同一个签名 (= 同一行)。"""
        led.record("写报告存到 D:/a/x.docx", "No module named 'docx'", "tool")
        led.record("写报告存到 C:/b/y.docx", "No module named 'docx'", "tool")
        assert led.stats()["total"] == 1, led.list_gaps()

    def test_non_gap_not_recorded(self, led):
        assert led.record("1+1等于几", "1+1=2。", "model.fallback") is None
        assert led.stats()["total"] == 0

    def test_worth_building_threshold(self, led):
        """★ 出现 >= 3 次 = 真需求 (阈值)。1~2 次不算。"""
        for _ in range(2):
            led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert led.worth_building() == [], "2 次不该进值得造"
        led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        wb = led.worth_building()
        assert len(wb) == 1 and wb[0]["count"] == 3, wb

    def test_status_flow(self, led):
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        gid = r["id"]
        assert led.set_status(gid, "building", "已排期") is True
        assert led.get(gid)["status"] == "building"
        assert led.set_status(gid, "built", "已造") is True
        assert led.get(gid)["status"] == "built"
        # 造出来的不再算"值得造"
        assert led.worth_building() == []

    def test_status_rejects_unknown(self, led):
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        with pytest.raises(ValueError):
            led.set_status(r["id"], "乱填")

    def test_dropped_not_in_worth(self, led):
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        for _ in range(3):
            led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        led.set_status(r["id"], "dropped", "不值得")
        assert led.worth_building() == []

    def test_forget(self, led):
        r = led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert led.forget(r["id"]) is True
        assert led.stats()["total"] == 0

    def test_sample_and_detail_kept(self, led):
        msg = "帮我做个视频"
        led.record(msg, "抱歉，我无法制作视频。", "model.fallback")
        g = led.list_gaps()[0]
        assert g["sample"] == msg and "无法" in g["detail"], g

    def test_persistence_across_instances(self, tmp_path):
        p = tmp_path / "gap.db"
        GapLedger(db_path=p).record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert GapLedger(db_path=p).stats()["total"] == 1

    def test_by_category_filter(self, led):
        led.record("识别图片", "No module named 'pytesseract'", "tool")
        led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        gaps = led.list_gaps(category="missing_dep")
        assert len(gaps) == 1 and gaps[0]["category"] == "missing_dep"

    def test_render_smoke(self, led):
        assert "空" in led.render()
        led.record("识别图片", "No module named 'pytesseract'", "tool")
        txt = led.render()
        assert "缺依赖" in txt and "识别图片" in txt, txt

    def test_threshold_configurable(self, tmp_path):
        led = GapLedger(db_path=tmp_path / "g.db", repeat_threshold=1)
        led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        assert len(led.worth_building()) == 1

    def test_default_threshold_is_3(self):
        assert REPEAT_THRESHOLD == 3

# ───────────────────── D. 隔离语义 (门禁/测试不写用户台账) ─────────────────────

class TestGapDbRedirect:
    """与 session_store 同一套规则 (那块踩过两轮, 这次一开始就按对的写):

      ① 不传 db_path (默认)         → 重定向
      ② 传的**就是**真实台账        → 重定向 (pipeline 用 get_gap_ledger(DATA_DIR))
      ③ 传别的路径 (测试临时库)      → 不动
    """

    REAL = Path(__file__).resolve().parent.parent / "data" / "gap_ledger.db"

    def test_default_redirected(self, tmp_path, monkeypatch):
        from core.gap_ledger import resolve_gap_db
        monkeypatch.setenv("TMM_GAP_DB", str(tmp_path / "gate.db"))
        assert resolve_gap_db().resolve() == (tmp_path / "gate.db").resolve()

    def test_explicit_real_redirected(self, tmp_path, monkeypatch):
        from core.gap_ledger import resolve_gap_db
        monkeypatch.setenv("TMM_GAP_DB", str(tmp_path / "gate.db"))
        assert resolve_gap_db(self.REAL).resolve() == (tmp_path / "gate.db").resolve()

    def test_explicit_other_untouched(self, tmp_path, monkeypatch):
        from core.gap_ledger import resolve_gap_db
        own = (tmp_path / "own.db").resolve()
        monkeypatch.setenv("TMM_GAP_DB", str(tmp_path / "gate.db"))
        assert resolve_gap_db(own).resolve() == own

    def test_no_env_keeps_real(self, monkeypatch):
        from core.gap_ledger import resolve_gap_db
        monkeypatch.delenv("TMM_GAP_DB", raising=False)
        assert resolve_gap_db().resolve() == self.REAL.resolve()
        assert resolve_gap_db(self.REAL).resolve() == self.REAL.resolve()
