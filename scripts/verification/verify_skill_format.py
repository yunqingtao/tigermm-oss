"""技能格式对齐 OpenClaw 真实格式 (SKILL.md) — 常驻验证器

对齐依据: OpenClaw 实物 (~/.openclaw/workspace/skills/<s>/SKILL.md) + 官方 docs。
本文件是 ad-hoc 验证留档, 不是 pytest 套件。跑法:

    python -B scripts/verification/verify_skill_format.py

覆盖: 无 skill.json 残留 / 每目录都有 SKILL.md / frontmatter 合法(含必须有 steps) /
      两引擎扫描 0 错误 / 嵌套 name 污染已修 / 匹配 9/9 (含 3 个负例)

改动历史:
  2026-09-19  堵漏: frontmatter 检查从 (steps 或 triggers) 收紧为**必须有非空 steps**
              —— 否则只有 triggers 的散文技能也能过审 ("技能散"的来源)
  2026-09-18  core/skill_registry.py  _parse_skill_md 改真 YAML 解析 (修嵌套 name: 污染)
              tmm_skills/*, skills/*    3 个技能统一为 SKILL.md (原 skill.json/workflow.md 已删)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent

def _fill(s: str) -> str:
    """把探针模板里的 @@PROJ@@ 换成真实项目根。

    ★ 2026-09-21: 探针脚本写在临时目录里, 自己推不出项目根 (`__file__` 指向 %TEMP%)。
      原来是**写死绝对路径** —— 公开/换机就会指向别处。现在用占位符, 写入时填充。
    """
    # ★ 用**正斜杠**注入: 探针里的路径常被拼进中文话术再交给引擎解析, 反斜杠会与 "/"
    #   混用导致解析失败 (实测: 会当成"没给目录"而落到桌面)。正斜杠在 Windows 上同样合法。
    return s.replace("@@PROJ@@", str(ROOT).replace("\\", "/"))


PY = sys.executable
P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def probe(src, timeout=240):
    """在子进程里跑探针, 返回 (stdout, stderr)。"""
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(tmp).write_text(_fill(src), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        return r.stdout, r.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


PROBE = r'''
import sys, os, json
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
from core.skill_loader import get_skill_index
from core.skill_registry import get_skill_registry

eng = get_skill_index(); idx = eng.get_index()
o = {"loader": {n: ((eng.get_dag(n).parse_errors if eng.get_dag(n) else ["?"]) or []) for n in idx}}
o["reg"] = sorted(get_skill_registry().scan(force=True).keys())

CASES = [("把桌面文件发给涛哥", "send-file-to-contact"), ("这个文件发给老王", "send-file-to-contact"),
         ("附件发给涛哥", "send-file-to-contact"), ("给涛哥发邮件", "send-email-to-contact"),
         ("发邮件告诉涛哥 明天开会", "send-email-to-contact"), ("给这个文件改个名", None),
         ("给我看看今天的天气", None), ("这首诗写入大哥.txt", None),
         ("画一个架构图", "diagram")]
res = []
for msg, want in CASES:
    ctx = eng.match_context(msg)
    got = None
    for line in ctx.splitlines():
        s = line.strip()
        if s.startswith("- ") and ":" in s:
            got = s[2:].split(":")[0]; break
    res.append([msg, got == want, got])
o["match"] = res
print(json.dumps(o, ensure_ascii=False))
'''


def main():
    # 1. 错格式已清
    stale_json = list(ROOT.glob("skills/**/skill.json")) + list(ROOT.glob("tmm_skills/**/skill.json"))
    chk("无 skill.json 残留", not stale_json, str([str(p) for p in stale_json]))
    stale_wf = [p for p in ROOT.glob("**/workflow.md") if "backup" not in str(p)]
    chk("无 workflow.md 残留 (臆测格式产物)", not stale_wf, str(stale_wf))

    # ★ 2026-09-24: 排除**测试夹具** (下划线开头, 如 _broken_no_fm) —— 它们是门禁
    #   自己造的临时目录, 万一清理失败 (Windows 占用) 不该连累本门禁假红。
    def _is_fixture(_p):
        return _p.parent.name.startswith("_") or _p.name.startswith("_")
    mds = [p for p in (list(ROOT.glob("skills/*/SKILL.md")) + list(ROOT.glob("tmm_skills/*/SKILL.md")))
           if not _is_fixture(p.parent)]
    # ★ 关系型断言: 每个技能目录都要有 SKILL.md (别写死数量 —— 加技能就得改验证器)
    skill_dirs = [d for d in (list(ROOT.glob("skills/*")) + list(ROOT.glob("tmm_skills/*")))
                  if d.is_dir() and not d.name.startswith("_")]
    missing = [d.name for d in skill_dirs if not (d / "SKILL.md").exists()]
    chk(f"每个技能目录都有 SKILL.md (共 {len(mds)} 个)", not missing, str(missing))
    chk("SKILL.md 数量 > 0", len(mds) > 0, str(len(mds)))

    # ★ 2026-09-19 堵漏: 原来只要求 (steps 或 triggers) —— 于是**只有 triggers 的散文技能**
    #   也能过审, 这就是"技能散"的来源 (装了但没有可执行 DAG)。现在**必须真有 steps**。
    bad, prose, nofm = [], [], []
    for m in mds:
        try:
            fm = yaml.safe_load(m.read_text(encoding="utf-8").split("---")[1])
            if not fm.get("name"):
                bad.append(f"{m.parent.name}: 缺 name"); continue
            steps = fm.get("steps")
            if not isinstance(steps, list) or not steps:
                prose.append(m.parent.name)      # 散文技能: 有 frontmatter 但没有可执行步骤
                continue
            for i, s in enumerate(steps):
                if not isinstance(s, dict) or not s.get("tool"):
                    bad.append(f"{m.parent.name}: step {i} 缺 tool")
        except Exception as e:
            nofm.append(f"{m.parent.name}: {e}")
    chk("frontmatter 全合法 (有 name + 非空 steps + 每步有 tool)", not bad and not nofm,
        f"非法={bad} 解析失败={nofm}")
    chk(f"★ 无散文技能 (每个都有可执行 steps) 共 {len(mds)} 个", not prose,
        f"只有 triggers 没 steps: {prose}")

    # 2. 引擎扫描 / 名污染 / 匹配
    out, err = probe(PROBE)
    try:
        d = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        chk("引擎探针可解析", False, f"{e} | stderr={err.strip()[-160:]}")
        d = None

    if d:
        errs = {k: v for k, v in d["loader"].items() if v}
        # ★ 关系型: 引擎必须扫到**目录里的每个技能**, 且 0 错误
        # (别写死数量 —— 每加一个技能就得改验证器, 那是脆弱断言)
        names = {m.parent.name for m in mds}
        not_loaded = sorted(names - set(d["loader"].keys()))
        chk(f"SkillWorkflowEngine 扫到全部 {len(names)} 个且 0 错误",
            not not_loaded and not errs, f"缺={not_loaded} 错误={errs}")
        not_reg = sorted(names - set(d["reg"]))
        chk(f"SkillRegistry 扫到全部 {len(names)} 个", not not_reg, str(not_reg))
        polluted = [n for n in d["reg"] if n.startswith("$") or "params" in n]
        chk("技能名无 YAML 嵌套污染", not polluted, str(polluted))
        bad_m = [r[0] for r in d["match"] if not r[1]]
        chk(f"匹配正确 {len(d['match']) - len(bad_m)}/{len(d['match'])}", not bad_m, str(bad_m))
        neg = [r for r in d["match"] if r[2] is None]
        chk(f"负例 ({len(neg)} 个) 全不命中", all(r[2] is None for r in neg), str(neg))

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
