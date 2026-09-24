"""感知层守卫 — 常驻验证器 (2026-09-20)

为什么要有这一份: 感知层写的是 data/perception.json, 而它有两处会**反向影响模型行为**:
  ① 纠正记录 → get_correction_context() 注入系统提示词
     ([Learned corrections — do NOT repeat these mistakes])
  ② 实体/关系 → 注入项目上下文
所以"感知层被污染"不是"多两条记录", 而是行为被改。本份守四件事:

  A ★ 纠正检测误报: 裸 `别` 命中 识别/特别/区别; 裸 `不是` 命中 是不是(提问)
  B ★★ probe 守卫: probe=True 的运行不得写感知 (三处写入点都要有守卫)
  C ★★ 隔离三态: TMM_PERCEPTION_FILE 指向沙箱 / 不设变量 → 真库
  D ★ 结构回归: 标记表结构 + 每个 analyze 调用点都有 probe 判断
  E 零副作用: 真感知库指纹不变 (本验证器自己跑真 pipeline 也不许污染)
"""
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F = [], []
REAL_PERC = ROOT / "data" / "perception.json"
PY = sys.executable or "python"


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def _fp(p: Path):
    return (p.stat().st_size, hashlib.md5(p.read_bytes()).hexdigest()[:12]) if p.is_file() else None


INNOCENT = ["你能识别我的电脑吗", "帮我识别图片文字", "这个和那个有什么区别", "特别想知道天气",
            "级别不够吧", "是不是该换个方案", "把文件转换成 PDF", "分别处理这两个", "告别过去",
            "换个字体", "别人怎么说", "别的呢", "个别情况", "差别很大", "鉴别一下", "辨别真假",
            "性别的区别", "派别之争", "交换文件", "把 A 替换成 B"]
REAL = ["不对，重来", "不是这样，改成红色", "错了，应该是苏州", "别再忘了", "别这样",
        "不要那样做", "重新来一遍", "改成红色", "换成苏州"]


def main() -> int:
    before = _fp(REAL_PERC)
    try:
        from core.perception import PerceptionEngine
        pe = PerceptionEngine.__new__(PerceptionEngine)

        # ── A 纠正检测 ──
        print("        —— A ★ 纠正检测 (误报 = 污染系统提示词) ——")
        fp = [m for m in INNOCENT if pe._detect_corrections(m)]
        chk(f"★ {len(INNOCENT)} 句正常话 0 误报", not fp, f"误报: {fp}")
        miss = [m for m in REAL if not pe._detect_corrections(m)]
        chk(f"★ {len(REAL)} 句真纠正 0 漏判", not miss, f"漏判: {miss}")
        chk("裸 `别` 不命中 识别/区别/特别/级别",
            all(pe._detect_corrections(f"这是{w}的事") == []
                for w in ("识别", "区别", "特别", "级别", "分别", "告别", "个别", "差别")))
        chk("`换` 不命中 转换成/替换成/变成",
            all(pe._detect_corrections(w) == [] for w in ("把文件转换成 PDF", "把 A 替换成 B", "把水变成冰")))
        chk("一句话最多记一条", len(pe._detect_corrections("不对，不是这样，错了")) == 1)
        m0 = pe._detect_corrections("不对，重来")
        chk("marker 是可读词 (不是正则)", bool(m0) and m0[0]["marker"] == "不对", str(m0))

        # ── D 结构回归 ──
        print("        —— D ★ 结构回归 ——")
        ms = PerceptionEngine._CORRECTION_MARKERS
        chk("标记表是 (词, 正则) 二元组列表", isinstance(ms, list) and len(ms) >= 8
            and all(len(x) == 2 for x in ms), str(ms)[:120])
        pats = dict(ms)
        chk("★ 裸 `别` 没回到表里", pats.get("别") != "别", str(pats.get("别")))
        chk("★ 裸 `不是` 没回到表里", pats.get("不是") != "不是", str(pats.get("不是")))
        chk("★ 过松的裸 `换` 已弃用", "换" not in pats)
        ok = True
        for w, pt in ms:
            try:
                re.compile(pt)
            except Exception:
                ok = False
        chk("表里正则全可编译", ok)

        src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
        lines = src.split("\n")
        sites = [i for i, l in enumerate(lines) if "perception.analyze(" in l and not l.strip().startswith("#")]
        chk("找到 perception.analyze 写入点", bool(sites), "定位方式可能失效, 请更新验证器")
        ung = [(i + 1, lines[i].strip()[:60]) for i in sites
               if "probe" not in "\n".join(lines[max(0, i - 6):i + 1])]
        chk(f"★★ 全部 {len(sites)} 个写入点都有 probe 守卫", not ung, f"漏网: {ung}")

        import inspect as _inspect
        from core.pipeline import Level4Pipeline
        for nm in ("_process_external", "_ir_chain_exec", "_analysis_preread"):
            sig = _inspect.signature(getattr(Level4Pipeline, nm))
            chk(f"{nm} 收 probe 参数 (默认 False = 加性)",
                "probe" in sig.parameters and sig.parameters["probe"].default is False)

        # ── B probe 守卫真跑 ──
        print("        —— B ★★ probe=True 不写感知 (真跑) ——")
        sbox = Path(tempfile.mkdtemp(prefix="_vfy_perc_"))
        env = dict(os.environ)
        env["TMM_PERCEPTION_FILE"] = str(sbox / "perc.json")
        env["TMM_SESSION_DB"] = str(sbox / "s.db")
        env["TMM_GAP_DB"] = str(sbox / "g.db")
        code = f'''
import asyncio, logging, os, sys, types
sys.path.insert(0, r"{ROOT}"); os.chdir(r"{ROOT}")
logging.disable(logging.CRITICAL)
import core.mcp_client as _mc
_mc.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
    load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [], tools=[], get_tool=lambda *a, **k: None)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
from storage.session_store import SessionStore
from pathlib import Path
sb = Path(r"{sbox}")
cfg = _load_config(); pl = Level4Pipeline(ModelClient(cfg), cfg)
pl.sessions = SessionStore(sb / "s.db")
calls = []
class Spy:
    def analyze(self, m, r, memory=None): calls.append(m); return {{}}
    def get_project_context(self): return ""
    def get_graph_context(self): return ""
pl.perception = Spy()
asyncio.run(pl._process_external("你能识别我的电脑吗", "ollama", 0.0, probe=True))
print("SPY_CALLS", len(calls))
print("PERC_EXISTS", (sb / "perc.json").exists())
'''
        r = subprocess.run([PY, "-B", "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=300)
        out = r.stdout
        chk("★ probe=True 时感知 analyze 未被调用", "SPY_CALLS 0" in out, out[-300:])
        shutil.rmtree(sbox, ignore_errors=True)

        # ── C 隔离三态 ──
        print("        —— C ★★ 隔离三态 ——")
        probe_py = ("import sys; sys.path.insert(0, r'%s'); "
                    "import core.perception as P; print(P.PERCEPTION_FILE)" % ROOT)
        e1 = dict(os.environ)
        e1["TMM_PERCEPTION_FILE"] = "C:/tmp/fake_perc.json"
        o1 = subprocess.run([PY, "-c", probe_py], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=e1).stdout.strip()
        chk("★ 设了变量 → 指到沙箱", o1.replace("\\", "/").endswith("C:/tmp/fake_perc.json"), o1)
        e2 = {k: v for k, v in os.environ.items() if k != "TMM_PERCEPTION_FILE"}
        o2 = subprocess.run([PY, "-c", probe_py], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=e2).stdout.strip()
        chk("★ 没变量 → 真库 (活服务不受影响)", o2.endswith("data\\perception.json") or
            o2.endswith("data/perception.json"), o2)
        chk("门禁注入感知隔离变量",
            "TMM_PERCEPTION_FILE" in (ROOT / "scripts" / "hermes_verify.py").read_text(
                encoding="utf-8", errors="replace"))

        # ── E 零副作用 ──
        print("        —— E 零副作用 ——")
        chk("★ 真感知库未被本验证器改动", _fp(REAL_PERC) == before,
            f"{before} -> {_fp(REAL_PERC)}")
        j = __import__("json").loads(REAL_PERC.read_text(encoding="utf-8")) \
            if REAL_PERC.is_file() else {}
        bad = [c for c in (j.get("corrections") or [])
               if "识别" in str(c.get("user_said", "")) or "特别" in str(c.get("user_said", ""))]
        chk("★ 真库里没有'识别/特别'类假纠正", not bad, str(bad)[:160])
    finally:
        pass
    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
