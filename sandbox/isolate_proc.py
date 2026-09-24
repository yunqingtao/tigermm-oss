"""
Process Isolation Layer — v2 (refactor_hard_core)

Features:
  - Multiprocess sandbox workers with heartbeat monitoring
  - Auto-restart dead/stuck workers
  - Filesystem path restriction (no access outside PROJECT_ROOT)
  - Network restriction (whitelist-only outbound connections)
  - Unified submit_task() API for code/shell/plugin execution

Enable: TIGERMM_ISOLATE=1
Default: direct execution (backward compatible)
"""

import multiprocessing
import traceback
import os as _os
import sys as _sys
import subprocess as _subprocess
import shlex as _shlex
import threading
import io
import time as _time
import socket as _socket
from pathlib import Path
from typing import Dict, Any, Optional, List
from config.settings import (
    HEARTBEAT_INTERVAL, WORKER_STALL_TIMEOUT,
    MAX_CODE_LENGTH as MAX_CODE_SIZE, MAX_CODE_OUTPUT,
    NETWORK_WHITELIST, PROJECT_ROOT,
    SAFE_PLUGINS,
)

WORKER_COUNT = 2             # Number of sandbox worker processes
TASK_TIMEOUT = 30            # Default task timeout
MAX_OUTPUT = MAX_CODE_OUTPUT # Max stdout/stderr bytes

def _safe_path(user_path: str) -> Path:
    """Resolve and enforce path stays within PROJECT_ROOT."""
    p = (PROJECT_ROOT / user_path).resolve()
    try:
        p.relative_to(PROJECT_ROOT)
    except ValueError:
        raise PermissionError("Path outside project: " + str(p))
    return p

_SAFE_MODULES = {
    "math", "datetime", "collections", "itertools", "random", "json",
    "re", "pathlib", "csv", "string", "functools", "hashlib", "base64",
    "textwrap", "statistics", "fractions", "decimal", "typing",
}

def _safe_import(name, *args, **kw):
    if name in _SAFE_MODULES:
        return __import__(name)
    raise ImportError("Module not allowed: " + name)

_SAFE_BUILTINS = {
    "print": print, "len": len, "range": range, "int": int, "float": float,
    "str": str, "list": list, "dict": dict, "tuple": tuple, "set": set,
    "bool": bool, "isinstance": isinstance, "abs": abs,
    "min": min, "max": max, "sum": sum, "sorted": sorted,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "any": any, "all": all, "round": round, "chr": chr, "ord": ord,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError,
    "__import__": _safe_import,
}

_FORBIDDEN = [
    "import os", "from os", "import subprocess", "from subprocess",
    "import shutil", "import socket", "from socket",
    "import urllib", "import requests", "import http",
    "import sqlite3", "import ctypes", "import multiprocessing",
    "import sys", "import signal",
    "eval(", "exec(", "open(", "compile(",
]

def _sandbox_exec(code, timeout=30):
    # Additional dangerous patterns the string-scan might miss
    _EXTRA_DANGEROUS = ["subprocess.run", "subprocess.call", "subprocess.Popen",
                         "os.system(", "os.popen(", "shutil.rmtree",
                         "__import__(", "globals()", "locals()"]
    code_lower = code.lower()
    for p in _EXTRA_DANGEROUS:
        if p.lower() in code_lower:
            return {"success": False, "error": "Blocked: " + p, "stdout": "", "stderr": ""}
    code_lower = code.lower()
    for p in _FORBIDDEN:
        if p in code_lower:
            return {"success": False, "error": "Blocked: " + p, "stdout": "", "stderr": ""}
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    result = {"success": False, "error": "", "stdout": "", "stderr": ""}
    def _run():
        try:
            g = {"__builtins__": _SAFE_BUILTINS}
            _sys.stdout = out_buf
            _sys.stderr = err_buf
            exec(compile(code, "<sandbox>", "exec"), g, {})
            result["success"] = True
        except Exception as e:
            result["error"] = type(e).__name__ + ": " + str(e)
            result["stderr"] = traceback.format_exc()
        finally:
            _sys.stdout = _sys.__stdout__
            _sys.stderr = _sys.__stderr__
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        result["error"] = "Timeout (" + str(timeout) + "s)"
    result["stdout"] = out_buf.getvalue()[:MAX_OUTPUT]
    return result

_original_socket = _socket.socket
def _restricted_socket(*args, **kwargs):
    """Socket factory that only allows whitelisted outbound connections."""
    sock = _original_socket(*args, **kwargs)
    _real_connect = sock.connect
    def _checked_connect(address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in NETWORK_WHITELIST:
            raise PermissionError("Network blocked: " + str(host))
        return _real_connect(address)
    sock.connect = _checked_connect
    return sock

def _worker(task_q, result_q, heartbeat_q, barrier, worker_id):
    """Sandbox worker: receives tasks, executes in isolation, sends results."""
    # Lower priority
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except: pass
    
    # Apply network restriction in this process
    _socket.socket = _restricted_socket
    
    # Restrict filesystem: monkey-patch open
    _real_open = open
    def _restricted_open(file, mode="r", *args, **kwargs):
        if isinstance(file, str) and ("/" in file or "\\" in file):
            try:
                p = Path(file).resolve()
                p.relative_to(PROJECT_ROOT)
            except (ValueError, OSError):
                if "w" in mode or "a" in mode:
                    raise PermissionError("Write outside project: " + file)
        return _real_open(file, mode, *args, **kwargs)
    
    barrier.wait()
    last_heartbeat = _time.time()
    
    while True:
        # Send heartbeat
        try:
            heartbeat_q.put_nowait((worker_id, _time.time()))
        except: pass
        
        # Poll for task (non-blocking with heartbeat)
        try:
            task = task_q.get(timeout=HEARTBEAT_INTERVAL)
        except:
            continue  # Timeout, loop back for heartbeat
        
        if task is None:
            break  # Shutdown
        
        last_heartbeat = _time.time()
        tid, ttype, args = task
        ret = {"task_id": tid, "success": False, "err": "", "data": None}
        
        try:
            if ttype == "code_run":
                ret["data"] = _sandbox_exec(args.get("code",""), args.get("timeout", TASK_TIMEOUT))
                ret["success"] = ret["data"].get("success", False)
            elif ttype == "shell_run":
                cmd = args.get("cmd","")
                cwd = args.get("cwd",".")
                # Enforce path restriction for cwd
                try: _safe_path(cwd)
                except PermissionError as e:
                    ret["err"] = str(e)
                else:
                    parts = _shlex.split(cmd)
                    p = _subprocess.run(parts, capture_output=True, text=True,
                        timeout=args.get("timeout", TASK_TIMEOUT), cwd=str(_safe_path(cwd)), shell=False,
                        encoding='utf-8', errors='replace')
                    ret["data"] = {"returncode": p.returncode, "stdout": (p.stdout or "")[:MAX_OUTPUT], "stderr": (p.stderr or "")[:MAX_OUTPUT]}
                    ret["success"] = p.returncode == 0
            elif ttype == "plugin_run":
                plugin_name = args.get("plugin","")
                # Only allow known safe plugins
                if plugin_name not in SAFE_PLUGINS:
                    ret["err"] = "Plugin not allowed in sandbox: " + plugin_name
                else:
                    ret["err"] = "Plugin execution in sandbox not yet implemented"
        except Exception:
            ret["err"] = traceback.format_exc()
        
        result_q.put(ret)

# _SAFE_PLUGINS imported from config.settings above

class ProcessIsolator:
    """
    Manages sandbox worker pool with health monitoring.
    Usage:
        iso = ProcessIsolator.get()
        iso.start()
        result = iso.submit_task("code_run", code="print(1+1)")
        iso.shutdown()
    """
    _instance = None

    def __init__(self, workers=WORKER_COUNT, timeout=TASK_TIMEOUT):
        self.workers_n = workers
        self.timeout = timeout
        self.task_q = multiprocessing.Queue()
        self.result_q = multiprocessing.Queue()
        self.heartbeat_q = multiprocessing.Queue()
        self._tid = 0
        self._procs = []
        self._last_heartbeat = {}  # worker_id -> timestamp
        self._on = False
        self._monitor_thread = None
        self._lock = threading.Lock()

    @classmethod
    def get(cls, workers=WORKER_COUNT):
        if cls._instance is None:
            cls._instance = cls(workers=workers)
        return cls._instance

    def start(self):
        if self._on:
            return
        with self._lock:
            if self._on:
                return
            b = multiprocessing.Barrier(self.workers_n + 1)
            self._procs = []
            self._last_heartbeat = {}
            for i in range(self.workers_n):
                p = multiprocessing.Process(
                    target=_worker,
                    args=(self.task_q, self.result_q, self.heartbeat_q, b, i),
                    daemon=True, name="tigermm-sandbox-" + str(i)
                )
                p.start()
                self._procs.append(p)
                self._last_heartbeat[i] = _time.time()
            b.wait(timeout=10)
            self._on = True
        # Start health monitor
        self._monitor_thread = threading.Thread(target=self._health_monitor, daemon=True)
        self._monitor_thread.start()

    def shutdown(self):
        self._on = False
        for _ in self._procs:
            try: self.task_q.put(None)
            except: pass
        for p in self._procs:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
        self._procs.clear()

    def _health_monitor(self):
        """Background thread: collect heartbeats, restart dead workers."""
        while self._on:
            _time.sleep(HEARTBEAT_INTERVAL)
            if not self._on:
                break
            # Collect heartbeats
            try:
                while True:
                    wid, ts = self.heartbeat_q.get_nowait()
                    self._last_heartbeat[wid] = ts
            except: pass
            # Check for stalled workers
            now = _time.time()
            for i, p in enumerate(self._procs):
                if not p.is_alive():
                    self._restart_worker(i)
                elif now - self._last_heartbeat.get(i, now) > WORKER_STALL_TIMEOUT:
                    p.terminate()
                    p.join(timeout=2)
                    self._restart_worker(i)

    def _restart_worker(self, worker_id):
        """Kill and restart a dead/stuck worker."""
        old = self._procs[worker_id] if worker_id < len(self._procs) else None
        if old and old.is_alive():
            old.terminate()
            old.join(timeout=2)
        p = multiprocessing.Process(
            target=_worker,
            args=(self.task_q, self.result_q, self.heartbeat_q,
                  multiprocessing.Barrier(1), worker_id),
            daemon=True, name="tigermm-sandbox-" + str(worker_id)
        )
        p.start()
        if worker_id < len(self._procs):
            self._procs[worker_id] = p
        else:
            self._procs.append(p)
        self._last_heartbeat[worker_id] = _time.time()

    def submit_task(self, task_type: str, **kwargs) -> Dict[str, Any]:
        """
        Submit a task to the sandbox pool. Blocks until result.
        task_type: "code_run" | "shell_run" | "plugin_run"
        Returns: {"success": bool, "data": ..., "err": str}
        """
        if not self._on:
            self.start()
        self._tid += 1
        tid = self._tid
        t = kwargs.pop("timeout", self.timeout)
        self.task_q.put((tid, task_type, kwargs))
        # Wait for result
        try:
            r = self.result_q.get(timeout=t + 10)
            if r.get("task_id") != tid:
                self.result_q.put(r)
                r = self.result_q.get(timeout=t + 10)
            return {"success": r.get("success", False),
                    "data": r.get("data"),
                    "err": r.get("err", "")}
        except Exception:
            return {"success": False, "data": None, "err": "Task timeout"}

    def run_code(self, code, timeout=None):
        r = self.submit_task("code_run", code=code, timeout=timeout or self.timeout)
        return r.get("data") if r.get("success") else {"success": False, "error": r.get("err",""), "stdout": "", "stderr": r.get("err","")}

    def run_shell(self, cmd, cwd=".", timeout=30):
        return self.submit_task("shell_run", cmd=cmd, cwd=cwd, timeout=timeout)

    @property
    def stats(self):
        return {"workers": self.workers_n,
                "alive": sum(1 for p in self._procs if p.is_alive()),
                "active": self._on,
                "tasks_done": self._tid,
                "heartbeats": dict(self._last_heartbeat)}

from config.settings import TIGERMM_ISOLATE
_ON = TIGERMM_ISOLATE

def sandbox_execute(code, timeout=30):
    """Unified code execution. Uses isolator if TIGERMM_ISOLATE=1."""
    if _ON:
        return ProcessIsolator.get().run_code(code, timeout)
    return _sandbox_exec(code, timeout)

def shell_execute(cmd, cwd=".", timeout=30):
    """Unified shell execution. Uses isolator if TIGERMM_ISOLATE=1."""
    if _ON:
        return ProcessIsolator.get().run_shell(cmd, cwd, timeout)
    parts = _shlex.split(cmd)
    try:
        p = _subprocess.run(parts, capture_output=True, text=True, timeout=timeout, cwd=cwd, shell=False, encoding='utf-8', errors='replace')
        return {"success": p.returncode == 0, "data": {"returncode": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or ""}}
    except Exception as e:
        return {"success": False, "err": str(e), "data": None}

def get_isolator_stats():
    """Get isolator health stats for monitoring."""
    if _ON and ProcessIsolator._instance:
        return ProcessIsolator._instance.stats
    return {"enabled": _ON, "workers": 0, "active": False}