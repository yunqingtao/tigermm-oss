"""
Tiger.M.M Risk Classification - command risk grading
Distilled from OpenWorker risk.py + PermissionEngine

Five risk levels:
  READ       -> auto-approved, never needs inbox
  WRITE_LOCAL-> needs approval (once per session)
  EXEC       -> needs approval (every time)
  EXTERNAL   -> needs approval, eligible for standing rules
  NETWORK    -> always needs approval, never auto
"""
from enum import Enum


class RiskClass(str, Enum):
    READ = "read"
    WRITE_LOCAL = "write"
    EXEC = "exec"
    EXTERNAL = "external"
    NETWORK = "network"


RISK_ORDER = [RiskClass.READ, RiskClass.WRITE_LOCAL, RiskClass.EXEC, RiskClass.EXTERNAL, RiskClass.NETWORK]

READ_COMMANDS = [
    "ls", "dir", "cat", "type", "head", "tail", "pwd", "echo",
    "grep", "findstr", "find", "wc", "which", "where",
    "git status", "git diff", "git log", "git show", "git branch",
    "python --version", "node --version", "pip list", "pip show",
    "whoami", "hostname", "date", "time",
]

WRITE_COMMANDS = [
    "mkdir", "rmdir", "cp", "copy", "mv", "move", "ren", "rename",
    "del", "rm", "touch",
    "git add", "git commit", "git checkout", "git merge",
]

EXTERNAL_COMMANDS = [
    "curl", "wget", "git push", "git pull", "git fetch", "git clone",
    "pip install", "pip uninstall", "npm install", "npm uninstall",
    "python -m pip", "winget", "choco", "brew", "apt", "apt-get",
]

SERVICE_COMMANDS = [
    "flask run", "uvicorn", "gunicorn", "python -m http",
    "node server", "npm start", "npm run dev", "yarn start",
    "docker", "systemctl", "service",
]


def classify_command(cmd: str) -> RiskClass:
    cmd_lower = cmd.lower().strip()
    for prefix in SERVICE_COMMANDS:
        if cmd_lower.startswith(prefix):
            return RiskClass.NETWORK
    for prefix in EXTERNAL_COMMANDS:
        if cmd_lower.startswith(prefix):
            return RiskClass.EXTERNAL
    for prefix in WRITE_COMMANDS:
        if cmd_lower.startswith(prefix):
            return RiskClass.WRITE_LOCAL
    for prefix in READ_COMMANDS:
        if cmd_lower.startswith(prefix):
            return RiskClass.READ
    if cmd_lower.startswith("python") or cmd_lower.startswith("python3"):
        return RiskClass.EXEC
    if cmd_lower.startswith("node ") or cmd_lower.startswith("npm run"):
        return RiskClass.EXEC
    return RiskClass.EXEC


def classify_batch(cmds: list[str]) -> RiskClass:
    if not cmds:
        return RiskClass.READ
    return max((classify_command(c) for c in cmds), key=lambda r: RISK_ORDER.index(r))


def is_consequential(risk: RiskClass) -> bool:
    return risk is not RiskClass.READ
