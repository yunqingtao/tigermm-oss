"""tests for core/learn_loop.py — 学习闭环 (记录→置信→生效→测量→衰减)。

全部离线: SQLite 落在 tmp_path, 不碰真 data/learned_rules.db。
守什么:
  · 三态语义: created / reinforced / superseded
  · ★ 置信: 命中 3 次 → 稳固(weight=2); 被反证: 稳固→降级, 低置信→撤销
  · ★ 衰减: 久未命中 → stale (不删除); 超上限 → 淘汰低分
  · ★ 偏好立即生效 (hints) / 答案规则命中计 hits
  · 待学池: 未答上的问题入池, 补答案后成规则且问题标记已答
  · 零污染: 不写源码 (auto_guide.py 字节不变)
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.learn_loop import LearnLoop


@pytest.fixture
def ll(tmp_path):
    return LearnLoop(db_path=tmp_path / "ll.db", data_dir=tmp_path)


# ─────────────────────────── 记录三态 ───────────────────────────

class TestRecord:
    def test_created(self, ll):
        r = ll.record_preference("default_recipient", "涛哥", "以后默认发给涛哥")
        assert r["action"] == "created" and r["rule"]["weight"] == 1
        assert r["rule"]["status"] == "active"

    def test_reinforced_same_value(self, ll):
        a = ll.record_preference("default_recipient", "涛哥", "x")
        b = ll.record_preference("default_recipient", "涛哥", "y")
        assert b["action"] == "reinforced" and b["id"] == a["id"]
        assert b["rule"]["weight"] == 2

    def test_superseded_different_value(self, ll):
        a = ll.record_preference("default_recipient", "涛哥", "x")
        b = ll.record_preference("default_recipient", "小李", "y")
        assert b["action"] == "superseded"
        old = ll.get(a["id"])
        assert old["status"] == "rejected" and "取代" in (old["note"] or "")
        assert ll.get(b["id"])["status"] == "active"

    def test_invalid_input_is_noop(self, ll):
        assert ll.record("preference", "", "v")["action"] == "invalid"
        assert ll.record("preference", "k", "")["action"] == "invalid"
        assert ll.stats()["by_status"] == {}

    def test_reinforce_revives_stale(self, ll):
        r = ll.record_preference("k", "v", "x")
        ll._set_status(r["id"], "stale")
        assert ll.get(r["id"])["status"] == "stale"
        ll.record_preference("k", "v", "x")
        assert ll.get(r["id"])["status"] == "active"


# ─────────────────────────── 置信 / 测量 ───────────────────────────

class TestConfidence:
    def test_hits_promote_to_stable(self, ll):
        r = ll.record_answer("北京天气", "晴 25 度", "x")
        assert ll.get(r["id"])["weight"] == 1
        for _ in range(2):
            ll.note_hit(r["id"])
        assert ll.get(r["id"])["weight"] == 1, "才 2 次不该升级"
        out = ll.note_hit(r["id"])                      # 第 3 次
        assert out["promoted"] is True and ll.get(r["id"])["weight"] == 2

    def test_hit_on_missing_rule_is_safe(self, ll):
        assert ll.note_hit(9999) == {}

    def test_contradiction_demotes_stable(self, ll):
        r = ll.record_answer("北京天气", "晴", "x")
        ll.note_hit(r["id"]); ll.note_hit(r["id"]); ll.note_hit(r["id"])   # → weight 2
        out = ll.note_correction(r["id"], "说错了")
        assert out["action"] == "demoted"
        rule = ll.get(r["id"])
        assert rule["weight"] == 1 and rule["status"] == "active", "稳固的只降级, 不撤销"

    def test_contradiction_rejects_low_confidence(self, ll):
        r = ll.record_answer("北京天气", "晴", "x")
        out = ll.note_correction(r["id"], "说错了")
        assert out["action"] == "rejected"
        assert ll.get(r["id"])["status"] == "rejected"

    def test_misses_counted(self, ll):
        r = ll.record_answer("q", "a", "x")
        ll.note_correction(r["id"]); ll.note_hit(r["id"])
        assert ll.get(r["id"])["misses"] == 1
        assert ll.get(r["id"])["hits"] == 1


# ─────────────────────────── 衰减 ───────────────────────────

class TestDecay:
    def test_stale_after_ttl(self, tmp_path):
        ll = LearnLoop(db_path=tmp_path / "d.db", data_dir=tmp_path, ttl_days=0)
        r = ll.record_answer("旧问题", "旧答案", "x")
        old = time.time() - 100
        c = ll._conn(); c.execute("UPDATE rules SET created_at=?, last_hit_at=NULL WHERE id=?", (old, r["id"]))
        c.commit(); c.close()
        out = ll.decay()
        assert out["stale"] == 1 and ll.get(r["id"])["status"] == "stale"

    def test_stable_rules_not_decayed(self, tmp_path):
        ll = LearnLoop(db_path=tmp_path / "d2.db", data_dir=tmp_path, ttl_days=0)
        r = ll.record_answer("q", "a", "x")
        ll.note_hit(r["id"]); ll.note_hit(r["id"]); ll.note_hit(r["id"])    # weight=2
        c = ll._conn(); c.execute("UPDATE rules SET created_at=? WHERE id=?", (time.time() - 100, r["id"]))
        c.commit(); c.close()
        assert ll.decay()["stale"] == 0
        assert ll.get(r["id"])["status"] == "active"

    def test_evict_over_cap_keeps_best(self, tmp_path):
        ll = LearnLoop(db_path=tmp_path / "d3.db", data_dir=tmp_path, max_rules=2, ttl_days=9999)
        ids = [ll.record_answer(f"问题{i}", f"答案{i}", "x")["id"] for i in range(4)]
        # 让其中一条命中 3 次 → 稳固, 应该活下来
        for _ in range(3):
            ll.note_hit(ids[0])
        out = ll.decay()
        active = [r["id"] for r in ll.list_rules(status="active")]
        assert out["evicted"] == 2 and len(active) == 2
        assert ids[0] in active, "高分规则不该被淘汰"
        # 同分(0 分)时按"最早创建"先淘汰 —— 确定性排序, 不靠 SQLite 默认顺序
        assert ids[1] not in active and ids[2] not in active, "同分应淘汰最早的"
        assert ids[3] in active

    def test_decay_never_deletes(self, tmp_path):
        ll = LearnLoop(db_path=tmp_path / "d4.db", data_dir=tmp_path, ttl_days=0)
        r = ll.record_answer("q", "a", "x")
        ll.decay()
        assert ll.get(r["id"]), "休眠不等于删除"


# ─────────────────────────── 生效 ───────────────────────────

class TestApply:
    def test_answer_rule_fires_and_counts_hit(self, ll):
        r = ll.record_answer("北京天气怎么样", "晴, 25 度", "x")
        hit = ll.apply("北京天气怎么样")
        assert hit and hit["id"] == r["id"]
        assert ll.get(r["id"])["hits"] == 1

    def test_answer_rule_ignores_unrelated(self, ll):
        ll.record_answer("北京天气怎么样", "晴", "x")
        assert ll.apply("帮我写个周报") is None

    def test_fuzzy_match_same_topic(self, ll):
        r = ll.record_answer("北京天气怎么样", "晴", "x")
        hit = ll.apply("北京天气怎么样啊")                 # 多一个字
        assert hit and hit["id"] == r["id"]

    def test_rejected_rule_not_applied(self, ll):
        r = ll.record_answer("北京天气怎么样", "晴", "x")
        ll.reject(r["id"])
        assert ll.apply("北京天气怎么样") is None

    def test_hints_only_active_prefs(self, ll):
        ll.record_preference("language", "chinese", "用中文")
        h = ll.hints()
        assert "language" in h and "chinese" in h
        assert "已学会的偏好" in h

    def test_hints_marks_confirmed(self, ll):
        ll.record_preference("language", "chinese", "x")
        ll.record_preference("language", "chinese", "x")   # weight 2
        assert "已确认" in ll.hints()

    def test_hints_empty_when_none(self, ll):
        assert ll.hints() == ""

    def test_stale_pref_not_in_hints(self, ll):
        r = ll.record_preference("language", "chinese", "x")
        ll._set_status(r["id"], "stale")
        assert ll.hints() == ""


# ─────────────────────────── 观察 (从对话学) ───────────────────────────

class TestObserve:
    def test_learns_preference(self, ll):
        got = ll.observe("以后默认发给涛哥")
        assert "preference" in got
        assert ll.hints() and "涛哥" in ll.hints()

    def test_learns_explicit_correction_with_prev_turn(self, ll):
        got = ll.observe("不对，应该说摄氏度", prev_user="上海现在多少度",
                         prev_assistant="上海 25 摄氏度")
        assert "answer" in got
        hit = ll.apply("上海现在多少度")
        assert hit and hit["value"] == "摄氏度"

    def test_vague_correction_acknowledged_not_learned(self, ll):
        got = ll.observe("不对", prev_user="上海温度")
        assert "correction" in got and "answer" not in got
        assert ll.list_rules(kind="answer") == []

    def test_correction_penalizes_prior_answer_rule(self, ll):
        r = ll.record_answer("上海温度", "25华氏度", "x")
        ll.observe("不对，应该说摄氏度", prev_user="上海温度")
        assert ll.get(r["id"])["status"] == "rejected", "低置信规则被纠正 → 撤销"

    def test_slash_command_not_learned(self, ll):
        assert ll.observe("/stats") == {}
        assert ll.stats()["by_status"] == {}

    def test_open_question_recorded(self, ll):
        got = ll.observe("介绍一下量子纠缠的退相干时间",
                         response="我不知道这个问题的答案", route="model.fallback")
        assert "open_question" in got
        assert ll.stats()["open_questions"] == 1

    def test_open_question_dedup_counts(self, ll):
        ll.observe("怪问题", response="不知道", route="model.fallback")
        ll.observe("怪问题", response="不知道", route="model.fallback")
        qs = ll.open_questions()
        assert len(qs) == 1 and qs[0]["count"] == 2

    def test_normal_answer_not_an_open_question(self, ll):
        ll.observe("帮我写周报", response="已生成周报.docx", route="model.fallback")
        assert ll.open_questions() == []

    def test_answer_open_question_creates_rule(self, ll):
        ll.observe("什么是泊松分布", response="不知道", route="model.fallback")
        r = ll.answer_open_question("什么是泊松分布", "描述单位时间内随机事件发生次数的分布")
        assert r["action"] == "created"
        assert ll.open_questions() == [], "补答后问题应标记已答"
        assert ll.apply("什么是泊松分布")["value"].startswith("描述单位时间")

    def test_preference_wins_over_correction(self, ll):
        """'以后…' 是强信号, 不该被 '不对' 抢走。"""
        got = ll.observe("不对，以后默认发给小李", prev_user="发给谁")
        assert "preference" in got and "answer" not in got


# ─────────────────────────── 人工门 + 零污染 ───────────────────────────

class TestGate:
    def test_reject_and_activate_roundtrip(self, ll):
        r = ll.record_preference("k", "v", "x")
        assert ll.reject(r["id"]) and ll.get(r["id"])["status"] == "rejected"
        assert ll.activate(r["id"]) and ll.get(r["id"])["status"] == "active"

    def test_forget_deletes(self, ll):
        r = ll.record_preference("k", "v", "x")
        assert ll.forget(r["id"]) and ll.get(r["id"]) == {}

    def test_stats_shape(self, ll):
        ll.record_preference("k", "v", "x")
        ll.record_answer("q", "a", "x")
        s = ll.stats()
        assert s["by_status"]["active"] == 2
        assert s["by_kind"]["preference"] == 1 and s["by_kind"]["answer"] == 1
        assert "hits" in s and "misses" in s and "open_questions" in s

    def test_persistence_across_instances(self, tmp_path):
        db = tmp_path / "keep.db"
        LearnLoop(db_path=db, data_dir=tmp_path).record_preference("k", "v", "x")
        again = LearnLoop(db_path=db, data_dir=tmp_path)
        assert again.hints() and "v" in again.hints()

    def test_never_touches_source(self, tmp_path):
        """★ 学习只写数据, 绝不改源码 (self_evolve 的旧设计就是往源码写规则)。"""
        src = Path(__file__).resolve().parent.parent / "core" / "auto_guide.py"
        before = src.read_bytes()
        ll = LearnLoop(db_path=tmp_path / "s.db", data_dir=tmp_path)
        for msg in ("以后默认发给涛哥", "不对，应该说摄氏度", "记住用中文"):
            ll.observe(msg, prev_user="北京天气", response="不知道", route="model.fallback")
        assert src.read_bytes() == before


# ─────────────────────────── 共享匹配口径 ───────────────────────────

class TestTextMatch:
    def test_cjk_bigram_similarity_tolerates_wording(self):
        """★ 中文没空格 —— 整句当一个 token 会永远匹配不上 (学到的规则成死的)。"""
        from core.text_match import similarity
        assert similarity("北京天气怎么样啊", "北京天气怎么样") == 1.0
        assert similarity("上海现在多少度", "上海现在多少度") == 1.0
        assert similarity("帮我写个周报", "北京天气怎么样") < 0.3

    def test_similarity_ignores_unrelated(self):
        from core.text_match import similarity
        assert similarity("今天吃什么", "北京天气怎么样") == 0.0

    def test_similarity_empty(self):
        from core.text_match import similarity
        assert similarity("", "北京天气") == 0.0 and similarity("北京天气", "") == 0.0

    def test_overlap_basic(self):
        from core.text_match import keyword_overlap
        assert keyword_overlap("北京天气怎么样", "北京天气怎么样") == 1.0
        assert keyword_overlap("帮我写个周报", "北京天气怎么样") == 0.0

    def test_char_level_noise_rejected(self):
        """历史教训: 字符级会把 '帮我修复一个bug' 匹配到 '帮我写个病毒'。"""
        from core.text_match import keyword_overlap
        assert keyword_overlap("帮我修复一个bug", "帮我写个病毒") < 0.4

    def test_empty_target(self):
        from core.text_match import keyword_overlap, tokenize
        assert keyword_overlap("任意", "") == 0.0
        assert tokenize("") == set()

    def test_autoguide_delegates_to_shared(self):
        """★ AutoGuide 与学习闭环必须用**同一口径** (否则学了不生效)。"""
        from core.auto_guide import AutoGuide
        from core.text_match import keyword_overlap
        ag = AutoGuide()
        for a, b in [("北京天气怎么样", "北京天气怎么样"), ("帮我写周报", "北京天气"),
                     ("修复bug", "帮我写个病毒")]:
            assert ag._keyword_overlap(a, b) == keyword_overlap(a, b)


# ─────────────────────── 教学句解析 (路由与 observe 共用) ───────────────────────

class TestParsePreference:
    """★ 事故驱动: "以后默认发给涛哥" 曾被 IR chain 当发送执行 → 真发了垃圾邮件。
    现在由 learn.teach 路由用 require_start=True 严格判定, 这里守住解析口径。
    """

    def test_strict_requires_teach_start(self, ll):
        assert ll.parse_preference("以后默认发给涛哥", require_start=True) is not None
        assert ll.parse_preference("默认用中文回复", require_start=True) is not None
        assert ll.parse_preference("帮我记住用中文", require_start=True) is not None
        # 句中才出现教学词 → 严格模式不认 (避免抢走真命令)
        assert ll.parse_preference("把文件发给涛哥以后再说", require_start=True) is None

    def test_loose_mode_matches_anywhere(self, ll):
        """observe 用宽松模式 —— 多学不亏。"""
        assert ll.parse_preference("不对，以后默认发给小李", require_start=False) is not None

    def test_entity_form_excluded(self, ll):
        """'记住：张三 邮箱 z@t.com' 是实体记忆, 不是偏好 (交给实体链路)。"""
        for m in ("记住：张三 邮箱 z@t.com", "记住 张三 电话 13800000000",
                  "记住 老王 地址 济南市历下区"):
            assert ll.parse_preference(m, require_start=True) is None, m

    def test_key_inference(self, ll):
        assert ll.parse_preference("以后默认发给涛哥", True)[0] == "default_recipient"
        assert ll.parse_preference("默认用中文回复", True) == ("language", "chinese")
        assert ll.parse_preference("总是先备份再改", True)[0] == "general"

    def test_no_side_effect(self, ll):
        """★ 纯函数: 解析不能落库 (路由 match 必须无副作用)。"""
        ll.parse_preference("以后默认发给涛哥", True)
        assert ll.stats()["by_status"] == {}

    def test_rejects_slash_and_at(self, ll):
        assert ll.parse_preference("/learn list", True) is None
        assert ll.parse_preference("@deepseek 默认发给涛哥", True) is None

    def test_rejects_too_long(self, ll):
        assert ll.parse_preference("以后" + "发" * 80, True) is None

    def test_observe_uses_shared_parser(self, ll):
        """observe 与路由必须共用一份解析 (口径不分叉)。"""
        got = ll.observe("以后默认发给涛哥")
        assert "preference" in got
        rules = ll.list_rules(kind="preference")
        assert rules and rules[0]["key"] == "default_recipient" and rules[0]["weight"] == 1
