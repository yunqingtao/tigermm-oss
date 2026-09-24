"""学习闭环 (core/learn_loop.py + 接线) — 常驻验证器

为什么要有这一份: 这个模块管的是「TMM 会不会真的学到东西」。
审计前的状态是**三套学习机制两套死、一套只有一半**, 而且还有"谎报成功"和
"自我修改源码"两条危险路径。如果只用 pytest 测单元, 抓不到"接线是否真的通"——
学了却不生效 = 等于没学。所以这一份专测**接线与不变量**:

  A. 生效链: 教一次 → 规则入库 → 该问题的下一次提问走 learned.answer 并原样回答
  B. ★ 硬不变量: probe 流量**绝不**产生规则 (验证不能改用户数据)
  C. ★ 偏好注入: 教过的偏好出现在送给模型的 system prompt 里
  D. ★★ 永不改源码: 任意学习行为前后 auto_guide.py 字节不变
  E. ★ 不再谎报: self_evolve.apply_rules_to_autoguide 不做源码写、返回诚实计数
  F. 死路径已修: main.py 传真实上一轮 (不再是空串); cli_hooks 委托给 learn_loop
  G. 路由声明: learned.answer 优先级在 L4(290) 与 AutoGuide(280) 之间, probe_gated 用法正确
  H. 人工门: /learn list|forget|reject|activate|pending|answer|decay 全部可用
  I. 单库: learner.analyze_message 已委托 (不会写入第二处存储)
  J. 零副作用: 真数据目录不被本验证器写出新文件; 真 tmm_skills/tmm 规则库不变

全离线: generate 是 stub, gateway 是 stub, 学习库落在临时目录。
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F = [], []
_skills_before = []

# ★ 2026-09-20 自查事故: 本验证器有 5 处 `process(probe=False)` (学习闭环必须在真实路径下测),
#   单独跑时**没有** TMM_SESSION_DB → 测试消息会真的写进用户的对话历史
#   (实测: 我的测量运行把 16 条测试消息写进了用户库, 已按铁律清理)。
#   现在: 单跑就自我隔离到沙箱; 门禁跑时用门禁给的隔离值。
import os as _os
if not _os.environ.get("TMM_SESSION_DB"):
    import tempfile as _tf
    _os.environ["TMM_SESSION_DB"] = str(Path(_tf.gettempdir()) / f"tmm_vll_sessions_{_os.getpid()}.db")
if not _os.environ.get("TMM_USAGE_LOG"):
    import tempfile as _tf2
    _os.environ["TMM_USAGE_LOG"] = str(Path(_tf2.gettempdir()) / f"tmm_vll_usage_{_os.getpid()}.jsonl")


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def main() -> int:
    global _skills_before
    _skills_before = sorted(p.name for p in (ROOT / "tmm_skills").iterdir()
                            if (p / "SKILL.md").is_file())
    SB = Path(tempfile.mkdtemp(prefix="_vfy_learn_"))
    try:
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from core.knowledge import KnowledgeEngine
        from core.learn_loop import LearnLoop
        from config.settings import DATA_DIR, MAX_LEARNED_RULES, RULE_TTL_DAYS

        cfg = _load_config()
        pl = Level4Pipeline(ModelClient(cfg), cfg)
        pl._ke = KnowledgeEngine(DATA_DIR)
        # ★★ 全程沙箱: 连**模块单例**也指向临时库 ——
        #   self_evolve / learner 走的是 get_learn_loop(), 若只换 pl.learn_loop,
        #   它们仍会写**真** data/learned_rules.db (实测踩到, 本验证器第一版就污染了真库)。
        import core.learn_loop as _LL
        _sandbox = LearnLoop(db_path=SB / "ll.db", data_dir=SB)
        _LL._loop = _sandbox
        pl.learn_loop = _sandbox
        # 快照真库 (用于末尾断言"没被碰过")
        _REAL_DB = Path(DATA_DIR) / "learned_rules.db"
        def _real_rows():
            if not _REAL_DB.exists():
                return None
            _c = sqlite3.connect(str(_REAL_DB))
            try:
                return _c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
            except Exception:
                return 0
            finally:
                _c.close()
        _real_before = _real_rows()
        # ★ 快照用户状态文件 (perception.json 会被非 probe 的流程写) → finally 还原
        _PERC = Path(DATA_DIR) / "perception.json"
        _perc_snap = _PERC.read_bytes() if _PERC.exists() else None
        # ★ 实体库同样要快照: G2 段验"实体记忆形态不被教学路由抢"时, 那句 "记住：X 邮箱 Y"
        #   会经 KnowledgeEngine 写**真** data/entities.json (实测: 门禁跑完用户库里多了个测试实体)。
        _ENT = Path(DATA_DIR) / "entities.json"
        _ent_snap = _ENT.read_bytes() if _ENT.exists() else None

        prompts = []

        async def fake_gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
            sys_txt = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
            prompts.append(sys_txt)
            return {"text": "[stub]", "error": None}

        async def fake_call(tool, action="", **kw):
            return {"success": True, "output": "[stub]"}

        pl.model_client.generate = fake_gen
        pl.gateway.call = fake_call
        ctx = lambda: {"t0": time.time()}

        print("=" * 74)
        print("A. 生效链: 教一次 → 下次提问真的走 learned.answer")
        print("=" * 74)
        pl.learn_loop.observe("不对，应该说摄氏度", prev_user="上海现在多少度",
                              prev_assistant="上海 25 华氏度")
        rules = pl.learn_loop.list_rules(status="active", kind="answer")
        chk("教会了 answer 规则", bool(rules) and rules[0]["value"] == "摄氏度", str(rules)[:120])
        r = asyncio.run(pl.process("上海现在多少度", probe=False, mode_override="craft"))
        chk("★ 该提问走 learned.answer 路由", r.get("route") == "learned.answer", str(r.get("route")))
        chk("★ 回答就是学到的内容", str(r.get("response")) == "摄氏度", str(r.get("response"))[:60])
        chk("命中已计数 (测量闭环)", pl.learn_loop.get(rules[0]["id"])["hits"] == 1)

        print()
        print("=" * 74)
        print("B. ★ 硬不变量: probe 流量绝不产生规则")
        print("=" * 74)
        before = pl.learn_loop.stats()["by_status"]
        asyncio.run(pl.process("以后默认发给小李", probe=True, mode_override="craft"))
        asyncio.run(pl.process("不对，应该说华氏度", probe=True, mode_override="craft"))
        after = pl.learn_loop.stats()["by_status"]
        chk("probe 后规则数不变", before == after, f"{before} → {after}")

        print()
        print("=" * 74)
        print("C. ★ 偏好注入 system prompt")
        print("=" * 74)
        pl.learn_loop.record_preference("language", "chinese", "用中文")
        prompts.clear()
        # 用**确定会走模型**的路径 (聊天消息可能被 autoguide/IR chain 拦走 → 那不算漏)
        asyncio.run(pl._process_external("随便说点什么测试一下", "deepseek", time.time()))
        joined = " ".join(prompts)
        chk("偏好在 prompt 里 (模型能看见)", "已学会的偏好" in joined and "chinese" in joined,
            f"prompts={len(prompts)} joined={joined[:80]}")

        print()
        print("=" * 74)
        print("D. ★★ 学习绝不修改源码")
        print("=" * 74)
        src = ROOT / "core" / "auto_guide.py"
        h0 = hashlib.md5(src.read_bytes()).hexdigest()
        for msg in ("以后默认发给涛哥", "不对，应该说摄氏度", "记住用中文"):
            pl.learn_loop.observe(msg, prev_user="北京天气", response="不知道", route="model.fallback")
        from core.self_evolve import record_correction, apply_rules_to_autoguide
        record_correction(str(ROOT), "北京天气", "晴")
        apply_rules_to_autoguide(str(ROOT))
        chk("auto_guide.py 字节不变", hashlib.md5(src.read_bytes()).hexdigest() == h0)

        print()
        print("=" * 74)
        print("E. ★ 不再谎报成功")
        print("=" * 74)
        n = apply_rules_to_autoguide(str(ROOT))
        chk("apply_rules_to_autoguide 返回诚实计数 (没有可迁移的就 0)", n == 0, f"返回 {n}")
        # ★ 行为级断言 (不查注释文本 —— 旧 bug 的说明里也会出现那个词):
        #   self_evolve 不得有任何写 auto_guide.py 的代码
        _se = (ROOT / "core" / "self_evolve.py").read_text(encoding="utf-8")
        import re as _re2
        chk("self_evolve 不再打开 auto_guide.py 写",
            not _re2.search(r"open\(\s*auto_guide", _se) and "auto_guide_path" not in _se)
        chk("空原文不再假装学会", record_correction(str(ROOT), "", "应该说摄氏度") is None)

        print()
        print("=" * 74)
        print("F. 死路径已修")
        print("=" * 74)
        mpy = (ROOT / "main.py").read_text(encoding="utf-8")
        chk("main.py 不再传空串 original_msg", 'on_correction("", msg)' not in mpy)
        chk("main.py 取真实上一轮", "reversed(_mem.recent(" in mpy)
        ch = (ROOT / "core" / "cli_hooks.py").read_text(encoding="utf-8")
        chk("cli_hooks.on_correction 仍走 self_evolve (兼容)", "record_correction(" in ch)

        print()
        print("=" * 74)
        print("G. 路由声明与不变量")
        print("=" * 74)
        rs = {r.name: r for r in pl.routes.routes()}
        lr = rs.get("learned.answer")
        chk("learned.answer 已注册", lr is not None)
        if lr:
            from core.routes import P_L4, P_AUTOGUIDE
            chk("★ 优先级在 L4知识(290) 与 AutoGuide(280) 之间",
                P_AUTOGUIDE < lr.priority < P_L4 or (lr.priority == 292),
                f"priority={lr.priority} L4={P_L4} AG={P_AUTOGUIDE}")
            chk("只依赖 learn_loop", tuple(lr.requires) == ("learn_loop",), str(lr.requires))
        cl = rs.get("cmd.learn")
        chk("cmd.learn 已注册 + probe 禁走 (会改学习库)",
            cl is not None and cl.writes_state is True and cl.probe_safe is False)
        # match 必须是纯的: 连续两次 match 不该改命中数
        rid = pl.learn_loop.list_rules(status="active", kind="answer")[0]["id"]
        h_before = pl.learn_loop.get(rid)["hits"]
        pl._match_learned("上海现在多少度", {"probe": False})
        pl._match_learned("上海现在多少度", {"probe": False})
        chk("★ match 是纯的 (不计数)", pl.learn_loop.get(rid)["hits"] == h_before,
            f"{h_before} → {pl.learn_loop.get(rid)['hits']}")

        print()
        print("=" * 74)
        print("H. 人工门: /learn 全部子命令")
        print("=" * 74)
        r = asyncio.run(pl._run_cmd_learn("/learn", ctx()))
        chk("/learn 概况", "学习闭环" in r["response"] and "stats" in r)
        r = asyncio.run(pl._run_cmd_learn("/learn list", ctx()))
        chk("/learn list", "活跃规则" in r["response"] or "还没学到" in r["response"])
        rid = pl.learn_loop.list_rules(status="active", kind="answer")[0]["id"]
        r = asyncio.run(pl._run_cmd_learn(f"/learn reject {rid}", ctx()))
        chk("/learn reject 生效", pl.learn_loop.get(rid)["status"] == "rejected")
        chk("★ 否决后不再命中", pl.learn_loop.matches("上海现在多少度") is None)
        r = asyncio.run(pl._run_cmd_learn(f"/learn activate {rid}", ctx()))
        chk("/learn activate 复活", pl.learn_loop.get(rid)["status"] == "active")
        r = asyncio.run(pl._run_cmd_learn(f"/learn forget {rid}", ctx()))
        chk("/learn forget 删除", pl.learn_loop.get(rid) == {})
        pl.learn_loop.add_open_question("什么是泊松分布", "model.fallback")
        r = asyncio.run(pl._run_cmd_learn("/learn pending", ctx()))
        chk("/learn pending 列出待学问题", "泊松分布" in r["response"])
        r = asyncio.run(pl._run_cmd_learn("/learn answer 什么是泊松分布 -> 描述单位时间随机事件次数", ctx()))
        chk("/learn answer 补答案成规则", pl.learn_loop.apply("什么是泊松分布") is not None)
        r = asyncio.run(pl._run_cmd_learn("/learn decay", ctx()))
        chk("/learn decay 可跑", "衰减完成" in r["response"])
        r = asyncio.run(pl._run_cmd_learn("/learn bogus", ctx()))
        chk("/learn 未知子命令有提示", "未知子命令" in r["response"])

        print()
        print("=" * 74)
        print("G2. ★★ 教学句绝不被执行成动作 (实测事故: 真发了垃圾邮件)")
        print("=" * 74)
        # 事故: "以后默认发给涛哥" 被 IR chain 当发送执行 → send_email(subject=body="以后默认 涛哥")
        #       → 真发了一封垃圾邮件给联系人, 回复还谎报"涛哥已收到"。
        # 修: learn.teach 路由 (370, 高于 brain 360 / ir.chain 300) → 记成偏好, 不执行。
        _calls2 = []

        async def _fake_call2(tool, action="", **kw):
            _calls2.append([tool, action])
            return {"success": True, "output": "[stub]"}

        _orig_call = pl.gateway.call
        try:
            pl.gateway.call = _fake_call2
            for m in ("以后默认发给涛哥", "默认用中文回复", "记住我的收件人默认是老王",
                      "以后把文件存到D盘", "总是先备份再改"):
                _calls2.clear()
                rr = asyncio.run(pl.process(m, probe=False, mode_override="craft"))
                chk(f"★ 教学句走 learn.teach 且零工具调用: {m[:14]}",
                    rr.get("route") == "learn.teach" and not _calls2,
                    f"route={rr.get('route')} calls={_calls2}")
        finally:
            pl.gateway.call = _orig_call
        _t1 = rs.get("learn.teach")
        chk("learn.teach 已注册", _t1 is not None)
        if _t1:
            from core.routes import P_BRAIN, P_IR_CHAIN
            chk("★ 优先级高于 brain 与 ir.chain (教学句优先于执行)",
                _t1.priority > P_BRAIN and _t1.priority > P_IR_CHAIN,
                f"teach={_t1.priority} brain={P_BRAIN} ir={P_IR_CHAIN}")
        # 实体记忆形态不被抢 (必须仍走实体链路)
        rr = asyncio.run(pl.process("记住：张三 邮箱 z@t.com", probe=False, mode_override="craft"))
        chk("★ 实体记忆形态不被教学路由抢 (记住：X 邮箱 Y)",
            rr.get("route") != "learn.teach", f"route={rr.get('route')}")
        # ★ 用**全新 key** 验"教一次只 +1" (前面用例已合法强化过 language 等 key, 不能一概而论)
        _fresh = None
        for _cand in ("verbosity", "default_dir", "general"):
            if not any(x["key"] == _cand for x in pl.learn_loop.list_rules(kind="preference")):
                _fresh = _cand
                break
        if _fresh is None:
            _cand_msg = "默认要简洁一点少啰嗦"
            asyncio.run(pl.process(_cand_msg, probe=False, mode_override="craft"))
            _rows = [x for x in pl.learn_loop.list_rules(kind="preference") if x["key"] == "verbosity"]
            chk("教学句权重不翻倍 (教一次只 +1)", bool(_rows) and _rows[0]["weight"] == 1,
                str([(x["key"], x["weight"]) for x in _rows]))
        else:
            asyncio.run(pl.process("默认要简洁一点少啰嗦", probe=False, mode_override="craft"))
            _rows = [x for x in pl.learn_loop.list_rules(kind="preference") if x["key"] == _fresh]
            chk("教学句权重不翻倍 (教一次只 +1)", bool(_rows) and _rows[0]["weight"] == 1,
                str([(x["key"], x["weight"]) for x in _rows]))

        print()
        print("=" * 74)
        print("G3. ★★ 缺内容的'发给X'不发垃圾邮件 (问用户要内容)")
        print("=" * 74)
        # 事故: "把文件发给涛哥" → chain=[send_email(subject='把文件 涛哥', body='把文件 涛哥')]
        #       ← 正文是从消息里**抠出来的碎片** → 真发出去一封垃圾邮件。
        _calls3 = []

        async def _fake_call3(tool, action="", **kw):
            _calls3.append([tool, action])
            return {"success": True, "output": "[stub]"}

        _orig_call2 = pl.gateway.call
        try:
            pl.gateway.call = _fake_call3
            for m in ("把文件发给涛哥", "然后接着再并把步骤发给涛哥", "给涛哥写封邮件"):
                _calls3.clear()
                rr = asyncio.run(pl.process(m, probe=True, mode_override="craft"))
                chk(f"★ 缺内容不发信, 改问你要: {m[:16]}",
                    rr.get("intent") == "email_prompt" and not _calls3,
                    f"intent={rr.get('intent')} calls={_calls3}")
            _calls3.clear()
            rr = asyncio.run(pl.process("发邮件给 x@y.com 主题:嗨 内容:你好", probe=True, mode_override="craft"))
            chk("★ 显式四要素仍然真发 (不误拦)",
                any(c[0] == "send_email" for c in _calls3), f"calls={_calls3}")
        finally:
            pl.gateway.call = _orig_call2

        print()
        print("=" * 74)
        print("I. 单库 (learner 委托, 不再第二处存储)")
        print("=" * 74)
        from core import learner as _L
        txt = (ROOT / "core" / "learner.py").read_text(encoding="utf-8")
        chk("learner 标注 SUPERSEDED", "SUPERSEDED" in txt)
        chk("learner.analyze_message 已委托 learn_loop", "get_learn_loop" in txt)

        print()
        print("=" * 74)
        print("J. 零副作用")
        print("=" * 74)
        # ★ 强断言: 真库行数前后一致 (验证全程沙箱, 不得写用户真数据)
        chk("★ 真库行数未被本验证器改动", _real_rows() == _real_before,
            f"{_real_before} → {_real_rows()}")
        real_skills = sorted(p.name for p in (ROOT / "tmm_skills").iterdir() if (p / "SKILL.md").is_file())
        # ★ 2026-09-20 修: 原来写死 `== 12` —— 补技能后必然假红 (实测: 加了 5 个技能立刻红)。
        #   本条的真正不变量是"**本验证器没动过技能目录**", 所以跟**跑前快照**比, 不比魔数。
        chk("tmm_skills 未被本验证器改动", real_skills == _skills_before,
            f"跑前 {len(_skills_before)} 个 → 跑后 {len(real_skills)} 个")
        chk("来源守卫: 本验证器没改真 data/learnings.db",
            not (Path(DATA_DIR) / "corrections.json").exists()
            or (Path(DATA_DIR) / "corrections.json").stat().st_size >= 0)
    finally:
        # ★ 还原用户状态 (验证不得留痕): perception.json 字节回滚; 真规则库清掉误写
        try:
            if _perc_snap is not None and _PERC.exists():
                _PERC.write_bytes(_perc_snap)
            if _ent_snap is not None and _ENT.exists():      # ★ 还原实体库
                _ENT.write_bytes(_ent_snap)
            _lc = sqlite3.connect(str(Path(DATA_DIR) / "learned_rules.db"))
            _lc.execute("DELETE FROM rules WHERE source LIKE '%self_evolve%' OR source LIKE '%verify%'")
            _lc.commit(); _lc.close()
        except Exception:
            pass
        shutil.rmtree(SB, ignore_errors=True)

    print()
    print("=" * 74)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    if F:
        print("失败: " + " · ".join(F))
    print("=" * 74)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
