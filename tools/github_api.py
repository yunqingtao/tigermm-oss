"""
GitHub REST — 第一方工具 (2026-09-20 批次3)
========================================================================
不用 gh CLI (本机没装), 直接打 GitHub REST API —— 只依赖标准库 urllib。
token 读取顺序: 环境变量 GH_TOKEN → keys_github.json → keys.json["github"]
  · keys_github.json 已在 .gitignore 的 keys*.json 规则内 (**不进 git**)
凭据只在本机读取, 任何输出/日志里都不回显 token。

action:
  whoami        我是谁 (验 token 是否有效)
  repos         我的仓库列表 (按最近更新)
  issues        待办 issue (跨仓库汇总, 或指定 repo)
  pulls         待办 PR
  create_issue  开 issue (需 repo/title, 可选 body)  ← 唯一的写操作
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

PLUGIN = {
    "name": "github_api",
    "description": "GitHub REST: 查仓库 / 待办 issue / 待办 PR / 开 issue。需要 token (GH_TOKEN 或 keys_github.json)。",
    "version": "1.0",
    "trigger": ["github", "仓库", "issue", "pull request"],
    "permission": ["system", "network"],
    "category": "dev",
}

API = "https://api.github.com"
TIMEOUT = 25


# ── 凭据 ────────────────────────────────────────────────────────────
def _token() -> str:
    t = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if t:
        return t
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel, *path in (("keys_github.json", "github", "key"),
                       ("keys_github.json", "github", "token"),
                       ("keys.json", "github", "key")):
        p = os.path.join(root, rel)
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            for k in path:
                d = d[k] if isinstance(d, dict) else {}
            if isinstance(d, str) and d.strip():
                return d.strip()
        except Exception:
            continue
    return ""


def _call(method: str, path: str, token: str, body: dict | None = None):
    """→ (status, data | error_text)

    ★ 2026-09-22: 出网到 api.github.com 会**间歇性 TLS 握手超时**
    (`URLError: _ssl.c:989: The handshake operation timed out`; 实测配额 5000/5000 未耗尽,
    不是限流) —— 抖动多为瞬时, 所以网络层失败重试 3 次再认输。
    只重试**网络层**失败 (status 0); HTTP 错误码照原样返回 (那是服务端给了回答)。
    """
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last = (0, "")
    for _try in range(3):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "TigerMM/1.0")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            return e.code, detail or str(e)          # 服务端有回答 → 不重试
        except Exception as e:
            last = (0, f"{type(e).__name__}: {e}")
            if _try < 2:
                time.sleep(1.5)
    return last


def _brief_issue(it: dict) -> str:
    labels = ",".join(l.get("name", "") for l in (it.get("labels") or [])) or "-"
    return (f"#{it.get('number')} [{it.get('state')}] {str(it.get('title'))[:70]} "
            f"| 标签:{labels[:30]} | 更新:{str(it.get('updated_at'))[:10]} "
            f"| {it.get('comments', 0)} 评论")


# ── 主入口 ──────────────────────────────────────────────────────────
async def run(action: str = "", repo: str = "", state: str = "open",
              title: str = "", body: str = "", limit: int = 10, **kwargs) -> dict:
    tok = _token()
    if not tok:
        return {"success": False,
                "output": "没找到 GitHub token。请设置环境变量 GH_TOKEN，"
                          "或把 token 写进 keys_github.json（该文件已在 .gitignore 内，不会进 git）。",
                "error": "no github token configured"}
    try:
        limit = max(1, min(int(limit or 10), 50))
    except Exception:
        limit = 10
    action = (action or "whoami").strip()

    # ① 我是谁
    if action == "whoami":
        st, d = _call("GET", "/user", tok)
        if st != 200 or not isinstance(d, dict):
            return {"success": False, "output": f"token 验真失败 (HTTP {st}): {d}", "error": str(d)}
        return {"success": True, "user": d.get("login"), "name": d.get("name"),
                "repos": (d.get("public_repos") or 0) + (d.get("total_private_repos") or 0),
                "output": f"GitHub 身份: {d.get('login')} · 仓库 {(d.get('public_repos') or 0)+ (d.get('total_private_repos') or 0)} 个 · 额度 5000/h"}

    # ② 仓库列表
    if action in ("repos", "repo_list", "repositories"):
        st, d = _call("GET", f"/user/repos?sort=updated&per_page={limit}", tok)
        if st != 200 or not isinstance(d, list):
            return {"success": False, "output": f"取仓库列表失败 (HTTP {st}): {d}", "error": str(d)}
        lines = [f"{r.get('full_name')} [{ '私有' if r.get('private') else '公开'}] "
                 f"★{r.get('stargazers_count', 0)} 更新:{str(r.get('updated_at'))[:10]}"
                 for r in d]
        return {"success": True, "repos": [r.get("full_name") for r in d],
                "count": len(d),
                "output": f"共 {len(d)} 个仓库 (按最近更新):\n" + "\n".join("  " + l for l in lines)}

    # ③④ issue / PR 待办
    if action in ("issues", "pulls", "prs", "pr"):
        want_pr = action in ("pulls", "prs", "pr")
        scope = [repo] if repo else None
        if not scope:
            st, d = _call("GET", f"/user/repos?sort=updated&per_page={limit}", tok)
            if st != 200 or not isinstance(d, list):
                return {"success": False, "output": f"取仓库列表失败 (HTTP {st}): {d}", "error": str(d)}
            scope = [r.get("full_name") for r in d]
        rows, errs = [], []
        for full in scope[:limit]:
            path = (f"/repos/{full}/pulls?state={state}&per_page=20" if want_pr
                    else f"/repos/{full}/issues?state={state}&per_page=20")
            st, d = _call("GET", path, tok)
            if st != 200 or not isinstance(d, list):
                errs.append(f"{full}: HTTP {st} {str(d)[:60]}")
                continue
            for it in d:
                # /issues 会连带返回 PR —— 查 issue 时剔除 (PR 自带 pull_request 字段)
                if not want_pr and it.get("pull_request"):
                    continue
                rows.append(f"[{full}] " + _brief_issue(it))
        kind = "PR" if want_pr else "issue"
        if not rows and errs:
            return {"success": False, "output": "查询失败: " + "; ".join(errs[:3]), "error": errs[:3]}
        head = f"待办 {kind} ({state}): {len(rows)} 条" + (f" · 覆盖 {len(scope)} 个仓库" if len(scope) > 1 else "")
        if errs:
            head += f" · {len(errs)} 个仓库查不动"
        return {"success": True, "items": rows, "count": len(rows), "scope": scope,
                "output": head + ("\n" + "\n".join("  " + r for r in rows[:30]) if rows else "\n  (空)")}

    # ⑤ 开 issue (写操作: 必须显式给 repo + title)
    if action in ("create_issue", "new_issue"):
        if not repo or not title:
            return {"success": False,
                    "output": "开 issue 需要仓库 (owner/repo) 和标题。例: 开 issue 到 yunqingtao/x 标题:「登录失败」",
                    "error": "missing repo/title"}
        if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
            return {"success": False, "output": f"仓库名格式不对: {repo} (应为 owner/repo)", "error": "bad repo"}
        st, d = _call("POST", f"/repos/{repo}/issues", tok,
                      {"title": title, "body": body or ""})
        if st not in (200, 201) or not isinstance(d, dict):
            return {"success": False, "output": f"开 issue 失败 (HTTP {st}): {d}", "error": str(d)}
        return {"success": True, "url": d.get("html_url"), "number": d.get("number"),
                "output": f"已开 issue #{d.get('number')}: {d.get('html_url')}"}

    # ⑤ 巡检 (aggregate): 身份 + 仓库 + 待办 issue + 待办 PR —— 一次调用给全,
    #    静态 DAG 只能固定动作, 所以"我的 github 有什么动静"用这一步就够。
    if action in ("digest", "overview", "巡检"):
        st, me = _call("GET", "/user", tok)
        if st != 200 or not isinstance(me, dict):
            return {"success": False, "output": f"token 验真失败 (HTTP {st}): {me}", "error": str(me)}
        st, rps = _call("GET", f"/user/repos?sort=updated&per_page={limit}", tok)
        if st != 200 or not isinstance(rps, list):
            return {"success": False, "output": f"取仓库列表失败 (HTTP {st}): {rps}", "error": str(rps)}
        parts = [f"身份: {me.get('login')} · 仓库 {len(rps)} 个 (按最近更新)"]
        for r in rps:
            parts.append(f"  {r.get('full_name')} [{'私有' if r.get('private') else '公开'}]"
                         f" ★{r.get('stargazers_count', 0)} 更新:{str(r.get('updated_at'))[:10]}")
        errs = []
        for r in rps[:limit]:
            full = r.get("full_name")
            for kind, path in (("issue", f"/repos/{full}/issues?state=open&per_page=20"),
                               ("PR", f"/repos/{full}/pulls?state=open&per_page=20")):
                st, d = _call("GET", path, tok)
                if st != 200 or not isinstance(d, list):
                    errs.append(f"{full}/{kind}: HTTP {st}")
                    continue
                items = [it for it in d if (kind == "PR") or not it.get("pull_request")]
                if not items:
                    continue
                parts.append(f"  {full} 待办 {kind} {len(items)} 条:")
                parts += ["    " + _brief_issue(it) for it in items[:10]]
        if errs:
            parts.append(f"  (有 {len(errs)} 个查询没成功: {', '.join(errs[:3])})")
        return {"success": True, "user": me.get("login"),
                "repos": [r.get("full_name") for r in rps],
                "output": "\n".join(parts)}

    return {"success": False, "output": f"未知操作: {action}", "error": f"unknown action {action}"}
