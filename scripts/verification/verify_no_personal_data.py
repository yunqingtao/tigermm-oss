"""公开前"洗净"门禁 — 分发包(工作树 + **全部 git 历史**)零个人数据 / 零凭据

为什么要有这一门
═════════════════════════════════════════════════════════════════════
2026-09-21 实测打脸: 昨天说"分发包零凭据/零个人数据" —— **不准**。
用对了模式一扫, 分发包里躺着:
  · 活的 Server酱 key (明文, 2 个文件 → 谁拿到包谁能往用户微信推消息)
  · 7 个 .bat 启动器写死作者的开发目录与家目录 (别人拿到点了没反应)
  · 门禁脚本里 100+ 处写死的绝对路径 / 用户名
  · 个人邮箱、机器名、VPS IP
昨天的扫描为什么漏: ① 只扫了几类想到的模式 (没扫 Server酱这种 SCT 前缀)
                  ② **只扫了工作树, 没扫 git 历史** —— 而公开后历史同样是可见的

本门禁守住的不变量
─────────────────────────────────────────────────────────────────
  [0] 判据有牙齿: 黑名单非空 + 种一个已知标识必须被抓到 (正控)
                  + 干净文件不误报 (负控)
  [A] 分发包**工作树**: 零黑名单命中、零通用凭据形态、零绝对路径泄露
  [B] 分发包**git 历史**: 所有 commit 的所有 blob 同样要零命中
      (公开 = 历史也公开; 只洗工作树等于没洗)
  [C] 通用形态 (不必依赖黑名单): 私钥块 / 各类 token 前缀 / 手机号 / 真实邮箱
      —— 允许表里只放**测试夹具**(xxx@example.com 之类)

黑名单为什么在仓库外
─────────────────────────────────────────────────────────────────
把标识写进本文件 = 门禁自己泄露它们 (而门禁随包出门)。
所以: 黑名单由 `_personal_denylist.txt`(仓库外, 被 "_personal_*" 模式排除)提供。
文件缺失 → **判红** —— 不许"取不到就当通过"。

跑法
─────────────────────────────────────────────────────────────────
  python scripts/verification/verify_no_personal_data.py
  python scripts/verification/verify_no_personal_data.py --pkg D:\\path\\to\\dist

自清理: 只读; 正控用临时目录, 用完即删。
"""
import argparse
import os
import re
import shutil
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PKG = ROOT.parent.parent / "tmm-dist"          # 默认 <项目根上级>/tmm-dist


def resolve_pkg(arg: str | None = None) -> Path:
    """定位要扫的分发包 —— ★ 2026-09-22 修: 原来只有"同级 tmm-dist"一条规则,
    本门禁**随包一起出门** ⇒ 在包里跑时它会算出 `D:\\tmm-dist` (少了一层) 而扫不到自己。
    现在的顺序: ① --pkg 显式 ② 环境变量 TMM_DIST_DIR ③ 本树就是包(有分发说明.md) ④ 同级 tmm-dist
    """
    if arg:
        return Path(arg).resolve()
    env = (os.environ.get("TMM_DIST_DIR") or "").strip()
    if env:
        return Path(env).resolve()
    if (ROOT / "分发说明.md").exists():          # 自己就在分发包里
        return ROOT
    return DEFAULT_PKG


DENY_FILE = ROOT / "_personal_denylist.txt"                  # 真实标识 (只在开发仓)
DENY_TEMPLATE = ROOT / "_personal_denylist.example.txt"      # 通用形态 (随分发包出门)

# 通用形态 (与黑名单无关, 永远不许出现) → 说明
GENERIC = {
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----": "私钥文件内容",
    r"\b(gho_|ghp_|github_pat_)[A-Za-z0-9_]{20,}": "GitHub token",
    r"\bSCT\d{6,}[A-Za-z0-9]*": "Server酱 SendKey",
    r"\bAKID[A-Za-z0-9]{12,}": "腾讯云 SecretId",
    r"(?<!\w)sk-[A-Za-z0-9]{24,}(?!\w)": "OpenAI/百炼风格 key",
    r"(?<!\d)1[3-9]\d{9}(?!\d)": "手机号 (11 位)",
    r"[A-Za-z0-9._%+-]+@(?:qq|163|126|gmail|outlook|hotmail|sina|foxmail)\.(?:com|cn|net)": "真实邮箱域名",
}
# ★ 2026-09-21: 具体的"绝对路径/作者标识"模式**不写在本文件里** —— 本文件随包出门,
#   写在这里 = 门禁自己泄露。它们由仓库外的 `_personal_denylist.txt` 提供 (见 load_denylist)。
# 允许表 (测试夹具 / 文档示例) —— 命中这些不算问题
ALLOW = {
    "13800000000", "13900000000", "13800001111", "13900002222", "13812345678",
    "test@example.com", "tester@example.com", "user@example.com", "example@example.com",
    "admin@site.org", "zhangsan@test.com", "tao@test.com", "tao_new@test.com",
    "sk-abc123", "sk-abcdef",           # 单测夹具 (顺序串, 非真 key)
    "keys(LAPTOP-xxx).json",            # 文档里描述机器名变体的写法 (字面 xxx)
    "xxx", "...",                       # 文档/注释里的占位写法
    "sk-abc...7890",                    # 门禁自检样例串 (不是 key)
}
TEXT_EXT = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".bat", ".cmd", ".ini",
            ".cfg", ".toml", ".ps1", ".sh", ".html", ".css", ".js", ".spec", ".in"}
MAX_BYTES = 3_000_000

P, F, S = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def warn(name, detail=""):
    print("  WARN " + name + (f"   <- {detail}" if detail else ""))


def skip(name, why=""):
    S.append(name)
    print("  SKIP " + name + (f"   ({why})" if why else ""))


def configured_secret_values():
    """→ (vals, files_seen)。files_seen>0 但 vals 为空 = 这层静默失效 (调用方必须判红)。"""
    """→ [(label, value), ...] 从本机凭据文件里读出**实际值**, 用来反查泄露。

    ★ 2026-09-21 加 (被真事打脸): 黑名单里只有**模式**(如 SCT\\d{6,}), 抓不到
      "把一个真凭据当样张写进代码" 这类泄露。实测就发生过: 门禁文件里硬写着真的
      163 授权码当测试样张, 而它随包出门 → 真凭据被公开。现在补上这一层:
      把本机 keys.json / keys_github.json / relay_tokens.json 的值全拿出来, 在
      仓库内容与历史里反查。凭据文件不存在(如别人的 clone) → 返回空, 该层跳过。
    """
    out, files_seen = [], 0
    for rel, sec, field in (("keys.json", "mail", "password"), ("keys.json", "sms", "secret_id"),
                            ("keys.json", "sms", "secret_key"), ("keys.json", "cli", "password"),
                            ("keys.json", "notify", "serverchan"), ("keys.json", "deepseek", "key"),
                            ("keys.json", "mimo", "key"), ("keys.json", "qwen", "key"),
                            ("keys_github.json", "github", "key")):
        _f = ROOT / rel
        if not _f.is_file():
            continue
        files_seen += 1
        try:
            d = json.loads(_f.read_text(encoding="utf-8"))
            v = str((d.get(sec) or {}).get(field) or "").strip()
            if len(v) >= 8:
                out.append((f"{rel}:{sec}.{field}", v))
        except Exception as e:
            print(f"  WARN  读不出 {rel} 的 {sec}.{field}: {type(e).__name__}: {e}")
    try:
        rt = json.loads((ROOT / "relay_tokens.json").read_text(encoding="utf-8"))
        for kk, vv in rt.items():
            if kk.startswith("_") or not vv:
                continue
            if len(str(vv)) >= 8:
                out.append((f"relay_tokens.json:{kk}", str(vv)))
    except Exception:
        pass
    return out, files_seen


def load_denylist():
    """→ [(label, compiled_regex), ...]；两份都没有才返回 None (调用方判红)。

    ★ 真实那份只在开发仓 (不进分发); 分发包里带的是模板 —— 所以依次尝试:
      真实 → 模板 → 都没有 = 判红 (不许"取不到就当通过")。
    """
    f = DENY_FILE if DENY_FILE.is_file() else (DENY_TEMPLATE if DENY_TEMPLATE.is_file() else None)
    if f is None:
        return None
    out = []
    for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append((line, re.compile(line[3:], re.I)))
        else:
            out.append((line, re.compile(re.escape(line), re.I)))
    return out


def scan_text(text, rules):
    """→ [(label, masked_hit), ...] (去重)"""
    hits = {}
    for label, rx in rules:
        for m in rx.findall(text):
            v = m if isinstance(m, str) else (m[0] if m else "")
            if not v or v in ALLOW:
                continue
            if any(a in v or v in a for a in ALLOW if len(a) > 6):
                continue
            hits[(label, _mask(v))] = True
    return list(hits)


def _mask(v):
    v = str(v)
    return v[:3] + "*" * max(1, len(v) - 6) + v[-3:] if len(v) > 8 else v[:1] + "***"


def iter_files(pkg: Path):
    for p in pkg.rglob("*"):
        if not p.is_file():
            continue
        if any(x in p.parts for x in (".git", "__pycache__", ".pytest_cache")):
            continue
        if p.suffix.lower() not in TEXT_EXT or p.stat().st_size > MAX_BYTES:
            continue
        yield p


def git_blobs(pkg: Path):
    """→ [(rev_path, bytes)] 全部 commit 的全部 blob (公开=历史也公开)。"""
    try:
        revs = subprocess.run(["git", "rev-list", "--all"], cwd=str(pkg), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=180)
        if revs.returncode != 0 or not (revs.stdout or "").strip():
            return None, "git rev-list 失败或无历史"
        seen, out = set(), []
        for rev in (revs.stdout or "").split():
            ls = subprocess.run(["git", "ls-tree", "-r", "-z", rev], cwd=str(pkg), capture_output=True,
                                timeout=180)
            for ent in ls.stdout.decode("utf-8", "replace").split("\0"):
                if not ent:
                    continue
                meta, _, path = ent.partition("\t")
                parts = meta.split()
                if len(parts) < 3 or parts[1] != "blob":
                    continue
                if path in seen:
                    continue
                seen.add(path)
                if Path(path).suffix.lower() not in TEXT_EXT:
                    continue
                cat = subprocess.run(["git", "cat-file", "-p", parts[2]], cwd=str(pkg),
                                     capture_output=True, timeout=120)
                if cat.stdout and len(cat.stdout) <= MAX_BYTES:
                    out.append((f"{rev[:7]}:{path}", cat.stdout))
        return out, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=None, help="分发包路径 (默认自动定位: 环境变量 → 本树 → 同级 tmm-dist)")
    ap.add_argument("--max-hits", type=int, default=40)
    args = ap.parse_args()
    pkg = resolve_pkg(args.pkg)

    print("=" * 78)
    print("公开前洗净门禁 — 分发包零个人数据 / 零凭据 (工作树 + 全部 git 历史)")
    print(f"    包: {pkg}")
    print("=" * 78)

    deny = load_denylist()
    print("\n[0] 判据自身的有效性")
    if deny is None:
        chk(f"黑名单文件存在 ({DENY_FILE.name} 或模板 {DENY_TEMPLATE.name})", False,
            f"找不到: {DENY_FILE} / {DENY_TEMPLATE}")
        print("\n结果: 判红 —— 黑名单缺失时不许静默通过")
        print("=" * 78)
        return 1
    chk(f"黑名单已载入 ({'真实' if DENY_FILE.is_file() else '模板'})", len(deny) >= 5, f"仅 {len(deny)} 条")
    # 正控: 种一个黑名单里的字面量 → 必须被抓
    #   ★ 2026-09-22 修 (模板场景失效): 随包的模板**全是正则、没有字面量**,
    #   于是 `planted=None` → 正控自己失败 ("没抓到 None"), 而这不是判据没牙齿,
    #   是**夹具没东西可种**。修法: 没有字面量就从 GENERIC 里合成一条假的种进去
    #   (假值只进临时夹具, 不进仓库)。
    tmp = Path(tempfile.mkdtemp(prefix="hermes-verify-deny-"))
    try:
        probe = tmp / "planted.py"
        planted = next((l for l, _ in deny if not l.startswith("re:")), None)
        if planted is None:
            planted = "ghp_" + ("A1b2C3d4E5f6G7h8I9j0" * 2)[:24]     # 形如 GitHub token 的假值
        probe.write_text(f"contact = {planted!r}\n", encoding="utf-8")
        rules = deny + [(k, re.compile(k)) for k in GENERIC]
        caught = scan_text(probe.read_text(encoding="utf-8"), rules)
        chk("★ 正控: 种入一个黑名单标识会被抓到 (判据有牙齿)", bool(caught),
            f"没抓到 {planted!r}")
        # 负控: 干净内容不该误报
        clean = tmp / "clean.py"
        clean.write_text("HOST = '127.0.0.1'\nCONTACT = 'user@example.com'\n", encoding="utf-8")
        chk("★ 负控: 干净文件不误报 (允许表生效)", not scan_text(clean.read_text(encoding="utf-8"), rules))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not pkg.is_dir():
        skip("分发包扫描", f"目录不存在: {pkg}")
        print("\n结果: 0 PASS / 0 FAIL (包不存在, 跳过) —— 先跑 make_dist_repo.py")
        print("=" * 78)
        return 0

    rules = deny + [(k, re.compile(k)) for k in GENERIC]

    print("\n[A] 分发包工作树")
    files = list(iter_files(pkg))
    chk("扫到文件 (判据自证有数据)", len(files) >= 100, f"仅 {len(files)} 个可读文本文件")
    w_hits = {}
    for p in files:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for label, hit in scan_text(txt, rules):
            w_hits.setdefault(f"{p.relative_to(pkg).as_posix()}  [{label} = {hit}]", 0)
            w_hits[f"{p.relative_to(pkg).as_posix()}  [{label} = {hit}]"] += 1
    if w_hits:
        chk(f"★ 工作树零命中 (共 {len(w_hits)} 条)", False, "")
        for k in sorted(w_hits)[:args.max_hits]:
            print(f"        {k}")
        if len(w_hits) > args.max_hits:
            print(f"        ... 还有 {len(w_hits) - args.max_hits} 条")
    else:
        chk("★ 工作树零命中 (零个人数据 / 零凭据)", True)

    print("\n[B] 分发包 git 历史 (公开后历史同样可见)")
    blobs, err = git_blobs(pkg)
    if blobs is None:
        chk("读 git 历史", False, err or "")
    else:
        chk(f"读到历史 blob (自证有数据)", len(blobs) > 0, f"{len(blobs)} 个")
        h_hits = {}
        for name, raw in blobs:
            txt = raw.decode("utf-8", "replace")
            for label, hit in scan_text(txt, rules):
                h_hits[f"{name}  [{label} = {hit}]"] = True
        if h_hits:
            chk(f"★ 历史零命中 (共 {len(h_hits)} 条, {len(set(k.split(':')[0] for k in h_hits))} 个 commit)", False, "")

            for k in sorted(h_hits)[:args.max_hits]:
                print(f"        {k}")
            if len(h_hits) > args.max_hits:
                print(f"        ... 还有 {len(h_hits) - args.max_hits} 条")
            print("        处置建议: 重建仓库(全新单提交) 比改写历史更干净")
        else:
            commits = subprocess.run(["git", "rev-list", "--count", "--all"], cwd=str(pkg),
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
            chk(f"★ 历史零命中 (共 {commits.stdout.strip() or '?'} 个 commit)", True)

    # ── [C] 实际凭据值反查 (本机有凭据文件时才有意义) ──
    #   ★ 2026-09-21 加 (被真事打脸): 黑名单里只有**模式**(如 SCT\d{6,}), 抓不到
    #   "把真凭据当样张写进代码" 这类泄露 —— 实测就发生过 (门禁文件里硬写着真授权码,
    #   而它随包出门)。这一层拿本机凭据的**实际值**反查包里与历史。
    print("\n[C] 实际凭据值反查 (拿本机凭据去包里/历史里找)")
    vals, files_seen = configured_secret_values()
    if not vals:
        chk("★ 凭据文件在时这层不静默失效", files_seen == 0,
            f"看到 {files_seen} 个凭据文件却读出 0 个值 → 这层形同虚设")
        print("  SKIP  本机没有可读的凭据文件 —— 这一层跳过 (别人的 clone 本就没有凭据)")
    else:
        hits_c = []
        for _p in files:
            try:
                _txt = _p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for _lb, _vv in vals:
                if _vv in _txt:
                    hits_c.append(f"{_p.relative_to(pkg).as_posix()}  [{_lb}]")
        if blobs:
            for _name, _raw in blobs:
                _txt = _raw.decode("utf-8", "replace")
                for _lb, _vv in vals:
                    if _vv in _txt:
                        hits_c.append(f"{_name}  (git 历史)  [{_lb}]")
        chk(f"★ 实测 {len(vals)} 个真实凭据值均未出现 (在工作树 + git 历史里)", not hits_c, hits_c[:5])

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
