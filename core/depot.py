"""Depot — 对外检索与**安全评估** (第 3 步: 出门找, 但只搬"形式"回来)。

═══════════════════════════════════════════════════════════════════════
定位 (承接第 1 步台账 / 第 2 步本地自救)
═══════════════════════════════════════════════════════════════════════
    第 1 步  /gap              看见缺什么
    第 2 步  /gap rescue       先在本地自救 (能造就造, 缺依赖给命令)
    第 3 步  /depot            ★ 只对"本地真试过、救不了"的缺口出门找
    第 4 步  /depot adopt      ★ 安全且适用 → 做成我们自己的 (人工可见的生效点)

★ 铁律 (用户明确要求 + TMM 一贯风格):
   ① **只读检索** —— 只发 GET, 只读元数据; 绝不执行候选的任何代码。
   ② **先测安全再操作** —— 没有 verdict=safe 的东西不 adopt。
   ③ **只搬形式, 自造实现** —— 外部素材按我们自己的风格重做; 别人的代码放"外挂位"引用,
      不并进我们仓库 (与"外部素材只取形式按自有风格重制"同一条规矩)。
   ④ **可追溯** —— 每个装上的东西记来源/许可证/时间/QC 证据, 随时能查能摘。

═══════════════════════════════════════════════════════════════════════
安全评估做什么 (全部静态, 不 import / 不执行 / 不进用户环境)
═══════════════════════════════════════════════════════════════════════
    A 许可证       白名单 (MIT/Apache/BSD/ISC/Unlicense) vs 灰区 (GPL/LGPL copyleft)
                   vs 红区 (无许可证/自定义/unknown) —— 没许可证 = 法律上不能用
    B 维护状态     最后发布/推送时间 · 是否 archived · star 数 (只作参考不作门槛)
    C 兼容性       requires_python 是否含本机 3.11 · 是否 yanked · dev status
    D ★ 包内容静态扫描  下载 wheel → 沙箱解压 → 扫源码:
                   - ★ `.pth` 文件 (解释器启动即执行 → 供应链投毒经典手法) = **阻断项**
                   - 执行外部命令 / os.system / eval+base64 / 网络 / 注册表 / ctypes
                   - 读凭据相关路径 (ssh/aws/keys/密码)
                   - setup.py 存在与否 (wheel 里不该有)
                   只统计与报点, **不判定"恶意"** —— 由人看。不执行任何东西。
    E 名称混淆    与知名包名近似 (编辑距离小) → 可能是抢注/投毒包, 重点提示

verdict 三档: safe (可 adopt) / caution (需人判断, 默认不 adopt) / reject (有阻断项)
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger("core.depot")

try:
    from config.settings import DATA_DIR
except Exception:
    DATA_DIR = Path(__file__).parent.parent / "data"

INSTALLED_FILE = "depot_installed.json"
UA = "TMM-Depot/1.0 (+local agent supply-chain check)"
HTTP_TIMEOUT = 25

# ── 许可证分档 ──
LICENSE_OK = {                       # 可自用/可引用 (宽松)
    "MIT", "MIT LICENSE", "APACHE-2.0", "APACHE 2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE",
    "BSD", "ISC", "UNLICENSE", "THE UNLICENSE", "PYTHON-2.0", "PSF-2.0", "0BSD", "CC0-1.0",
}
LICENSE_CAREFUL = {                  # copyleft: 引用/外挂可以, 并入我们仓库要谨慎
    "GPL-2.0", "GPL-3.0", "GPL-2.0-ONLY", "GPL-3.0-ONLY", "LGPL-2.1", "LGPL-3.0",
    "AGPL-3.0", "MPL-2.0", "EUPL-1.2", "NOASSERTION",
}
LICENSE_BAD = {"", "NONE", "UNKNOWN", "PROPRIETARY", None}

# ── ★ 阻断项: 命中即 reject (不 adopt) ──
BLOCKERS = {
    "pth_file": "wheel 里有 .pth 文件 —— 解释器启动时自动执行, 供应链投毒经典手法",
    "setup_py_in_wheel": "wheel 里带 setup.py —— 正常 wheel 不该有 (构建脚本混入)",
    "no_license": "没有许可证 —— 法律上不能拿来用",
}

# ── 风险点 (命中=caution 并列出, 由人看; 不是"恶意"判定) ──
RISK_PATTERNS = [
    ("exec_eval", re.compile(r"(?<![\w.])(exec|eval)\s*\(")),
    ("subprocess", re.compile(r"subprocess\.|os\.system\s*\(|os\.popen\s*\(|pty\.spawn")),
    ("network", re.compile(r"\brequests\.(get|post)|\burllib\.request|socket\.socket\(|"
                           r"\bhttpx\.|\baiohttp\.|http\.client")),
    ("winreg", re.compile(r"\bwinreg\b|\b_winreg\b|HKEY_LOCAL_MACHINE|HKEY_CURRENT_USER")),
    ("ctypes", re.compile(r"\bctypes\b|\bwindll\b|\bCDLL\b")),
    # ★ 两种顺序都要认 (实测漏洞: 经典写法 `exec(base64.b64decode(...))` 把 exec 写在**前面**,
    #   我第一版只认 "base64…decode…exec" 的顺序 → 反而漏掉最常见的那种)。
    ("base64_exec", re.compile(
        r"(?:(?:exec|eval|compile)\s*\([^\n]{0,80}base64[^\n]{0,40}(?:decode|b64decode))"
        r"|(?:base64[^\n]{0,40}(?:decode|b64decode)[^\n]{0,80}(?:exec|eval|compile)\s*\()")),
    ("credential_paths", re.compile(r"\.ssh[/\\]|\.aws[/\\]|id_rsa|credentials\.json|"
                                    r"\.netrc|password\s*=|api[_-]?key\s*=", re.I)),
    ("pickle_load", re.compile(r"pickle\.load|marshal\.load|dill\.load")),
    ("obfuscated_blob", re.compile(r"\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}"
                                   r"\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}")),
]

# ── 知名包名 (供名称混淆检测) ──
POPULAR = [
    "requests", "numpy", "pandas", "flask", "django", "urllib3", "certifi", "setuptools",
    "pip", "wheel", "boto3", "pyyaml", "cryptography", "pillow", "lxml", "scrapy",
    "openai", "anthropic", "httpx", "aiohttp", "pydantic", "sqlalchemy", "pytest",
]


# ═══════════════════════════════════════════════════════════════════
# 检索 (只读 HTTP)
# ═══════════════════════════════════════════════════════════════════

def _gh_token() -> str:
    """取 GitHub token (可选) —— 只为**提高限额**, 没有也能跑。

    为什么加 (2026-09-20 实测): depot 原来全是**未认证**请求, 而 GitHub 未认证限额
    只有 **60 次/小时**。今天两次门禁脆弱红都源于此 (取不到 SKILL.md 清单 → 提示行消失
    → 断言红; 或"读不到许可证"→ 假结论)。带上 token 后 5000 次/小时。
    凭据查找顺序与项目其他工具一致 (env → keys_github.json → keys.json); 没有就返回空
    (保持原来的匿名行为, **不引入新依赖也不强制配 key**)。
    """
    tok = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        return tok
    root = Path(__file__).parent.parent          # 本文件在 core/ 下 → 上一级是项目根
    for name in ("keys_github.json", "keys.json"):
        try:
            p = root / name
            if not p.is_file():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            g = d.get("github") if isinstance(d.get("github"), dict) else d
            t = (g or {}).get("key") or (g or {}).get("token") or ""
            if isinstance(t, str) and t.strip():
                return t.strip()
        except Exception:
            continue
    return ""


def _get(url: str, timeout: int = HTTP_TIMEOUT) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if "api.github.com" in url:
        _t = _gh_token()
        if _t:
            headers["Authorization"] = f"Bearer {_t}"   # 提限额; 失败时下面照常报错
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict:
    return json.loads(_get(url, timeout).decode("utf-8", "replace"))


def pypi_meta(name: str) -> dict | None:
    """查一个包在 PyPI 的元数据 (只读)。不存在返回 None。"""
    try:
        d = _get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    except Exception as e:
        logger.debug("pypi_meta %s failed: %s", name, e)
        return None
    i = d.get("info") or {}
    urls = d.get("urls") or []
    wheel = next((u for u in urls if u.get("packagetype") == "bdist_wheel"), None)
    lic = (i.get("license_expression") or i.get("license") or "").strip()
    if lic and len(lic) > 60:                      # 有些包把整段许可证塞进 license 字段
        lic = lic[:60]
    return {
        "source": "pypi",
        "name": i.get("name") or name,
        "version": i.get("version"),
        "license": lic,
        "requires_python": i.get("requires_python") or "",
        "requires_dist": list(i.get("requires_dist") or []),
        "classifiers": list(i.get("classifiers") or []),
        "project_urls": dict(i.get("project_urls") or {}),
        "yanked": bool(i.get("yanked")),
        "summary": (i.get("summary") or "")[:200],
        "releases": len(d.get("releases") or {}),
        "latest_upload": (urls[0].get("upload_time_iso_8601") if urls else None),
        "wheel_url": (wheel or {}).get("url"),
        "wheel_name": (wheel or {}).get("filename"),
        "wheel_size": (wheel or {}).get("size"),
    }


def search_github(query: str, limit: int = 8) -> list[dict]:
    """GitHub 仓库检索 (只读)。未认证限 10 次/分。失败返回 []。"""
    url = ("https://api.github.com/search/repositories?q="
           + urllib.parse.quote(query) + f"&sort=stars&per_page={int(limit)}")
    try:
        d = _get_json(url)
    except Exception as e:
        logger.debug("search_github failed: %s", e)
        return []
    out = []
    for it in d.get("items") or []:
        out.append({
            "source": "github",
            "repo": it.get("full_name"),
            "stars": it.get("stargazers_count"),
            "license": (it.get("license") or {}).get("spdx_id"),
            "archived": bool(it.get("archived")),
            "pushed_at": (it.get("pushed_at") or "")[:10],
            "language": it.get("language"),
            "description": (it.get("description") or "")[:160],
            "url": it.get("html_url"),
            "open_issues": it.get("open_issues_count"),
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# 纯函数: 名称混淆 / 时效 / 许可证分档
# ═══════════════════════════════════════════════════════════════════

def _lev(a: str, b: str) -> int:
    """编辑距离 (短字符串足够)。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def typosquat_suspects(name: str) -> list[str]:
    """包名与知名包高度近似 (编辑距离 1~2 且不相同) → 抢注/投毒嫌疑。"""
    n = (name or "").lower().replace("-", "").replace("_", "")
    hits = []
    for p in POPULAR:
        if n == p:
            continue
        d = _lev(n, p)
        if 1 <= d <= 2:
            hits.append(p)
    return hits


def days_since(iso: str, now: float = None) -> float | None:
    """ISO 时间 → 距今多少天 (失败 None)。"""
    if not iso:
        return None
    try:
        import datetime as _dt
        s = iso.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        ref = _dt.datetime.fromtimestamp(now or time.time(), tz=_dt.timezone.utc)
        return max(0.0, (ref - dt).total_seconds() / 86400.0)
    except Exception:
        return None


def license_class(lic: str) -> str:
    """许可证分档: ok / careful / bad。"""
    L = (lic or "").strip().upper()
    if not L or L in {x.upper() for x in LICENSE_BAD if x}:
        return "bad"
    for k in LICENSE_OK:
        if L == k.upper() or L.startswith(k.upper()):
            return "ok"
    for k in LICENSE_CAREFUL:
        if L == k.upper() or L.startswith(k.upper()) or k.upper() in L:
            return "careful"
    # 宽松关键词兜底 (许可证字段常写整段话)
    #   ★ 收尾不能只靠 \b —— 实测 "GPLv3" / "LGPL-2.1" 这类写法后面紧跟字母或数字,
    #     \bGPL\b 匹配不到 → 真 GPL 被误判成"没许可证"(bad)。所以再加 "GPLV?" 形态。
    if re.search(r"\bGNU GENERAL PUBLIC LICENSE\b|\bGPL[vV]?\d?\b|\bAGPL[vV]?\d?\b|"
                 r"\bLGPL[vV]?\d?\b|\bMPL[ -]?\d", L):
        return "careful"
    if re.search(r"\bMIT\b|\bAPACHE\b|\bBSD\b|\bISC\b|PUBLIC DOMAIN|"
                 r"\bGNU LESSER\b", L):
        return "ok"
    return "bad"


# ═══════════════════════════════════════════════════════════════════
# ★ 包内容静态扫描 (下载 → 沙箱解压 → 扫文本; 绝不 import / 执行)
# ═══════════════════════════════════════════════════════════════════

def download_wheel(url: str, dest_dir: Path) -> Path | None:
    """下载 wheel 到沙箱 (只读 HTTP, 不安装)。"""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        fn = Path(urllib.parse.urlparse(url).path).name or "pkg.whl"
        p = dest_dir / fn
        p.write_bytes(_get(url, timeout=60))
        return p
    except Exception as e:
        logger.debug("download_wheel failed: %s", e)
        return None


def scan_wheel(path: Path, max_files: int = 400) -> dict:
    """静态扫描 wheel 内容。**不 import、不执行**任何候选代码。

    返回 {ok, file_count, py_files, pth_files, setup_py, blockers[], risks[]}
    """
    out = {"ok": False, "file_count": 0, "py_files": 0, "pth_files": [],
           "setup_py": False, "blockers": [], "risks": [], "error": ""}
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        out["error"] = f"不是合法 zip/wheel: {type(e).__name__}"
        return out
    with zf:
        names = zf.namelist()
        out["ok"] = True
        out["file_count"] = len(names)
        out["pth_files"] = [n for n in names if n.lower().endswith(".pth")]
        out["setup_py"] = any(Path(n).name.lower() == "setup.py" for n in names)
        pys = [n for n in names if n.lower().endswith(".py")][:max_files]
        out["py_files"] = len([n for n in names if n.lower().endswith(".py")])
        seen = {}
        for n in pys:
            try:
                txt = zf.read(n).decode("utf-8", "replace")
            except Exception:
                continue
            # 去掉注释/文档串噪声 (粗粒度: 只按行首 # 过滤)
            body = "\n".join(l for l in txt.splitlines() if not l.lstrip().startswith("#"))
            for label, pat in RISK_PATTERNS:
                if pat.search(body):
                    seen.setdefault(label, []).append(n)
        out["risks"] = [{"kind": k, "files": v[:5], "count": len(v)}
                        for k, v in sorted(seen.items())]
    if out["pth_files"]:
        out["blockers"].append("pth_file")
    if out["setup_py"]:
        out["blockers"].append("setup_py_in_wheel")
    return out


# ═══════════════════════════════════════════════════════════════════
# ★ 风险点"可达性"分类 (纯函数)
# ═══════════════════════════════════════════════════════════════════
# 为什么需要: 静态扫描是**粗**的 —— 实测 ddgs 扫出 `subprocess`, 一看是在
#   `cli.py` 的 `serve --detach` 里**起它自己的 API 服务** (sys.executable -m uvicorn
#   ddgs.api_server), 而我们只用 `from ddgs import DDGS` —— 库路径根本不经过 cli.py。
# 若不加区分, 每个带 CLI 的包都会被一律记成"有风险", 闸门就变成了噪音 (人开始无视它)。
# 所以按**路径形态**分两类: 入口/工具路径 (库导入通常不经过) vs 库路径 (会经过)。
ENTRY_ONLY = re.compile(
    r"(^|/)(cli|__main__|main|_cli|console|gui)\.py$"
    r"|/(api_server|server|servers|scripts|tools|tests?|examples?|docs?|benchmarks?|"
    r"build_scripts|setup_tools)/", re.I)


def risk_reach(risks: list) -> dict:
    """把扫描到的风险点按路径分档。返回 {lib, entry_only}。

    lib        = 在库路径里 → 我们用 `import` 就会经过, 必须认真看
    entry_only = 只在 CLI/服务/测试/示例等入口路径 → 库导入通常不经过
    ★ 这是**启发式**, 不是证明: entry_only 仍需人扫一眼 (所以 verdict 不会因此判 safe)。
    """
    lib, entry = [], []
    for r in (risks or []):
        files = list(r.get("files") or [])
        entry_hit = [f for f in files if ENTRY_ONLY.search("/" + f.lstrip("/"))]
        lib_hit = [f for f in files if f not in entry_hit]
        if lib_hit:
            lib.append({**r, "files": lib_hit})
        if entry_hit:
            entry.append({**r, "files": entry_hit})
    return {"lib": lib, "entry_only": entry}


# ═══════════════════════════════════════════════════════════════════
# ★ 综合裁决 (纯函数 → 可离线测试)
# ═══════════════════════════════════════════════════════════════════

def verdict(ev: dict, py_version: str = "3.11", now: float = None) -> dict:
    """证据 → 裁决。ev 见 pypi_meta()/scan_wheel() 的合并结果。

    返回 {level: safe|caution|reject, score, reasons[], blockers[], risks[]}
    ★ 判定原则: **有阻断项就 reject**; 缺证据不算 safe (没扫到内容 → caution)。
    """
    reasons, blockers = [], []
    score = 0

    # ① 许可证
    lic = ev.get("license") or ""
    lc = license_class(lic)
    if lc == "bad":
        blockers.append("no_license")
        reasons.append(f"许可证不明/没有 ({lic or '空'}) —— 不能拿来用")
    elif lc == "careful":
        score -= 1
        reasons.append(f"许可证 {lic} 属 copyleft —— 外挂引用可以, 并入我们仓库要谨慎")
    else:
        score += 1
        reasons.append(f"许可证 {lic} (宽松)")

    # ② 阻断项
    for b in (ev.get("blockers") or []):
        blockers.append(b)
        reasons.append(BLOCKERS.get(b, b))

    # ③ 维护状态
    d = days_since(ev.get("latest_upload") or ev.get("pushed_at") or "", now)
    if d is not None:
        if d > 730:
            score -= 1
            reasons.append(f"最后发布 {d/365:.1f} 年前 —— 可能已停维护")
        elif d <= 180:
            score += 1
            reasons.append(f"最近 {d:.0f} 天内有发布 (活跃)")
    if ev.get("archived"):
        score -= 1
        reasons.append("仓库已 archived")

    # ④ 兼容性
    rp = ev.get("requires_python") or ""
    m = re.findall(r"(\d+)\.(\d+)", rp)
    if rp and m:
        cur = tuple(int(x) for x in py_version.split(".")[:2])
        low = tuple(int(x) for x in m[0])
        if cur < low:
            score -= 2
            reasons.append(f"要求 Python {rp}, 本机 {py_version} 不满足 —— 装不上")
        else:
            score += 1
            reasons.append(f"Python 要求 {rp} ⊇ 本机 {py_version}")
    if ev.get("yanked"):
        blockers.append("yanked")
        reasons.append("该版本已被 PyPI 撤回 (yanked)")

    # ⑤ 名称混淆
    sus = typosquat_suspects(ev.get("name") or ev.get("repo") or "")
    if sus:
        score -= 2
        reasons.append(f"包名与知名包近似: {sus} —— 抢注/投毒嫌疑, 重点核对")

    # ⑥ 风险点 (静态扫描) —— 按可达性分档
    reach = risk_reach(ev.get("risks") or [])
    lib_kinds = sorted({r["kind"] for r in reach["lib"]})
    ent_kinds = sorted({r["kind"] for r in reach["entry_only"]})
    reviewable = False
    if lib_kinds:
        score -= 1
        reasons.append("静态扫描命中**库路径**风险点: " + ", ".join(lib_kinds)
                       + " (import 会经过, 必须认真看)")
    if ent_kinds:
        score -= 1
        reasons.append("静态扫描命中**入口路径**风险点: " + ", ".join(ent_kinds)
                       + " (仅在 CLI/服务/测试入口, 库导入不经过)")
        if not lib_kinds:
            # ★ 只有入口路径风险 → 可人工核对后采用 (gate 不自动放行)
            reviewable = True
            reasons.append("→ 风险点全在入口路径, 库导入不经过; "
                           "**人工核对确认后可采用** (/depot adopt <包> --reviewed \"理由\")")

    # ⑦ 缺证据 (没扫内容)
    if not ev.get("scanned"):
        score -= 1
        reasons.append("**没扫包内容** (只看了元数据) —— 够不上 safe")

    if blockers:
        level = "reject"
    elif score >= 3:
        level = "safe"
    elif score >= 0:
        level = "caution"
    else:
        level = "reject"
    return {"level": level, "score": score, "reasons": reasons,
            "blockers": blockers, "risks": [r["kind"] for r in (ev.get("risks") or [])],
            "lib_risks": lib_kinds, "entry_only_risks": ent_kinds,
            "reviewable": bool(reviewable and level == "caution")}


def render_verdict(name: str, ev: dict, v: dict) -> str:
    """裁决 → 给人看的文本。"""
    icon = {"safe": "✓ 安全", "caution": "⚠ 需人判断", "reject": "✗ 不采用"}[v["level"]]
    lines = [f"{icon}  {name}  (评分 {v['score']})"]
    if ev.get("version"):
        lines.append(f"  版本 {ev['version']} · 许可证 {ev.get('license') or '无'}"
                     f" · 体积 {ev.get('wheel_size') or '?'}B")
    if ev.get("summary"):
        lines.append(f"  简介: {ev['summary']}")
    for r in v["reasons"]:
        lines.append(f"  · {r}")
    if v["blockers"]:
        lines.append(f"  ★ 阻断项: {', '.join(v['blockers'])} → 不 adopt")
    elif v["level"] == "safe":
        lines.append("  → 可 adopt (安全且够格)")
    elif v.get("reviewable"):
        lines.append("  → 可人工核对后采用: /depot adopt <包> --reviewed \"核对结论\"")
    else:
        lines.append("  → 不自动 adopt; 要上需人明确点头")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 已装清单 (可追溯: 来源/许可证/时间/QC 证据/决策)
# ═══════════════════════════════════════════════════════════════════

def _installed_path(data_dir: Path = None) -> Path:
    return Path(data_dir or DATA_DIR) / INSTALLED_FILE


def load_installed(data_dir: Path = None) -> dict:
    p = _installed_path(data_dir)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_installed(name: str, entry: dict, data_dir: Path = None) -> dict:
    """记一笔"装上的东西"。additive, 不覆盖历史 (改了就 append 一个事件)。"""
    p = _installed_path(data_dir)
    data = load_installed(data_dir)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rec = data.get(name) or {"name": name, "events": []}
    rec["events"].append({"at": now, **entry})
    rec.update({k: v for k, v in entry.items() if k in
                ("source", "license", "version", "kind", "url", "verdict")})
    rec["updated_at"] = now
    data[name] = rec
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def render_installed(data_dir: Path = None) -> str:
    data = load_installed(data_dir)
    if not data:
        return "[已装清单] 空 —— 还没从外面拿过东西。"
    lines = [f"[已装清单] {len(data)} 项 (来源·许可证·时间都可追溯)"]
    for n, r in sorted(data.items()):
        ev = (r.get("events") or [{}])[-1]
        lines.append(f"  · {n}  [{r.get('kind', '?')}] 许可证 {r.get('license') or '?'}"
                     f"  {ev.get('at', '')}  ({r.get('source') or '?'})")
    return "\n".join(lines)
