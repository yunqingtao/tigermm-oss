# -*- coding: utf-8 -*-
r"""本机 git 仓库只读状态 —— 回答「你改了什么 / 有什么没提交 / 最近的提交」。

为什么要有这个文件 (2026-09-25 只读勘查抓到的真缺口)
═══════════════════════════════════════════════════════════════════
勘查两面都做了, 结论与**最初的猜测不同**, 记在这里免得后人重走:

  ① 工具层**不缺**: `tools/shell_exec.py` 已存在、可见、在 SAFE_PLUGINS 里,
     实测 `git status --short` / `git log --oneline -3` 都被放行且真跑出结果。
     ⇒ 所以本工具**不是**为了"能跑 git"。
  ② 缺口在**路由**: 10 条真实说法 (含字面 `git status` / `你改了什么` /
     `有什么没提交的` / `最近的改动`) **全部空路由** ⇒ 掉兜底(模型闲聊)。
     根因: `ACTION_INTENTS` 里 git/repo/commit/提交/仓库/status **零命中**。

★ 那为什么不直接把这些话接到 shell_exec, 而要单独一个工具?
  ① **注入面**: shell_exec 收的是**命令字符串**。要把自然话变成命令, 就得由路由层
     拼串 —— 用户原话一旦进到命令里, "只读"就靠不住了。
     本工具反过来: **命令是写死的常量, 用户输入一个字都进不去** (见 `_GIT_VERBS`)。
  ② **输出洁癖**: 本仓 `git status --short` 一次吐 **700+ 行** (728 项未提交),
     直接甩给用户 = 一屏垃圾。本工具只回**概览 + 各桶抽样 + "还有 N 项"**。
  ③ 只读边界要能**自证**: 本工具没有任何写动作 (commit/add/push/checkout 全不在动词表里),
     `verify_repo_status.py` 拿"跑完 HEAD 没变 + 工作树没变"当判据。

★ 实测口径 (2026-09-25):
    本仓的 git 仓库根在**项目目录的上一级** (项目目录只是它的一个子目录), 所以
    git 报的路径形如 `<项目名>/xxx`。别把"本工具根目录"当成"仓库根",
    两种都显示 (见 `_head_line`), 免得用户看路径看糊涂。

    ★★ 这段**故意不写具体盘符/机器路径** —— 本文件会随分发包出门。
       第一版我在这里写了"实测: 仓库根是 D:\<开发机路径>"当口径, 结果被
       `verify_no_personal_data` 在**包里**抓到 3 条命中 (工作树 + 历史),
       逼着重建了一次包。**写实现说明时别把开发机路径抄进交付文件。**

★ 铁律:
  ① **只读** —— 动词表只有 status/diff/log/rev-parse 四个, 且不接受任何用户传入的命令
     或额外参数 (传了也忽略, 并在结果里说明)。
  ② **不猜** —— 不是 git 仓库 / 没装 git ⇒ 如实报, 不许编一个像样的答案。
  ③ **输出干净** —— 无调试行、无 [local X.Xs]、无引擎前缀。

★ 元数据: 只写 `TOOL` 即可 —— `core/tool_schema.py:141` 是
  `getattr(mod,"TOOL") or getattr(mod,"PLUGIN")`, TOOL 优先。
"""
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent          # 本工具所属项目根 (本文件在 tools/ 下)

#: ★ 只读动词表 —— 全部是**常量**, 用户输入不参与拼接。
#: 想加动词先问: 它会不会改动工作树/历史/远端? 会 ⇒ 加进来就等于把这个工具变成写工具。
_GIT_VERBS = {
    "status":        ["status", "--porcelain=v1", "-b"],
    "diff":          ["diff", "--stat", "--no-color"],
    "diff_cached":   ["diff", "--cached", "--stat", "--no-color"],
    "log_template":  ["log", "--oneline", "--no-color"],
    "count":         ["rev-list", "--count", "HEAD"],
    "branch":        ["rev-parse", "--abbrev-ref", "HEAD"],
    "toplevel":      ["rev-parse", "--show-toplevel"],
    # ★ 2026-09-25 加 (补掉"谁改的 README"这个空路由): 文件级改动历史。
    #   `--follow` 会跨改名追同一个文件; `--` 之后才是路径 —— 路径要**单独一个 argv**,
    #   且已经过 `_resolve_file` 校验 (仓库内、非选项形态), 不经 shell ⇒ 不是注入面。
    "log_file":      ["log", "--follow", "--oneline", "--no-color"],
    "ls_files":      ["ls-files"],
    # ★★ 为什么还要"未跟踪"那一半 (2026-09-25 实测): 本仓 565 个**已跟踪**文件里
    #   找不到 `scripts/hermes_verify.py` —— 它**未被跟踪**(715 项未提交里的一员)。
    #   只查已跟踪 ⇒ 用户问"谁改的 hermes_verify.py"会得到"找不到这个文件", 而文件就在那儿。
    #   两半合起来才是"仓库里有哪些文件", 且 git 自己会尊重 .gitignore (比 rglob 扫盘准且快)。
    "ls_untracked":  ["ls-files", "--others", "--exclude-standard"],
}

_MAX_LIST = 8          # status/diff 最多列几条细节
_MAX_PER_BUCKET = 3    # 每个桶最多抽样几条 (保证样本能反映构成)
_MAX_LOG = 20          # log 上限

_DESC = ("本机 git 仓库只读状态 —— 工作树有没有未提交改动、改了哪些文件、"
         "最近的提交、当前分支、某个文件的改动历史 (谁改的)。只读, 不会 commit/add/push。")

#: ★★ 2026-09-25 (真引擎实测抓到的第二个真缺陷, 比第一个更隐蔽):
#:   `TOOL.keywords` 会被 **plugin.keyword**(优先级 500) 拿去匹配, 而那条路是
#:   **把命中的关键词从原话里删掉**再当 `query` 传给工具:
#:       实测 "看一下最近的提交" 命中关键词 "最近的提交" ⇒ 工具收到 query="看一下的"
#:   ⇒ 工具既拿不到 action(那条路不传), 也认不出被删空的原话 ⇒ 只能回默认 status。
#:   修法: **关键词表只留"默认动作就是正确答案"的那批**(问改动/未提交);
#:   需要特定动作的说法(提交记录/改动详情)交给 **IR 链** —— 那边
#:   `ACTION_INTENTS` 会把 `action=log/diff` 一起带上(build_chain 明确传 action)。
#:   判据: 关键词里**一个"需要非默认动作"的说法都不许有**。
_KEYWORDS = ["未提交", "没提交", "工作树", "仓库状态", "git状态", "git 状态", "git status",
             "你改了什么", "改了啥", "改了哪些文件", "动了哪些文件", "有什么改动",
             "最近的改动", "工作区干净"]

TOOL = {
    "name": "repo_status",
    "description": _DESC,
    "keywords": _KEYWORDS,
    "parameters": {
        "action": {"type": "string",
                   "description": "status (默认) 改动概览 · diff 改动量统计 · log 最近提交 · branch 当前分支",
                   "required": False},
        "limit": {"type": "integer", "description": "log / file_history 取几条 (默认 5, 上限 20)", "required": False},
        "file": {"type": "string",
                 "description": "file_history 用: 要查的文件 (仓库内相对路径或只要文件名, 如 README)",
                 "required": False},
        "path": {"type": "string",
                 "description": "可选: 另一个 git 仓库目录 (默认就是本项目自己的仓库)",
                 "required": False},
    },
}


def _resolve_repo(path: str) -> tuple[Path | None, str]:
    """校验 path → (仓库目录, 错误)。空 path = 本项目的根。**只收目录, 不收命令。**"""
    if not path:
        return ROOT, ""
    p = Path(str(path))
    if not p.is_absolute():
        return None, "path 得是绝对路径: %r" % path
    if not p.is_dir():
        return None, "这个目录不存在: %s" % p
    if not (p / ".git").exists():
        return None, "这个目录不是 git 仓库 (没有 .git): %s" % p
    return p, ""


def _git(verb_key: str, repo: Path, extra: list | None = None) -> tuple[bool, str, str]:
    """跑一条**固定** git 命令。→ (ok, stdout, err)

    绝不接受外部传入的命令字符串 —— 参数只有动词表的 key 与**已校验**的附加项。
    """
    args = ["git"] + list(_GIT_VERBS[verb_key]) + list(extra or [])
    try:
        p = subprocess.run(args, cwd=str(repo), capture_output=True, timeout=20,
                           shell=False)                       # ← 不经 shell, 无注入面
    except FileNotFoundError:
        return False, "", "没找到 git 可执行文件 (PATH 里没有)"
    except subprocess.TimeoutExpired:
        return False, "", "git 超时 (20s)"
    out = (p.stdout or b"").decode("utf-8", errors="replace")
    err = (p.stderr or b"").decode("utf-8", errors="replace")
    if p.returncode != 0:
        return False, out, (err.strip() or ("git 退出码 %d" % p.returncode))
    return True, out, err


def _head_line(repo: Path) -> str:
    ok, out, _ = _git("toplevel", repo)
    top = out.strip() if ok and out.strip() else str(repo)
    ok2, out2, _ = _git("branch", repo)
    br = out2.strip() if ok2 and out2.strip() else "(未知)"
    if Path(top) == repo:
        return "仓库: %s · 分支 %s" % (top, br)
    return "仓库根: %s (本次看的是子目录 %s) · 分支 %s" % (top, repo.name, br)


def _act_status(repo: Path) -> dict:
    ok, out, err = _git("status", repo)
    if not ok:
        return {"success": False, "error": "读不到仓库状态: %s" % err}
    lines = [l for l in out.splitlines() if l.strip()]
    branch_note = ""
    if lines and lines[0].startswith("##"):
        branch_note = lines[0][2:].strip()
        lines = lines[1:]
    buckets = {"改": [], "新": [], "删": [], "改名": [], "其他": []}
    for l in lines:
        code, name = l[:2], l[3:].strip()
        _c = code.strip()
        if _c in ("??", "A", "AM"):
            # A = 已 add 进暂存区的新文件 —— 原来漏了它, 落到"其他"里 (实测 2 条 SKILL.md)
            buckets["新"].append(name)
        elif "D" in code:
            buckets["删"].append(name)
        elif "R" in code:
            buckets["改名"].append(name)
        elif _c in ("M", "MM", "T"):
            buckets["改"].append(name)
        else:
            buckets["其他"].append(name)
    total = len(lines)
    head = _head_line(repo)
    # 只在 -b 真带来 ahead/behind 之类信息时才补一句 —— 裸分支名与上一行重复
    if branch_note and ("ahead" in branch_note or "behind" in branch_note):
        head += "  [%s]" % branch_note
    if total == 0:
        return {"success": True, "output": head + "\n工作树干净: 没有未提交的改动。"}
    parts = ["未提交 %d 项" % total]
    for k in ("改", "新", "删", "改名", "其他"):
        if buckets[k]:
            parts.append("%s %d" % (k, len(buckets[k])))
    txt = [head, "工作树不干净: " + " · ".join(parts)]
    # 各桶轮流抽样 —— 只抽"改"会让人以为全是改动 (本仓实际 297 新 / 331 删)
    shown = 0
    for i in range(_MAX_PER_BUCKET):
        if shown >= _MAX_LIST:
            break
        for k in ("改", "新", "删", "改名", "其他"):
            if shown >= _MAX_LIST or i >= len(buckets[k]):
                continue
            txt.append("  [%s] %s" % (k, buckets[k][i]))
            shown += 1
    if total > shown:
        txt.append("  …还有 %d 项没列 (要看全就在终端跑 git status)" % (total - shown))
    return {"success": True, "output": "\n".join(txt)}


def _stat_lines(out: str) -> tuple[list, str]:
    lines = [l for l in out.splitlines() if l.strip()]
    files = [l for l in lines if "|" in l]
    summary = ""
    for l in reversed(lines):
        if "changed" in l or "insertion" in l or "deletion" in l:
            summary = l.strip()
            break
    return files, summary


def _act_diff(repo: Path) -> dict:
    ok, out, err = _git("diff", repo)
    if not ok:
        return {"success": False, "error": "读不到改动统计: %s" % err}
    files, summary = _stat_lines(out)
    staged = False
    if not files:
        ok2, out2, _ = _git("diff_cached", repo)
        if ok2:
            files, summary = _stat_lines(out2)
            staged = bool(files)
        if not files:
            return {"success": True,
                    "output": _head_line(repo) +
                              "\n工作树没有已跟踪文件的改动 (未跟踪的新文件见 action=status)。"}
    txt = [_head_line(repo),
           "已跟踪文件的改动: %d 个文件%s" % (len(files), " (已暂存)" if staged else "")]
    for l in files[:6]:
        txt.append("  " + l.strip())
    if len(files) > 6:
        txt.append("  …还有 %d 个文件" % (len(files) - 6))
    if summary:
        txt.append("合计: " + summary)
    return {"success": True, "output": "\n".join(txt)}


def _act_log(repo: Path, limit: int) -> dict:
    try:
        n = max(1, min(int(limit or 5), _MAX_LOG))
    except (TypeError, ValueError):
        n = 5
    ok, out, err = _git("log_template", repo, ["-n", str(n)])
    if not ok:
        return {"success": False, "error": "读不到提交历史: %s" % err}
    lines = [l for l in out.splitlines() if l.strip()]
    if not lines:
        return {"success": True, "output": "这个仓库还没有提交。"}
    okc, outc, _ = _git("count", repo)
    _total = outc.strip() if okc and outc.strip() else ""
    txt = ["最近 %d 次提交%s · %s" %
           (len(lines), (" (一共 %s 次)" % _total) if _total else "", _head_line(repo))]
    for l in lines:
        txt.append("  " + l.strip())
    return {"success": True, "output": "\n".join(txt)}


#: ★★ 2026-09-25 加 file_history: 补掉「谁改的 X」这条**一直问不着**的说法。
#:   只读勘查实测两种坏法 (都比"空路由"更隐蔽):
#:     ① `谁改的 README` (没扩展名) ⇒ **空路由** ⇒ 掉兜底闲聊;
#:     ② `谁改的 core/intent_router.py` (带路径/扩展名) ⇒ 被 **file_read 抢走** ⇒
#:        系统**把文件内容读出来**当答案 —— 用户问"谁改的", 收到的是文件正文 (答非所问)。
#:   安全边界与 status/log 同一套: 命令是常量, 路径经 `_resolve_file` 校验后**单独一个 argv**, 不经 shell。

def _resolve_file(repo: Path, raw: str) -> tuple[str, str, list]:
    """把用户说的文件名解析成**仓库内相对路径**。→ (相对路径, 错误, 候选列表)

    边界 (都为了不让"文件"变成注入面或猜谜):
      · 拒 `-` 开头 (会被 git 当选项)、拒绝对路径/盘符、拒 `..`;
      · 只认仓库里**有据可查**的文件 (已跟踪 / 磁盘上确实存在);
      · 名字对得上多个 ⇒ 把候选列出来让用户说准 (绝不挑一个"看起来像"的)。
    """
    s = str(raw or "").strip().strip('"\'`')
    if not s:
        return "", "要告诉我查哪个文件 (例: 谁改的 README)", []
    if s.startswith("-"):
        return "", "文件名不能以 - 开头: %r" % raw, []
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if s.startswith("/") or re.match(r"^[A-Za-z]:", s) or ".." in s.split("/"):
        return "", ("这个工具只看**本机 git 仓库**里的文件, 收的是仓库内相对路径; "
                     "你给的是 %r" % raw), []
    ok, out, _ = _git("ls_files", repo)
    tracked = [l.strip() for l in (out or "").splitlines() if l.strip()] if ok else []
    ok2, out2, _ = _git("ls_untracked", repo)      # 未跟踪的那一半 (见动词表注释)
    untracked = [l.strip() for l in (out2 or "").splitlines() if l.strip()] if ok2 else []
    low = s.lower()
    tset = set(tracked)
    cands = []
    for f in tracked + untracked:
        base = f.rsplit("/", 1)[-1]
        bl = base.lower()
        if f == s or base == s:
            rank = 0                            # 同名
        elif f.endswith("/" + s) or bl == low:
            rank = 1                            # 结尾对上 (用户给的是项目内路径)
        elif low in bl:
            rank = 2                            # 名字里含这个词
        elif low in f.lower():
            rank = 3                            # 只在路径里出现
        else:
            continue
        cands.append((f.count("/"), 0 if f in tset else 1, rank, len(f), f))
    if not cands:
        return "", "仓库里没有找到跟 %r 对得上的文件" % raw, []
    cands.sort()
    best = cands[0]
    same = [c for c in cands if c[:3] == best[:3]]      # 并列才算"说不准"
    if len(same) == 1:
        return best[4], "", []
    return "", "", [c[4] for c in same[:6]]


def _act_file_history(repo: Path, file: str, limit: int) -> dict:
    """某个文件的改动历史 (谁改的)。**只读**: log --follow。"""
    rel, err, cand = _resolve_file(repo, file)
    if err:
        return {"success": False, "error": err}
    if cand:
        return {"success": True,
                "output": ("仓库里有 %d 个文件跟这个名字对得上 —— 说得更准一点再问我:\n%s"
                           % (len(cand), "\n".join("  " + c for c in cand)))}
    ok, out, e = _git("log_file", repo, ["-n", str(max(1, min(int(limit or 5), _MAX_LOG))),
                                        "--", rel])
    if not ok:
        return {"success": False, "error": "读不到改动历史: %s" % e}
    lines = [l for l in (out or "").splitlines() if l.strip()]
    if not lines:
        return {"success": True,
                "output": "%s 没有提交记录 —— git 从来没记过它 (新文件 / 没被跟踪), "
                          "所以查不到改动历史 · %s" % (rel, _head_line(repo))}
    txt = ["%s 的改动历史 (最近 %d 次) · %s" % (rel, len(lines), _head_line(repo))]
    for l in lines:
        txt.append("  " + l.strip())
    return {"success": True, "output": "\n".join(txt)}


#: 从用户原话里推断动作 —— ★★ 为什么必须有这个 (2026-09-25 真引擎实测抓到的真缺陷):
#:   真引擎对这几句话走的是 **plugin.keyword**(优先级 500) 而不是 IR 链(300), 而那个处理体
#:   (`core/pipeline.py::_run_plugin_keyword`) **只传 query / city, 不传 action**:
#:       await self.gateway.plugin_mgr.call(name, query=query, city=city_q)
#:   ⇒ 工具回落默认值 ⇒ **动作被吞**: 实测 "看一下最近的提交" / "这个仓库有几个提交"
#:     回的都是 status(未提交清单), 而用户要的是提交记录。
#:   修在工具侧 (而不是改管线): 少了 action 就**按 query 自己认** —— 两条路都对,
#:   且不动任何既有路由的行为。
_ACTION_HINTS = (
    ("log",    ("提交记录", "提交历史", "最近的提交", "最近提交", "最近几次提交", "几次提交",
                 "几个提交", "提交日志", "commit", "一共几次", "一共几个")),
    ("diff",   ("改动详情", "改动统计", "diff", "改了多少行", "增删", "行数改")),
    ("branch", ("当前分支", "哪个分支", "什么分支")),
)


def _infer_action(query: str) -> str:
    """调用方没给 action 时, 从原话认。认不出 ⇒ status (与旧默认一致, 纯加性)。"""
    q = (query or "").lower()
    if not q:
        return "status"
    best, best_hit = "status", ""
    for act, hints in _ACTION_HINTS:
        for h in hints:
            if h in q and len(h) > len(best_hit):
                best, best_hit = act, h
    return best


async def run(action: str = "", limit: int = 5, path: str = "", query: str = "",
              file: str = "", **kwargs) -> dict:
    """只读仓库状态。action: status(默认) / diff / log / branch / file_history

    `query` = 调用方传进来的原话 (plugin.keyword 那条路会传); 不给 action 时用它认动作。
    `file`  = file_history 用 (某个文件的改动历史 —— 谁改的)。
    """
    # ★ 只读保证: 任何外来命令/参数一律忽略 —— 本工具的命令只有 _GIT_VERBS 里的常量。
    ignored = [k for k in kwargs if k not in ("action", "limit", "path", "query", "file")]
    act = (action or "").strip().lower()
    if act in ("repo", "changes", "改动", "状态"):
        act = "status"
    if not act:                                  # 没给动作 → 按原话认
        act = _infer_action(query)
    repo, verr = _resolve_repo(path)
    if repo is None:
        return {"success": False, "error": verr}
    try:
        if act == "status":
            r = _act_status(repo)
        elif act in ("diff", "diffstat", "改动明细"):
            r = _act_diff(repo)
        elif act in ("log", "history", "提交"):
            r = _act_log(repo, limit)
        elif act in ("branch", "分支"):
            r = {"success": True, "output": "当前分支: " + _head_line(repo)}
        elif act in ("file_history", "filelog", "谁改的", "改动历史"):
            r = _act_file_history(repo, file, limit)
        else:
            return {"success": False,
                    "error": "不认识的动作 %r —— 可用: status / diff / log / branch / file_history"
                             % action}
    except Exception as e:                                   # noqa: BLE001
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e)}
    if ignored:
        r = dict(r)
        r["note"] = "忽略了不支持的参数 %s (本工具只读, 不接受自定义命令)" % ",".join(ignored)
    return r
