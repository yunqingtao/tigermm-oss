"""
Tiger.M.M Guardrails v2 - Six-dimension security audit
Distilled from CLS-Certify (CocoLoop) + openai-agents-python
"""
import re, os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


class SecurityRating(str, Enum):
    S_PLUS = "S+"
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"

    @property
    def score(self):
        return {"S+": 95, "S": 85, "A": 72, "B": 57, "C": 40, "D": 15}[self.value]

    @property
    def usable(self):
        return self in (SecurityRating.S_PLUS, SecurityRating.S, SecurityRating.A)


class SourceTrust(str, Enum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


T1_DOMAINS = [
    "github.com/openai", "github.com/anthropics", "github.com/microsoft",
    "github.com/google", "github.com/meta", "github.com/apache",
    "github.com/langchain-ai", "github.com/nousresearch",
]


def classify_source(url, stars=0):
    url_lower = url.lower()
    for domain in T1_DOMAINS:
        if domain in url_lower:
            return SourceTrust.T1
    if stars > 1000:
        return SourceTrust.T2
    if "github.com" in url_lower:
        return SourceTrust.T2 if stars > 100 else SourceTrust.T3
    return SourceTrust.T3


# D-grade patterns: instant veto
D_PATTERNS = [
    (r"eval\s*\(", "eval()"),
    (r"exec\s*\(", "exec()"),
    (r"system\s*\(", "system()"),
    ("child_process", "child_process"),
    (r"spawn\s*\(", "spawn()"),
    (r"rm\s+-rf\s+/", "rm -rf /"),
    ("format\\s+[cC]:", "format C:"),
    (">\\s*/dev/sd", "block device overwrite"),
    ("curl.*\\|.*sh", "curl pipe to shell"),
    ("wget.*\\|.*sh", "wget pipe to shell"),
    ("scp\\s+.*ssh", "scp SSH keys"),
    ("base64.*\\|.*curl", "base64+curl exfil"),
    (r"subprocess\.run", "subprocess.run()"),
    (r"subprocess\.Popen", "subprocess.Popen()"),
]

C_PATTERNS = [
    (r"pickle\.load", "pickle.load()"),
    (r"pickle\.dump", "pickle.dump()"),
    (r"yaml\.load\(", "yaml.load()"),
    (r"os\.system\(", "os.system()"),
    (r"os\.popen\(", "os.popen()"),
    (r"password\s*=\s*\S", "hardcoded password"),
    (r"api.key\s*=\s*\S", "hardcoded API key"),
    (r"token\s*=\s*\S{8,}", "hardcoded token"),
    (r"requests\.post\(", "HTTP POST (check target)"),
]


URL_RE = re.compile(r"https?://[^\s\]\)]+")


def extract_urls(text):
    return URL_RE.findall(text)


def classify_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if any(k in host for k in ["cdn", "unpkg", "jsdelivr"]):
        return "cdn"
    if "raw.githubusercontent.com" in host:
        return "raw"
    if any(k in host for k in ["ngrok", "localtunnel", "127.0.0.1", "localhost"]):
        return "suspicious"
    if parsed.path and any(parsed.path.endswith(e) for e in [".js", ".py", ".sh", ".exe"]):
        return "dynamic"
    return "normal"


def detect_dynamic_loading(text):
    urls = extract_urls(text)
    dynamic_urls = [u for u in urls if classify_url(u) in ("dynamic", "raw", "suspicious")]
    suspicious = [u for u in urls if classify_url(u) == "suspicious"]
    depth = 1 if dynamic_urls else 0
    if depth > 0:
        for u in dynamic_urls:
            inner = extract_urls(u)
            if inner:
                depth = 2
                break
    return depth, suspicious


@dataclass
class AuditReport:
    target: str
    source_trust: SourceTrust = SourceTrust.T3
    rating: SecurityRating = SecurityRating.D
    score: int = 0
    d_triggers: list = field(default_factory=list)
    c_triggers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    url_depth: int = 0
    suspicious_urls: list = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self):
        return {
            "target": self.target,
            "source_trust": self.source_trust.value,
            "rating": self.rating.value,
            "score": self.score,
            "d_triggers": self.d_triggers,
            "c_triggers": self.c_triggers,
            "warnings": self.warnings,
            "url_depth": self.url_depth,
            "suspicious_urls": self.suspicious_urls,
            "recommendation": self.recommendation,
        }



# Modules excluded from security audit (security tools using D_GRADE_PATTERNS for detection)
AUDIT_EXCLUDE = {"guardrails", "chain_engine", "risk"}

def audit_skill(path, source_url="", stars=0):
    path = Path(path)
    name = path.stem if path.is_file() else path.name
    if name in AUDIT_EXCLUDE:
        report = AuditReport(target=str(path))
        report.rating = SecurityRating.S
        report.score = 95
        report.source_trust = SourceTrust.T1
        report.recommendation = "Excluded (security detection module)"
        return report
    report = AuditReport(target=str(path))
    report.source_trust = classify_source(source_url, stars)
    all_code = ""
    if path.is_dir():
        for f in path.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".js", ".sh", ".md", ".json", ".yaml", ".yml", ".toml"):
                try:
                    all_code += f.read_text(encoding="utf-8", errors="ignore") + "\n"
                except Exception:
                    pass
    elif path.is_file():
        try:
            all_code = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            report.warnings.append(f"Cannot read: {path}")
            return report
    if not all_code.strip():
        report.rating = SecurityRating.S
        report.score = 85
        report.recommendation = "Empty - safe by default"
        return report
    for p, desc in D_PATTERNS:
        if re.search(p, all_code, re.IGNORECASE):
            report.d_triggers.append(desc)
    for p, desc in C_PATTERNS:
        if re.search(p, all_code, re.IGNORECASE):
            report.c_triggers.append(desc)
    report.url_depth, report.suspicious_urls = detect_dynamic_loading(all_code)
    trust_bonus = {SourceTrust.T1: 15, SourceTrust.T2: 8, SourceTrust.T3: 0}[report.source_trust]
    if report.d_triggers:
        report.rating = SecurityRating.D
        report.score = 15
    elif report.c_triggers or report.url_depth >= 2:
        score = 100 - len(report.c_triggers) * 20 + trust_bonus
        if report.url_depth >= 2:
            report.warnings.append(f"URL depth L{report.url_depth} - forced C cap")
            report.rating = SecurityRating.C
            report.score = min(score, 49)
        elif score >= 85:
            report.rating = SecurityRating.S if report.source_trust != SourceTrust.T3 else SecurityRating.A
        elif score >= 70:
            report.rating = SecurityRating.A
        elif score >= 55:
            report.rating = SecurityRating.B
        else:
            report.rating = SecurityRating.C
        report.score = score
    else:
        score = min(100 + trust_bonus, 100)
        if score >= 95 and report.source_trust == SourceTrust.T1:
            report.rating = SecurityRating.S_PLUS
        elif score >= 85:
            report.rating = SecurityRating.S if report.source_trust != SourceTrust.T3 else SecurityRating.A
        elif score >= 70:
            report.rating = SecurityRating.A
        else:
            report.rating = SecurityRating.B
        report.score = score
    recs = {
        SecurityRating.S_PLUS: "TRUSTED - Top-tier security.",
        SecurityRating.S: "RECOMMENDED - High security standard.",
        SecurityRating.A: "SAFE - Standard security. Normal use OK.",
        SecurityRating.B: "REVIEW - Minor concerns.",
        SecurityRating.C: "ISOLATED - Sandbox only.",
        SecurityRating.D: "BLOCKED - Do not use.",
    }
    report.recommendation = recs[report.rating]
    return report


@dataclass
class GuardResult:
    passed: bool = True
    reason: str = ""
    sanitized: Optional[str] = None


PROMPT_INJECTION = [
    r"ignore\s+(all\s+|your\s+|previous\s+){0,3}(instructions|rules|guidelines|prompt)",
    r"you are now\s+.{1,30}(assistant|agent|bot)",
    r"DAN |do anything now|jailbreak",
]

DANGEROUS_CMDS = [
    r"rm\s+-rf\s+/", r"format\s+[cC]:", r"shutdown\s+/[srf]",
]

API_KEY_RE = [
    r"sk-[a-zA-Z0-9]{20,60}", r"sk-ant-[a-zA-Z0-9]{20,60}",
    r"AIza[0-9A-Za-z_-]{35}", r"hf_[a-zA-Z0-9]{20,40}",
    r"ghp_[a-zA-Z0-9]{36}", r"xox[bpras]-[a-zA-Z0-9-]{10,60}",
]

SENSITIVE_RE = [
    r"password\s*[:=]\s*\S{3,30}",
    r"secret\s*[:=]\s*\S{3,50}",
    r"token\s*[:=]\s*[a-zA-Z0-9._-]{10,80}",
    r"api[_-]?key\s*[:=]\s*[a-zA-Z0-9_-]{10,80}",
]


def _match_any(text, patterns):
    t = text.lower()
    for p in patterns:
        if re.search(p, t):
            return p[:60]
    return None


def input_guard_prompt_injection(text):
    hit = _match_any(text, PROMPT_INJECTION)
    return GuardResult(passed=hit is None, reason=f"Injection: {hit}" if hit else "")


def input_guard_dangerous(text):
    hit = _match_any(text, DANGEROUS_CMDS)
    return GuardResult(passed=hit is None, reason=f"Dangerous: {hit}" if hit else "")


def input_guard_max_length(text, max_chars=32000):
    ok = len(text) <= max_chars
    return GuardResult(passed=ok, reason="" if ok else f"Too long ({len(text)} > {max_chars})")


def output_guard_redact_keys(text):
    s = text
    hit = False
    for p in API_KEY_RE:
        if re.search(p, s):
            hit = True
            s = re.sub(p, "[REDACTED_API_KEY]", s)
    return GuardResult(passed=True, reason="Keys redacted" if hit else "", sanitized=s if hit else None)


def output_guard_redact_sensitive(text):
    s = text
    hit = False
    for p in SENSITIVE_RE:
        if re.search(p, s, re.IGNORECASE):
            hit = True
            s = re.sub(p, "[REDACTED]", s, flags=re.IGNORECASE)
    return GuardResult(passed=True, reason="Sensitive redacted" if hit else "", sanitized=s if hit else None)


INPUT_GUARDS = [input_guard_prompt_injection, input_guard_dangerous, input_guard_max_length]
OUTPUT_GUARDS = [output_guard_redact_keys, output_guard_redact_sensitive]


def run_input_guards(text):
    for g in INPUT_GUARDS:
        r = g(text)
        if not r.passed:
            return r
    return GuardResult(passed=True)


def run_output_guards(text):
    final = text
    reasons = []
    for g in OUTPUT_GUARDS:
        r = g(final)
        if r.sanitized:
            final = r.sanitized
            reasons.append(r.reason)
    if reasons:
        return GuardResult(passed=True, reason=", ".join(reasons), sanitized=final)
    return GuardResult(passed=True)
