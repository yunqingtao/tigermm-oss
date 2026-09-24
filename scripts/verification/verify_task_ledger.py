# -*- coding: utf-8 -*-
r"""日常干活账 —— 常驻验证器 (2026-09-23 立)

命题 (用户原话): "TMM的帐本空问题解决了嘛"
  真人用了 50 天, learnings=2 / rules=0 / gaps=2 (还都是测试留下的) —— 账几乎是空的。
  根因不是管道坏 (技能账 115 条正常), 而是**触发面太窄**: 前四本账只在"出事/被教"时写,
  **正常把活干成了什么都不记** ⇒ 用得越多账越空。
  本模块补的就是这一路: 每轮真实对话记一条 (能力类别/路由/成败/工具)。

四段:
  A 挂钩在位 (零副作用): 模块/网关工具记录/最外层记账/`/ledger` 路由/失败即闭
  B 纯函数 (零副作用): 分类·成败·隐私不变量 (不存用户原话)·命令不记
  C 真引擎 (隔离库 + 真人信号): 真跑几轮 → 账本真长; 路由**必须是真的**
    (若全是 mode.gate ⇒ 用户被卡在问答模式: 那是状态问题, 报红并说清)
  D 反向 (隔离库): 无 TMM_LIVE / 有 TMM_PROBE → 一个字都不许写
"""
import asyncio
import glob
import os
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))
os.chdir(ROOT)
from _probe_env import activate          # noqa: E402
activate()
import logging                           # noqa: E402
logging.disable(logging.CRITICAL)

DESK = Path(os.path.expanduser("~/Desktop"))
P, F, S = [], [], []


def chk(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS " if c else "  FAIL ") + n + (("   <- " + str(d)) if d and not c else ""))


def skip(n, why):
    S.append(n)
    print(f"  SKIP {n}   <- {why}")


def _model_probe(PL):
    """三态: (可用, 原因, 依赖是否在位)。

    ★ 交付包**没有凭据** ⇒ 依赖不在位 ⇒ 依赖模型的断言必须 SKIP, 不许报红
      (否则用户拿到包一跑就是红, 分不清"缺密钥"和"功能坏")。
      依赖**在位却调不通** ⇒ 红 (静默跳过 = 假绿)。
    """
    kf = ROOT / "keys.json"
    dep = any((kf.is_file(), os.environ.get("DASHSCOPE_API_KEY"),
               os.environ.get("QWEN_API_KEY"), os.environ.get("DEEPSEEK_API_KEY"),
               os.environ.get("OPENAI_API_KEY"), os.environ.get("MIMO_API_KEY")))
    last = ""
    for _ in range(2):
        try:
            r = asyncio.run(asyncio.wait_for(
                PL.model_client.generate(PL._mode_pick_model(None),
                                         [{"role": "user", "content": "ok"}], max_tokens=4),
                timeout=60))
            if isinstance(r, dict) and not r.get("error"):
                return True, "", bool(dep)
            last = str((r or {}).get("error"))[:90]
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:90]
        time.sleep(1)
    return False, last or "未知", bool(dep)


def main() -> int:
    print("=" * 90)
    print("日常干活账 (task_ledger) —— 『用得越多账越厚』的底账")
    print("=" * 90)
    import core.task_ledger as TL

    # ── A. 挂钩在位 ─────────────────────────────────────────
    print("\n[A.挂钩] 模块 / 工具记录 / 最外层记账 / /ledger 路由")
    pipe = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
    chk("模块在 (core/task_ledger.py)", (ROOT / "core" / "task_ledger.py").is_file())
    chk("① ToolGateway 有本轮工具记录 (turn_tools)",
        "turn_tools: list = []" in pipe and "self.turn_tools.append(tool_name)" in pipe)
    chk("② 最外层回合记账 (与降级汇总同一位置)",
        "from core.task_ledger import get_task_ledger" in pipe
        and pipe.count("get_task_ledger().record(") == 1)
    chk("③ /ledger 路由已注册 (probe_safe 只读)",
        'reg(R(name="cmd.ledger"' in pipe and "probe_safe=True" in pipe.split('reg(R(name="cmd.ledger"')[1][:260])
    chk("失败即闭: 无 TMM_LIVE 不写 + probe 优先",
        'TMM_LIVE' in (ROOT / "core" / "task_ledger.py").read_text(encoding="utf-8"))
    # ★★ 新增账本必须进**两处**隔离表, 缺一处就会被门禁写脏真账
    #   (2026-09-23 实测: 真账被全量套件写了 11 行 —— 因为只加了一处/一处都没加)
    _penv = (ROOT / "scripts" / "verification" / "_probe_env.py").read_text(encoding="utf-8")
    _run = (ROOT / "scripts" / "hermes_verify.py").read_text(encoding="utf-8")
    _src = (ROOT / "core" / "task_ledger.py").read_text(encoding="utf-8")
    for _var, _path in (("TMM_TASK_DB", "task_ledger.db"),):
        chk(f"★★ 隔离表① _probe_env.ISOLATED 有 {_var}",
            f'"{_var}": "{_path}"' in _penv, _var)
        chk(f"★★ 隔离表② runner 注入有 {_var}",
            f'env["{_var}"]' in _run, _var)
    chk("★★ 账本路径受环境变量控制 (TMM_TASK_DB)",
        "TMM_TASK_DB" in _src and "resolve_db" in _src)

    # ── B. 纯函数 ───────────────────────────────────────────
    print("\n[B.判据] 分类·成败·隐私不变量")
    chk("分类: 规划链→规划任务", TL.categorize("x", "planner.multi_step") == "规划任务")
    chk("分类: 技能→技能", TL.categorize("x", "skill.trigger_first") == "技能")
    chk("分类: 插件→查询", TL.categorize("x", "plugin.keyword") == "查询")
    chk("分类: fallback→问答", TL.categorize("x", "model.fallback") == "问答")
    chk("分类: 无路由但有 file_ops→文件", TL.categorize("x", "", ["file_ops"]) == "文件")
    chk("成败: 正常回复→成", TL.judge_ok("已经写好放到桌面了", "planner.multi_step") is True)
    chk("成败: ✗ 失败信号→败", TL.judge_ok("✗ 写文件 没执行成功", "l4.knowledge") is False)
    chk("成败: 追问用户算完成一轮 (健康交互)",
        TL.judge_ok("请提供收件人、主题和内容", "model.fallback") is True)
    _rec = TL.classify_turn("写一首关于青山绿水的七言绝句", "写好了", "planner.multi_step")
    chk("★★ 隐私不变量: 账本**不含用户原话** (可随包带走)",
        "青山绿水" not in str(_rec), _rec)
    chk("命令不记账 (/ledger)", TL.classify_turn("/ledger", "账…", "cmd.ledger") is None)
    chk("点名不记账 (@deepseek)", TL.classify_turn("@deepseek 你好", "你好", "model.explicit") is None)
    chk("空消息不记账", TL.classify_turn("  ", "x", "model.fallback") is None)

    # ── C. 真引擎 (隔离库) ──────────────────────────────────
    print("\n[C.真引擎] 真跑几轮 → 账本真长 (隔离库, 不碰真账)")
    iso = Path(tempfile.mkdtemp(prefix="hermes_verify_task_ledger_"))
    MJ = ROOT / "data" / "mode.json"
    _mbak = MJ.read_bytes() if MJ.exists() else None
    _env_keys = ("TMM_TASK_DB", "TMM_SESSION_DB", "TMM_GAP_DB", "TMM_PERCEPTION_FILE",
                 "TMM_USAGE_LOG", "TMM_SKILL_USAGE_FILE", "TMM_ARTIFACTS_FILE", "TMM_LIVE")
    _saved = {k: os.environ.get(k) for k in _env_keys}
    for k, f in (("TMM_TASK_DB", "task.db"), ("TMM_SESSION_DB", "sess.db"),
                 ("TMM_GAP_DB", "gap.db"), ("TMM_PERCEPTION_FILE", "perc.json"),
                 ("TMM_USAGE_LOG", "usage.jsonl"), ("TMM_SKILL_USAGE_FILE", "skill.jsonl"),
                 ("TMM_ARTIFACTS_FILE", "art.jsonl")):
        os.environ[k] = str(iso / f)
    os.environ.pop("TMM_PROBE", None)          # 要测"真人"路径
    os.environ["TMM_LIVE"] = "1"
    TL.reset_task_ledger()
    d0 = set(glob.glob(str(DESK / "*")))
    made = []
    try:
        from main import _load_config
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        cfg = _load_config()
        PL = Level4Pipeline(ModelClient(cfg), cfg)
        lg = TL.get_task_ledger()
        chk("账本库已隔离 (不是用户真账)", str(iso) in str(lg.db_path), lg.db_path)
        _mode = PL.modes.get()
        chk("当前模式 = craft (实干)", _mode == "craft",
            f"模式是 '{_mode}' —— 问答模式什么都不执行, 账本自然写不出真活")
        ok_m, _merr, _mdep = _model_probe(PL)
        if not ok_m:
            if _mdep:
                chk("★ 依赖在位却调不通模型 → 不许静默跳过真跑段", False, _merr)
                return 1
            for _n in ("三轮都到达真实路由 (非全 mode.gate)",
                       "账本真长了 (+3 轮, 期望 3)",
                       "分类是派生的能力类别, 不是'通用'",
                       "工具维度有记录 (file_ops)"):
                skip(_n, "本机无可用模型 (交付包无凭据) —— 非功能缺陷")
        b0 = lg.stats()["turns"]
        routes, wrote = [], []
        for m in ["写一首诗放桌面", "北京天气", "什么是诗"]:
            r = asyncio.run(PL.process(m, probe=True)) or {}
            routes.append((m, str(r.get("route") or "")))
        for q in sorted(set(glob.glob(str(DESK / "*"))) - d0):
            made.append(q)
        wrote = [rt for _m, rt in routes if rt]
        if ok_m:
            chk("★ 三轮都到达真实路由 (不是全 mode.gate)", len(wrote) == 3
                and not all(rt == "mode.gate" for _m, rt in routes), routes)
        st = lg.stats()
        if ok_m:
            chk(f"★ 账本真长了 (+{st['turns'] - b0} 轮, 期望 3)",
                st["turns"] - b0 == 3, st["turns"])
            chk("★ 分类是派生的能力类别, 不是'通用'",
                all(r["category"] not in ("未分类", "通用") for r in st["by_category"])
                and len(st["by_category"]) >= 3, st["by_category"])
            chk("★ 工具维度有记录 (file_ops)", "file_ops" in lg.tools_seen(), lg.tools_seen())
        chk("★ 账本确实记了东西 (无需模型)", st["turns"] - b0 >= 1, st["turns"])
        chk("★ 按天聚合有当天", any(r["day"] == st["last_day"] for r in st["by_day"]))
        txt = lg.render()
        chk("★ /ledger 渲染有内容且无空行", "干活账" in txt and "\n\n" not in txt)
    finally:
        for q in made:
            try:
                Path(q).unlink(missing_ok=True)
            except Exception:
                pass
        if _mbak is not None:
            MJ.write_bytes(_mbak)              # ★ 用户状态还原
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        TL.reset_task_ledger()
        shutil.rmtree(iso, ignore_errors=True)
    left = sorted(set(glob.glob(str(DESK / "*"))) - d0)
    chk("★ 桌面零残留", not left, left)

    # ── D. 反向: 失败即闭 ───────────────────────────────────
    print("\n[D.反向] 非真人会话一个字都不许写")
    iso2 = Path(tempfile.mkdtemp(prefix="hermes_verify_tl_neg_"))
    try:
        for label, envset in (("无 TMM_LIVE", {}), ("有 TMM_PROBE", {"TMM_LIVE": "1", "TMM_PROBE": "1"})):
            for k in ("TMM_LIVE", "TMM_PROBE"):
                os.environ.pop(k, None)
            os.environ.update(envset)
            os.environ["TMM_TASK_DB"] = str(iso2 / f"{abs(hash(label))}.db")
            TL.reset_task_ledger()
            lg2 = TL.TaskLedger(db_path=iso2 / f"{abs(hash(label))}.db")
            r = lg2.record("写一首诗放桌面", "好了", "planner.multi_step")
            chk(f"★ {label} → 不记账", r.get("ok") is False and lg2.stats()["turns"] == 0, r)
    finally:
        for k in ("TMM_LIVE", "TMM_PROBE", "TMM_TASK_DB"):
            os.environ.pop(k, None)
        TL.reset_task_ledger()
        shutil.rmtree(iso2, ignore_errors=True)

    print("\n" + "=" * 90)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for x in F:
        print("  -", x)
    print("=" * 90)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
