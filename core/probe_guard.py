# -*- coding: utf-8 -*-
r"""探针自证 —— 让「验证流量不写用户真数据」这条纪律**不依赖任何人记得设变量**。

为什么要有这个模块 (2026-09-25 实踩: 一条真账本被多记了 6 轮)
═══════════════════════════════════════════════════════════════════
记账类写盘点的既有判据是:

    TMM_PROBE=1        → 不写      (硬闸)
    没有 TMM_LIVE=1    → 不写      (失败即闭)

纪律本身是对的, 但它**信的是环境变量**, 而:

    main.py 在 **import 时**就 `os.environ.setdefault("TMM_LIVE", "1")`
    ⇒ 探针只要写一句 `from main import _load_config` 拿配置,
      **整个探针进程看起来就是真人会话**;
      忘了设 TMM_PROBE 的探针 ⇒ 测试流量写进**用户的真账本**。

实测: 一个真引擎证据探针 (7 句原话) 把 6 轮记进了真账本, 靠探针末尾那条
      "真库 md5 未动" 断言才发现 (今天 212 → 应为 206)。同仓另有 8 个 `_probe_*.py`
      同样裸 `import main` ⇒ 这个坑随时会再踩。
      ★ 更早的两次同源事故: 真对话库被探针写进 8 行 / 18 行 (见 `_probe_env.py` 文件头)。

修法: 判据里**再加一条「进程形状」** —— 入口脚本像验证脚本(`verify_*` / `_probe_*` /
      `tests/` / pytest), 且目标**就是真数据路径**时, 一律拒写。两点设计是关键:

  ① 判据放在**代码里**, 不靠调用方记得设环境变量 (黑名单式防守永远漏, 这条已经被
     skill_usage 的注释写明白了; 现在把它前移到**数据落盘的最后一道**)。
  ② 只在**真数据路径**上拒写 —— 门禁常故意用**隔离路径**测"真人会话该记账"那条分支
     (实测 `verify_artifact_ledger` / `verify_task_ledger` 就是拿沙箱路径测 live 分支),
     把隔离路径上的写入一起拒掉 = 把门禁编软。

逃生口: 显式 `TMM_PROBE=0` 表示"我知道, 这次是故意的"。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 项目根 (core/ 的上一级)

#: 入口脚本名像这些 ⇒ 当验证进程 (与"门禁靠磁盘发现"同一套命名约定)。
_PROBE_PREFIXES = ("_probe_", "verify_", "test_")
_PROBE_BASENAMES = ("pytest", "py.test", "pytest.exe")


def _entry_candidates() -> list:
    out = []
    try:
        import __main__ as _m
        f = getattr(_m, "__file__", "") or ""
        if f:
            out.append(f)
    except Exception:
        pass
    try:
        if sys.argv and sys.argv[0]:
            out.append(sys.argv[0])
    except Exception:
        pass
    return out


def is_probe_process() -> bool:
    """本进程是不是验证/探针进程。

    判据 (任一命中): ① 入口脚本名以 `_probe_/verify_/test_` 开头;
                      ② 入口在 `scripts/verification/` 或 `tests/` 下;
                      ③ 入口是 pytest, 或本进程已加载 pytest;
                      ④ `TMM_PROBE=1` (既有约定, 保留)。
    显式 `TMM_PROBE=0` ⇒ 直接判**不是** (逃生口)。
    """
    _p = (os.environ.get("TMM_PROBE") or "").strip().lower()
    if _p in ("1", "true", "yes"):
        return True
    if _p in ("0", "false", "no"):
        return False
    try:
        if "pytest" in sys.modules:
            return True
    except Exception:
        pass
    for f in _entry_candidates():
        p = str(f).replace("\\", "/").lower()
        if not p:
            continue
        base = p.rsplit("/", 1)[-1]
        if base in _PROBE_BASENAMES or base.startswith("pytest"):
            return True
        if base.startswith(_PROBE_PREFIXES):
            return True
        if "/scripts/verification/" in p or "/tests/" in p:
            return True
    return False


def is_real_data_path(target) -> bool:
    """target 是不是**用户的真实数据**文件: 在项目 `data/` 下, 且不在探针沙箱里。"""
    if target in (None, ""):
        return False
    try:
        q = Path(str(target)).resolve()
        q.relative_to((ROOT / "data").resolve())
    except Exception:
        return False
    return not any(str(x).lower().startswith("_probe_") for x in q.parts)


def block_reason(target=None, what: str = "写用户数据") -> str:
    """该拒写 ⇒ 返回原因串 (非空); 否则空串。

    调用方写法:
        _r = probe_guard.block_reason(self.db_path, what="日常干活账")
        if _r:
            return {"ok": False, "error": _r}
    """
    if not is_probe_process():
        return ""
    if target is not None and not is_real_data_path(target):
        return ""                       # 隔离路径 ⇒ 门禁在测 live 分支, 放行
    return ("验证进程不写用户真数据 (%s): 入口像验证脚本 ⇒ 按探针处理。"
            "探针请用 scripts/verification/_probe_env.activate() 拿隔离路径; "
            "确要真写请显式 TMM_PROBE=0" % what)
