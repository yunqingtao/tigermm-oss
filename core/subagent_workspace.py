# -*- coding: utf-8 -*-
"""子 agent 工作区隔离 —— 补「第五节第 2 条」的 0 分项 (2026-09-26 立)

为什么有
═══════════════════════════════════════════════════════════════
TMM 的 `SubAgent` 从前只有**工具白名单, 没有目录隔离** —— 两个并行子 agent 写同一个
路径就互相覆盖。这不是理论风险, 是**已经栽过**的一次 (file_ops write 覆盖掉另一个
agent 刚写的改动)。对照: Claude Code / Muse Code 给每个子 agent 一个独立 worktree,
**隔离不成就拒绝执行**, 绝不静默退回共享目录。

做法 (三条硬规矩, 照命令沙箱的同一套口径)
───────────────────────────────────────────────────────────────
1. 每个子 agent 一个**自己的工作区**: `tmp/subagent_ws/<名字>_<短id>/`
2. 写入类路径参数**必须落在自己的工作区里**; 出去就**拒绝**, 并把原话告诉 agent
   (让它改路径重试) —— **不许替它改写路径偷偷跑**。
3. 工作区建不出来 (磁盘/权限) ⇒ **拒绝执行这个子 agent**, 不退回共享目录。

与命令沙箱的关系
───────────────────────────────────────────────────────────────
沙箱管的是"能跑什么命令 / 在哪个目录跑"; 本模块管的是"**写哪个文件**"。
两者都在**工具调用**这一层拦, 但一个是 shell 一个是文件参数, 互不替代:
shell 被拦了还能用 file_ops 写错地方, 所以两个都要有。

分级 (为什么不是一刀切拦住所有路径参数)
───────────────────────────────────────────────────────────────
`to` / `city` 这类参数在别的工具里**不是路径** (发邮件收件人、天气城市)。
所以只对**名字像路径**且**值长得像路径** (带盘符/斜杠) 的参数判, 其余放行 ——
判据写在 `_looks_like_path()` 里, 门禁有反向用例 (确认它不误伤收件人)。
"""
import os
import re
import shutil
import time
import uuid
from pathlib import Path

#: 参数名像"路径"的 (只有这些才做检查)
_PATHISH_KEYS = (
    "path", "file", "filepath", "file_path", "filename", "dest", "dest_path",
    "destination", "out", "out_path", "output", "output_path", "save_to",
    "src", "src_path", "source", "source_path", "dir", "directory", "folder",
)
#: 明确**不是**路径的 (反例, 防误伤: 发件人/城市/关键词)
_NOT_PATH_KEYS = ("to", "cc", "bcc", "city", "keyword", "query", "subject",
                  "name", "text", "content", "body", "title")

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_ABS = re.compile(r"^(?:\\\\|/[^/]|[A-Za-z]:[\\/])")


def _looks_like_path(v) -> bool:
    """值长得像路径吗 —— 只认**绝对路径**形态 (相对路径留给调用方按 cwd 解释)。"""
    if not isinstance(v, str) or not v.strip():
        return False
    s = v.strip().strip('"\'“”')
    return bool(_ABS.match(s))


class SubAgentWorkspace:
    """一个子 agent 的工作区: 目录 + 写路径的放行判据。"""

    def __init__(self, name: str, root: Path | None = None, create: bool = True):
        self.name = re.sub(r"[^\w\u4e00-\u9fa5.-]", "_", str(name or "agent"))[:24]
        self._root_base = Path(root) if root else None
        uid = uuid.uuid4().hex[:8]
        base = self._root_base or (Path(__file__).resolve().parent.parent / "tmp" / "subagent_ws")
        self.dir = (base / f"{self.name}_{uid}").resolve()
        self.created = False
        self.reason = ""
        self.translated = []      # 被按工作区解释的相对路径 (审计用, 不是静默改)
        if create:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                # 真写一个探针文件: mkdir 成功不等于可写 (权限/只读盘)
                probe = self.dir / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                self.created = True
            except Exception as e:                      # noqa: BLE001
                self.created, self.reason = False, f"{type(e).__name__}: {e}"

    # ────────── 判一个工具调用能不能过 ──────────
    def _is_path_key(self, k: str) -> bool:
        kl = str(k).lower()
        if kl in _NOT_PATH_KEYS:
            return False
        if kl in _PATHISH_KEYS:
            return True
        return any(t in kl for t in ("path", "file", "dir"))

    def guard(self, tool_name: str, kwargs: dict) -> tuple[bool, str, dict]:
        """返回 (放行?, 拒绝原因, 可能被**按工作区解释**过的 kwargs)。

        两种路径, 两种待遇 (★ 这个区分是第一版没做对、被门禁 E1 抓出来的):
          · **显式外部绝对路径** (带盘符的那种) ⇒ **拒绝**, 绝不改写成工作区路径。
            改写成工作区 = "静默换个地方写", 比拒绝更坏 (它会以为写进了那个位置)。
          · **裸相对名** (`报告.txt`) ⇒ 按**工作区**解释 (等于每个 agent 有自己的 cwd)。
            不是"偷偷改": 提示词里已经明说工作区在哪、相对名落在工作区里。
            第一版把相对名原样透传 ⇒ 落到**共享的项目目录** —— 那正是要治的碰撞,
            门禁 E1 当场判红。
        """
        if not self.created:
            return False, (f"子 agent [{self.name}] 的工作区建不出来 "
                           f"({self.reason}) —— 按规矩**拒绝执行**, 不退回共享目录。"), kwargs
        bad, translated = [], {}
        out = dict(kwargs or {})
        for k, v in (kwargs or {}).items():
            if not self._is_path_key(k):
                continue
            if not isinstance(v, str) or not v.strip():
                continue
            if _looks_like_path(v):
                if not self.contains(v):
                    bad.append(f"{k}={v}")
                continue
            # 相对名: 只处理"看起来就是个文件名/子路径"的 (排除纯说明性文本)
            raw = v.strip().strip('"\'“”')
            if re.search(r"[\\/]", raw) or re.search(r"\.[A-Za-z0-9]{1,6}$", raw):
                out[k] = str(self.path_for(raw))
                translated[k] = out[k]
        if bad:
            return False, (f"子 agent [{self.name}] 只许在自己的工作区里写文件。\n"
                           f"被拦: {', '.join(bad)}\n"
                           f"你的工作区: {self.dir}\n"
                           f"改到这个目录下再调一次 (外部路径一律不许写)。"), kwargs
        if translated:
            self.translated.append(translated)
        return True, "", out

    def contains(self, p) -> bool:
        try:
            s = str(p).strip().strip('"\'“”')
            rp = Path(s)
            if not rp.is_absolute():
                rp = (self.dir / rp)
            rp = rp.resolve()
            return rp == self.dir or self.dir in rp.parents
        except Exception:
            return False

    def path_for(self, filename: str) -> Path:
        """把相对名/文件名落到工作区里 (agent 的"cwd").

        保留子目录结构 (a/b.txt → ws/a/b.txt), 但**不许越出工作区** (../ 会被夹住)。
        """
        rel = Path(str(filename).replace("\\", "/").strip().strip('"\'“”'))
        rel = Path(*[x for x in rel.parts if x not in ("..", ".", "/", "", ":")])
        return self.dir / rel

    # ────────── 清理 ──────────
    def cleanup(self, keep: bool = True):
        if not keep and self.dir.exists():
            shutil.rmtree(self.dir, ignore_errors=True)


class ScopedGateway:
    """把真 gateway 包一层: 路径参数先过工作区判据。

    ★ 为什么用代理而不是改 ToolGateway: 出口闸门/凭据代管都挂在**唯一接缝**上,
      动它风险大; 工作区是"某个子 agent 的私事", 用代理包起来最干净 ——
      也不影响主 agent 的调用 (它没有工作区限制)。
    """

    def __init__(self, real, ws: SubAgentWorkspace):
        self._real = real
        self.ws = ws
        self.refused = []          # 被拦下的调用 (给门禁/审计看)

    def __getattr__(self, item):                  # 其余属性透传
        return getattr(self._real, item)

    async def call(self, tool_name: str, timeout: int = None, **kwargs) -> dict:
        ok, reason, kwargs = self.ws.guard(tool_name, kwargs)
        if not ok:
            self.refused.append({"tool": tool_name, "reason": reason, "ts": time.time()})
            return {"success": False, "error": reason, "output": reason,
                    "workspace_denied": True}
        return await self._real.call(tool_name, timeout=timeout, **kwargs)


def new_workspace(name: str, root=None) -> SubAgentWorkspace:
    return SubAgentWorkspace(name, root=root)


def workspace_enabled() -> bool:
    """总开关: 默认开 (加性 —— 从前压根没有隔离, 现在是"有隔离")。

    关掉它只有一个用途: 兼容老行为做对照。生产环境不该关。
    `TMM_SUBAGENT_WS=0` 关闭。
    """
    return os.environ.get("TMM_SUBAGENT_WS", "1") != "0"
