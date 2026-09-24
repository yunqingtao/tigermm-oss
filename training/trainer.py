"""
Tiger.M.M Natural Language Trainer
Feeds questions, records responses, scores automatically, tracks progress.
"""
import subprocess, json, os, re, time, sys
from datetime import datetime
from pathlib import Path

TRAIN_DIR = Path(__file__).parent
QUESTIONS_FILE = TRAIN_DIR / "questions.json"
RESULTS_DIR = TRAIN_DIR / "results"
TMM_DIR = TRAIN_DIR.parent
TMM_CMD = [sys.executable, "main.py", "--cli", "--no-check"]

GREEN = '\033[32m'; RED = '\033[31m'; YELLOW = '\033[33m'
DIM = '\033[2m'; BOLD = '\033[1m'; RST = '\033[0m'


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def score_response(response, question):
    """Score a response against expectations."""
    score = 0
    max_score = 100
    details = []
    clean = strip_ansi(response)

    # 1. Tool usage (40 points)
    expected_tools = question.get("expect_tools", [])
    found_tools = []
    for tool in expected_tools:
        if f"/tool {tool}" in clean or tool in clean.lower():
            found_tools.append(tool)
    tool_score = int(len(found_tools) / max(len(expected_tools), 1) * 40)
    score += tool_score
    if expected_tools:
        details.append(f"tools: {found_tools}/{expected_tools} ({tool_score}/40)")

    # 2. Keyword presence (30 points)
    expected_kw = question.get("expect_keywords", [])
    found_kw = [kw for kw in expected_kw if kw.lower() in clean.lower()]
    kw_score = int(len(found_kw) / max(len(expected_kw), 1) * 30)
    score += kw_score
    if expected_kw:
        details.append(f"keywords: {found_kw}/{expected_kw} ({kw_score}/30)")

    # 3. Response quality (20 points) — non-empty, not error
    if clean and len(clean) > 20:
        score += 10
    else:
        details.append("response too short (-10)")
    if "error" not in clean.lower() and "失败" not in clean and "ERR" not in clean:
        score += 10
    else:
        details.append("contains error (-10)")

    # 4. Speed bonus (10 points) — under max_time
    elapsed = question.get("_elapsed", 999)
    max_time = question.get("max_time", 30)
    if elapsed < max_time:
        score += 10
    else:
        details.append(f"slow: {elapsed:.0f}s > {max_time}s (-10)")

    return min(score, 100), "; ".join(details) if details else "perfect"


def run_tmm(message, timeout=60):
    """Start TMM, send message, get response."""
    proc = subprocess.Popen(
        TMM_CMD,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(TMM_DIR), text=True, encoding="utf-8", errors="replace"
    )
    try:
        proc.stdin.write(message + "\n")
        proc.stdin.flush()
        time.sleep(timeout * 0.3)
        proc.stdin.write("/exit\n")
        proc.stdin.flush()
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        return "", "TIMEOUT"


def run_all():
    """Run all test questions and score."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        questions = json.load(f)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = {"timestamp": timestamp, "results": [], "summary": {}}
    total_score = 0

    print(f"\n{BOLD}═══ TMM Trainer — {len(questions)} questions ═══{RST}\n")

    for i, q in enumerate(questions, 1):
        qid = q["id"]
        print(f"[{i}/{len(questions)}] {qid}: {q['prompt'][:60]}...", end=" ", flush=True)

        t0 = time.time()
        stdout, stderr = run_tmm(q["prompt"], q.get("max_time", 30))
        elapsed = time.time() - t0
        q["_elapsed"] = elapsed

        # Extract just the response (after first prompt echo)
        clean = strip_ansi(stdout)
        # Find the model response — after "Tiger.M.M >>>" and before next prompt
        parts = clean.split("Tiger.M.M >>>")
        response = parts[-1].strip() if len(parts) > 1 else clean[-500:]

        score, detail = score_response(response, q)

        total_score += score
        status = f"{GREEN}PASS{RSAT}" if score >= 60 else f"{YELLOW}WARN{RSAT}" if score >= 30 else f"{RED}FAIL{RSAT}"

        print(f"{status} {score}/100 ({elapsed:.1f}s)")
        if score < 100:
            print(f"  {DIM}{detail}{RSAT}")

        results["results"].append({
            "id": qid,
            "prompt": q["prompt"],
            "score": score,
            "elapsed": round(elapsed, 1),
            "detail": detail,
            "response_snippet": response[:200],
            "tags": q.get("tags", [])
        })

        # Save intermediate
        with open(RESULTS_DIR / f"run_{timestamp}.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    avg = total_score / len(questions)
    results["summary"] = {
        "total": len(questions),
        "average": round(avg, 1),
        "passed": sum(1 for r in results["results"] if r["score"] >= 60),
        "failed": sum(1 for r in results["results"] if r["score"] < 60)
    }

    # Final report
    print(f"\n{BOLD}═══ Results ═══{RSAT}")
    print(f"  Average: {avg:.0f}/100")
    print(f"  Passed:  {results['summary']['passed']}/{len(questions)}")
    print(f"  Failed:  {results['summary']['failed']}/{len(questions)}")

    # Per-tag breakdown
    tag_scores = {}
    for r in results["results"]:
        for tag in r.get("tags", []):
            if tag not in tag_scores:
                tag_scores[tag] = []
            tag_scores[tag].append(r["score"])
    print(f"\n  {BOLD}By tag:{RSAT}")
    for tag in sorted(tag_scores):
        scores = tag_scores[tag]
        avg_t = sum(scores) / len(scores)
        bar = "█" * int(avg_t / 10) + "░" * (10 - int(avg_t / 10))
        print(f"    {tag:15s} {bar} {avg_t:.0f}%")

    with open(RESULTS_DIR / f"run_{timestamp}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n  Report: {RESULTS_DIR / f'run_{timestamp}.json'}")


if __name__ == "__main__":
    run_all()
