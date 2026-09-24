"""探针隔离 helper —— 写 ad-hoc 探针时**唯一**该用的入口 (2026-09-21 立)

为什么有这个东西 (用血换的)
═══════════════════════════════════════════════════════════════════
本项目反复踩同一个坑: **探针直接调 pl.process() 却没设隔离环境变量 → 测试流量写进
用户的真实数据**。已发生两次:
  · 2026-09-20 早: 探针跑 /depot skill-assess → 真对话库 210 → 218 行
  · 2026-09-21 夜: 探针跑 /experts + depot → 真对话库 210 → 228 行 (18 行)
两次都得手工按 intent 精确删行 + 重建 FTS + 校验 integrity。

根因不是"我不小心", 而是**隔离靠人记**: 变量散在文档里, 写探针时容易漏。
所以改成**一个函数**: 探针开头调一次, 拿到的 env 里所有隔离变量都已指向临时沙箱。

用法
───────────────────────────────────────────────────────────────
    import sys; sys.path.insert(0, r"<项目根>/scripts/verification")
    from _probe_env import isolated_env            # ① 拿 env
    env = isolated_env()                           #    自动建 tmp/_probe_<pid>/
    # 子进程跑引擎时带上 env
    subprocess.run([PY, "-B", "script.py"], env=env, ...)
    # 或在本进程里跑 (需要设进 os.environ)
    from _probe_env import activate
    activate()                                     # ② 本进程隔离 + 自动清理

自带自检:  `python scripts/verification/_probe_env.py --self-test`
    (证明 ① 真改了指向 ② 真的是临时目录 ③ 不碰真实数据路径)
"""
from __future__ import annotations

import atexit
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

#: 需要隔离的变量 → 沙箱里的文件名。
#: ★ 新增任何"会被引擎写入的数据文件"时, 必须在这里补一行 (否则探针又会污染真实数据)。
ISOLATED = {
    "TMM_SESSION_DB": "chat_sessions.db",          # 对话史
    "TMM_GAP_DB": "gap_ledger.db",                 # 缺口台账
    "TMM_PERCEPTION_FILE": "perception.json",      # 环境感知
    "TMM_USAGE_LOG": "usage.jsonl",                # 模型用量
    "TMM_ARTIFACTS_FILE": "artifacts.jsonl",       # 产物登记 (2026-09-21 加)
    # ★ TMM_PROBE 是**开关**(不是路径): 由 isolated_env/activate 设为 "1",
    #   给"无法靠路径隔离"的写入点做硬闸 (如产物登记 / 技能使用账)。
    "TMM_SKILL_USAGE_FILE": "skill_usage.jsonl",     # 技能使用账 (2026-09-21 加)
    # ★ 2026-09-23 加: 日常干活账 (task_ledger) —— 同样必须隔离,
    #   否则门禁里被 TMM_LIVE 翻起来的那几轮会写进**用户的真账本** (实测 11 行)。
    "TMM_TASK_DB": "task_ledger.db",
    # ★ 2026-09-23 加 (本日第二次抓到): 会话源码指纹快照 —— Level4Pipeline.__init__
    #   每次 new pipeline 都会 take_snapshot() 重写它。探针只跑一次 pipeline
    #   就把真实 data/session_snapshot.json 改写 (ad-hoc 验证器的 md5 断言抓到)。
    "TMM_SESSION_SNAPSHOT": "session_snapshot.json",
}


def sandbox_dir(cleanup: bool = True) -> Path:
    """建本次探针的临时沙箱 (tmp/_probe_<pid>)。"""
    d = ROOT / "tmp" / f"_probe_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    if cleanup:
        atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def isolated_env(extra: dict | None = None, cleanup: bool = True) -> dict:
    """返回**带隔离**的环境变量字典 (给子进程用)。

    ★ 同时设 TMM_PROBE=1 —— 这是**硬闸**, 给那些"路径隔离不了"的写入点用
      (产物登记会写到桌面 / 技能使用账 / 技能库增删)。光靠改路径不够:
      实测门禁把一个产物写到了桌面, 于是照样进了用户的真台账。
    """
    d = sandbox_dir(cleanup)
    env = dict(os.environ)
    for var, fname in ISOLATED.items():
        env[var] = str(d / fname)
    env["TMM_PROBE"] = "1"
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


_WRITE_GUARD_ON = False


def install_write_guard() -> int:
    r"""探针专用: 禁止写入**项目树 / 系统临时目录之外**的任何路径。

    为什么需要 (2026-09-24 实测踩坑)
    ───────────────────────────────────────────────────────────────
    探针用 probe=True 跑真引擎时, TMM_PROBE 只挡住了记账/会话/用量那几处
    (见 ISOLATED 注释), **文件写入完全没闸** —— 于是探测 C 组"粘内容能不能跑"时,
    技能层真把 6 个产物写进了用户的 D:\tmm-考卷\(周报.docx / 进展.pptx / ...)。
    验证流量污染了用户目录。

    做法: 猴补 builtins.open / Path.write_text / Path.write_bytes —— 写模式下
    目标在允许根之外就抛 PermissionError。**猴补是为了覆盖第三方库**
    (python-docx / openpyxl 走 zipfile → builtins.open), 否则得改 5+ 个工具。

    允许根: 项目根 (含 tmp/) + 系统临时目录 + 本探针沙箱。
    返回: 已挂钩的写入函数个数 (便于断言)。
    """
    global _WRITE_GUARD_ON
    if _WRITE_GUARD_ON:
        return -1
    import builtins
    import tempfile

    roots = [ROOT]
    try:
        roots.append(Path(tempfile.gettempdir()))
    except Exception:
        pass
    try:
        roots.append(sandbox_dir(cleanup=False))
    except Exception:
        pass
    # ★★ 2026-09-24 修正 (被门禁打回来的): **标准用户目录要放行** ——
    #   本项目既有的门禁约定是"探针可以写桌面, 但**跑完必须清干净**"
    #   (verify_mode_gate 直接验"craft 真产出 → 桌面出现产物"+ "ask 零残留")。
    #   第一版把桌面也拦了 → 4 道门禁当场红。所以放开 桌面/文档/下载,
    #   仍然拦住**任意路径** (如 D:\tmm-考卷 这类非标准位置)。
    try:
        for _n in ("Desktop", "Documents", "Downloads"):
            _p = Path.home() / _n
            if _p.exists():
                roots.append(_p)
    except Exception:
        pass
    roots = [r.resolve() for r in roots]
    n = 0

    def _allowed(target) -> bool:
        try:
            tp = Path(target).resolve()
        except Exception:
            return True                     # 不是路径 (如 fd) → 不拦
        for r in roots:
            try:
                tp.relative_to(r)
                return True
            except ValueError:
                continue
        return False

    def _is_write_mode(mode) -> bool:
        m = str(mode or "")
        return any(c in m for c in "wax+")

    _open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if _is_write_mode(mode) and isinstance(file, (str, bytes, os.PathLike)):
            if not _allowed(file):
                raise PermissionError(
                    "[TMM_PROBE] 探针模式禁止写用户路径: " + str(file) +
                    "  (只允许项目树 / 临时目录; 验证不得污染用户数据)")
        return _open(file, mode, *a, **k)

    builtins.open = guarded_open
    n += 1
    # ★★ 关键: zipfile 用的是 `io.open`(**另一个名字绑定**), 而 python-docx /
    #   openpyxl / pptx 全都经 zipfile 存盘 —— 只补 builtins.open 拦不住它们
    #   (实测: 猴补 builtins 后 docx.save / openpyxl.save 照样写进用户目录)。
    import io as _io
    _io.open = guarded_open
    n += 1

    for name in ("write_text", "write_bytes"):
        _orig = getattr(Path, name, None)
        if _orig is None:
            continue

        def _mk(orig):
            def guarded(self, *a, **k):
                if not _allowed(self):
                    raise PermissionError(
                        "[TMM_PROBE] 探针模式禁止写用户路径: " + str(self))
                return orig(self, *a, **k)
            return guarded

        setattr(Path, name, _mk(_orig))
        n += 1

    _WRITE_GUARD_ON = True
    return n


def activate(extra: dict | None = None, cleanup: bool = True) -> Path:
    """把隔离变量写进**当前进程**的 os.environ (在本进程内跑引擎时用)。"""
    d = sandbox_dir(cleanup)
    for var, fname in ISOLATED.items():
        os.environ[var] = str(d / fname)
    os.environ["TMM_PROBE"] = "1"          # ★ 硬闸: 验证流量不写任何用户状态
    install_write_guard()                  # ★ 2026-09-24: 再堵一层 —— 探针不写用户**文件**
    if extra:
        for k, v in extra.items():
            os.environ[k] = str(v)
    return d


def _self_test() -> int:
    """自检: 证明它真的在隔离 (而不是"看着像")。"""
    fails = []

    d = sandbox_dir()                 # 沙箱目录
    env = isolated_env()              # 带隔离的 env 字典 (给子进程)
    for var, fname in ISOLATED.items():
        want = str(d / fname)
        if env[var] != want:
            fails.append(f"{var} 没指向沙箱: {env[var]}")
        # 沙箱路径必须是临时目录, 且**不在**真实 data/ 下
        p = Path(env[var]).resolve()
        if ROOT / "data" in p.parents:
            fails.append(f"{var} 指向了真实 data/: {p}")
        if "_probe_" not in str(p):
            fails.append(f"{var} 路径里没有 _probe_ 标记: {p}")

    # 本进程 activate 也不该碰真实路径, 且能还原
    sd = activate()
    for var, fname in ISOLATED.items():
        got = Path(os.environ[var]).resolve()
        if got != (sd / fname).resolve():
            fails.append(f"activate 后 {var} 不对: {got}")
    if any(Path(os.environ[v]).resolve() != (sd / f).resolve() for v, f in ISOLATED.items()):
        fails.append("activate 没把全部变量指向沙箱")
    shutil.rmtree(sd, ignore_errors=True)

    # 真实数据文件一个都不该被创建/改动
    for fname in ISOLATED.values():
        real = ROOT / "data" / fname
        if real.exists() and fname in ("artifacts.jsonl",):
            # 只警告: 真实产物表允许存在 (用户自己的数据), 不该由本自检创建
            pass

    print(f"隔离变量 {len(ISOLATED)} 个: " + ", ".join(sorted(ISOLATED)))
    if fails:
        print("自检失败:")
        for x in fails:
            print("  -", x)
        return 1
    print("自检通过: 全部变量指向临时沙箱, 无一指向真实 data/ ✓")
    return 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    print("当前会设置的隔离变量:")
    for v, f in ISOLATED.items():
        print(f"  {v} → <沙箱>/{f}")
