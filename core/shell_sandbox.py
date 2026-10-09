# -*- coding: utf-8 -*-
"""命令沙箱 (shell sandbox) —— 抄 Codex CLI 那件真正值钱的东西: **两个独立旋钮**。

Codex 的模型: 审批策略 (什么时候问你) 与**操作系统级沙箱** (真能干什么) 是两件事,
各管一头, 关掉一个另一个还在。TMM 以前只有前一个 (而且 shell_exec 干脆整条封死),
没有后一个 —— 结果是要么全禁 (能力没了), 要么放开了裸跑 (风险全担)。

本模块补上后一个旋钮的**本机版**。诚实的差距先说清: Codex 用的是 OS 强制
(内核/sandbox 机制), 本机这版是**策略级**的 (工作目录牢笼 + 环境擦洗 + 命令形状黑名单
+ 超时 + 输出上限), 挡不住有心的恶意代码 —— 它挡的是**常见的意外与顺手为之**。
所以它**不能**替代审批: 审批仍然要在 (见 egress_guard: shell 一律要批)。

三条硬规矩
───────────────────────────────────────────────────────────────
1. **管不住就不跑** (Codex 原文: refuses to run if it cannot enforce that boundary)。
   请求的工作目录不在允许的根里 → **拒绝**, 绝不"悄悄换个目录跑"。
2. **环境擦洗**: 子进程继承父进程的环境变量 —— 而父进程环境里有 API key
   (DASHSCOPE_API_KEY / 各家的 KEY)。原来的裸 subprocess 等于把钥匙塞进每个命令手里。
   本模块只放行一份**基线白名单**, 其余尤其"名字像凭据的"一律不传, 并**记下被挡的名字**
   (只记名字, 不记值)。
3. **逐段复核 + 形状黑名单**: 复合命令按 `&& || ; |` 拆段逐段判 (与 egress_guard 同一份规则),
   任一段命中危险形状 → 整条拒。

对外接口
───────────────────────────────────────────────────────────────
    get_sandbox()                      -> ShellSandbox
    sb.check(command, cwd=None)        -> {ok, reason, cwd_resolved, dropped_env:[名字]}
    sb.run(command, cwd=None, timeout=30) -> {success, output, rc, elapsed, sandbox:{...}}
    sb.allowed_roots() / sb.stats()
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("core.shell_sandbox")

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 20000

#: 子进程环境**只放行**这些 (其余全丢)。够跑常见的本地命令, 但足够少。
ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "OS",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "USERNAME", "COMPUTERNAME",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH",
    "PYTHONUTF8", "TZ", "TERM",
}

#: 名字像凭据的一律不传 (即便误伤也要误伤 —— 宁可命令报"缺环境变量", 不许漏钥匙)
SECRETISH = re.compile(r"(?i)(key|token|secret|password|passwd|credential|apikey|auth|bearer)")

#: 明确危险: 不靠"像不像", 靠形状 (与 egress_guard 同源, 这里自带一份免得循环依赖)
DENY_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*[rf]",
    r"\bdel\s+/[fsq]",
    r"\brd\s+/[sq]",
    r"\brmdir\s+/s",
    r"\bformat\s+[a-z]:",
    r"^\s*format\s*$",
    r"\bshutdown\b", r"\breboot\b", r"\blogoff\b",
    r"\breg\s+(add|delete|import|export)\b", r"\bregedit\b",
    r"\bdiskpart\b", r"\bchkdsk\b", r"\bmkfs", r"\bdd\s+if=",
    r"\bnet\s+(user|localgroup|share|session)",
    r"\bicacls\b", r"\bcacls\b", r"\btakeown\b",
    r"\btaskkill\b.*(/f|/im)", r"\bsc\s+delete\b", r"\bwmic\b.*delete",
    r"\bpowerShell\b.*(-enc|-e\s|Remove-Item\s+.*-Recurse)",
    r"\bgit\s+push\b.*--force", r"\bcurl\b.*\|\s*(ba)?sh", r"\bwget\b.*\|\s*(ba)?sh",
]
STAGE_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")

#: ★ 跨段的形状: 拆段之后 pipe 本身没了, 这些必须在**拆段之前**对整条命令判。
#   教训 (门禁当场抓到): `curl http://x | sh` 拆成 ["curl http://x", "sh"] 之后
#   每段都"干净" → 整条被放行。逐段复核是**补充**, 不是替代整体形状判断。
WHOLE_CMD_PATTERNS = [
    r"\|\s*(?:ba|z|k|c)?sh\b",          # 管道喂 shell
    r"\|\s*python[0-9.]*\b",             # 管道喂 python
    r"\|\s*(?:cmd|powershell|pwsh)\b",
    r"curl\b[^|]*\|\s*(?:ba|z|k|c)?sh",
    r"wget\b[^|]*\|\s*(?:ba|z|k|c)?sh",
]


class ShellSandbox:
    def __init__(self, roots=None, timeout=DEFAULT_TIMEOUT, max_output=MAX_OUTPUT_CHARS):
        self._roots = [Path(r) for r in (roots or self._default_roots())]
        self.timeout = int(timeout)
        self.max_output = int(max_output)
        self._runs = 0
        self._refused = 0

    @staticmethod
    def _default_roots():
        """允许在里面跑的根: 项目根 + 用户桌面临时区 + 系统临时区 (都不是全盘)。"""
        r = [ROOT, ROOT / "tmp"]
        try:
            r.append(Path(os.environ.get("TEMP") or os.environ.get("TMP") or (ROOT / "tmp")))
        except Exception:
            pass
        return [x for x in r if x]

    def allowed_roots(self):
        return [str(p) for p in self._roots]

    # ── 判定 ──────────────────────────────────────────────
    def _resolve_cwd(self, cwd=None):
        """工作目录解析。不在允许的根里 → (None, 理由)。铁律 1。"""
        raw = str(cwd or "").strip()
        cand = Path(raw) if raw else ROOT
        try:
            if not cand.is_absolute():
                cand = (ROOT / cand)
            cand = cand.resolve()
        except Exception as e:
            return None, f"工作目录解析失败: {e}"
        if not cand.exists() or not cand.is_dir():
            # 目录不存在不许"顺手建" —— 让人确认
            return None, f"工作目录不存在或不是目录: {cand}"
        for root in self._roots:
            try:
                rp = root.resolve()
            except Exception:
                continue
            if cand == rp or rp in cand.parents:
                return cand, ""
        return None, (f"工作目录不在允许的根里: {cand}（允许: {', '.join(self.allowed_roots())}）"
                      f" —— 拒绝执行, 不换目录偷跑")

    def _stages(self, command: str):
        return [s.strip() for s in STAGE_SPLIT.split(str(command or "")) if s.strip()]

    def _scan(self, command: str):
        # ① 先判跨段形状 (拆段会把管道拆没, 所以这步必须在拆段之前)
        raw = str(command or "")
        for pat in WHOLE_CMD_PATTERNS:
            if re.search(pat, raw, re.I):
                return f"整条命中危险形状 (管道喂解释器): {raw[:60]}"
        # ② 再逐段复核
        for i, st in enumerate(self._stages(command), 1):
            for pat in DENY_PATTERNS:
                if re.search(pat, st, re.I):
                    return f"第{i}段命中危险形状: {st[:60]}"
        return ""

    def _scrub_env(self):
        """只放行白名单既有变量; 返回 (env, 被挡掉的名字)。铁律 2。"""
        env, dropped = {}, []
        for k, v in os.environ.items():
            ku = k.upper()
            if ku in ENV_ALLOW:
                env[k] = v
                continue
            if SECRETISH.search(k):
                dropped.append(k)
        # 基线里若本身带凭据味道 (不该有), 也挡
        for k in list(env):
            if SECRETISH.search(k):
                dropped.append(k)
                env.pop(k, None)
        return env, sorted(set(dropped))

    def check(self, command, cwd=None):
        command = str(command or "").strip()
        if not command:
            return {"ok": False, "reason": "空命令"}
        cwd_resolved, why = self._resolve_cwd(cwd)
        if cwd_resolved is None:
            return {"ok": False, "reason": why}
        hit = self._scan(command)
        if hit:
            return {"ok": False, "reason": hit}
        _, dropped = self._scrub_env()
        return {"ok": True, "reason": "", "cwd_resolved": str(cwd_resolved),
                "dropped_env": dropped, "stages": self._stages(command)}

    # ── 执行 ──────────────────────────────────────────────
    def run(self, command, cwd=None, timeout=None):
        chk = self.check(command, cwd)
        if not chk.get("ok"):
            self._refused += 1
            return {"success": False, "output": "", "rc": None,
                    "error": f"沙箱拒绝: {chk.get('reason')}",
                    "sandbox": {"refused": True, "reason": chk.get("reason")}}
        env, dropped = self._scrub_env()
        t0 = time.time()
        self._runs += 1
        to = int(timeout or self.timeout)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        p = None
        try:
            # ★ 2026-09-26 修 (门禁当场抓到): 原来用 subprocess.run(..., timeout=)。
            #   在 Windows 上 shell=True 会先起 cmd.exe, 命令再是 cmd 的子进程。
            #   run() 超时时只 kill 掉 cmd, **孙进程还活着** ⇒ 它继续握着 stdout 管道
            #   ⇒ 内部 communicate() 等管道关闭 ⇒ 实测 timeout=1 的命令耗了 5.1 秒才返回
            #   ("超时"形同虚设, 命令照跑完)。改法: Popen + 进程组, 超时时用
            #   `taskkill /F /T` **连子孙一起杀**, 再兜底 p.kill(), 最后把管道抽干。
            p = subprocess.Popen(
                str(command), shell=True, cwd=chk["cwd_resolved"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, creationflags=flags,
            )
            out = (p.communicate(timeout=to)[0] or "").strip()
        except subprocess.TimeoutExpired:
            killed = False
            try:
                if os.name == "nt" and p is not None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, timeout=15)
                    killed = True
            except Exception:
                pass
            try:
                if p is not None:
                    p.kill()
            except Exception:
                pass
            try:
                if p is not None:
                    p.communicate(timeout=10)      # 抽干管道, 别再等
            except Exception:
                pass
            return {"success": False, "output": "", "rc": None,
                    "error": f"超时 {to}s 被终止 (含子孙进程)",
                    "sandbox": {"timeout": True, "killed_tree": killed, "dropped_env": dropped}}
        except Exception as e:
            return {"success": False, "output": "", "rc": None,
                    "error": f"执行失败: {type(e).__name__}: {e}",
                    "sandbox": {"dropped_env": dropped}}
        rc = p.returncode
        truncated = len(out) > self.max_output
        if truncated:
            out = (out[:self.max_output]
                   + f"\n…（输出超 {self.max_output} 字符已截断，尾部省略 {len(out) - self.max_output}）")
        return {
            "success": rc == 0,
            "output": out,
            "rc": rc,
            "elapsed": round(time.time() - t0, 2),
            "sandbox": {
                "cwd": chk["cwd_resolved"],
                "stages": len(chk["stages"]),
                "dropped_env": dropped,      # 只记名字
                "truncated": truncated,
                "enforced": "policy-level",  # ★ 诚实标注: 不是 OS 强制
            },
        }

    def stats(self):
        return {"runs": self._runs, "refused": self._refused,
                "roots": self.allowed_roots(), "enforcement": "policy-level (非 OS 强制)"}


_SB = None


def get_sandbox(**kw) -> ShellSandbox:
    global _SB
    if _SB is None:
        _SB = ShellSandbox(**kw)
    return _SB


def reload_sandbox(**kw):
    global _SB
    _SB = None
    return get_sandbox(**kw)
