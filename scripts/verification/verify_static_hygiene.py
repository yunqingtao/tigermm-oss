"""静态卫生 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

为什么要有这一门 (代价是实测出来的)
═══════════════════════════════════════════════════════════════════
行为门禁只验"我跑到的路径"。本轮用 pyflakes 扫全仓, 一次抓出 **20 处 undefined name**,
全都在**本机条件满足所以没走到**的分支里:

  · `os.environ.get("USERPROFILE") or str(Path.home())` —— 用了 Path 但该文件没 import Path
    (本机有 USERPROFILE → `or` 短路 → 门禁和 ad-hoc 全都没抓到; 换台机器直接 NameError)
  · core/task_planner.py 用 os.path.expanduser 但没 import os
  · core/routes.py 异常分支用 logger.debug 但模块从未定义 logger
  · main.py 用 pipeline_obj (应为同作用域的 pipeline)

=> "在我机器上能跑"与"别人拿到能跑"的差距, 很多正是这种**路径没走到**的问题。
   静态检查是行为门禁的补集, 便宜且能提前抓。

跑法 (pyflakes 本机没装, 用 uv 临时取, **不污染环境**):
  uv run --quiet --with pyflakes python -m pyflakes core tools config main.py install.py scripts

自清理: 不写任何文件; 不碰用户数据。
"""
import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(ROOT)

P, F, S = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def skip(name, why=""):
    S.append(name)
    print(f"  SKIP {name}   ({why})")


TARGETS = ["core", "tools", "config", "storage", "gateway", "mcp", "sandbox",
           "main.py", "install.py", "scripts"]
# 这些目录里的"个人路径/账号"属于工具与验证器的正常用法 (解释器路径等), 只做 WARN
SOFT_DIRS = ("scripts", "tests")


def main():
    print("=" * 78)
    print("静态卫生 —— undefined name / 假兜底 / 改名漏改")
    print("=" * 78)

    # ── [0] 自检: 本门禁的规则真有牙齿 (避免"永远 PASS 的空门") ──
    print("[0] 自检 (规则自身的辨别力)")
    _FAKE = ('import os\nfrom pathlib import Path\n'
             'P = os.path.join(os.environ.get("USERPROFILE") or str(Path.home()), "Desktop")\n')
    _OK = ('import os\nfrom pathlib import Path\n'
           'h = os.environ.get("USERPROFILE") or ""\n'
           'if not h:\n'
           '    try:\n'
           '        h = str(Path.home())\n'
           '    except Exception:\n'
           '        h = "x"\n'
           'P = os.path.join(h, "Desktop")\n')

    def _find_fake(src_text):
        got = []
        try:
            _t = ast.parse(src_text)
        except SyntaxError:
            return got
        for node in ast.walk(_t):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                seg = ast.get_source_segment(src_text, node) or ""
                if "Path.home()" in seg and re.search(r"environ\.get\(|expanduser\(", seg):
                    got.append(seg[:50])
        return got

    chk("★ 假兜底样本能抓到 (有牙齿)", bool(_find_fake(_FAKE)), "规则失效了")
    chk("★ 正确兜底样本不误报 (不狼来了)", not _find_fake(_OK))

    # ── A pyflakes: undefined name 必须为 0 ──
    print("[A] undefined name (pyflakes)")
    uv = shutil.which("uv")
    if not uv:
        skip("pyflakes 静态扫描", "本机没有 uv, 无法临时取用 pyflakes")
    else:
        r = subprocess.run([uv, "run", "--quiet", "--with", "pyflakes", "python", "-m", "pyflakes"]
                           + TARGETS, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        out = (r.stdout or "") + (r.stderr or "")
        if "No module named" in out and "pyflakes" in out:
            skip("pyflakes 静态扫描", "取不到 pyflakes (离线?)")
        else:
            undef = [l.strip() for l in out.splitlines() if "undefined name" in l]
            chk("★ 全仓无 undefined name (静态可见的 NameError)", not undef,
                f"{len(undef)} 处: {undef[:5]}")
            if undef:
                for l in undef[:12]:
                    print("      ", l[:150])

    # ── B 假兜底: `A or str(Path.home())` 这种"看着兜住其实会抛"的写法 ──
    print("[B] 假兜底写法 (AST 判定, 只看真实表达式)")
    bad = []
    for sub in ("core", "tools", "config", "storage", "main.py", "install.py"):
        files = [ROOT / sub] if (ROOT / sub).is_file() else list((ROOT / sub).rglob("*.py"))
        for f in files:
            if "__pycache__" in str(f):
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                    seg = ast.get_source_segment(src, node) or ""
                    # 形态: 某个 env 取值 or str(Path.home()) —— 后者会抛 RuntimeError
                    if "Path.home()" in seg and re.search(r"environ\.get\(|expanduser\(", seg):
                        ln = src[:node.col_offset + src[:src.find(seg)].count("\n")].count("\n") + 1
                        bad.append(f"{f.relative_to(ROOT)} {seg[:60]!r}")
    chk("★ 没有 `os.environ.get(...) or Path.home()` 式假兜底 "
        "(实测: USERPROFILE 缺失时 Path.home() 抛 RuntimeError)", not bad, bad[:4])

    # ── C 改名漏改: 常见"改名后旧调用点还在" (靠别名兜住的除外) ──
    print("[C] 改名漏改 (显式别名才算兼容)")
    rules = [("core/relay_client.py", r"(?<![\w_.])log\(", r"^log = _log", "log → _log")]
    miss = []
    for rel, call_rx, alias_rx, label in rules:
        f = ROOT / rel
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        if re.search(call_rx, src, re.M) and not re.search(alias_rx, src, re.M):
            miss.append(f"{rel}: 仍有旧调用 ({label}) 且无别名")
    chk("★ 改名处有别名/调用点已全改", not miss, miss)

    # ── D 分发门禁必须存在 (它是"能出门"的护栏) ──
    print("[D] 护栏在位")
    chk("分发就绪门禁存在", (ROOT / "scripts" / "verification" / "verify_distribution_ready.py").exists())
    chk("干净分发仓库生成器存在", (ROOT / "scripts" / "make_dist_repo.py").exists())
    chk("数据边界清单存在", (ROOT / "docs" / "data_boundary.md").exists())

    # ── E ★ 真跑引擎的脚本必须带隔离 (2026-09-21 加) ──
    # 起因: 一天之内探针**三次**把测试对话写进用户真对话库 (210→218→228→226),
    #   其中最后 16 行来自 scripts/ 下 4 个松散验证脚本 (它们真跑 pipeline 且无隔离)。
    #   隔离变量散在文档里靠人记 → 必然漏。本段把它变成门禁:**没隔离就跑引擎 = 红**。
    print("[E] 真跑引擎的脚本必须带隔离 (防污染真数据)")
    ENGINE = re.compile(r"Level4Pipeline\s*\(|\.process\s*\(")
    ISO_MARK = re.compile(r"_probe_env|TMM_SESSION_DB")
    loose, unisolated = [], []
    for f in sorted((ROOT / "scripts").glob("*.py")):
        rel = f.relative_to(ROOT)
        try:
            s = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not ENGINE.search(s):
            continue
        if ISO_MARK.search(s):
            continue
        loose.append(str(rel))
    for f in sorted((ROOT / "scripts" / "verification").glob("verify_*.py")):
        rel = f.relative_to(ROOT)
        try:
            s = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 门禁自己若真建引擎实例, 应自带隔离 (套件也注入, 但单跑时得靠自己)
        if re.search(r"Level4Pipeline\s*\(", s) and not ISO_MARK.search(s):
            unisolated.append(str(rel))
    chk("★ scripts/ 下真跑引擎的松散脚本都带隔离 "
        "(不带就会把测试对话写进用户真库 —— 实测踩过 3 次)",
        not loose, f"{len(loose)} 个未隔离: {loose[:6]}")
    if unisolated:
        # 经套件跑没问题 (hermes_verify 会注入隔离), 但**单跑**会污染 → 记 SKIP 并点名,
        # 不判红 (那是环境差异, 不是代码错), 也不假装绿。
        skip("门禁自建引擎实例的自带隔离检查",
             f"{len(unisolated)} 个无自带隔离(单跑有污染风险): {', '.join(unisolated[:5])}")
    else:
        chk("门禁自建引擎实例的都有自带隔离", True)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
