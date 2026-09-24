"""
CodeEngine — Tiger.M.M 代码工程引擎
====================================
Think → Write → Syntax Check → Execute → Verify → Fix (loop up to N rounds)

Provides:
  1. Sandboxed execution — write to code_pool/, run, capture output
  2. Syntax verification — py_compile before execution
  3. Test running — pytest on generated code
  4. Verify-fix loop — feed errors back for retry
  5. Output capture — stdout, stderr, exit code, timing
"""

import os, sys, time, subprocess, traceback, shutil, logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("core.code_engine")

CODE_POOL = Path(__file__).parent.parent / "data" / "code_pool"
MAX_OUTPUT = 8000
DEFAULT_TIMEOUT = 30


@dataclass
class CodeResult:
    """Result of a code execution attempt."""
    file_path: str = ""
    success: bool = False
    syntax_ok: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0
    error: str = ""
    round_num: int = 0
    scan_report: str = ""   # 静态安全扫描报告(见 core/static_scan.py)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "syntax_ok": self.syntax_ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:2000],
            "stderr": self.stderr[:1000],
            "elapsed": round(self.elapsed, 2),
            "error": self.error[:500],
            "round": self.round_num,
            "scan_report": self.scan_report[:500],
        }

    @property
    def feedback(self) -> str:
        """Feedback for the model to fix the code."""
        if self.syntax_ok and self.success and not self.scan_report:
            return ""
        parts = []
        if not self.syntax_ok:
            parts.append(f"SYNTAX ERROR:\n{self.stderr[:1500]}")
        elif self.stderr:
            parts.append(f"STDERR:\n{self.stderr[:1500]}")
        if self.stdout and not self.success:
            parts.append(f"STDOUT:\n{self.stdout[:1000]}")
        if self.error:
            parts.append(f"ERROR: {self.error[:500]}")
        if self.syntax_ok and self.scan_report:
            parts.append(f"STATIC SCAN:\n{self.scan_report[:1200]}")
        return "\n\n".join(parts)


class CodeEngine:
    """Code execution with verify-fix loop."""

    def __init__(self, max_rounds: int = 3, timeout: int = DEFAULT_TIMEOUT):
        self.max_rounds = max_rounds
        self.timeout = timeout
        CODE_POOL.mkdir(parents=True, exist_ok=True)
        self._session_dir: Optional[Path] = None

    def new_session(self, prefix: str = "run") -> Path:
        """Create isolated session directory under code_pool/."""
        ts = int(time.time())
        self._session_dir = CODE_POOL / f"{prefix}_{ts}"
        self._session_dir.mkdir(parents=True, exist_ok=True)
        return self._session_dir

    def write_code(self, filename: str, code: str) -> Path:
        """Write code to session dir. Returns file path."""
        if self._session_dir is None:
            self.new_session()
        fpath = self._session_dir / filename
        fpath.write_text(code, encoding="utf-8")
        logger.info("CodeEngine: wrote %s (%d chars)", fpath.name, len(code))
        return fpath

    def write_project(self, files: dict[str, str]) -> Path:
        """Write multiple files to session dir — multi-file project support.
        
        Args:
            files: {filename: code_content, ...} e.g. {"main.py": "...", "utils.py": "..."}
        Returns:
            Session directory Path.
        """
        if self._session_dir is None:
            self.new_session(prefix="proj")
        for fname, content in files.items():
            (self._session_dir / fname).write_text(content, encoding="utf-8")
            logger.info("CodeEngine: project file %s (%d chars)", fname, len(content))
        return self._session_dir

    def check_all_syntax(self) -> list[CodeResult]:
        """Check syntax of all .py files in session dir."""
        results = []
        if not self._session_dir:
            return results
        for fpath in sorted(self._session_dir.glob("*.py")):
            r = self.check_syntax(fpath)
            results.append(r)
        return results

    def run_entrypoint(self, entry: str = "main.py", args: list = None) -> CodeResult:
        """Execute the entry point file within the project directory context."""
        if not self._session_dir:
            return CodeResult(error="No session directory. Call write_project() first.")
        fpath = self._session_dir / entry
        if not fpath.exists():
            return CodeResult(error=f"Entrypoint not found: {entry}")
        return self.execute(fpath, args)

    def check_syntax(self, fpath: Path) -> CodeResult:
        """Run py_compile + 静态安全扫描 on a Python file."""
        result = CodeResult(file_path=str(fpath))
        try:
            import py_compile
            py_compile.compile(str(fpath), doraise=True)
            result.syntax_ok = True
        except py_compile.PyCompileError as e:
            result.syntax_ok = False
            result.stderr = str(e)
            result.error = f"Syntax error in {fpath.name}"
            return result
        # ── 静态安全扫描 (AST, 零依赖): 语法过 ≠ 安全过 ──
        try:
            from core.static_scan import scan_file, format_report
            _scan = scan_file(fpath)
            _report = format_report(_scan)
            if _report:
                result.scan_report = _report
                if not _scan.get("safe", True):
                    logger.warning("CodeEngine: static scan flagged %s (%s)",
                                   fpath.name, _scan.get("summary", ""))
        except Exception as e:
            logger.debug("static scan skipped: %s", e)
        return result

    def execute(self, fpath: Path, args: list = None) -> CodeResult:
        """Execute a Python file — prefer Docker sandbox, fallback to subprocess."""
        result = CodeResult(file_path=str(fpath))
        t0 = time.time()
        code = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        
        # Try Docker sandbox first
        try:
            from sandbox.docker_sandbox import DockerSandbox
            if DockerSandbox.is_available():
                dr = DockerSandbox.execute(code, timeout=self.timeout)
                result.exit_code = dr.get("returncode", 1)
                result.stdout = dr.get("stdout", "")
                result.stderr = dr.get("stderr", "")
                result.success = dr.get("success", False)
                if not result.success and not result.stderr:
                    result.error = dr.get("error", "")
                result.elapsed = time.time() - t0
                result.mode = "docker"
                return result
        except Exception as e:
            logger.debug(f"Docker execute failed, falling back: {e}")
        
        # Fallback: subprocess on host
        try:
            r = subprocess.run(
                [sys.executable, str(fpath)] + (args or []),
                capture_output=True, text=True,
                timeout=self.timeout,
                cwd=str(fpath.parent),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            result.exit_code = r.returncode
            result.stdout = r.stdout[:MAX_OUTPUT]
            result.stderr = r.stderr[:MAX_OUTPUT]
            result.success = r.returncode == 0
        except subprocess.TimeoutExpired:
            result.error = f"Timed out after {self.timeout}s"
            result.stderr = result.error
        except Exception as e:
            result.error = str(e)
            result.stderr = traceback.format_exc()[:MAX_OUTPUT]
        result.elapsed = time.time() - t0
        result.mode = "host"
        return result

    def run_tests(self, fpath: Path) -> CodeResult:
        """Run pytest on a file and return results."""
        result = CodeResult(file_path=str(fpath))
        t0 = time.time()
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", str(fpath), "-q", "--tb=short"],
                capture_output=True, text=True,
                timeout=self.timeout + 30,
                cwd=str(fpath.parent),
            )
            result.exit_code = r.returncode
            result.stdout = r.stdout[:MAX_OUTPUT]
            result.stderr = r.stderr[:MAX_OUTPUT]
            result.success = r.returncode == 0
        except subprocess.TimeoutExpired:
            result.error = "Tests timed out"
        except Exception as e:
            result.error = str(e)
            result.stderr = traceback.format_exc()[:MAX_OUTPUT]
        result.elapsed = time.time() - t0
        return result

    def run_pipeline(self, filename: str, code: str,
                     run_tests: bool = False, args: list = None) -> CodeResult:
        """Full pipeline: write -> syntax check -> execute -> (optional) test."""
        fpath = self.write_code(filename, code)
        syntax_result = self.check_syntax(fpath)
        if not syntax_result.syntax_ok:
            syntax_result.round_num = 0
            return syntax_result
        exec_result = self.execute(fpath, args)
        exec_result.syntax_ok = True
        exec_result.round_num = 0
        if run_tests:
            test_result = self.run_tests(fpath)
            test_result.syntax_ok = True
            test_result.round_num = 0
            return test_result
        return exec_result

    def run_project(self, files: dict[str, str], entry: str = "main.py",
                    args: list = None, run_tests: bool = False) -> CodeResult:
        """Multi-file project pipeline: write all → check all syntax → run entrypoint.
        
        Args:
            files: {filename: code_content} — all project files
            entry: which file to execute (default: main.py)
            args: CLI args for entrypoint
            run_tests: run pytest after execution
        Returns:
            CodeResult with aggregated info.
        """
        # 1. Write all files
        self.write_project(files)
        
        # 2. Check all syntax
        syntax_results = self.check_all_syntax()
        failed = [r for r in syntax_results if not r.syntax_ok]
        if failed:
            err = failed[0]
            err.round_num = 0
            return err
        
        # 3. Execute entrypoint
        result = self.run_entrypoint(entry, args)
        result.syntax_ok = True
        result.round_num = 0
        
        # 4. Optional tests
        if run_tests:
            test_result = self.run_tests(self._session_dir / entry)
            test_result.syntax_ok = True
            test_result.round_num = 0
            return test_result
        
        result.file_count = len(files)
        return result

    async def verify_fix_loop(self, filename: str, code: str,
                              fix_callback, run_tests: bool = False,
                              args: list = None) -> CodeResult:
        """Execute, on failure call fix_callback(code, feedback) -> new_code, retry."""
        current_code = code
        last_result = None
        for round_num in range(self.max_rounds):
            result = self.run_pipeline(filename, current_code,
                                       run_tests=run_tests and round_num == self.max_rounds - 1,
                                       args=args)
            result.round_num = round_num + 1
            if result.success:
                logger.info("CodeEngine: round %d/%d PASSED (%.2fs)",
                           round_num + 1, self.max_rounds, result.elapsed)
                return result
            feedback = result.feedback
            if not feedback:
                feedback = f"Exit code {result.exit_code}. Output: {result.stdout[:500]}"
            logger.info("CodeEngine: round %d/%d FAILED", round_num + 1, self.max_rounds)
            if last_result and last_result.stderr == result.stderr and last_result.exit_code == result.exit_code:
                logger.warning("CodeEngine: stuck - same error twice")
                result.error = f"Stuck: same error for 2 rounds. {result.error}"
                return result
            last_result = result
            try:
                current_code = await fix_callback(current_code, feedback)
            except Exception as e:
                result.error = f"Fix callback failed: {e}"
                return result
        if last_result:
            last_result.error = f"Failed after {self.max_rounds} rounds. {last_result.error}"
        return last_result or CodeResult(error=f"Failed after {self.max_rounds} rounds")

    def cleanup(self, keep_last: int = 5):
        """Remove old session dirs, keep most recent N."""
        if not CODE_POOL.exists():
            return
        dirs = sorted([d for d in CODE_POOL.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[keep_last:]:
            try:
                shutil.rmtree(d)
                logger.info("CodeEngine: cleaned up %s", d.name)
            except Exception:
                pass
