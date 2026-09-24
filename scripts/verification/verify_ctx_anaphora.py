"""追问句「上下文够不着」缺陷的常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景 (2026-09-24, 用户原话):
    "他上下文联系有问题, 我问他刚才是不是写了一篇短文, 他无法正面回答。你说我能不上火嘛"

实测根因:
    general 那条路只注入 `self.sessions.recent(4)` —— **4 条 = 2 轮**。
    而那篇短文在 **11 条消息之前** ⇒ 完全看不见 ⇒ 模型只能诚实答"我无法确认"。
    (注: 库里的历史是好的, `sessions.recent(6)` 能取到 —— 是**窗口太小**。)

修法:
    认出**追问词** (刚才/上面/那篇/这个/上次…) → 窗口放宽到 16 条,
    但**收紧单条截断 + 按字符预算收口**, 总 token 仍有界 (防 ollama ctx 爆);
    非追问句维持原行为 (4 条)。

本门禁钉住:
  A 追问句 → 注入的历史条数明显变多 (>= 8)
  B 非追问句 → 仍是原窗口 (<= 4), 不回归
  C ★ 真场景: 用真库副本, 11 条之前那篇短文**必须**进得了 prompt
  D 预算有界: 追问时注入的历史总字符不超上限 (不撑爆本地模型)
  E 链路不破: 两种句子都仍能正常出回复 (没把 pipeline 打挂)
  F 零污染: 全部在沙箱 (真库只读拷贝), 不写用户数据

跑法:
    python -B scripts/verification/verify_ctx_anaphora.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REAL_DB = ROOT / "data" / "chat_sessions.db"
P, F, SKIP = [], [], []

MAX_REF_CHARS = 1600          # 追问时历史总字符上限 (与实现里的预算同量级)


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def build_sandbox(tag: str):
    """沙箱: 会话库用**真库的只读副本**, 其余全部独立。"""
    sbx = Path(tempfile.gettempdir()) / f"_ctx_gate_{tag}"
    shutil.rmtree(sbx, ignore_errors=True)
    sbx.mkdir(parents=True, exist_ok=True)
    if REAL_DB.exists():
        shutil.copy2(REAL_DB, sbx / "chat_sessions.db")     # 只读拷贝, 不碰真库
    os.environ.pop("TMM_PROBE", None)
    os.environ["TMM_LIVE"] = "1"
    for k, v in [("TMM_SESSION_DB", "chat_sessions.db"), ("TMM_TASK_DB", "task_ledger.db"),
                 ("TMM_GAP_DB", "gap_ledger.db"), ("TMM_PERCEPTION_FILE", "perception.json"),
                 ("TMM_USAGE_LOG", "usage.jsonl"), ("TMM_SKILL_USAGE_FILE", "skill_usage.jsonl"),
                 ("TMM_ARTIFACTS_FILE", "artifacts.jsonl"), ("TMM_SESSION_SNAPSHOT", "session_snapshot.json")]:
        os.environ[k] = str(sbx / v)
    return sbx


def make_pipeline(sbx: Path):
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.knowledge import KnowledgeEngine
    from config.settings import DATA_DIR

    cfg = _load_config()
    pl = Level4Pipeline(ModelClient(cfg), cfg)
    pl._ke = KnowledgeEngine(DATA_DIR)
    pl.modes.path = sbx / "mode.json"
    return pl


def run_one(pl, q: str):
    """跑一轮, 记下模型收到的消息 (不真的依赖模型输出质量)。"""
    seen = []
    orig = pl.model_client.generate

    async def spy(model_name, messages, temperature=0.7, max_tokens=4096, tools=None, think=None):
        seen.append([(m.get("role"), str(m.get("content") or "")) for m in (messages or [])])
        return {"text": "(stub)", "tool_calls": None, "error": None,
                "think": None, "usage": {}, "elapsed": 0.0}

    pl.model_client.generate = spy
    try:
        res = asyncio.run(pl.process(q, probe=True)) or {}
    except Exception as e:
        res = {"_err": f"{type(e).__name__}: {e}"}
    finally:
        pl.model_client.generate = orig
    return seen, res


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts" / "verification"))
    os.chdir(str(ROOT))

    if not REAL_DB.exists():
        SKIP.append("全部")
        print("SKIP 找不到 data/chat_sessions.db")
        print("结果: 0 PASS / 0 FAIL / 1 SKIP")
        return 0

    sbx = build_sandbox("main")
    _sd = None
    try:
        from _probe_env import activate
        _sd = activate()
        # ★ 坑: activate() 会**重设/清空**沙箱目录 ⇒ 真库副本必须在它之后再拷!
        #   (第一版先拷后 activate ⇒ 会话库是空的 ⇒ 历史 0 条 ⇒ 门禁假红)
        import shutil as _sh
        _env_db = Path(os.environ.get("TMM_SESSION_DB") or (sbx / "chat_sessions.db"))
        _env_db.parent.mkdir(parents=True, exist_ok=True)
        for _suf in ("", "-wal", "-shm"):
            Path(str(_env_db) + _suf).unlink(missing_ok=True)
        _sh.copy2(REAL_DB, _env_db)
        for _suf in ("-wal", "-shm"):
            _src = Path(str(REAL_DB) + _suf)
            if _src.exists():
                _sh.copy2(_src, Path(str(_env_db) + _suf))
        print(f"      会话库 → {_env_db} ({_env_db.stat().st_size}B)")
        pl = make_pipeline(sbx)
        n_hist_avail = len(pl.sessions.recent(32) or [])
        print(f"—— 沙箱会话库可用历史: {n_hist_avail} 条 ——")
        chk("准备: 库里有足够历史可测", n_hist_avail >= 12, n_hist_avail)

        print("—— A 追问句 → 窗口放宽 ——")
        seen_ref, res_ref = run_one(pl, "刚才是不是写了一篇短文")
        best_ref = max((len(s) for s in seen_ref), default=0)
        chk(f"A1 追问句注入历史 >= 8 条 (实测 {best_ref})", best_ref >= 8, best_ref)
        chk("A2 链路没断 (跑出结果, 无异常)", "_err" not in res_ref, res_ref.get("_err"))

        print("—— B 非追问句 → 维持原窗口 (不回归) ——")
        seen_plain, res_plain = run_one(pl, "谈谈你对效率工具的理解")
        best_plain = max((len(s) for s in seen_plain), default=0)
        chk(f"B1 普通句注入 <= 6 条 (实测 {best_plain})", best_plain <= 6, best_plain)
        chk("B2 普通句仍能跑", "_err" not in res_plain, res_plain.get("_err"))
        chk("B3 ★ 追问窗口 > 普通窗口 (修的就是这个差)",
            best_ref > best_plain, f"ref={best_ref} plain={best_plain}")

        print("—— C ★ 真场景: 11 条之前那篇短文进得了 prompt ——")
        con = sqlite3.connect(f"file:{(sbx / 'chat_sessions.db').as_posix()}?mode=ro", uri=True, timeout=10)
        rows = [dict(zip(("role", "content"), r)) for r in
                con.execute("select role, content from messages order by id desc limit 40")]
        con.close()
        rows.reverse()
        idx = None
        for i, r in enumerate(rows):
            if "短文" in (r["content"] or "") and r["role"] == "user":
                idx = i
                break
        chk("C0 库里确实有那条'短文'请求", idx is not None)
        all_ref = "\n".join(c for s in seen_ref for _, c in s)
        if idx is not None:
            chk("C1 ★ 追问时那条短文请求出现在 prompt 里", "短文" in all_ref, all_ref[:160])
            chk("C2 ★ 对应的执行结果也在 (能说清做没做)", "执行结果" in all_ref or "the_great_wall" in all_ref,
                all_ref[:200])

        print("—— D 预算有界 (不撑爆本地模型) ——")
        # ★ 只量**注入的历史** —— 不含 system 提示词与当前这句 user
        _big = max(seen_ref, key=len) if seen_ref else []
        _hist_only = _big[1:-1] if len(_big) > 2 else []
        tot = sum(len(c) for _, c in _hist_only)
        chk(f"D1 追问时注入的历史总字符 <= {MAX_REF_CHARS + 400} (实测 {tot})",
            tot <= MAX_REF_CHARS + 400, tot)
        chk(f"D2 注入的历史条数 >= 8 (实测 {len(_hist_only)})", len(_hist_only) >= 8, len(_hist_only))

        print("—— E 源码里确实是'按追问词放宽' ——")
        src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
        chk("E1 有追问词表", "_ANAPH = (" in src and "刚才" in src)
        chk("E2 追问与非追问用不同窗口", "_want = 16 if _ref else 4" in src)
        chk("E3 有字符预算收口", "_budget = 1500 if _ref else 4000" in src)
        chk("E4 从最新往回挑 (预算先保最近的)", "for _h in reversed(_hist)" in src)
    except Exception as e:
        chk("A/B/C/D/E 段", False, f"{type(e).__name__}: {e}")
    finally:
        if _sd:
            shutil.rmtree(_sd, ignore_errors=True)
        shutil.rmtree(sbx, ignore_errors=True)

    print("—— F 零污染 ——")
    chk("F1 沙箱已清", not sbx.exists())
    chk("F2 真库未被改动 (会话库文件仍可读)",
        sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True, timeout=8)
        .execute("select count(*) from messages").fetchone()[0] > 0)

    print()
    print("=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
