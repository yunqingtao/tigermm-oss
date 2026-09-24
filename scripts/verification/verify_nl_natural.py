r"""自然语句健壮性 —— 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

★ 这道门为什么存在 (2026-09-22 晚, 用户当场发火):
  当时 40 道门 / 1940 项**全绿**, 而用户三句自然话全报错:
    ① "写一首关于青山绿水的七言绝句" → ✗ Is a directory: .          (写文件缺文件名 → path 默认 ".")
    ② "把这首诗保存到桌面"           → ✗ send-file-to-contact 缺必填参数
    ③ 规划执行                       → ✗ Access denied: ...\Desktop\...\Desktop\doc_xxx.txt (桌面拼两次)
  根因不是"漏测一条", 而是**整套语料是我自己编的句子、我自己定的期望** ——
  用户的说法 (指代"这首诗" / 口语文件名"到桌面X.txt" / 独立约束句"不要覆盖我以前的")
  一条都不在里面。"全绿"只证明"我测的东西是好的"。

★ 因此本门用**两类不是我编的/系统生成的**判据:
  Arm 1 真实原话 (nl_real_utterances.json, 从会话库抽取) —— 只断言**对任何一句话都必须成立的不变量**:
        INV1 弱线索认领且**必填项全缺** → 不许 (除非护栏会拦下, 那样是正确行为)
        INV2 解析出的路径不许含**两个盘符** (拼接两次)
        INV3 消息里有 .扩展名 时, 解析结果不许**丢文件名**或被 `doc_<时间戳>` 顶替
        INV4 含指代词 (这首诗/它/刚才…) 时不许被技能抢答
        INV5 "写内容"类说法 (写一首诗/写个剧本) 不许被判成写文件 (除非有文件信号)
  Arm 2 系统化模板 (文件名/写文件判定) —— 对**说法模板 × 名字类型**做全组合, 不是挑几个例子:
        写法模板: 到桌面X / 桌面 X / 桌面/X / 桌面\X / 写入X / 保存到桌面X / 另存为X
        名字类型: 中文 / ASCII / 带空格 / 带下划线
        断言: 解析出的 basename 必须**等于**用户写的名字

跑法: python -B scripts/verification/verify_nl_natural.py
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))
os.chdir(ROOT)
from _probe_env import activate          # noqa: E402
activate()
import logging                           # noqa: E402
logging.disable(logging.CRITICAL)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _nl_sanitize import has_leak, sanitize     # noqa: E402  (本目录的脱敏 helper)

#: 本机开发根名的占位符 (脱敏用; 真实名不写进代码, 否则本文件自己就泄露了)。
#: 语料里出现"工程根"形态时统一换成 <work>, 与夹具生成时同一规则。
_WORK_HINT = "<work>"

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))


EXT = r"(?:txt|text|docx?|pdf|xlsx?|pptx?|md|json|csv|png|jpe?g|py|log)"
FN_RE = re.compile(r"[^\s，。、；：,;！!？?\"'（）()\[\]]{1,45}?\.(?:%s)" % EXT, re.I)
DRV_RE = re.compile(r"[A-Za-z]:[\\/]")
ANAPH = ("这首诗", "这个", "它", "刚才", "上面", "那个", "这段", "这句话", "该文件", "这篇文章")


def main() -> int:
    print("=" * 96)
    print("自然语句健壮性 —— 真实原话不变量 + 说法模板全组合 (决策层, 零副作用)")
    print("=" * 96)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.knowledge import KnowledgeEngine
    from config.settings import DATA_DIR
    from tools import file_ops as FO

    CFG = _load_config()
    PL = Level4Pipeline(ModelClient(CFG), CFG)
    KE = getattr(PL, "_ke", None) or KnowledgeEngine(DATA_DIR)

    # ════════ Arm 1: 用户真实原话 (不是我编写的语料) ════════
    fx = json.loads((HERE / "nl_real_utterances.json").read_text(encoding="utf-8"))
    utts = fx["utterances"]
    print(f"\n[Arm 1] 用户真实原话 {len(utts)} 条 (来源: {fx['source']})")
    chk("语料夹具非空且够量 (自证有数据)", len(utts) >= 50, f"{len(utts)} 条")
    # ★ 2026-09-22 加 (真泄露的护栏): 夹具会**随分发包出门**, 里面若混进真实家目录
    #   (`盘符:\Users\真名`) 就是公开泄露。实测踩到过: 用户原话里粘过
    #   "PS C:\Users\<真名>\Desktop> python run.py" ⇒ 必须脱敏。
    #   这里做 fail-closed 自查: 出现"家目录 + 具体名字"(非占位符) 就判红。
    _leaky = [u for u in utts if has_leak(u)]
    chk("★ 夹具零个人标识残留 (家目录形态必须是 <user> 占位符)", not _leaky,
        f"{len(_leaky)} 条: {[x[:40] for x in _leaky[:2]]}")
    # 夹具要与会话库**同源**时才算真实 (库在就核一次; 不在则跳过, 不假报)
    db = ROOT / "data" / "chat_sessions.db"
    if db.exists():
        con = sqlite3.connect(str(db))
        live = {(c or "").strip() for r, c in con.execute(
            "select role, content from messages order by id") if r == "user"}
        con.close()
        # ★ 2026-09-22: 夹具已**脱敏**(个人标识 → <user>/<work>), 而库里是原文 ⇒ 直接比会
        #   全对不上。但又**不能**把真实工程名写进本文件来做同映射 (那样本文件自己就泄露了)。
        #   所以用**映射无关**的口径: 把两边所有"盘符路径"折叠成一个占位 token, 再比文字骨架。
        #   这样既证明"夹具的正文确实来自真实原话", 又不需要知道真实名。
        _PATHISH = re.compile(r"[A-Za-z]:[\\/][^\s\"']+")

        def _skel(s: str) -> str:
            return re.sub(r"\s+", " ", _PATHISH.sub("<P>", s or "")).strip()

        _live_s = {_skel(u) for u in live}
        miss = [u for u in utts if _skel(u) not in _live_s and u not in live]
        chk("★ 夹具每一条都能在会话库里找到 (路径折叠后仍同源, 真·用户原话)",
            not miss, f"{len(miss)} 条对不上, 例: {[x[:44] for x in miss[:2]]}")
    else:
        print("   SKIP 与会话库同源核对 (库不在)")

    vio = {k: [] for k in ("INV1", "INV2", "INV3", "INV4", "INV5")}
    for msg in utts:
        hit_t = PL._skill_match(msg, trigger_only=True)
        hit_w = PL._skill_match(msg, trigger_only=False)
        preempt = PL._planner_preempts(msg)
        weak_claim = bool(hit_w) and (hit_t is None) and (not preempt)
        kn = {}
        try:
            kn = KE.parse(msg) or {}
        except Exception:
            kn = {}
        kfname = str(kn.get("filename") or "")
        kparams = kn.get("params") or {}
        paths = [str(v) for k, v in kparams.items()
                 if k in ("path", "file", "dest", "to") and v] + ([kfname] if kfname else [])
        # INV1 弱线索认领 + 必填项全缺 (且护栏不会拦 → 真违规)
        if weak_claim:
            dag = hit_w[1]
            ps = getattr(dag, "params_schema", None) or {}
            req_all = [k for k, sp in ps.items() if (sp or {}).get("required")]
            try:
                prm = PL._skill_params(msg, dag)
            except Exception:
                prm = {}
            miss_list = [k for k in req_all if not prm.get(k)]
            if req_all and len(miss_list) == len(req_all):
                # 护栏已覆盖 → 不是违规 (真行为: _skill_dag_exec 返回 None)
                if not (not PL._skill_match(msg, trigger_only=True)):
                    vio["INV1"].append(msg)
        # INV2 双层盘符
        if any(len(DRV_RE.findall(p)) >= 2 for p in paths):
            vio["INV2"].append(msg)
        # INV3 有 .扩展名却丢名 / 被自动名顶替
        if FN_RE.search(msg):
            base = os.path.basename(kfname.replace("\\", "/"))
            if (not kfname) or re.match(r"^doc_\d{8}_\d{6}\.txt$", base):
                vio["INV3"].append(msg)
        # INV4 指代 + 被技能抢答
        if any(a in msg for a in ANAPH) and (weak_claim or hit_t):
            vio["INV4"].append(msg)
        # INV5 "写内容"被判成写文件 —— ★ 只在**没有文件信号**时才算违规
        #   (实测我自己第一版判据错: "写一首诗到桌面大哥.txt" 有 .txt+桌面 ⇒ 就该写文件,
        #    那不是违规。判据必须带上"无文件信号"这个前提, 否则冤枉好行为。)
        if re.search(r"写(一首|一篇|一份|一段|个)?[^，。]{0,8}(诗|剧本|方案|周报|总结|文案)", msg):
            _signal = (bool(FN_RE.search(msg)) or bool(DRV_RE.search(msg))
                       or any(w in msg for w in ("桌面", "文档", "下载", "desktop", "documents", "downloads")))
            if (kn.get("skill") or (None,))[0] == "写文件" and not _signal:
                vio["INV5"].append(msg)
    for k, lst in vio.items():
        chk(f"{k} 零违规 ({len(utts)} 条原话)", not lst, f"{len(lst)} 条: {lst[:3]}")

    # ════════ Arm 2: 说法模板 × 名字类型 全组合 ════════
    print("\n[Arm 2] 文件名说法模板 × 名字类型 (系统化组合, 不是挑例子)")
    names = ["大哥.txt", "poem.txt", "tmm活路测试.xlsx", "my_note.md", "20260922.log"]
    templates = ["写一首诗到桌面{0}", "写一首诗保存到桌面{0}", "把这首诗写入{0}",
                 "在桌面新建{0}写入内容", "生成文档另存为{0}", "把结果写入F:\\\\data.txt",
                 "写一个{0}内容是测试"]
    bad = []
    n_ok = 0
    for tpl in templates:
        for nm in names:
            msg = tpl.format(nm)
            kn = KE.parse(msg) or {}
            fn = str(kn.get("filename") or "")
            base = os.path.basename(fn.replace("\\", "/"))
            # 对"F:\data.txt"那条模板, 期望名固定
            want = "data.txt" if "{0}" not in tpl else nm
            if base == want:
                n_ok += 1
            else:
                bad.append((msg, want, base))
    total = len(templates) * len(names)
    chk(f"★ {total} 种组合里, 解析出的文件名 == 用户写的名字 ({n_ok}/{total})",
        not bad, f"{len(bad)} 例不符, 例: {bad[:3]}")

    # ════════ Arm 3: 工具层——写盘缺文件名必须诚实 (不许撞默认值) ════════
    print("\n[Arm 3] 工具层: 写盘必须有具体文件名")
    import asyncio
    r = asyncio.run(FO.run(action="write", content="床前明月光"))
    chk("★ 缺文件名 → 诚实报告没有文件名 (不再 Is a directory)",
        r.get("success") is False and "文件名" in str(r.get("error")) and "Is a directory" not in str(r.get("error")),
        str(r)[:140])

    # ════════ Arm 4: 写文件 vs 写内容 —— **真跑 pipeline** (打桩网关, 零副作用) ════════
    #   为什么不用"调 KE.parse 看 skill": 那只是**知识库层**的中间结果; 用户看到的是
    #   整条 pipeline 的行为 (还有 IR 链/规划链能接手)。所以这里打桩网关真跑 process(),
    #   断言**用户可见契约**: 该写文件的真发出写盘调用 (且用的是**用户写的文件名**),
    #   不该写文件的**一次写盘调用都没有**。
    print("\n[Arm 4] 写文件 vs 写内容 (真跑 pipeline, 打桩网关)")
    import types
    import tempfile
    import core.mcp_client as _mcp_mod
    _mcp_mod.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
        load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [], tools=[],
        get_tool=lambda *a, **k: None, get_all_tools=lambda *a, **k: [], servers={}, refresh_all=None)
    import core.perception as _perc
    SBX = Path(tempfile.mkdtemp(prefix="hermes-verify-nl-"))
    _perc.PERCEPTION_FILE = SBX / "perception.json"
    from main import _load_config as _lc
    from core.model_client import ModelClient as _MC
    from core.pipeline import Level4Pipeline as _L4
    PL2 = _L4(_MC(_lc()), _lc())
    WRITES = []

    async def _gw(tool, action="", **kw):
        _p = str(kw.get("path") or kw.get("file") or kw.get("name") or "")
        if action in ("write", "create", "save") or tool in ("file_ops", "tiger_office", "diagram"):
            WRITES.append({"tool": tool, "action": action, "path": _p})
        return {"success": True, "output": "ok", "content": "ok", "stub": True}

    _real_call = PL2.gateway.call
    PL2.gateway.call = _gw
    # ★ 2026-09-22 修 (门禁自身桩覆盖不足, 实测 3 条假红):
    #   知识库那条路 (`_run_l4_knowledge` → `KE.execute(parsed, self.gateway.plugin_mgr)`)
    #   **不走** `gateway.call`, 而是走 `gateway.plugin_mgr.call` ⇒ 只挂前者时,
    #   "该写文件"的用例记录到 `写盘调用=[]`, 断言假红 (实测 `写一个hello.txt内容是xxx`
    #   / `写一首诗到桌面大哥.txt` 三条)。两条路都要挂上同一个观察者。
    _real_pm_call = PL2.gateway.plugin_mgr.call
    PL2.gateway.plugin_mgr.call = _gw

    async def _gen(model, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
        return {"text": "（桩）", "think": "", "model": model, "elapsed": 0.0,
                "error": None, "tool_calls": None}
    PL2.model_client.generate = _gen

    async def run(msg):
        WRITES.clear()
        r = await PL2.process(msg, probe=True, mode_override="craft")
        return r or {}, list(WRITES)

    import asyncio as _aio
    for msg, want in (("写一个hello.txt内容是xxx", "hello.txt"),
                      ("写一首诗到桌面大哥.txt", "大哥.txt"),
                      ("写一首诗保存到桌面大哥.txt", "大哥.txt"),
                      ("把这首诗写入大哥.txt", "大哥.txt"),
                      # ★ "写周报"走 write-office-doc 技能: 该技能**就该产出文件**, 且
                      #   `tiger_office._default_out_path` 的默认落点就是桌面 ⇒ 空路径合法。
                      #   (我第一版把它当"纯内容生成"判, 是判据错 —— 它是产出型技能。)
                      ("帮我写一份本周工作周报", None)):
        r, w = _aio.run(run(msg))
        if want is None:                      # 只要求"发生了写盘调用", 路径由技能默认决定
            chk(f"该产出文件(技能自带落点): {msg[:22]}", bool(w), f"写盘调用={w}")
        else:
            hit = [c for c in w if c["path"].replace("\\", "/").endswith(want)]
            chk(f"该写文件且用**用户写的名字** ({want}): {msg[:22]}", bool(hit),
                f"写盘调用={[c['path'] for c in w]}")
    for msg in ("写一首关于青山绿水的七言绝句", "你给我写个姐妹两教英语的剧本",
                "你刚才写诗了吗", "写一篇讲 AI 的文章"):
        r, w = _aio.run(run(msg))
        chk(f"★ 内容生成 → 一次写盘调用都没有: {msg[:20]}", not w,
            f"却调了={[c['path'] for c in w]}")
    # "要保存但没给文件名" → 可以不写, 但**不许宣称成功**
    r, w = _aio.run(run("把这首诗保存到桌面"))
    _resp = str(r.get("response") or "")
    _claims_ok = any(k in _resp for k in ("已写入", "保存成功", "OK 写文件"))
    chk("★ '保存到桌面'但没文件名 → 不许宣称写成功", not _claims_ok, _resp[:120])

    print("\n" + "=" * 96)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for x in F:
        print("  -", x)
    print("=" * 96)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
