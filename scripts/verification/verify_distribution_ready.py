"""分发就绪 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: **"在我机器上能跑" ≠ "别人拿到能跑"**。这份门禁只做一件事:
把"分发包会带出去的东西"当证据来查 (证据面 = `git ls-files`, 不是我的印象)。

为什么用 git ls-files 而不是扫工作树:
  分发给别人的就是仓库内容。工作树里有一堆开发残留(备份/tmp/调试脚本),
  拿工作树当证据会"狼来了", 拿仓库当证据才是真会出门的东西。

四条硬检查 (BLOCKING):
  ① 明文凭据: 仓库里不得有 API key / 邮箱授权码 / bot token
       (这是我**实际踩到**的: tools/send_email.py:27 与 tools/check_mail.py:24
        曾把 163 授权码硬编码, 而这两个文件是被 git 跟踪的 → 一旦 push 就出门)
  ② 运行时个人路径: core/ tools/ config/ 的**代码**(非注释)里不得出现
       硬编码的用户名/个人邮箱/绝对用户目录 —— 别人机器上不存在
  ③ requirements.txt 存在, 且覆盖源码里所有第三方顶层 import
  ④ PROJECT_ROOT 不得依赖 os.getcwd() (从别的目录启动会指错根)

两条软检查 (WARN, 只提示不判红):
  · 数据边界清单存在 (docs/data_boundary.md) —— 说明哪些数据不随包出去
  · 开发残留(测试/脚本里的绝对路径) —— 不影响分发, 只提示

技术要点:
  · 用 tokenize 跳过注释/文档串 → 不误报 (踩过: 注释里提本机项目路径 被当缺陷)
  · 判定"个人路径"用**派生对照**: 把 Path.home() 的等价写法(USERPROFILE/Path.home())
    视为合法, 只有**字面量**用户目录才算问题
  · 无网络、无副作用 (只读文件)

跑法:
    "python" -B scripts/verification/verify_distribution_ready.py
"""
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from importlib.metadata import PackageNotFoundError, packages_distributions, version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {"backups", "__pycache__", ".venv", "venv", "tmp", "data", "sandbox",
             "training", "lottery", "demos", "skills", ".git"}
RUNTIME_DIRS = ("core", "tools", "config")
FIRST_PARTY = {"core", "tools", "config", "storage", "ui", "gateway", "mcp", "tests",
               "scripts", "api", "data_loader", "predictors", "knowledge", "web", "apps"}

# 明文凭据特征 (命中即红线)
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_\-]{16,}", "API key (sk-…)"),
    (r"gho_[A-Za-z0-9]{20,}", "GitHub OAuth token (gho_…)"),
    (r"tchat-[A-Za-z0-9]{8,}", "TMM relay token (tchat-…)"),
    # ★ 实证补漏 (两次): ① 原来只认 password/授权码, 大写 SMTP_PASSWORD 漏抓 → 加 re.I
    #   ② 牙齿测试又发现 `API_KEY = "…"` 这种**通用密钥变量**根本没有正则 → 补上键名变体。
    #   值要求 ≥12 字符, 且占位符(见 PLACEHOLDER_RX)放过, 否则 keys_template 会假红。
    (r'["\']?(?:password|passwd|pwd|授权码|auth_?code|api[_-]?key|apikey|secret[_-]?id|'
     r'secret[_-]?key|secret|access[_-]?token|bot[_-]?token|client[_-]?secret)["\']?\s*'
     r'(?:[:=]=?|!=)\s*["\'][^"\']{12,}["\']', "硬编码口令/密钥/Token"),
]
# 占位/夹具豁免: 分发模板本来就该有占位; 单测夹具是**故意造假串**。
#   ★ 实测踩到: tests/test_guardrails.py 的夹具是 sk-abc123def456… (顺序字母数字),
#     我的旧豁免只认 "..." / "<" / xxxxxx, 于是把夹具当红线报了出来 (假阳性)。
#     豁免太宽会漏真 key, 太窄会天天狼来了 —— 所以下面还带**自检**(见 [0] 段)。
PLACEHOLDER_RX = (r"(PLACEHOLDER|your[_-]?(?:api[_-]?)?key|your[_-]?token|your[_-]?secret"
                  r"|x{6,}|X{6,}|<|\.{3}|…|填|示例|placeholder"
                  r"|abc123|abcdef|qwerty|dummy|example|test[_-]?key|changeme|todo"
                  r"|(?:key|token|secret|pass)[-_]?here)")
# ★ 踩过两次的教训 (别再把豁免放宽):
#   ① 曾把 "123456"/"01234"/"6789" 当夹具标记 → 结果把含 1234567890 的**真 token**
#      一起放过了 (自检抓出)。夹具串 "sk-abc123def456…" 靠 abc123 就够, 无需数字串。
#   ② 曾漏 "your-api-key-here" 这类英文占位 → 假阳性。现在按"key/token/secret + here"识别。


def _is_placeholder(frag: str) -> bool:
    return bool(re.search(PLACEHOLDER_RX, frag, re.I))
# 个人标识特征 (代码里出现才算问题)
# ★ 2026-09-21 改: 具体标识**不写在本文件里** —— 本文件随包出门, 写在这里 = 门禁自己泄露。
#   标识由仓库外的 `_personal_denylist.txt` 提供 (被 make_dist_repo 的 "_personal_*" 排除);
#   文件缺失 → 判红 (不许"取不到就当通过")。这里只留**通用形态**。
GENERIC_PERSONAL = [
    (r"C:[\\/]Users[\\/](?!\{\}|%|\.\.\.)[A-Za-z0-9_.\-]{2,}", "硬编码用户目录"),
]


def _personal_patterns():
    """→ [(regex, label)] 或 None (两份都没有)。

    ★ 真实黑名单只在开发仓 (不进分发); 分发包里带的是模板 → 依次尝试 真实 → 模板。
    """
    f = ROOT / "_personal_denylist.txt"
    if not f.is_file():
        f = ROOT / "_personal_denylist.example.txt"
    if not f.is_file():
        return None
    out = []
    for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append((line[3:], "个人标识(黑名单)") if line.startswith("re:")
                   else (re.escape(line), "个人标识(黑名单)"))
    return out


def _tracked_files() -> list[str]:
    r = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return []
    # ★ 本文件自己要从"待检清单"里排除 —— 它内部有**故意写下的样例凭据字面量**
    #   (自检用的 SMTP_PASSWORD/API_KEY 样张)。不排除的话, 本文件一旦被 git 跟踪,
    #   门禁就会扫到自己 → 永久假红。
    #   实测: 在开发仓库里它当时未被跟踪(所以没假红), 生成分发仓库后 git add -A 把它纳入
    #   → 立刻假红 6 处。这个坑正是"在分发包上再跑一次门禁"暴露的。
    _self = {Path(__file__).name, "scripts/verification/" + Path(__file__).name,
             "scripts\\verification\\" + Path(__file__).name}
    return [l.strip() for l in (r.stdout or "").splitlines()
            if l.strip() and l.strip().replace("/", "\\").split("\\")[-1] != Path(__file__).name
            and l.strip() not in _self]


def _strip_comments(text: str) -> str:
    """返回"代码文本" —— **注释置空, 其余逐字节保留** (空格/缩进/行列位置全不动)。

    ★ 踩过 (2026-09-20, 由牙齿测试连带发现): 早先版本是把 token 重新拼起来
      (`"".join(tok.string)`), 但 tokenize 给出的 token **不含词间空白** →
      `dir_path = r"…"` 被拼成 `dir_path=r"…"`: 列号偏移、依赖空白分词的模式可能漏判/误判。
      安全扫描器里这种"静默削弱"最危险 (它不会报错, 只会悄悄少抓)。
      现在改成: 用 tokenize 找出注释的 (行,列) 区间, 在**原文本**上原地填等长空格。
      (Python 的 COMMENT token 恒为单行, 所以区间不会跨行。)
    """
    lines = text.splitlines(keepends=True)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            (sr, sc), (er, ec) = tok.start, tok.end
            if sr == er and 1 <= sr <= len(lines):
                ln = lines[sr - 1]
                lines[sr - 1] = ln[:sc] + " " * (ec - sc) + ln[ec:]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text            # 解析不了就退回原文 (宁可严一点)
    return "".join(lines)


def _code_only(path: Path) -> str:
    """读文件 → 去注释。字符串字面量**保留** (硬编码路径就藏在里面),
    注释里的 sk-xxx / 本机路径 不算 (那是说明文字)。"""
    try:
        return _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""


def _third_party_imports() -> dict:
    mods = {}
    for f in ROOT.rglob("*.py"):
        rel = f.relative_to(ROOT)
        if any(p in SKIP_DIRS for p in rel.parts) or rel.name.startswith("_"):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.setdefault(a.name.split(".")[0], set()).add(str(rel))
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                mods.setdefault(node.module.split(".")[0], set()).add(str(rel))
    std = set(sys.stdlib_module_names)
    return {m: v for m, v in mods.items() if m not in std and m not in FIRST_PARTY}


def main() -> int:
    P, F, W = [], [], []

    def chk(name, cond, detail=""):
        (P if cond else F).append(name)
        print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))

    def warn(name, detail=""):
        W.append(name)
        print(f"  WARN {name}" + (f"   <- {detail}" if detail else ""))

    print("=" * 78)
    print("分发就绪 —— 证据面: git ls-files (真正会出门的东西)")
    print("=" * 78)

    # ── [0] 自检: 验证器自己有没有辨别力 (避免"门禁变噪音") ──
    print("[0] 自检 (验证器自身的辨别力)")
    _real_like = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"          # 像真 key
    # ★ 2026-09-21: 夹具改成**拼接**写法 —— 公开仓库里出现 "sk-" + 20 位以上字母数字,
#   会被安全扫描器当疑似真 key (误报)。拼接后文件文本里不存在该字面量, 运行时值不变。
    _fixture = "sk-" + "abc123def456ghi789jkl012mno345pqr678stu"        # 单测夹具 (顺序串)
    _caught = any(re.search(rx, _real_like) and not _is_placeholder(_real_like)
                  for rx, _ in SECRET_PATTERNS)
    _exempt = _is_placeholder(_fixture)
    chk("★ 真 key 抓得到 (有辨别力)", _caught, "真 key 都抓不到 → 门禁失效")
    chk("★ 单测夹具放过 (不狼来了)", _exempt, "夹具被当红线 → 门禁变噪音")
    # ★ 2026-09-21 修 (真泄露): 这里原来写的是**真的** 163 授权码 (拿真凭据当样张),
    #   而本文件随包出门 → 真凭据被公开。改用合成样张, 断言效力不变 (同样过 SECRET_PATTERNS)。
    chk("★ 真授权码抓得到", any(re.search(rx, 'password = "TestAuth9Xk2Qm7Zp"', re.I)
                                and not _is_placeholder('password = "TestAuth9Xk2Qm7Zp"')
                                for rx, _ in SECRET_PATTERNS))
    # ★ 这次牙齿测试暴露的盲点: 正则曾大小写敏感 → 大写写法漏抓 (真实代码里很常见)
    chk("★ 大写写法也抓得到 (SMTP_PASSWORD=…)", any(
        re.search(rx, 'SMTP_PASSWORD = "Qw3rTy9ZxC4vBnM7"', re.I)
        and not _is_placeholder('SMTP_PASSWORD = "Qw3rTy9ZxC4vBnM7"')
        for rx, _ in SECRET_PATTERNS))
    chk("★ API_KEY 大写也抓得到", any(
        re.search(rx2, 'API_KEY = "a1b2c3d4e5f6g7h8"', re.I) for rx2, lb in SECRET_PATTERNS
        if "key" in lb.lower() or "口令" in lb))
    # ★ 去注释必须"只清注释, 别动别的" —— 早先版本把 token 拼回, 丢了词间空格
    #   (`dir_path = r"…"` → `dir_path=r"…"`), 属安全扫描器的静默削弱。这里钉死:
    _sample = 'USER = testuser   # 注释: sk-abcdef1234567890 与 本机项目路径\n'
    _st = _strip_comments(_sample)
    chk("★ 去注释保留词间空格 (不把 token 粘起来)", 'USER = testuser' in _st, repr(_st))
    chk("★ 去注释只清注释 (代码原样, 注释消失)",
        ('sk-abcdef' not in _st) and ('本机项目路径' not in _st) and ('testuser' in _st), repr(_st))
    chk("★ 去注释后行数不变 (行号可信)", _st.count(chr(10)) == _sample.count(chr(10)))

    tracked = _tracked_files()
    chk("仓库可读 (git ls-files 有输出)", bool(tracked), "不是 git 仓库?")
    if not tracked:
        print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
        return 1
    print(f"  受检文件: {len(tracked)} 个 (git 跟踪)")

    # ── ① 明文凭据 ──
    print("\n[1] 明文凭据 (仓库里不得有)")
    secret_hits = []
    for rel in tracked:
        p = ROOT / rel
        if not p.is_file() or p.suffix.lower() not in (".py", ".json", ".bat", ".txt", ".md", ".ini", ".yaml", ".yml", ".sh", ".ps1"):
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for rx, label in SECRET_PATTERNS:
            # ★ 实证踩坑: 这里原来没开 IGNORECASE → 大写写法 `SMTP_PASSWORD = "…"` 抓不到!
            #   (牙齿测试时注入 SMTP_PASSWORD 竟然全绿, 才发现)。真实代码里大写很常见。
            for m in re.finditer(rx, txt, re.I):
                ln = txt[:m.start()].count("\n") + 1
                # 模板/占位/单测夹具放过 (分发用的 keys_template.json 就该有占位)
                if _is_placeholder(m.group(0)):
                    continue
                secret_hits.append(f"{rel}:{ln} {label}")
    chk("★ 仓库内无明文 API key / 授权码 / token", not secret_hits, secret_hits[:6])
    if secret_hits:
        print("       命中明细:")
        for h in secret_hits[:12]:
            print("        ·", h)

    # ── ② 运行时个人路径 ──
    print("\n[2] 运行时个人路径 (core/tools/config 的代码里)")
    path_hits = []
    for rel in tracked:
        rp = Path(rel)
        if not rel.endswith(".py") or not rp.parts or rp.parts[0] not in RUNTIME_DIRS:
            continue
        code = _code_only(ROOT / rel)
        _pp = _personal_patterns()
    if _pp is None:
        chk("黑名单文件存在 (真实或模板)", False,
            "缺了它这条检查会静默失效 —— 不许当通过")
        _pp = []
    for rx, label in (GENERIC_PERSONAL + _pp):
            for m in re.finditer(rx, code):
                ln = code[:m.start()].count("\n") + 1
                path_hits.append(f"{rel}:{ln} {label}")
    chk("★ 运行时代码无个人路径/账号字面量", not path_hits, path_hits[:6])
    if path_hits:
        for h in path_hits[:12]:
            print("        ·", h)

    # ── ③ requirements 覆盖 ──
    print("\n[3] requirements.txt 覆盖")
    req = ROOT / "requirements.txt"
    chk("requirements.txt 存在", req.exists())
    if req.exists():
        txt = req.read_text(encoding="utf-8")
        listed = {m.group(1).lower().replace("_", "-")
                  for m in re.finditer(r"^([A-Za-z0-9_.\-]+)\s*==", txt, re.M)}
        pkg_map = packages_distributions()
        missing, unmapped = [], {}
        for m, files in sorted(_third_party_imports().items()):
            dists = pkg_map.get(m)
            if not dists:
                unmapped[m] = sorted(files)[:2]
                continue
            if not any(d.lower().replace("_", "-") in listed for d in dists):
                missing.append(f"{m} (发行版 {dists}) ← {sorted(files)[:2]}")
        chk(f"★ 所有第三方 import 都在 requirements 里 ({len(_third_party_imports())} 个模块)",
            not missing, missing[:6])
        if missing:
            for m in missing[:10]:
                print("        ·", m)
        if unmapped:
            warn(f"有 {len(unmapped)} 个 import 映射不到发行版 (需人工判断)", list(unmapped)[:6])

    # ── ④ PROJECT_ROOT 不依赖 cwd ──
    print("\n[4] PROJECT_ROOT 不依赖工作目录")
    st = ROOT / "config" / "settings.py"
    chk("config/settings.py 存在", st.exists())
    if st.exists():
        s = st.read_text(encoding="utf-8", errors="replace")
        line = next((l.strip() for l in s.splitlines() if "PROJECT_ROOT" in l and "=" in l), "")
        chk("★ PROJECT_ROOT 不用 os.getcwd() 兜底", "getcwd()" not in line, line[:120])
        chk("PROJECT_ROOT 可由环境变量覆盖", "MARY3_ROOT" in s, "没有覆盖口")

    # ── ⑤ 软检查 ──
    print("\n[5] 软检查 (WARN, 不判红)")
    boundary = ROOT / "docs" / "data_boundary.md"
    if boundary.exists():
        print("  PASS 数据边界清单存在 (docs/data_boundary.md)")
        P.append("数据边界清单存在")
    else:
        warn("缺数据边界清单 docs/data_boundary.md —— 分不出哪些数据不该出门")
    dev_hits = 0
    for rel in tracked:
        if not rel.endswith(".py") or Path(rel).parts[0] in RUNTIME_DIRS:
            continue
        code = _code_only(ROOT / rel)
        if any(re.search(rx, code, re.I) for rx, _lb in _pp):
            dev_hits += 1
    if dev_hits:
        warn(f"开发文件里有 {dev_hits} 个绝对路径 (tests/scripts, 不影响分发)")
    else:
        P.append("开发文件无硬编码绝对路径")

    # ── ⑥ 排除表不能把"项目在用的东西"排掉 (2026-09-20 加) ──
    print("\n[6] 分发包排除表 (排掉的里面有没有项目在用的代码)")
    _mdr = ROOT / "scripts" / "make_dist_repo.py"
    if not _mdr.exists():
        warn("没有 scripts/make_dist_repo.py, 跳过排除表检查")
    else:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("_mdr_gate", _mdr)
        _m = _ilu.module_from_spec(_spec)
        try:
            _spec.loader.exec_module(_m)
        except Exception as e:
            chk("make_dist_repo 可导入", False, f"{type(e).__name__}: {e}")
            _m = None
        if _m is not None:
            # 收集全仓 import 语句里的顶层模块名
            _imports = {}
            for _f in ROOT.rglob("*.py"):
                if any(x in _f.parts for x in ("backups", ".git", "__pycache__", "tmp", ".pytest_cache")):
                    continue
                _code = _code_only(_f)
                for _m2 in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", _code, re.M):
                    _imports.setdefault(_m2.group(1), set()).add(str(_f.relative_to(ROOT)))
            # 被排除的目录, 有几个真的是项目里 import 的顶层包?
            _bad = []
            for _d in sorted(_m.SKIP_DIRS):
                _p = ROOT / _d
                if not _p.is_dir():
                    continue
                _users = _imports.get(_d)
                if _users:
                    _bad.append(f"{_d}/ ← 被 {sorted(_users)[:2]} import")
            chk("★ 被排除的目录里没有被项目 import 的代码 (踩过: sandbox/ 就被误排)",
                not _bad, _bad)
            # 反向: 项目里 import 的顶层包, 目录必须在盘上 (缺失=静默能力缺失)
            _missing = []
            for _mod, _users in _imports.items():
                if _mod in ("core", "tools", "config", "utils", "api", "ui", "cli", "storage",
                            "gateway", "mcp", "tests", "scripts", "tmm_skills"):
                    if not (ROOT / _mod).exists():
                        _missing.append(_mod)
            chk("★ 引擎 import 的顶层包都存在", not _missing, _missing)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(W)} WARN" if W else ""))
    for f in F:
        print("  -", f)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
