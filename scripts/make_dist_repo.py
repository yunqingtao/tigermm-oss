# -*- coding: utf-8 -*-
"""生成"干净分发仓库" (选项 C) —— 只提交当前工作树, 不带开发史。

为什么选 C: 现有仓库历史里有真凭据 (keys.json 内容在提交 b8f251a 里)。
  A 只本地用 = 永远不能开源; B 重写历史 = 动现有仓库全部提交; C 另起干净仓库 = 最稳。
  分发包本来也不该带开发史 (备份/tmp/调试脚本/个人数据)。

做法 (非破坏性): 新建目录, 按 docs/data_boundary.md 复制**可分发**内容, git init + 首次提交。
  ★ 绝不碰现有仓库 (不 fetch/不 push/不改它的历史)。

用法:
  python scripts/make_dist_repo.py [目标目录]     默认 <项目根上级>/tmm-dist (TMM_DIST_DIR 可覆盖)
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
# 默认 = 项目根的**上一级**目录下 tmm-dist (本机即 <分发包目录>); 可用 TMM_DIST_DIR 覆盖
DEFAULT_DST = Path(os.environ.get("TMM_DIST_DIR") or (SRC.parent.parent / "tmm-dist"))

# 不随包出去 (与 docs/data_boundary.md 对应)
# ★★ 2026-09-20 重做 (判据修正): 原表按"看起来像不像个人目录"排除, 把**项目内容**也排了。
#   踩过的原型: `sandbox/` 被当个人目录排除, 但它被 core/code_engine.py 与
#   api/routers/code.py import —— 换成"归属"判据才发现 (零引用不等于不重要)。
#   现在的判据: **只排"真噪音 + 个人数据 + 运行产物"**, 项目内容一律留下。
#   放回的: sandbox/(引擎代码!) reports/(测试报告) training/ demos/ lottery/(双色球子系统)
SKIP_DIRS = {".git", "backups", "tmp", "logs", "voice-memos",      # 个人数据/运行产物
             "__pycache__", ".pytest_cache", ".hermes",            # 缓存
             "code_pool"}                                          # 沙箱产物

#: ★★★ 2026-09-24 加: **按相对路径**排除 —— 监控日报 (docs/monitor/) 里有
#:   ① 用户绝对路径 (机器上的项目根, 属个人标识) ② **对话原话摘录** (用户重发清单),
#:   属"运行产物 + 个人数据", 绝不能进分发包。
#:   实测: 发布闸抓出 **278 条命中** (18 个文件), 直接 fail-closed 拒绝推送。
#:   为什么不用 SKIP_DIRS: 那是"目录名"匹配, 会把**任何**叫 monitor 的目录都排掉 (太粗)。
SKIP_REL_PREFIXES = ("docs/monitor",)
SKIP_FILES = {"keys.json", "keys_github.json", "keys.enc", ".crypto.key", "tmm_output.log",
              "watchdog.py",     # ★ 2026-09-21: 个人运维脚本(你的 VPS/SSH密钥/中继/Server酱) → 不进分发
              "tmm_app.log", "web_chat.log", "web_ui.err.log", "web_ui.out.log",
              "ws_fix.err.log", "ws_fix.out.log", "ws_indep.err.log", "ws_indep.out.log",
              "ws_qwen.err.log", "ws_qwen.out.log", "ws313.err.log", "ws313.out.log",
              "screen_before.png", "screen_now.png", "screen1.png", "screen2.png",
              "tool_test_screenshot.png", "agent_test.txt", "test_fix.txt",
              "keys.json.bak_gemma", "relay_tokens.json",
              # ★ 2026-09-20 判据修正: 原先把 8 个"一次性开发脚本"也排除了。
              #   用户明确: 自用仓库要"利用好", 零引用不等于没价值 → 全部放回。
              #   唯一例外 fix_tchat.py: 它写死**另一个项目**的路径 (D:\tools\tchat-apk),
              #   与本项目无关, 属噪音 → 继续排除。
              "fix_tchat.py",
              # ★ 2026-09-20 实测: tools/test.png 是调试残留 (零引用), 躺在代码目录里
              #   → 排除。而 lottery/output/*.png 是**自有子系统的生成图**, 保留。
              "test.png",
              # ★ 2026-09-21 (公开前洗净): 个人开发残留 —— 里面写死了作者的目录/机器名/账号,
              #   且都不是产品功能。留在开发仓, 不进分发。
              "PROGRESS.md", "pipeline_recovered.py", "test_ollama_2round.py", "test_ollama_tools.py",
              "_audit_task1.py", "_audit_task2.py", "_audit_task3.py", "_patch_all.py",
              "_patch_pass_rate.py", "_search_hermes.py", "_test_pass_rate.py", "_verify_backup.py",
              "untrack_secrets.sh", "TMM_深度功能测试报告.md"}
SKIP_PATTERNS = ("keys(*", "relay_tokens(*", "*.bak*", "*.vfybak", "*.advbak", ".hermes-tmp.*",
                 "_personal_denylist.txt",   # ★ 2026-09-21: 真实个人标识黑名单 → 只在开发仓
                                             #   (模板 `_personal_denylist.example.txt` 随包, 里面没有真标识)
                 "*.tmp", "*.orig", "*.rej", "*.debug", "*.log", "*.pyc", "memory_node_*",
                 # ★ 2026-09-21 (实测): 门禁套件会临时生成探针文件 (verify_zz_probe_*.py) 并自清;
                 #   但**套件被中途杀掉时它会留在原地** → 下次自动推送就把它推上公开仓库。
                 #   这类"验证过程的临时件"一律不进包 (交付物禁混调试痕迹)。
                 "verify_zz_probe_*", "*.vfybak", "*.advbak", "*.probe_tmp*", "_probe_*",
                 # ★ 2026-09-20: "_patch_*/_audit_*/_check_keys/..." 原来在此被排除 ——
                 #   那批是开发过程脚本 (审计/修补/自查), 属"历史资产", 已放回。
                 "*.zip",              # 大备份包 (55MB 级 —— git 不适合装这个)
                 # ★ 2026-09-20 实测补漏: 这三类曾随包出门
                 "*(LAPTOP-*)*",      # 带机器名的残留 (tf_1077(LAPTOP-xxx).txt ChainTest_OK 等
                                      #   —— 零字节垃圾, 且泄露主机名)
                 "*.key",             # 加密密钥 (.crypto(LAPTOP-xxx).key —— 绝不能出门)
                 "*.ico.dup", "tf_*ChainTest_OK")
# ★ 实测补漏三次 —— 排除表是"分发包的边检", 必须逐条被验证过:
#   ① 只写 "*.bak" → 漏 `pipeline.py.bak_graph1`; 加 "*.bak_*"/"*.bak.*" → 又漏 `cli.py.bak3`
#      → 统一 "*.bak*" (任何含 .bak 的名字)
#   ② `docs/memory_node_*.md` 本来就在 docs/data_boundary.md 里写明"不出去"(含个人项目与决策),
#      但排除表里没有这条 → 两个记忆节点真跟着进了包 (ad-hoc 验证抓出)
#   ③ ".log/.pyc" 原来只在 main() 里另判一次 → 逻辑分散, skipped() 单测时看不出 → 收进本表,
#      现在 skipped() 是**唯一**判据 (main 只调它)
# 个人数据目录: 建空目录(带 .gitkeep)而不是整个不建 —— 引擎起来要用
EMPTY_DIRS = ["data", "backups", "voice-memos", "logs", "tmp"]


#: ★ 2026-09-22 白名单: 名字像"临时探针"但**其实是常驻 helper**, 必须随包。
#   实测踩到 (`_probe_*` 通配误伤): `_probe_env.py` 是"探针隔离 helper"(把测试流量关进
#   临时沙箱, 防污染用户真实数据), 被 `verify_nl_natural` / `verify_chain_decision` /
#   `verify_self_lookup` / `verify_static_hygiene` **四个门禁 import**。
#   它被排掉 ⇒ 这四个门禁在**分发包里直接 ModuleNotFoundError 跑不起来**
#   (在公开站 clone 下来实测才发现的: 门禁 exit=1 且无结果行)。
KEEP_NAMES = {"_probe_env.py", "_nl_sanitize.py"}


def skipped(name: str) -> bool:
    """**唯一**的"不随包出去"判据 (按文件名)。main() 只调它, 不再另判 ——
    逻辑分散会让单测看不出漏网 (踩过: .log/.pyc 曾在 main 里另判一次)。

    ★ 2026-09-20 再修: 把 SKIP_FILES(精确名单) **并进来**。
      之前判据第二处分散: main() 里写 `p.name in SKIP_FILES or skipped(p.name)`,
      于是"精确名单"这一半在 skipped() 外面 —— 单测 skipped() 测不到它
      (ad-hoc 探针就这么踩了一次: 断言 skipped('calculator.py') 为真, 实际 False)。
      现在 skipped() = 精确名单 ∪ 通配模式, 一处判完。"""
    from fnmatch import fnmatch
    if name in KEEP_NAMES:          # 白名单优先 (常驻 helper 不受"_probe_*"通配误伤)
        return False
    return name in SKIP_FILES or any(fnmatch(name, p) for p in SKIP_PATTERNS)


DIST_README_NAME = "分发说明.md"   # 生成物 (镜像时不当"源里没有"删掉)


def write_dist_readme(dst: Path) -> Path:
    """写**给收件人**的上手说明 —— 必须在 commit **之前**调用, 否则永远不在仓库里。

    踩过 (2026-09-20): 原来这段写在 main() 里 commit **之后** → 收件人 clone 下来
    反而看不到这份说明 (而它是"怎么跑起来"的第一入口)。现在提取成函数, 由
    main() 与 scripts/daily_push.py **共用同一处内容**。
    """
    p = dst / DIST_README_NAME
    p.write_text(
        "# 这是 Tiger.M.M 的分发包\n\n"
        "由 `scripts/make_dist_repo.py` 从开发仓库生成 —— **不含开发史、不含个人数据**。\n\n"
        "## 三步跑起来\n\n"
        "```bat\n"
        "python install.py     :: 装依赖 + 生成 keys.json + 自检\n"
        ":: 编辑 keys.json 填 API Key（或只装 Ollama，零成本试）\n"
        "python main.py        :: CLI 入口\n"
        "python web_server.py  :: 网页版 → http://localhost:8800\n"
        "```\n\n"
        "\n## 关于门禁：在**分发包**里跑 `scripts/hermes_verify.py` 会有若干门报红 —— 属预期\n\n"
        "这套门禁是**开发树**的自检工具（守的是「改动别打坏东西」），其中不少要读\n"
        "**本机数据/凭据**（会话库、已装清单、API Key、本机环境表…）—— 那些按设计**不随包**。\n"
        "所以在新机器上它们会红，红的原因是「依赖不在」，**不是你的代码坏了**。\n\n"
        "在分发包里**应该全绿**的是这几道（不依赖本机数据，可当交付自检用）：\n"
        "```bat\n"
        "python scripts/verification/verify_no_personal_data.py   :: 个人标识/凭据零命中\n"
        "python scripts/verification/verify_distribution_ready.py:: 分发就绪\n"
        "python scripts/verification/verify_nl_natural.py        :: 自然语句不变量\n"
        "python scripts/verification/verify_chain_exec.py        :: 链条契约\n"
        "python scripts/verification/verify_danger_guard.py      :: 危险目标拦截\n"
        "python scripts/verification/verify_self_lookup.py       :: 缺信息自查（端到端那层会自动跳过）\n"
        "```\n"
        "其余门禁请在**你自己的开发树**里跑（那时本地数据齐了，才是有意义的判据）。\n"
        "详细说明见 `README.md`；数据边界见 `docs/data_boundary.md`。\n",encoding="utf-8")
    return p


def main() -> int:
    dst = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DST
    if dst.exists():
        if dst.name and (dst / ".git").exists():
            print(f"★ 目标已存在且是 git 仓库: {dst}\n  先删掉或换个目录 (我不动它)。")
            return 1
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)

    n_files = n_skip = 0
    for p in SRC.rglob("*"):
        rel = p.relative_to(SRC)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        # ★★★ 2026-09-24: 按**相对路径前缀**排除 (监控日报等运行产物+个人数据)
        _rs = rel.as_posix()
        if any(_rs == _pre or _rs.startswith(_pre + "/") for _pre in SKIP_REL_PREFIXES):
            continue
        if rel.parts and rel.parts[0] in EMPTY_DIRS:
            continue          # 个人数据目录: 后面建空的
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        if skipped(p.name):        # 精确名单已并入 skipped(), 这里不再另判
            n_skip += 1
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst / rel)
        n_files += 1

    for d in EMPTY_DIRS:
        (dst / d).mkdir(parents=True, exist_ok=True)
        (dst / d / ".gitkeep").write_text("", encoding="utf-8")

    print(f"复制完成: {n_files} 个文件 (跳过 {n_skip} 个个人/调试文件)")
    print(f"目标: {dst}")

    # 致收件人的上手说明 (内容见 write_dist_readme, 必须在提交前写)
    write_dist_readme(dst)

    r = subprocess.run(["git", "init", "-q"], cwd=str(dst), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dst), capture_output=True)
    r2 = subprocess.run(["git", "-c", "user.name=TMM", "-c", "user.email=tmm@local",
                         "commit", "-q", "-m",
                         "Tiger.M.M 分发包 (由 scripts/make_dist_repo.py 生成, 不含开发史与个人数据)"],
                        cwd=str(dst), capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("git init + 首次提交:", "OK" if r2.returncode == 0 else r2.stderr[:120])
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(dst), capture_output=True,
                        text=True, encoding="utf-8", errors="replace").stdout.strip()
    print("提交后工作树:", st if st else "干净 ✓ (所有交付文件都已入库)")
    if st:
        print("   ★ 有未提交文件 —— 说明有文件是提交后才生成的, 收件人 clone 不到:",
              st.splitlines()[:5])

    # ★ 在**新仓库**上跑分发门禁 —— 让它证明这个包是干净的
    gate = dst / "scripts" / "verification" / "verify_distribution_ready.py"
    if gate.exists():
        g = subprocess.run([sys.executable, "-B", str(gate)], cwd=str(dst),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        out = (g.stdout or "") + (g.stderr or "")
        tail = [l.strip() for l in out.splitlines() if "结果:" in l]
        print("\n新包上的分发门禁:", tail[-1] if tail else "(无结果行)")
        for l in out.splitlines():
            if l.strip().startswith("FAIL "):
                print("   ★", l.strip()[:130])
    else:
        print("★ 新包里没有分发门禁? 异常")


    print("\n下一步 (要我继续就说):")
    print(f"  1. 试跑:  cd {dst} && python install.py")
    print(f"  2. 看包:  cd {dst} && git log --oneline   (应只有 1 个提交)")
    print(f"  3. 发布:  在 GitHub 建空仓库后 git remote add origin … && git push -u origin master")
    return 0


if __name__ == "__main__":
    sys.exit(main())
