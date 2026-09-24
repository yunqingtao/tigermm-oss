"""技能 DAG 执行层 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: **新技能零代码接入** —— 只放一个 SKILL.md, 不改 pipeline 一行, 就能被匹配 + 执行。

背景: `core/skill_loader.py` 的 `execute_dag` 早就写好了(注释自称 "the clean path:
parser -> DAG -> runner"), 但 `set_gateway()` **无人调用** → `self._gateway` 永远 None
→ 就算被调也返回 "No ToolGateway set"。而 `match_context` / `execute` 生产链路零调用。
所以技能"感觉不到"不是格式问题, 是**整条链路没接线**。

2026-09-18 落地: `core/pipeline.py` 新增技能 DAG 层 (`_skill_match` / `_entity_resolve` /
`_skill_params` / `_skill_dag_exec`), 位置在现有链路(三模式闸门/IR chain/knowledge)之后、
AutoGuide 之前 —— 只接住前面都没处理的请求, **纯加性**。
关键设计: **不走模型**。本地 1.5B 无法把技能说明转成工具调用
(实测: 只把技能名当文本念出来, 甚至编造 shell 命令), 所以参数用规则从消息提取。

跑法:
    python -B scripts/verification/verify_skill_dag_layer.py

★ 沙箱用**项目内** tmp/_dagger_sandbox —— Temp 不在 file_ops 允许根内(安全设计)。
自清理: 删临时技能 + 沙箱 + 探测产物。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
SKILL_DIR = ROOT / "tmm_skills" / "test-copy-file"
BROKEN_DIR = ROOT / "tmm_skills" / "_broken_no_fm"
SANDBOX = ROOT / "tmp" / "_dagger_sandbox"

SKILL_MD = '''---
name: test-copy-file
version: "1.0"
description: 测试用 —— 把源文件内容复制到目标文件
permission: system
timeout: 30
tags: [测试拷贝]
triggers: [测试拷贝]
requires_tools: [file_ops]
params:
  src:
    type: path
    required: true
    desc: 源文件
  dst:
    type: path
    required: true
    desc: 目标文件
steps:
  - id: read
    tool: file_ops
    action: read
    input:
      path: $params.src
    output: content
    on_error: stop
  - id: write
    tool: file_ops
    action: write
    input:
      path: $params.dst
      content: $steps.read.content
    on_error: stop
---
# test-copy-file
临时测试技能。
'''

BROKEN_MD = '''name: _broken_no_fm
description: 故意漏掉 --- 分隔符, 应被跳过且留下日志
steps:
  - id: x
    tool: file_ops
    input: {}
'''

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


PROBE = r'''
import sys, os, json, asyncio, logging
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
from core.knowledge import KnowledgeEngine
from core import skill_loader as SL
from config.settings import DATA_DIR
from gateway.plugin_mgr import PluginManager

cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg)
pl._ke = KnowledgeEngine(DATA_DIR)
eng = SL.get_skill_engine()
SBX = r"@@PROJ@@/tmp/_dagger_sandbox"
os.makedirs(SBX, exist_ok=True)
out = {}

idx = eng.get_index()
out["scanned"] = "test-copy-file" in idx
dag = eng.get_dag("test-copy-file") if out["scanned"] else None
out["dag_valid"] = bool(dag and dag.is_valid() and len(dag.steps) == 2)
out["schema"] = list((getattr(dag, "params_schema", {}) or {}).keys()) if dag else []
out["step_tools"] = [s.get("tool") for s in (dag.steps if dag else [])]
out["broken_skipped"] = "_broken_no_fm" not in idx

out["match_new"] = (pl._skill_match("测试拷贝 a.txt 到 b.txt") or (None,))[0]
out["match_other"] = (pl._skill_match("今天天气怎么样") or (None,))[0]
out["match_email"] = (pl._skill_match("发邮件给涛哥") or (None,))[0]
out["match_file"] = (pl._skill_match("把 report.txt 发给涛哥") or (None,))[0]

src, dst = f"{SBX}/_dag_src.txt", f"{SBX}/_dag_dst.txt"
for p in (src, dst):
    if os.path.exists(p): os.remove(p)
with open(src, "w", encoding="utf-8") as f: f.write("DAG 引用测试 OK")
msg = f"测试拷贝 {src} 到 {dst}"
hit = pl._skill_match(msg)
out["params"] = pl._skill_params(msg, hit[1]) if hit else {}
r = asyncio.run(pl._skill_dag_exec(msg, 0.0))
out["exec"] = {"response": str(r.get("response"))[:220] if r else None,
               "intent": r.get("intent") if r else None,
               "skill": r.get("skill") if r else None}
out["dst_exists"] = os.path.exists(dst)
out["dst_content"] = open(dst, encoding="utf-8").read() if os.path.exists(dst) else ""
out["src_content"] = open(src, encoding="utf-8").read()

r2 = asyncio.run(pl._skill_dag_exec("测试拷贝", 0.0))
out["missing"] = {"response": str(r2.get("response"))[:160] if r2 else None,
                  "missing": r2.get("missing") if r2 else None}

bad_dst = f"{SBX}/_dag_should_not_exist.txt"
if os.path.exists(bad_dst): os.remove(bad_dst)
r3 = asyncio.run(pl._skill_dag_exec(f"测试拷贝 {SBX}/_nope_missing.txt 到 {bad_dst}", 0.0))
out["fail_stop"] = {"response": str(r3.get("response"))[:180] if r3 else None,
                    "dst_created": os.path.exists(bad_dst)}

pm = PluginManager(DATA_DIR)
outside = os.path.join(os.environ.get("TEMP") or tempfile.gettempdir(),
                            "_definitely_outside.txt")
with open(outside, "w", encoding="utf-8") as f: f.write("x")
try:
    rr = asyncio.run(pm.call("file_ops", action="read", path=outside))
    out["outside_err"] = str((rr or {}).get("error") or rr.get("output") or rr)[:160]
except Exception as e:
    out["outside_err"] = f"{type(e).__name__}: {e}"[:160]
try: os.remove(outside)
except Exception: pass
rr2 = asyncio.run(pm.call("file_ops", action="read", path=f"{SBX}/_definitely_missing.txt"))
out["missing_err"] = str((rr2 or {}).get("error") or rr2.get("output") or rr2)[:160]

print("<<<J>>>" + json.dumps(out, ensure_ascii=False))
'''


def probe(src, timeout=300):
    fd, tmp = tempfile.mkstemp(suffix=".py"); os.close(fd)
    Path(tmp).write_text(_fill(src), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        return r.stdout, r.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    try:
        SKILL_DIR.mkdir(parents=True, exist_ok=True)
        (SKILL_DIR / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        BROKEN_DIR.mkdir(parents=True, exist_ok=True)
        (BROKEN_DIR / "SKILL.md").write_text(BROKEN_MD, encoding="utf-8")
        SANDBOX.mkdir(parents=True, exist_ok=True)

        out, err = probe(PROBE)
        try:
            d = json.loads(out.split("<<<J>>>")[1].strip().splitlines()[0])
        except Exception as e:
            chk("探针可解析", False, f"{e} | out={out[-220:]} | err={err[-220:]}")
            d = None

        if d:
            chk("新技能被扫描到", d["scanned"])
            chk("解析成合法 DAG (2 步)", d["dag_valid"], str(d.get("step_tools")))
            chk("参数 schema 正确", d["schema"] == ["src", "dst"], str(d["schema"]))
            chk("步骤工具正确", d["step_tools"] == ["file_ops", "file_ops"], str(d["step_tools"]))
            chk("无 frontmatter 技能被跳过", d["broken_skipped"])

            chk("新技能被匹配", d["match_new"] == "test-copy-file", str(d["match_new"]))
            chk("无关句不匹配", d["match_other"] is None, str(d["match_other"]))
            chk("发邮件句 → send-email", d["match_email"] == "send-email-to-contact", str(d["match_email"]))
            chk("发文件句 → send-file", d["match_file"] == "send-file-to-contact", str(d["match_file"]))

            pr = d["params"]
            chk("两个 path 参数按序分配",
                pr.get("src", "").endswith("_dag_src.txt") and pr.get("dst", "").endswith("_dag_dst.txt"),
                json.dumps(pr, ensure_ascii=False))
            ex = d["exec"]
            chk("执行返回 skill_dag", ex["intent"] == "skill_dag", str(ex))
            chk("返回技能名", ex["skill"] == "test-copy-file", str(ex))
            chk("执行成功 (非 ✗)", not str(ex["response"]).startswith("✗"), str(ex["response"]))
            chk("★ 目标文件被创建", d["dst_exists"], str(ex))
            chk("★ $steps.read.content 正确传值",
                d["dst_content"] == d["src_content"] and bool(d["dst_content"]),
                f"src={d['src_content']!r} dst={d['dst_content']!r}")

            ms = d["missing"]
            chk("缺必填参数 → 报 ✗", str(ms["response"]).startswith("✗"), str(ms))
            chk("列出缺的参数名", bool(ms["missing"]) and set(ms["missing"]) == {"src", "dst"}, str(ms))
            fs = d["fail_stop"]
            chk("源不存在 → 报失败", "✗" in str(fs["response"]), str(fs))
            chk("★ on_error:stop — 未创建目标文件", fs["dst_created"] is False, str(fs))

            oe, me = d["outside_err"], d["missing_err"]
            chk("越权路径报 Access denied", "Access denied" in oe or "outside" in oe.lower(), oe)
            chk("真不存在才报 File not found", "not found" in me.lower(), me)
            chk("两种情况报错文本不同", oe != me, f"{oe} | {me}")

        junk = [str(p) for p in [ROOT / "文档.txt", Path.home() / "Desktop" / "文档.txt"] if p.exists()]
        chk("项目根/桌面无垃圾文件", not junk, str(junk))

        r = subprocess.run([PY, "-B", "-m", "pytest", "tests/", "-q", "--no-header",
                            "-p", "no:cacheprovider"], cwd=str(ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=580)
        tail = [l for l in r.stdout.split("\n") if "passed" in l or "failed" in l]
        print("        pytest:", tail[-1] if tail else r.stdout.strip()[-80:])
        # 基线已从 "3 failed" 变为 **全绿** (2026-09-18 _lay_state 契约修复)
        failed = {l[7:].split(" ")[0] for l in r.stdout.split("\n") if l.startswith("FAILED ")}
        chk("canonical 全绿 (0 failed)", not failed, str(sorted(failed)))
    finally:
        # ★★ 2026-09-24 加固 (真 bug, 实测): 原来只 `rmtree(ignore_errors=True)` ——
        #   Windows 上文件被占用时它**静默失败**, 夹具 `_broken_no_fm` 残留在
        #   tmm_skills/ 里, 连带把 verify_skill_format / verify_skill_gaps /
        #   verify_skill_reachability 三道门禁全弄红 (假红, 排查了半天)。
        #   改为: 重试 + **断言真的没了** (残留就报红, 别再静默)。
        import time as _t_dag
        for dirp in (SKILL_DIR, BROKEN_DIR, SANDBOX):
            if not dirp.exists():
                continue
            for _attempt in range(5):
                shutil.rmtree(dirp, ignore_errors=True)
                if not dirp.exists():
                    break
                _t_dag.sleep(0.4)
            if dirp.exists():                     # 清不掉 → 出声 (不静默)
                print(f"  FAIL 夹具/沙箱清理失败(残留): {dirp}")
                F.append(f"夹具残留 {dirp.name}")

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
