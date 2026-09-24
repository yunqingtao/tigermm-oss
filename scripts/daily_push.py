#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日自动推送 —— 把开发仓库的当前状态镜像到分发包目录, 提交并推到 GitHub。

定位 (用户 2026-09-20 拍板 "每日自动推送 + 恢复MD")
═══════════════════════════════════════════════════════════════════
手工推一次的仓库只是"某一天的快照", 不是备份。这个脚本让仓库变成**活备份**:
每天把工作树的最新状态推上去 → 任何时候都能回到之前某天的状态; 丢了机器, 换台机器
clone 就能继续。

它做什么
───────────────────────────────────────────────────────────────
  ① 用 make_dist_repo.py 的**同一套排除逻辑** (单一来源, 不另写一份判据)
     把开发仓库镜像进分发包目录 (新增/覆盖/删除, 保持与源一致)
  ② 有变化才 commit (没变化就什么都不做, 不产生空提交)
  ③ 推到 GitHub (私有仓库); 凭据取 GH_TOKEN 或 keys_github.json
  ④ 输出一行摘要, 供日志/定时任务查看

用法
───────────────────────────────────────────────────────────────
  python scripts/daily_push.py                 # 镜像 + 提交 + 推送
  python scripts/daily_push.py --dry-run       # 只看会有什么变化, 不提交不推送
  python scripts/daily_push.py --no-push       # 只提交到本地

★ 推送凭据不进 .git/config, 不进命令行 URL —— 用 http.extraheader 临时传。
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 默认 = 项目根的**上一级**目录下 tmm-dist; 可用 TMM_DIST_DIR 覆盖
DEFAULT_DST = Path(os.environ.get("TMM_DIST_DIR") or'' or (Path(__file__).resolve().parents[2].parent / "tmm-dist"))


def load_filter():
    """复用 make_dist_repo.py 的排除逻辑 (单一来源)。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("mdr", ROOT / "scripts" / "make_dist_repo.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def mirror(m, dst: Path) -> tuple:
    """把 SRC 按排除表镜像到 dst (新增/覆盖/删除)。返回 (新增, 更新, 删除)。"""
    added = updated = 0
    want = set()
    for p in m.SRC.rglob("*"):
        rel = p.relative_to(m.SRC)
        if any(part in m.SKIP_DIRS for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] in m.EMPTY_DIRS:
            continue
        if p.is_dir():
            continue
        if m.skipped(p.name):
            continue
        target = dst / rel
        want.add(str(rel).replace("\\", "/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(p, target); added += 1
        else:
            if target.stat().st_size != p.stat().st_size or \
               target.read_bytes() != p.read_bytes():
                shutil.copy2(p, target); updated += 1

    # 删除源里已不存在、或被排除表新挡住的旧文件 (镜像语义)
    # ★ 例外: **生成物**不当"源里没有"处理 —— 分发说明.md 由 write_dist_readme() 生成,
    #   它不在源树里, 第一版镜像逻辑把 1 个真文件删了 (dry-run 抓到)。
    generated = {getattr(m, "DIST_README_NAME", "分发说明.md")}
    removed = 0
    for f in sorted(dst.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        rel = str(f.relative_to(dst)).replace("\\", "/")
        if rel.endswith(".gitkeep") or rel in generated:
            continue
        if rel not in want:
            f.unlink(); removed += 1

    # 空目录占位 (引擎起来要用)
    for d in m.EMPTY_DIRS:
        (dst / d).mkdir(parents=True, exist_ok=True)
        gk = dst / d / ".gitkeep"
        if not gk.exists():
            gk.write_text("", encoding="utf-8")
    return added, updated, removed


def git(args, cwd, env=None):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=600)


def load_token() -> str:
    tok = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        return tok
    for name in ("keys_github.json", "keys.json"):
        p = ROOT / name
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        g = d.get("github") if isinstance(d.get("github"), dict) else d
        t = (g or {}).get("key") or (g or {}).get("token") or ""
        if isinstance(t, str) and t.strip():
            return t.strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="镜像 + 提交 + 推送 (每日自动推送)")
    ap.add_argument("--dst", default=str(DEFAULT_DST), help="分发包目录")
    ap.add_argument("--dry-run", action="store_true", help="不提交不推送")
    ap.add_argument("--no-push", action="store_true", help="只本地提交")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--repo", default="yunqingtao/tigermm", help="GitHub 仓库 <owner>/<name>")
    args = ap.parse_args()
    dst = Path(args.dst)

    if not (dst / ".git").exists():
        print(f"★ {dst} 不是 git 仓库 —— 先跑 python scripts/make_dist_repo.py")
        return 2

    m = load_filter()
    added, updated, removed = mirror(m, dst)

    # ★ 2026-09-21 加 (公开仓库的安全网): 镜像完**先过洗净门禁**, 有个人数据/凭据就拒绝提交+推送。
    #   为什么放这里: 这个仓库是**自动**推的, 人不会天天盯着 —— 万一哪天排除表漏了一类,
    #   下一个 30 分钟它就已经在公网上了。门禁扫 dst(将要提交的那棵树) 的工作树 + git 历史。
    gate = ROOT / "scripts" / "verification" / "verify_no_personal_data.py"
    if gate.is_file() and not os.environ.get("TMM_SKIP_CLEAN_GATE"):
        g = subprocess.run([sys.executable, "-B", str(gate), "--pkg", str(dst)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=1200)
        gout = (g.stdout or "") + (g.stderr or "")
        tail = [l.strip() for l in gout.splitlines() if l.strip().startswith(("结果:", "★ 工作树零命中", "★ 历史零命中"))]
        if g.returncode != 0:
            print("★ 洗净门禁未过 —— **拒绝提交/推送** (防止个人数据/凭据进公开仓库)")
            for l in gout.splitlines():
                if "[" in l and ("=" in l or "命中" in l):
                    print("   " + l.strip()[:150])
            print("   (要临时跳过: 设 TMM_SKIP_CLEAN_GATE=1 —— 只在你确认过风险时用)")
            return 3
        print("  洗净门禁: " + (tail[-1] if tail else "通过"))

    if hasattr(m, "write_dist_readme"):
        m.write_dist_readme(dst)        # 生成物: 与 make_dist_repo 共用同一处内容
    print(f"镜像: +{added} 新增, ~{updated} 更新, -{removed} 删除")

    st = git(["status", "--porcelain"], dst).stdout.strip()
    if not st:
        print("没有变化 —— 不提交 (避免空提交)")
        return 0
    if args.dry_run:
        print("--dry-run: 有变化但未提交。变化前 10 项:")
        for l in st.splitlines()[:10]:
            print("   ", l)
        return 0

    git(["add", "-A"], dst)
    msg = f"每日自动推送 ({added}新增/{updated}更新/{removed}删除)"
    r = git(["-c", "user.name=TMM", "-c", "user.email=tmm@local", "commit", "-q", "-m", msg], dst)
    if r.returncode != 0:
        print("★ 提交失败:", (r.stderr or r.stdout or "")[:200])
        return 1
    print("已提交:", msg)

    if args.no_push:
        print("--no-push: 未推送")
        return 0

    tok = load_token()
    if not tok:
        print("★ 没有 GitHub 凭据 (GH_TOKEN / keys_github.json) —— 已提交到本地, 未推送")
        return 1
    url = git(["remote", "get-url", "origin"], dst).stdout.strip()
    if not url:
        # ★ 2026-09-20 实测: 重新生成分发包 (rm -rf + git init) 会**丢掉 origin**,
        #   计划任务第一次跑就撞上 "origin does not appear to be a git repository"。
        #   这里自愈: 按仓库名补回 origin (干净 URL, 不带 token)。
        url = f"https://github.com/{args.repo}.git"
        git(["remote", "add", "origin", url], dst)
        print("(origin 缺失 → 已补)", url)
    hdr = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    r = git(["-c", f"http.https://github.com/.extraheader={hdr}", "push", "origin",
             f"HEAD:{args.branch}", "--force"], dst)
    ok = r.returncode == 0
    note = (r.stderr or "").replace(tok, "***").strip()
    print("推送:", "✓ 成功" if ok else f"★ 失败 {note[:200]}")
    print(f"   远程: {url}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
