"""
SelfRegulation Engine v1.0 — Agent Self-Awareness & Constraint Compliance
Tiger.M.M's missing module: boundary awareness, constraint enforcement, self-audit

Handles:
  1. Constraint compliance: parse user forbids/requires, reject violations before execution
  2. Self-boundary awareness: know which module can do what, never cross boundaries
  3. Permission control: strict scope enforcement, never escalate without authorization
  4. Post-execution self-audit: verify entire pipeline against original constraints
"""

import re, os, json, time
from typing import Optional, Dict, List, Any, Tuple, Set
from dataclasses import dataclass, field
from enum import Enum


class ModuleBoundary(Enum):
    """Which module is currently active"""
    KNOWLEDGE = "knowledge"     # auto_guide: pattern matching, rules, entities
    REASONING = "reasoning"     # ChainEngine: step-by-step deduction
    ORCHESTRATION = "orch"     # OrchestrationEngine: task execution
    EXECUTION = "execution"     # Direct model: code generation, explanation
    SYSTEM = "system"          # OpenClaw: Win32, registry, processes
    WEB = "web"                # Hermes: scraping, API, crawling


@dataclass
class ModuleCapability:
    """What each module is allowed to do"""
    name: str
    boundary: ModuleBoundary
    can_read_files: bool = False
    can_write_files: bool = False
    can_run_scripts: bool = False
    can_execute_commands: bool = False
    can_access_web: bool = False
    can_access_system: bool = False
    can_probe_environment: bool = False
    can_scan_directories: bool = False
    can_read_source_code: bool = False
    requires_confirmation: bool = False


# Module capability matrix — strict boundaries
MODULE_CAPABILITIES = {
    ModuleBoundary.KNOWLEDGE: ModuleCapability(
        name="Knowledge Engine",
        boundary=ModuleBoundary.KNOWLEDGE,
        can_read_files=False,      # knowledge is cached, no file access
        can_write_files=False,
        can_run_scripts=False,
    ),
    ModuleBoundary.REASONING: ModuleCapability(
        name="Chain Reasoning",
        boundary=ModuleBoundary.REASONING,
        can_read_files=True,       # can read files for reasoning
        can_run_scripts=True,      # can run scripts for verification
        can_scan_directories=True, # pre-scan for related files
        can_read_source_code=True, # read source for understanding
    ),
    ModuleBoundary.ORCHESTRATION: ModuleCapability(
        name="Orchestration Engine",
        boundary=ModuleBoundary.ORCHESTRATION,
        can_run_scripts=True,      # execute pipeline steps
        can_write_files=False,     # orchestration doesn't write, scripts do
        can_scan_directories=False, # forbidden: orchestration must not probe
        can_read_source_code=False, # forbidden: orchestration follows plan, doesn't explore
        can_probe_environment=False, # forbidden: no pre-execution environment probes
    ),
    ModuleBoundary.EXECUTION: ModuleCapability(
        name="Model Execution",
        boundary=ModuleBoundary.EXECUTION,
        can_read_files=True,
        can_write_files=True,
        can_run_scripts=True,
        can_execute_commands=True,
        can_probe_environment=True,
        can_scan_directories=True,
        requires_confirmation=True,
    ),
    ModuleBoundary.SYSTEM: ModuleCapability(
        name="OpenClaw System",
        boundary=ModuleBoundary.SYSTEM,
        can_read_files=True,
        can_write_files=True,
        can_run_scripts=True,
        can_execute_commands=True,
        can_access_system=True,
        can_probe_environment=True,
        can_scan_directories=True,
        requires_confirmation=True,
    ),
    ModuleBoundary.WEB: ModuleCapability(
        name="Hermes Web",
        boundary=ModuleBoundary.WEB,
        can_access_web=True,
        can_write_files=True,
        can_read_source_code=True,
        can_probe_environment=False,
    ),
}


class BoundaryViolation(Exception):
    """Raised when a module tries to cross its boundary"""
    def __init__(self, module: str, action: str, detail: str):
        self.module = module
        self.action = action
        self.detail = detail
        super().__init__(f"BOUNDARY VIOLATION [{module}]: {action} - {detail}")


class SelfRegulationEngine:
    """Core self-awareness and constraint compliance engine"""

    def __init__(self):
        self.active_boundary = ModuleBoundary.KNOWLEDGE
        self.violations: List[BoundaryViolation] = []
        self.action_log: List[Dict] = []
        self.user_constraints: List[Dict] = []
        self.pre_execution_rules: List[Dict] = []
        self.post_execution_checks: List[Dict] = []

    # ================================================================
    # 1. CONSTRAINT COMPLIANCE (指令遵守)
    # ================================================================

    def parse_user_constraints(self, message: str) -> List[Dict]:
        """Extract all user constraints from message"""
        constraints = []

        # Forbidden actions
        forbid_patterns = [
            (r'严禁.*?(?:执行|使用|运行|调用|读取|查看|扫描)\s*(\S+)', 'forbid_command'),
            (r'不得\s*(.+?)(?:[；;。]|$)', 'forbid_action'),
            (r'禁止\s*(.+?)(?:[；;。]|$)', 'forbid_action'),
            (r'不允许\s*(.+?)(?:[；;。]|$)', 'forbid_action'),
        ]
        for pattern, ctype in forbid_patterns:
            for m in re.finditer(pattern, message):
                constraints.append({
                    "type": ctype,
                    "target": m.group(1).strip(),
                    "severity": "HARD",
                    "source_text": m.group(0),
                })

        # Required actions
        require_patterns = [
            (r'必须\s*(.+?)(?:[；;。]|$)', 'require'),
            (r'只能\s*(.+?)(?:[；;。]|$)', 'require_exclusive'),
            (r'一定\s*(.+?)(?:[；;。]|$)', 'require'),
            (r'务必\s*(.+?)(?:[；;。]|$)', 'require'),
        ]
        for pattern, ctype in require_patterns:
            for m in re.finditer(pattern, message):
                constraints.append({
                    "type": ctype,
                    "target": m.group(1).strip(),
                    "severity": "HARD",
                    "source_text": m.group(0),
                })

        # Execution mode constraints
        if re.search(r'串行执行|依次执行|逐个执行', message):
            constraints.append({
                "type": "execution_mode", "target": "serial",
                "severity": "HARD", "source_text": "serial execution required"
            })
        if re.search(r'并行执行|同时执行', message):
            constraints.append({
                "type": "execution_mode", "target": "parallel",
                "severity": "HARD", "source_text": "parallel execution required"
            })

        # Circuit breaker
        if re.search(r'熔断|报错.*?(?:停止|跳过|中断|终止)', message):
            constraints.append({
                "type": "circuit_breaker", "target": "on_error",
                "severity": "HARD", "source_text": "circuit breaker on error"
            })

        # Bidirectional
        if re.search(r'双向闭环|回传.*?主控|上报.*?执行', message):
            constraints.append({
                "type": "bidirectional", "target": "state_feedback",
                "severity": "HARD", "source_text": "bidirectional closed loop required"
            })

        self.user_constraints = constraints
        return constraints

    def check_constraint(self, action: str, target: str) -> Tuple[bool, str]:
        """Check if an action violates any user constraint. Returns (allowed, reason)."""
        for c in self.user_constraints:
            if c["type"] in ("forbid_command", "forbid_action"):
                forbidden = c["target"].lower()
                action_lower = f"{action} {target}".lower()

                # Check specific forbidden commands
                forbidden_terms = re.findall(r'(\w+(?:\.exe|\.py|\s+命令)?)', forbidden)
                for term in forbidden_terms:
                    term = term.strip().lower()
                    if term and term in action_lower:
                        return False, f"CONSTRAINT VIOLATION: {c['source_text']}"

                # Check semantic matches
                if any(kw in forbidden for kw in ['dir', 'ls', 'list', '目录', '清单']):
                    if any(kw in action_lower for kw in ['dir', 'ls', 'list', 'scan', 'probe', 'explore']):
                        return False, f"CONSTRAINT VIOLATION: {c['source_text']} - no environment probing"

                if any(kw in forbidden for kw in ['源码', 'source', 'code', '读取脚本', 'read']):
                    if action == "read_file" or "source" in action_lower:
                        return False, f"CONSTRAINT VIOLATION: {c['source_text']} - no source code reading"

            if c["type"] == "require_exclusive":
                # Must only do this, nothing else
                pass

            if c["type"] == "execution_mode" and c["target"] == "serial":
                # Enforced by OrchestrationEngine
                pass

        return True, ""

    # ================================================================
    # 2. BOUNDARY AWARENESS (自我行为甄别)
    # ================================================================

    def set_boundary(self, boundary: ModuleBoundary):
        """Switch active module boundary"""
        self.active_boundary = boundary

    def get_capability(self) -> ModuleCapability:
        """Get capabilities of currently active module"""
        return MODULE_CAPABILITIES.get(self.active_boundary)

    def can_i(self, action: str) -> Tuple[bool, str]:
        """Check if current module is allowed to perform an action"""
        cap = self.get_capability()
        if not cap:
            return True, ""  # Unknown module, allow

        checks = {
            "read_file": cap.can_read_files,
            "write_file": cap.can_write_files,
            "run_script": cap.can_run_scripts,
            "execute_command": cap.can_execute_commands,
            "web_access": cap.can_access_web,
            "system_access": cap.can_access_system,
            "probe_environment": cap.can_probe_environment,
            "scan_directory": cap.can_scan_directories,
            "read_source_code": cap.can_read_source_code,
        }

        for check_name, allowed in checks.items():
            if check_name in action.lower() or action.lower() in check_name:
                if not allowed:
                    return False, (
                        f"BOUNDARY VIOLATION: {cap.name} cannot {action}. "
                        f"Only allowed: "
                        f"read_files={cap.can_read_files}, "
                        f"run_scripts={cap.can_run_scripts}, "
                        f"scan_dirs={cap.can_scan_directories}, "
                        f"read_source={cap.can_read_source_code}"
                    )

        return True, ""

    def enforce_boundary(self, action: str, target: str) -> Tuple[bool, str]:
        """Full boundary check: module capability + user constraints"""
        # Check module capability first
        allowed, reason = self.can_i(action)
        if not allowed:
            self.violations.append(BoundaryViolation(
                self.active_boundary.value, action, reason
            ))
            return False, reason

        # Then check user constraints
        allowed, reason = self.check_constraint(action, target)
        if not allowed:
            self.violations.append(BoundaryViolation(
                self.active_boundary.value, action, reason
            ))
            return False, reason

        return True, ""

    # ================================================================
    # 3. PERMISSION CONTROL (权限管控)
    # ================================================================

    def assess_permission_level(self) -> str:
        """Determine current permission level"""
        cap = self.get_capability()
        if not cap:
            return "unknown"

        if cap.can_access_system or cap.can_execute_commands:
            return "L3_AUTONOMOUS"
        if cap.can_write_files or cap.can_run_scripts:
            return "L2_CONTROLLED"
        return "L1_RESTRICTED"

    def require_escalation(self, target_level: str) -> bool:
        """Check if action requires permission escalation"""
        current = self.assess_permission_level()
        levels = {"L1_RESTRICTED": 1, "L2_CONTROLLED": 2, "L3_AUTONOMOUS": 3}
        return levels.get(target_level, 0) > levels.get(current, 0)

    # ================================================================
    # 4. POST-EXECUTION SELF-AUDIT (执行后自查)
    # ================================================================

    def audit_execution(self, steps: List[Dict], original_constraints: List[Dict]) -> Dict:
        """Post-execution self-audit: verify everything was compliant"""
        findings = {
            "violations": [],
            "warnings": [],
            "compliance_score": 1.0,
            "summary": "",
        }

        # Check each step against each constraint
        for step in steps:
            action = step.get("action", "")
            target = step.get("target", "")
            for c in original_constraints:
                allowed, reason = self.check_constraint(action, target)
                if not allowed:
                    findings["violations"].append({
                        "step": step,
                        "constraint": c,
                        "reason": reason,
                    })
                    findings["compliance_score"] -= 0.1

        # Check for unauthorized environment probing
        for step in steps:
            target_lower = step.get("target", "").lower()
            if any(kw in target_lower for kw in ['dir', 'ls', 'list', 'cat', 'type', 'env', 'whoami']):
                findings["warnings"].append({
                    "step": step,
                    "issue": "Potential unauthorized environment probe",
                })

        # Check for missing steps
        if original_constraints and not steps:
            findings["warnings"].append({
                "issue": "No steps executed despite constraints being present"
            })

        # Generate summary
        score = findings["compliance_score"]
        if score >= 0.9:
            findings["summary"] = f"COMPLIANT ({score:.0%}): All constraints respected"
        elif score >= 0.7:
            findings["summary"] = f"PARTIAL ({score:.0%}): Minor violations detected"
        else:
            findings["summary"] = f"NON-COMPLIANT ({score:.0%}): Significant violations"

        return findings

    # ================================================================
    # 5. PRE-EXECUTION GUARD (执行前守卫)
    # ================================================================

    def guard_action(self, action: str, target: str, 
                     active_boundary: ModuleBoundary = None) -> Tuple[bool, str, Dict]:
        """Complete pre-execution guard: boundary + constraint + permission"""
        if active_boundary:
            self.set_boundary(active_boundary)

        # Layer 1: Module boundary check
        allowed, reason = self.enforce_boundary(action, target)
        if not allowed:
            return False, reason, {"layer": "boundary", "violation": reason}

        # Layer 2: Permission check
        cap = self.get_capability()
        if cap and cap.requires_confirmation:
            # Actions from execution modules need user confirmation
            pass  # Let OrchestrationEngine handle confirmation

        # Layer 3: Constraint check (already done in enforce_boundary)

        self.action_log.append({
            "time": time.time(),
            "action": action,
            "target": target,
            "boundary": active_boundary.value if active_boundary else self.active_boundary.value,
            "allowed": True,
        })

        return True, "", {"layer": "all_clear"}

    def get_violation_report(self) -> str:
        """Generate violation report"""
        if not self.violations:
            return "No boundary violations detected."
        lines = [f"BOUNDARY VIOLATIONS ({len(self.violations)}):"]
        for v in self.violations:
            lines.append(f"  [{v.module}] {v.action}: {v.detail}")
        return "\n".join(lines)


# ================================================================
# 6. MODULE BOUNDARY AWARENESS INJECTION
# ================================================================

    # ================================================================
    # 6. METACOGNITION — Self-Agent review & improvement
    # ================================================================

    def record_execution(self, tool: str, action: str, success: bool,
                         elapsed: float, model: str = "", error: str = ""):
        """Record a tool execution for later review."""
        entry = {
            "time": time.time(),
            "tool": tool,
            "action": action,
            "success": success,
            "elapsed": elapsed,
            "model": model,
            "error": error[:200] if error else "",
        }
        self.action_log.append(entry)
        # Keep last 500 entries
        if len(self.action_log) > 500:
            self.action_log = self.action_log[-300:]

    def review_recent(self, n: int = 50) -> dict:
        """Analyze recent executions, return findings."""
        recent = self.action_log[-n:] if len(self.action_log) >= n else self.action_log
        if not recent:
            return {"findings": [], "summary": "no data"}

        findings = []
        # 1. Failure rate by tool
        tool_stats = {}
        for e in recent:
            t = e["tool"]
            if t not in tool_stats:
                tool_stats[t] = {"total": 0, "failures": 0, "total_elapsed": 0}
            tool_stats[t]["total"] += 1
            tool_stats[t]["total_elapsed"] += e["elapsed"]
            if not e["success"]:
                tool_stats[t]["failures"] += 1

        for tool, stats in tool_stats.items():
            rate = stats["failures"] / max(stats["total"], 1)
            avg_time = stats["total_elapsed"] / max(stats["total"], 1)
            if rate > 0.3:
                findings.append({
                    "type": "unreliable_tool",
                    "tool": tool,
                    "failure_rate": round(rate, 2),
                    "detail": f"{tool} fails {int(rate*100)}% of the time ({stats['failures']}/{stats['total']})",
                })
            if avg_time > 5.0 and stats["total"] >= 3:
                findings.append({
                    "type": "slow_tool",
                    "tool": tool,
                    "avg_elapsed": round(avg_time, 1),
                    "detail": f"{tool} averages {avg_time:.1f}s per call",
                })

        # 2. Error pattern clustering
        errors = [e["error"] for e in recent if e["error"]]
        if errors:
            common = max(set(errors), key=errors.count) if len(set(errors)) < len(errors) else ""
            if common:
                findings.append({
                    "type": "recurring_error",
                    "pattern": common[:100],
                    "count": errors.count(common),
                    "detail": f"'{common[:60]}' occurred {errors.count(common)} times",
                })

        # 3. Model effectiveness
        model_stats = {}
        for e in recent:
            m = e["model"] or "unknown"
            if m not in model_stats:
                model_stats[m] = {"total": 0, "failures": 0}
            model_stats[m]["total"] += 1
            if not e["success"]:
                model_stats[m]["failures"] += 1
        for model, stats in model_stats.items():
            if stats["total"] >= 5:
                rate = stats["failures"] / stats["total"]
                if rate > 0.4:
                    findings.append({
                        "type": "model_performance",
                        "model": model,
                        "failure_rate": round(rate, 2),
                        "detail": f"Model {model} has high failure rate ({int(rate*100)}%)",
                    })

        return {
            "findings": findings,
            "summary": f"Reviewed {len(recent)} executions, {len(findings)} issues found",
            "tool_stats": {t: {"total": s["total"], "failures": s["failures"]}
                          for t, s in tool_stats.items()},
        }

    def suggest_improvements(self) -> list[str]:
        """Generate actionable suggestions from recent execution data."""
        review = self.review_recent(50)
        suggestions = []
        for f in review.get("findings", []):
            if f["type"] == "unreliable_tool":
                suggestions.append(
                    f"Tool '{f['tool']}' unreliable ({f['failure_rate']:.0%} fail). "
                    f"Consider adding fallback or retry logic."
                )
            elif f["type"] == "slow_tool":
                suggestions.append(
                    f"Tool '{f['tool']}' slow ({f['avg_elapsed']:.1f}s avg). "
                    f"Consider async or timeout increase."
                )
            elif f["type"] == "recurring_error":
                suggestions.append(
                    f"Recurring error '{f['pattern'][:60]}' ({f['count']}x). "
                    f"May need a fix or workaround."
                )
            elif f["type"] == "model_performance":
                suggestions.append(
                    f"Model '{f['model']}' performing poorly ({f['failure_rate']:.0%} fail). "
                    f"Consider routing away or checking API health."
                )
        if not suggestions:
            suggestions.append("All systems nominal. No issues detected.")
        return suggestions

    def self_diagnose(self) -> dict:
        """One-shot self-check: what is TMM's current health?"""
        review = self.review_recent(30)
        violations = len(self.violations)
        suggestions = self.suggest_improvements()

        # Health score: starts at 100, deductions for issues
        score = 100
        for f in review.get("findings", []):
            if f["type"] == "unreliable_tool":
                score -= 15
            elif f["type"] == "slow_tool":
                score -= 5
            elif f["type"] == "recurring_error":
                score -= 10
            elif f["type"] == "model_performance":
                score -= 10
        score -= violations * 5
        score = max(0, min(100, score))

        status = "healthy" if score >= 80 else "degraded" if score >= 50 else "critical"

        return {
            "health_score": score,
            "status": status,
            "violations": violations,
            "issues_found": len(review.get("findings", [])),
            "suggestions": suggestions,
            "summary": review.get("summary", ""),
        }


MODULE_BOUNDARY_PROMPT = """
MODULE BOUNDARIES (you MUST respect these):

KNOWLEDGE MODE (auto_guide):
  ALLOWED: return cached knowledge, match patterns, respond to simple queries
  FORBIDDEN: read files, run scripts, scan directories, access web, execute commands

REASONING MODE (ChainEngine):
  ALLOWED: read relevant files, run scripts for verification, scan for related files
  FORBIDDEN: environment probes (dir/ls/cat), random exploration, executing without purpose

ORCHESTRATION MODE (OrchestrationEngine):
  ALLOWED: execute pipeline steps in order, report results, follow plan exactly
  FORBIDDEN: environment probes, reading source code, scanning directories, 
             adding extra steps, running commands outside the plan
  CRITICAL: Orchestration is NOT exploration. Follow the plan, nothing more.

EXECUTION MODE (Model):
  ALLOWED: read/write files, run scripts, execute commands, probe environment
  REQUIRES: user confirmation for destructive actions
  FORBIDDEN: nothing explicitly, but respect all user constraints

SYSTEM MODE (OpenClaw):
  ALLOWED: Win32 API, process control, registry, system commands
  REQUIRES: user confirmation for all system-level operations

WEB MODE (Hermes):
  ALLOWED: HTTP requests, web scraping, API calls, JS parsing
  FORBIDDEN: accessing local files, running local commands

When user says '严禁/禁止/不得', those are HARD CONSTRAINTS across ALL modules.
"""


# Export manifest
SELFREG_MANIFEST = {
    "name": "SelfRegulation Engine",
    "version": "1.0",
    "modules": [
        "parse_user_constraints - Extract all constraints from user message",
        "check_constraint - Verify action against user constraints",
        "set_boundary / can_i - Module boundary enforcement",
        "guard_action - Three-layer pre-execution guard",
        "audit_execution - Post-execution self-audit",
        "get_violation_report - Full violation tracking",
    ],
    "boundary_matrix": {
        "knowledge": "Pattern matching only, no file/script access",
        "reasoning": "Read files + run scripts for verification",
        "orchestration": "Execute pipeline steps, NO probing/exploring",
        "execution": "Full access, requires confirmation",
        "system": "Win32 API, requires confirmation",
        "web": "HTTP/API only, no local access",
    },
    "critical_rules": [
        "Orchestration NEVER probes environment",
        "Orchestration NEVER reads source code",
        "Orchestration NEVER scans directories",
        "User '严禁/禁止' = HARD constraint across ALL modules",
        "Every module stays within its boundary",
    ],
}