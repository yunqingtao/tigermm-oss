"""静默失败门禁 —— "没办成却像办成了" 必须为零 (2026-09-21 立)

为什么要有它
═══════════════════════════════════════════════════════════════════
本项目全仓有 ~190 处"完整吞掉异常"的处理器。绝大多数**正当**(探测/清理/可选依赖/
多级降级链), 所以"禁止吞异常"是错的规矩 —— 那种门禁只会被加白名单加到失效。

真正致命的是其中一类: **吞掉之后, 用户以为办成了**。实测两起:
  · `/voice` TTS 播放失败只进日志 → 用户以为语音也回了 (静默消失)
  · 门禁离线时 pyflakes 被跳过 → 记成 PASS ("全绿"是假的)
两起都是**人肉翻出来的**。

所以本门禁不追求"零吞异常", 只钉三件**可机器判**的事:
  [A] 运行时代码里 **裸 `except:` 必须为 0** —— 它连 KeyboardInterrupt/SystemExit
      都吞 (Ctrl+C 按不动、退不出), 且永远说不清"吞了什么"。
  [B] **降级出声机制必须在线**: core/result.py 可用 + process() 外面那层包装在位。
  [C] **已知的三个高危静默点必须接 degrade** (钉死防回退):
      /voice 语音播报、证据链重答、审计层。以后新增"用户以为会发生"的操作,
      往 HIGH_RISK 里加一条即可。

判据自身有效性
═══════════════════════════════════════════════════════════════════
  · 正控: 合造一个裸 except → [A] 必须抓到 (否则门禁是摆设)
  · 负控: 合造一个 `except Exception: degrade(...)` → 不得误报
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {"backups", ".git", "__pycache__", "tmp", ".pytest_cache", "node_modules"}
RUNTIME_DIRS = ("core", "tools", "api", "gateway", "storage", "mcp")

# 高危静默点: (文件, 必须出现的 degrade 特征串, 说明)
#   ★ 这些是"用户以为会发生的事"。以后发现新的, 往这里加一行 —— 门禁就永久盯着它。
#   ★ 判据用"字符串必须作为 degrade/_deg 的实参出现", 不管别名 --- 免得换别名就绕过去。
HIGH_RISK = [
    ("core/pipeline.py", "语音播报失败", "/voice 语音播报失败 (实测: 本机缺 playsound → 语音静默消失)"),
    ("core/pipeline.py", "语音回复未生成", "/voice 整段 TTS 失败"),
    ("core/pipeline.py", "证据链重答失败", "证据链重答失败 (原来 pass: 用户拿到未经验证的回答)"),
    ("core/pipeline.py", "审计层未能运行", "审计层未能运行 (原来 pass: 自查形同虚设)"),
    # ★ 学习路径三处 (2026-09-21 深夜补): 学习失败=越用越傻的根因, 绝不能静默
    ("core/pipeline.py", "学习闭环记录失败", "学习闭环记录失败 (原来 logger.debug 吞: 学没学上无人知)"),
    ("core/pipeline.py", "能力缺口未记账", "能力缺口未记账 (原来 logger.debug 吞)"),
    ("core/pipeline.py", "学习规则未衰减", "学习规则未衰减 (原来 except: pass)"),
    ("core/pipeline.py", "产物登记失败", "产物登记失败 (归档台账要能自曝)"),
]

P, F, S = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def skip(name, why):
    S.append(name)
    print(f"  SKIP {name}   ({why})")


def _bare_excepts(path: Path) -> list:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    return [h.lineno for n in ast.walk(tree) if isinstance(n, ast.Try)
            for h in n.handlers if h.type is None]


def _runtime_py():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if any(x in rel.parts for x in SKIP_DIRS):
            continue
        if rel.parts and rel.parts[0] in RUNTIME_DIRS:
            yield p


def main() -> int:
    print("=" * 78)
    print("静默失败门禁 —— '没办成却像办成了' 必须为零")
    print("=" * 78)

    # ── [0] 判据自身的有效性 ──
    print("\n[0] 判据自身的有效性 (牙齿)")
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".py", text=True)
    import os
    os.close(fd)
    tp = Path(tmp)
    try:
        tp.write_text("try:\n    x = 1\nexcept:\n    pass\n", encoding="utf-8")
        _pos = bool(_bare_excepts(tp))          # 正控: 裸 except 必须被抓
        tp.write_text('try:\n    x = 1\nexcept Exception as e:\n    degrade("a", str(e))\n', encoding="utf-8")
        _neg_bare = not _bare_excepts(tp)       # 负控: 规范写法不得误报
    finally:
        tp.unlink(missing_ok=True)
    chk("★ 正控: 裸 except 会被抓到 (判据有牙齿)", _pos, "裸 except 都抓不到 → 本门禁无效")
    chk("★ 负控: except Exception + degrade 不误报", _neg_bare)

    # ── [A] 裸 except 归零 ──
    print("\n[A] 运行时代码: 裸 `except:` 必须为 0")
    bare = {}
    for p in _runtime_py():
        lns = _bare_excepts(p)
        if lns:
            bare[str(p.relative_to(ROOT))] = lns
    n_bare = sum(len(v) for v in bare.values())
    chk(f"★ 裸 except 归零 (扫描 {sum(1 for _ in _runtime_py())} 个运行时文件)",
        n_bare == 0, f"{n_bare} 处: {bare}")
    if n_bare:
        for f, v in bare.items():
            print(f"        {f}: {v}")

    # ── [B] 降级出声机制在线 ──
    print("\n[B] 降级出声机制 (core/result.py + process() 包装)")
    rp = ROOT / "core" / "result.py"
    chk("core/result.py 存在", rp.is_file())
    if rp.is_file():
        self_test = subprocess.run([sys.executable, "-B", str(rp)], cwd=str(ROOT),
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=120)
        chk("★ core/result.py 自带自检通过 (degrade/drain/去重/三态)",
            self_test.returncode == 0, ((self_test.stdout or "") + (self_test.stderr or ""))[-200:])
    pl = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
    chk("★ process() 被降级包装 (Level4Pipeline.process = _process_with_degrade)",
        "_process_with_degrade" in pl and "Level4Pipeline.process = _process_with_degrade" in pl)
    chk("★ 包装是加性: 无降级时不加 degraded 键",
        'if _mk:' in pl and 'out["degraded"] = _R.items()' in pl, "没看到条件式追加")
    # ★ 2026-09-24: 深度计数从模块级 dict 改为 contextvars (修长驻进程泄漏)。
    #   断言同步钉新写法: 用 ContextVar + token 式 reset (保证异常路径也归零)。
    chk("★ 只在最外层回合 reset/汇总 (嵌套调用不冲掉外层登记)",
        '_pipeline_depth_var.get()' in pl and 'ContextVar(' in pl)
    chk("★ 深度计数用 contextvars 且 token 式 reset (防长驻进程泄漏)",
        '_pipeline_depth_var.reset(_tok)' in pl and "_pipeline_depth = {\"n\": 0}" not in pl)

    # ── [C] 高危静默点必须接 degrade ──
    print("\n[C] 已知高危静默点: 必须出声 (钉死防回退)")
    for rel, needle, why in HIGH_RISK:
        t = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        # 必须作为 degrade/_deg 的实参 (裸字符串不算 —— 免得写成注释糊弄过去)
        used = re.search(r'(?:degrade|_deg)\s*\(\s*[fru]*"\s*' + re.escape(needle), t) is not None
        chk(f"★ {why}", used, f"未在 {rel} 找到 degrade(\"{needle}\") 调用")

    # ── [D] 留痕率 (信息项, 不判红) ──
    print("\n[D] 静默面 (信息项 —— 不判红, 只给趋势)")
    tot_h, silent, with_deg = 0, 0, 0
    for p in _runtime_py():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Try):
                continue
            for h in n.handlers:
                tot_h += 1
                seg = ast.get_source_segment(src, h) or ""
                if re.search(r"(degrade|_deg)\s*\(", seg):
                    with_deg += 1
                body = [b for b in h.body
                        if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                and isinstance(b.value.value, str))]
                if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue, ast.Break)):
                    silent += 1
    print(f"        运行时异常处理器总数: {tot_h}")
    print(f"        已接降级登记(degrade): {with_deg} 处")
    print(f"        完全静默(pass/continue): {silent} 处 ({silent * 100 // max(tot_h, 1)}%)")
    print("        口径: 探测/清理/可选依赖失败允许静默, 所以只做趋势观察, 不设阈值。")

    print("\n" + "=" * 78)
    tail = f"结果: {len(P)} PASS / {len(F)} FAIL"
    if S:
        tail += f" / {len(S)} SKIP"
    print(tail)
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
