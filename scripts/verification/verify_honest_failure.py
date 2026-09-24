"""知识库技能「谎报成功」修复 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

用户报的现象 (原话贴的终端输出):
    实干 / knowledge_exec / local / 3.85s
    OK 写文件: Is a directory: . Specify a filename.
→ 操作明明失败了, 却配着 "OK" 前缀显示。

根因 (core/knowledge.py execute() 末尾):
    # Return error as success output so pipeline shows it to user
    return {'success': True, 'output': err_msg}      ← 故意的谎报
连带危害: skill_loader / workflow 靠 result['success'] 判断是否中断链条
          → 失败被谎报成成功, 多步工作流会继续往下跑。

本轮一并修掉的相邻真缺陷:
  1. knowledge.execute 失败 → success=False + error (诚实)
  2. pipeline/brain 失败分支 → "✗ <技能> 没执行成功: <原因>" (不再落到模型猜"已完成")
  3. pipeline 两处 `str(prev)` 兜底 → 裸 dict 泄漏给用户 (实测 "写文件" 打印出 Python dict)
  4. knowledge.execute 用裸文件名覆盖用户给的完整路径 → 文件写去桌面 (实测指定 D:\...\tmp 落到 Desktop)
  5. file_ops 绝对路径 + write 落在目录 → 提前返回目录导致 "Access denied"; 改为清晰的
     "Is a directory: ... Specify a filename."
  6. intent_router 把触发词本身当内容 ("写入文件" → content="文件") → 写出内容为"文件"的垃圾文件

跑法:
    python -B scripts/verification/verify_honest_failure.py

断言: 失败必报失败 / 无裸 dict 泄漏 / 不写垃圾文件 / 成功路径仍正确 / pytest 基线。
自清理: 删掉本脚本产生的临时文件; 不碰用户数据。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time          # ← 2026-09-20 加: 清理重试需要退避等待
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
import sys, os, json, asyncio, logging
sys.path.insert(0, r"@@PROJ@@"); os.chdir(r"@@PROJ@@")
logging.disable(logging.CRITICAL)
from main import _load_config
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline
from config.settings import DATA_DIR
from core.knowledge import KnowledgeEngine
from gateway.plugin_mgr import PluginManager

cfg = _load_config()
pl = Level4Pipeline(ModelClient(cfg), cfg)
ke = KnowledgeEngine(DATA_DIR); pm = PluginManager(DATA_DIR)
out = {}

# A. 真 pipeline: 垃圾输入必须诚实报错, 不得泄漏 dict
out["pipeline"] = []
for msg in ["写文件", "写入文件", "写一行字", "创建文件"]:
    r = asyncio.run(pl.process(msg, probe=True))
    resp = str(r.get("response"))
    out["pipeline"].append([msg, r.get("intent"), resp[:150],
                            resp.strip().startswith("{")])

# B. ke.execute 失败语义
async def kb():
    res = []
    for msg in ["写文件", "写一段文字"]:
        p = ke.parse(msg)
        r = await ke.execute(p, pm)
        res.append([msg, bool(r.get("success")), str(r.get("error"))[:80]])
    # 成功路径: 指定目录 + 文件名 → 必须写对地方
    p = ke.parse(f"在 @@PROJ@@/tmp 新建 hv_verify.txt 写入 你好")
    r = await ke.execute(p, pm)
    # ★ 2026-09-22 加: 期望落点用**相对路径**表达 ("tmp/hv_verify.txt") ——
    #   原来断言写的是 `"mary3" in 输出`, 把**开发仓目录名**写进了判据 ⇒
    #   树被复制/改名/在 clone 里跑时必红 (gate 脆弱, 与"不写死门禁名"同一族)。
    res.append(["dir+name", bool(r.get("success")),
                str(r.get("output") or r.get("error"))[:110],
                os.path.join("tmp", "hv_verify.txt")])
    return res
out["ke"] = asyncio.run(kb())
print("<<<J>>>" + json.dumps(out, ensure_ascii=False))
'''


def probe(src, timeout=420):
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    Path(tmp).write_text(_fill(src), encoding="utf-8")
    try:
        r = subprocess.run([PY, "-B", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        return r.stdout, r.stderr
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    # 静态: 谎报已移除
    # 注意断言要打在**代码**上, 不能打在注释上 —— "Return error as success output" 这句
    # 现在只出现在我的说明注释里 (v1 的断言就误报了这条)
    k = (ROOT / "core" / "knowledge.py").read_text(encoding="utf-8")
    code_lines_only = "\n".join(l for l in k.split("\n") if not l.strip().startswith("#"))
    chk("knowledge.execute 不再把错误包成 success=True",
        "'success': True, 'output': err_msg" not in code_lines_only)
    chk("失败返回 success False + error",
        "return {'success': False, 'error': str(error_msg)}" in k)
    # pipeline: 输出提取处不得再有 str(prev) 兜底。
    # 排除 ①注释行 ②ir_trace 调试轨迹记录 (那本来就是给排障看的, 记录原始 dict 是合理的)
    pl_src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    pl_code = "\n".join(l for l in pl_src.split("\n") if not l.strip().startswith("#"))
    leftovers = [l.strip() for l in pl_code.split("\n")
                 if "str(prev)" in l
                 and '"args"' not in l and '"result"' not in l]
    chk("输出提取处无 str(prev) 裸 dict 兜底", not leftovers, str(leftovers[:2]))
    chk("brain 失败分支诚实", "没执行成功" in (ROOT / "core" / "brain.py").read_text(encoding="utf-8"))
    chk("intent_router 触发词不当内容",
        'if content in ("文件", "文档"' in (ROOT / "core" / "intent_router.py").read_text(encoding="utf-8"))

    out, err = probe(PROBE)
    d = None
    try:
        d = json.loads(out.split("<<<J>>>")[1].strip().splitlines()[0])
    except Exception as e:
        chk("探针可解析", False, f"{e} | out={out[-180:]} | err={err[-180:]}")

    if d:
        print("        —— 真 pipeline: 垃圾输入 ——")
        dirties = [x for x in d["pipeline"] if x[3]]
        chk("无裸 dict 泄漏", not dirties, str([(x[0], x[2][:60]) for x in dirties]))
        okfail = [x for x in d["pipeline"] if "✗" in x[2]]
        chk(f"垃圾输入全部诚实报错 ({len(okfail)}/{len(d['pipeline'])})",
            len(okfail) == len(d["pipeline"]), str([(x[0], x[2][:60]) for x in d["pipeline"] if "✗" not in x[2]]))

        print("        —— ke.execute 语义 ——")
        fails = [x for x in d["ke"] if x[0] != "dir+name"]
        chk("失败报 success=False", all(not x[1] for x in fails),
            str([(x[0], x[1]) for x in fails]))
        chk("失败带 error 文本", all(x[2] for x in fails), str(fails))
        succ = [x for x in d["ke"] if x[0] == "dir+name"]
        if succ:
            s = succ[0]
            chk("指定目录的成功写入", s[1] is True, str(s))
            # ★ 判据与**目录名无关** (2026-09-22 修): 用探针给的相对落点做"结尾匹配",
            #   不再要求输出里出现开发仓的目录名 —— 那样换个目录跑必假红。
            _want = str(s[3]).replace("\\", "/").lower() if len(s) > 3 else "tmp/hv_verify.txt"
            _got = str(s[2]).replace("\\", "/").lower()
            chk("写到了指定目录 (不是桌面)",
                _got.endswith(_want) and "desktop" not in _got, f"want=…/{_want} got={s[2]}")

    # 清理探测产生的文件
    # ★ 2026-09-20 修 (真缺陷: 清理失败把整个验证器掀翻, 0 PASS / 1 FAIL):
    #   原来这里是裸 `f.unlink()` —— 无重试无兜底。Windows 上文件刚被写/读时若仍被占用
    #   (Defender 实时扫描、句柄未及时释放), 会抛 PermissionError: [WinError 32],
    #   于是**所有断言都过了, 却因为一句清理报错整门判红**。
    #   实测: tmp/hv_verify.txt 被上一轮崩掉时留下的残留占着 → 连续两次 6 秒崩;
    #         手工删掉残留后立刻 12 PASS / 0 FAIL。
    #   修法: 带退避重试 + 失败只提示不致命 (清理不是被测行为, 不该有判红权)。
    def _rm_quiet(p, tries=5):
        for k in range(tries):
            try:
                p.unlink(missing_ok=True)
                return True
            except PermissionError:
                time.sleep(0.4 * (k + 1))
            except Exception:
                break
        return not p.exists()

    _left = []
    for n in ["hv_verify.txt", "文档.txt"]:
        for dd in [ROOT, ROOT / "tmp", Path.home() / "Desktop"]:
            f = dd / n
            if f.exists() and not _rm_quiet(f):
                _left.append(str(f))
    chk("探测产物已清理干净 (清理不致命)", not _left, f"仍被占用: {_left}")

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
    sys.exit(main())
