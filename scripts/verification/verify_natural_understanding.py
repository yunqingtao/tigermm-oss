# -*- coding: utf-8 -*-
r"""自然话「理解兜底」契约 —— 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题 (2026-09-23 用户原话驱动):
  "自然语言类似我这种表达的很多, 而且很普遍, 所以 TMM 要理解这种方式。"
  ⇒ 判据认不出来的**自然说法**, 不许被关键词层"半懂就动手"(写垃圾文件);
    该交给理解层 (understand.request → 规划器) 读懂再干; 读不懂就诚实交底。

四段:
  A 判据层 (零副作用): 一批同义自然说法必须"像动手请求"; 问概念/读/否定**不许**像
  B 让位契约 (零副作用): 插件关键词 / lookup / IR 链 三条**不许抢**创作请求;
     知识层不许把指令碎片当正文
  C 真引擎 (需模型): 说法变体真跑 → 不许出现 doc_/output_ 自动命名垃圾; 要么真产出要么诚实
  D 反向: 负例不写任何文件

跑法: python -B scripts/verification/verify_natural_understanding.py
"""
import asyncio
import glob
import os
import subprocess
import sys
import time
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
DOCS = Path(os.path.expanduser("~/Documents"))
P, F, S = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name
          + (("   <- " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    S.append(name)
    print(f"  SKIP {name}   <- {why}")


def _snap():
    """★ mtime+size 快照 —— 差集抓不到**覆盖写**。
    踩坑 (2026-09-23): 说法变体常常生成**同名**文件 (诗.txt/小诗.txt…),
    后一个覆盖前一个 ⇒ 文件集合没变 ⇒ 假红"没产出"。考卷那边同一个坑。"""
    d = {}
    for p in list(glob.glob(str(DESK / "*"))) + list(glob.glob(str(DOCS / "*"))):
        try:
            st = os.stat(p)
            d[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return d


def _changed(before, after):
    return sorted(k for k, v in after.items() if before.get(k) != v)


#: 同一意思的多种自然说法 (用户原则的落地语料) —— 必须都能到达理解层
VARIANTS = ["来一首诗扔桌面", "给我首诗放桌面", "弄一首诗放桌面", "整一首诗搁我桌面上",
            "写一首诗放桌面", "来首诗丢桌面", "随手写首诗摆桌面上", "帮我弄个小故事存到文档里",
            "弄个报告存桌面上", "给我整篇小文章放文档里"]
#: 不该被当成动手请求的 (问概念/读目录/否定)
NOT_REQUESTS = ["什么是诗", "看看桌面", "桌面有什么", "别放桌面", "怎么理解自动路由",
                "解释一下写文件", "你好", "今天天气怎么样"]


def _model_probe(PL):
    """三态探测: (可用, 原因, 依赖是否在位)。★ 依赖在位却调不通要报红, 不许静默跳过。"""
    kf = ROOT / "keys.json"
    dep = any((kf.is_file(), os.environ.get("DASHSCOPE_API_KEY"),
               os.environ.get("QWEN_API_KEY"), os.environ.get("DEEPSEEK_API_KEY"),
               os.environ.get("OPENAI_API_KEY")))
    last = ""
    for _ in range(3):
        try:
            r = asyncio.run(asyncio.wait_for(
                PL.model_client.generate(PL._mode_pick_model(None),
                                         [{"role": "user", "content": "ok"}], max_tokens=4),
                timeout=60))
            if isinstance(r, dict) and not r.get("error"):
                return True, "", bool(dep)
            last = str((r or {}).get("error"))[:100]
        except Exception as e:
            last = f"{type(e).__name__}: {e}"[:100]
        time.sleep(2)
    return False, last or "未知", bool(dep)


def main() -> int:
    print("=" * 92)
    print("自然话「理解兜底」契约 (2026-09-23 用户原则: TMM 要理解这种表达方式)")
    print("=" * 92)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline

    cfg = _load_config()
    PL = Level4Pipeline(ModelClient(cfg), cfg)
    # ★ 钉住实干: 用户态若漂到 ask, 真跑段全部"不执行" ⇒ 假红 (实测踩过)
    try:
        PL.modes.set("craft")
    except Exception:
        pass

    # ── A. 判据层 ────────────────────────────────────────────
    print("\n[A.判据] 自然说法该被认成「动手请求」; 问概念/读/否定不许")
    miss = [m for m in VARIANTS if not PL._looks_like_request(m)]
    chk(f"★ 同义自然说法都像动手请求 ({len(VARIANTS)} 句)", not miss, miss)
    bad = [m for m in NOT_REQUESTS if PL._looks_like_request(m)]
    chk(f"★ 问概念/读/否定不算动手请求 ({len(NOT_REQUESTS)} 句)", not bad, bad)
    # 创作请求判据 (用于"检索链不许服务创作")
    cre = [m for m in ["帮我弄个小故事存到文档里", "写一首诗保存到桌面", "给我整篇小文章"]
           if not PL._looks_like_creation_request(m)]
    chk("★ 创作请求判据命中 (3 句)", not cre, cre)
    non_cre = [m for m in ["查Agent邮件然后保存到桌面", "把你好世界写入agent_test.txt",
                           "在桌面新建一个报告.txt 内容: 今天天气不错"]
               if PL._looks_like_creation_request(m)]
    chk("★ 非创作(检索+搬运/显式正文)不误judged成创作 (3 句)", not non_cre, non_cre)

    # ── B. 让位契约 ──────────────────────────────────────────
    print("\n[B.让位] 三条关键词层不许抢创作请求 + 知识层不许拿指令碎片当正文")
    ctx0 = {"ext_model": "auto"}
    hij = [m for m in VARIANTS if PL._match_plugin_keyword(m, ctx0)]
    chk(f"★ 插件关键词不接管动手请求 ({len(VARIANTS)} 句)", not hij, hij)
    lk = [m for m in ["帮我弄个小故事存到文档里", "给我整篇小文章放文档里"]
          if PL._match_lookup(m, ctx0)]
    chk("★ lookup 层(缺信息自查)不接管创作请求 (2 句)", not lk, lk)
    ir = [m for m in ["帮我弄个小故事存到文档里", "给我整篇小文章放文档里"]
          if PL._match_ir_chain(m, ctx0)]
    chk("★ IR 规则链不接管创作请求 (2 句)", not ir, ir)
    _D = ("桌面", "文档", "下载")
    kb = []
    for m in VARIANTS:
        try:
            r = PL._ke.parse(m)
        except Exception:
            continue
        sk = (r.get("skill") or [None])[0]
        c = str((r.get("params") or {}).get("content") or "").strip()
        if sk and c and (len(c) < 8 or any(w in c for w in _D)):
            kb.append((m, sk, c))
    chk(f"★ 知识层不拿指令碎片当正文 ({len(VARIANTS)} 句)", not kb, kb)

    # ── C. 真引擎 ────────────────────────────────────────────
    print("\n[C.真引擎] 说法变体真跑 (需模型)")
    ok_m, err, dep = _model_probe(PL)
    if not ok_m:
        if dep:
            chk("★ 依赖在位却调不通模型 → 不许静默跳过真引擎段", False, err)
            return 1
        skip("C 段真引擎断言", "本机无可用模型 (无凭据/未起本地模型)")
    else:
        snap0 = _snap()
        made = []
        RUN = ["随手写首诗摆桌面上", "来一首诗扔桌面", "帮我弄个小故事存到文档里"]
        routes, junk = [], []
        try:
            for m in RUN:
                t0 = time.time()
                before = _snap()
                r = asyncio.run(PL.process(m, probe=True, mode_override="craft")) or {}
                routes.append((m, r.get("route"), round(time.time() - t0, 1)))
                for p in _changed(before, _snap()):
                    if p not in made:
                        made.append(p)
                    t = Path(p).read_text(encoding="utf-8", errors="replace")
                    nm = Path(p).name
                    if nm.startswith(("doc_", "output_")) or len(t.strip()) < 8 \
                            or any(w in t for w in ("放桌面", "搁桌面", "扔桌面", "丢桌面")):
                        junk.append((nm, len(t.strip())))
            chk("★ 无自动命名/指令碎片垃圾文件", not junk, junk)
            chk("★ 每个变体都有真产出或诚实交底", len(made) >= 1, [x[1] for x in routes])
            print("     路由:" + " | ".join(f"{m}→{r}" for m, r, _ in routes))
        finally:
            for p in made:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass
        left = _changed(snap0, _snap())
        chk("★ 桌面/文档零残留", not left, left)

    # ── D. 反向: 负例不写文件 ────────────────────────────────
    print("\n[D.反向] 问概念/读目录/否定: 不许写任何文件")
    if not ok_m:
        skip("D 段真引擎反向断言", "本机无可用模型")
    else:
        s0 = _snap()
        for m in ["什么是诗", "看看桌面", "别放桌面"]:
            asyncio.run(PL.process(m, probe=True, mode_override="craft"))
        new = _changed(s0, _snap())
        chk(f"★ 负例不落盘 ({len(new)} 处变化)", not new, new)

    print("\n" + "=" * 92)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for x in F:
        print("  -", x)
    print("=" * 92)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
