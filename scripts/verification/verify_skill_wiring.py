"""技能接线 (方案 A: 只注入不强制) — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景: `core/skill_loader.match_context()` 此前**生产链路零调用** —— 技能解析/匹配都对,
但没有任何代码把它注入模型, 所以技能"感觉不到"。
2026-09-18 落地方案 A: pipeline 新增 `_skill_context()`, 在 `_process_external`
构建 system prompt 时注入匹配到的技能说明 (SKILL.md 机制), **由模型自己决定调哪些工具**。

跑法:
    python -B scripts/verification/verify_skill_wiring.py

覆盖: 源码接线 / _skill_context 行为(含负例) / compact 模式 / 异常安全 /
      加性(未动路由、无 hack) / pytest 基线。

边界(实测, 已知): IR chain (pipeline L1485) 在模型之前拦截简单指令 ——
"给涛哥发邮件"这类会走本地 chain, **到不了** _process_external, 故本注入对它们不生效。
本注入影响"走到模型这条路"的请求 + 未来新增技能(不必再改 pipeline)。
"""
import json
import os
import subprocess
import sys
import tempfile
import re
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

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


PROBE = r'''
import sys, os, json
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
import logging; logging.disable(logging.CRITICAL)
from core.pipeline import Level4Pipeline
pl = Level4Pipeline.__new__(Level4Pipeline)   # 不跑 __init__ (免起服务/载模型/调模型)
out = {}

CASES = [("把桌面文件发给涛哥", "send-file-to-contact"),
         ("给涛哥发邮件", "send-email-to-contact"),
         ("画一个架构图", "diagram"),
         ("给我看看今天的天气", None),
         ("这首诗写入大哥.txt", None),
         ("帮我把表格整理一下", None)]
res = []
for msg, want in CASES:
    ctx = Level4Pipeline._skill_context(pl, msg)
    got = None
    for line in ctx.splitlines():
        s = line.strip()
        if s.startswith("- ") and ":" in s:
            got = s[2:].split(":")[0]; break
    res.append([msg, want, got, got == want, len(ctx)])
out["normal"] = res

full = Level4Pipeline._skill_context(pl, "把桌面文件发给涛哥", compact=False)
cmp_ = Level4Pipeline._skill_context(pl, "把桌面文件发给涛哥", compact=True)
out["full_len"] = len(full)
out["cmp_len"] = len(cmp_)
out["cmp_txt"] = cmp_
out["cmp_has_h2"] = "##" in cmp_
out["cmp_has_proc"] = "执行流程" in cmp_
out["cmp_has_name"] = "- send-file-to-contact:" in cmp_

import core.skill_loader as SL
_orig = SL.get_skill_index
class Boom:
    def match_context(self, *a, **k):
        raise RuntimeError("boom")
SL.get_skill_index = lambda *a, **k: Boom()
try:
    out["safe"] = Level4Pipeline._skill_context(pl, "把桌面文件发给涛哥")
    out["safe_raised"] = None
except Exception as e:
    out["safe"] = None
    out["safe_raised"] = "%s: %s" % (type(e).__name__, e)
finally:
    SL.get_skill_index = _orig

print(json.dumps(out, ensure_ascii=False))
'''


def probe(src, timeout=240):
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(tmp).write_text(_fill(src), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        return r.stdout, r.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


E2E = r'''
import sys, os, json, time, asyncio, logging
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline

cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg)
cap = {}


async def fake_generate(model_name, messages, **kw):
    cap.setdefault("calls", []).append(messages)
    return {"content": "FAKE-RESPONSE", "usage": {}, "tool_calls": None}


pl.model_client.generate = fake_generate


def run(msg):
    cap.clear()
    try:
        asyncio.run(asyncio.wait_for(pl._process_external(msg, "deepseek", time.time()),
                                     timeout=60))
    except Exception:
        pass          # 循环收尾警告无关; 只要 capture 到了 messages 即可
    msgs = cap.get("calls") or []
    if not msgs:
        return {"no_call": True}
    m0 = msgs[0]
    sysmsg = m0[0].get("content", "") if isinstance(m0, list) and m0 else ""
    return {"sys_len": len(sysmsg),
            "has_skills": "[Skills matched]" in sysmsg,
            "has_file": "send-file-to-contact" in sysmsg,
            "has_email": "send-email-to-contact" in sysmsg}


out = {"pos": run("把桌面文件发给涛哥"), "neg": run("给我看看今天的天气预报")}
print("<<<JSON>>>" + json.dumps(out, ensure_ascii=False))
'''


def main():
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
    chk("_skill_context 已定义", "def _skill_context(self, message: str, compact: bool = False)" in src)
    chk("调用 match_context", "get_skill_index().match_context(message, max_skills=2)" in src)
    chk("_process_external 里已注入",
        "_skill_ctx = self._skill_context(message, compact=_is_ollama)" in src)

    out, err = probe(PROBE)
    d = None
    try:
        d = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        chk("探针可解析", False, f"{e} | out={out[-180:]} | err={err[-180:]}")

    if d:
        bad = [r for r in d["normal"] if not r[3]]
        chk(f"_skill_context 匹配正确 {len(d['normal']) - len(bad)}/{len(d['normal'])}", not bad,
            str([(r[0], r[1], r[2]) for r in bad]))
        pos = [r for r in d["normal"] if r[1]]
        chk(f"正例 {len(pos)} 个返回非空正文", all(r[4] > 0 for r in pos))
        neg = [r for r in d["normal"] if r[1] is None]
        chk(f"负例 {len(neg)} 个返回空串", all(r[4] == 0 for r in neg),
            str([(r[0], r[4]) for r in neg if r[4]]))

        chk("compact 更短", d["cmp_len"] < d["full_len"], f"{d['cmp_len']} vs {d['full_len']}")
        chk("compact 有上限 (<=600)", d["cmp_len"] <= 600, str(d["cmp_len"]))
        chk("compact 保留技能名", d["cmp_has_name"], d["cmp_txt"][:60])
        chk("compact 无 markdown 小标题 (##)", not d["cmp_has_h2"], d["cmp_txt"][:80])
        chk("compact 无正文小标题 (执行流程)", not d["cmp_has_proc"], d["cmp_txt"][:80])

        chk("match_context 抛错时不冒泡", d["safe_raised"] is None, str(d["safe_raised"]))
        chk("异常时返回空串", d["safe"] == "", repr(d["safe"]))

    # ★ 2026-09-19: 闸门已迁进路由表 → 断言"契约"而非"源码里有那行 if"
    _rt_src = (ROOT / "core" / "routes.py").read_text(encoding="utf-8")
    chk("★ 三模式闸门已迁入路由表 (mode.gate)",
        "mode.gate" in src and "P_GATE" in src and "P_GATE = " in _rt_src)
    # 注意: note 文本里含右括号 → 不能用 [^)]* 截断, 改成窗口匹配
    _gi = src.find('name="mode.gate"')
    _gw = src[_gi:_gi + 400] if _gi != -1 else ""
    chk("★ 闸门标了 writes_state (plan 会落盘) 且 probe_safe",
        _gi != -1 and "writes_state=True" in _gw and "probe_safe=True" in _gw, _gw[:120])
    chk("闸门优先级在命令(1000)之下、@模型(900)之上",
        re.search(r"P_GATE\s*=\s*(\d+)", _rt_src) is not None
        and 900 < int(re.search(r"P_GATE\s*=\s*(\d+)", _rt_src).group(1)) < 1000)
    chk("IR chain 仍在 _process_external 之前",
        src.find("async def _ir_chain_exec") < src.find("async def _process_external"))
    chk("注入点在 _process_external 内",
        src.find("_skill_ctx = self._skill_context") > src.find("async def _process_external"))

    print("        —— 端到端: 真实拼装 system prompt (拦截 model_client.generate, 零 token) ——")
    e2e_out, e2e_err = probe(E2E, timeout=500)
    e = None
    try:
        e = json.loads(e2e_out.split("<<<JSON>>>")[1].strip().splitlines()[0])
    except Exception as ex:
        chk("e2e 可解析", False, f"{ex} | {e2e_out[-150:]} | {e2e_err[-150:]}")

    if e:
        pos, neg = e.get("pos", {}), e.get("neg", {})
        if pos.get("no_call") or neg.get("no_call"):
            chk("e2e 到达模型 (未走 IR chain)", False,
                f"pos_no_call={pos.get('no_call')} neg_no_call={neg.get('no_call')}")
        else:
            chk("正例 system prompt 含 [Skills matched]", pos.get("has_skills") is True)
            chk("正例含正确技能 send-file-to-contact", pos.get("has_file") is True)
            chk("负例无技能注入", neg.get("has_skills") is False)
            chk("负例不含任何技能名",
                not neg.get("has_file") and not neg.get("has_email"))
            chk("注入确实增大了 prompt", pos.get("sys_len", 0) > neg.get("sys_len", 0),
                f"{pos.get('sys_len')} vs {neg.get('sys_len')}")

    r = subprocess.run([PY, "-B", "-m", "pytest", "tests/", "-q", "--no-header",
                        "-p", "no:cacheprovider"], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=580)
    tail = [l for l in r.stdout.split("\n") if "passed" in l or "failed" in l]
    print("        pytest:", tail[-1] if tail else r.stdout.strip()[-80:])
    # 基线已从 "3 failed (TestLayState 契约冲突)" 变为 **全绿** (2026-09-18 契约修复)
    failed = {l[7:].split(" ")[0] for l in r.stdout.split("\n") if l.startswith("FAILED ")}
    chk("canonical 全绿 (0 failed)", not failed, str(sorted(failed)))

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    # ★ 快照/还原用户状态: 本验证器会真跑 pipeline (probe 子进程),
    #   perception.json 会被写 (实测) → 结束时字节级还原, 保证门禁不留痕。
    _perc = ROOT / "data" / "perception.json"
    _snap = _perc.read_bytes() if _perc.exists() else None
    try:
        _code = main()
    finally:
        if _snap is not None:
            _perc.write_bytes(_snap)
    sys.exit(_code)
