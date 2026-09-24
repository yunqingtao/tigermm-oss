"""
Docker sandbox — run Python code in an isolated container.
Falls back to CodeSandbox if Docker is unavailable.
"""
import io, time, subprocess, tempfile, os
from config.settings import SANDBOX_TIMEOUT

DOCKER_IMAGE = "python:3.11-slim"
DOCKER_TIMEOUT = SANDBOX_TIMEOUT + 5

class DockerSandbox:
    """Execute Python code in Docker container — true isolation."""
    
    @staticmethod
    def is_available():
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
            return r.returncode == 0
        except:
            return False
    
    @staticmethod
    def execute(code, timeout=30):
        """Run code in Docker, return {success, stdout, stderr, error}."""
        try:
            # Write code to temp file
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', delete=False, encoding='utf-8'
            ) as f:
                f.write(code)
                tmp_path = f.name
            
            try:
                r = subprocess.run([
                    "docker", "run", "--rm",
                    "--network", "none",       # no network access
                    "--memory", "256m",         # memory limit
                    "--cpus", "1",              # CPU limit
                    "--read-only",              # read-only rootfs (except /tmp)
                    "--tmpfs", "/tmp:noexec",
                    "-v", f"{tmp_path}:/code/code.py:ro",
                    DOCKER_IMAGE,
                    "timeout", str(timeout),
                    "python", "-u", "/code/code.py"
                ], capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
                
                result = {
                    "success": r.returncode == 0,
                    "stdout": r.stdout[:10000] if r.stdout else "",
                    "stderr": r.stderr[:5000] if r.stderr else "",
                    "error": "",
                    "returncode": r.returncode,
                    "mode": "docker",
                }
                
                if not result["success"] and not result["stderr"]:
                    result["error"] = f"exit code {r.returncode}"
                
                return result
                
            finally:
                os.unlink(tmp_path)
                
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "",
                    "error": f"Docker timed out after {DOCKER_TIMEOUT}s", "mode": "docker"}
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": "",
                    "error": f"Docker error: {e}", "mode": "docker"}

def safe_execute(code, prefer_docker=True):
    """Execute code in Docker if available, else fallback to CodeSandbox."""
    if prefer_docker and DockerSandbox.is_available():
        return DockerSandbox.execute(code)
    from sandbox.code_sandbox import CodeSandbox
    return CodeSandbox.execute(code)
