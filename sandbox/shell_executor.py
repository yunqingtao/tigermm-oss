"""
Safe shell executor — shlex.split + risk blacklist + pipe chaining.
No shell=True injection vectors.
"""
import subprocess
import shlex
import logging
from typing import Dict, Any, Optional
from config.settings import SHELL_TIMEOUT, SHELL_BLACKLIST
import os
import re
import time

logger = logging.getLogger(__name__)


def _risk_check(cmd_str: str) -> Optional[str]:
    low = cmd_str.lower()
    for bad in SHELL_BLACKLIST:
        if bad in low:
            return "Risk blocked: " + bad
    return None


def run_sync(cmd_str: str, cwd: str = ".", timeout: int = None) -> Dict[str, Any]:
    """Execute shell command safely (shell=False, shlex.split)."""
    risk = _risk_check(cmd_str)
    if risk:
        return {"success": False, "err": risk, "out": ""}

    t = timeout or SHELL_TIMEOUT

    if '|' in cmd_str:
        # Pipe chaining
        parts_list = [[p.strip() for p in shlex.split(part)] for part in cmd_str.split('|')]
        procs = []
        prev_stdout = None
        for i, pargs in enumerate(parts_list):
            kw = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True}
            if i > 0:
                kw['stdin'] = prev_stdout
            if cwd:
                kw['cwd'] = cwd
            proc = subprocess.Popen(pargs, **kw, encoding='utf-8', errors='replace')
            if prev_stdout:
                prev_stdout.close()
            prev_stdout = proc.stdout
            procs.append(proc)
        last = procs[-1]
        try:
            stdout, stderr = last.communicate(timeout=t)
        except subprocess.TimeoutExpired:
            for p in procs: p.kill()
            return {"success": False, "err": "timeout", "out": ""}
        for p in procs[:-1]: p.wait()
        return {"success": last.returncode == 0, "code": last.returncode,
                "out": stdout or "", "err": stderr or ""}
    else:
        pargs = shlex.split(cmd_str)
        try:
            proc = subprocess.run(pargs, capture_output=True, text=True,
                                   timeout=t, cwd=cwd, shell=False,
                                   encoding='utf-8', errors='replace')
            return {"success": proc.returncode == 0, "code": proc.returncode,
                    "out": proc.stdout or "", "err": proc.stderr or ""}
        except subprocess.TimeoutExpired:
            return {"success": False, "err": f"timeout ({t}s)", "out": ""}
        except Exception as e:
            return {"success": False, "err": str(e), "out": ""}


def run_bg(cmd_str: str, cwd: str = "."):
    """Start background process (server, daemon). No output capture."""
    risk = _risk_check(cmd_str)
    if risk:
        logger.error("Blocked: %s", risk)
        return None
    pargs = shlex.split(cmd_str)
    return subprocess.Popen(pargs, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, cwd=cwd, shell=False)


async def run_async(cmd_str: str, cwd: str = ".", timeout: int = None) -> Dict[str, Any]:
    """Async shell execution via asyncio.to_thread."""
    import asyncio
    return await asyncio.to_thread(run_sync, cmd_str, cwd, timeout)
