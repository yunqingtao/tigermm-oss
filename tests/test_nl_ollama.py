"""
Tiger.M.M Natural Language Execution Test Suite — OLLAMA edition.
Uses local Ollama (qwen3.5:9b) for fast, reliable testing.
"""
import asyncio, json, time, sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))
import os
os.environ["MARY3_ROOT"] = str(Path(__file__).parent.parent)

from core.pipeline import Level4Pipeline
from core.model_client import ModelClient
from config.settings import DATA_DIR
from main import _load_config

TEST_CASES = [
    # ── Simple / Local (no model needed) ──
    ("T01", "你好", ["identity", "greeting", "entity_query", "tech"], 5),
    ("T02", "你是谁", ["identity", "greeting", "entity_query", "tech"], 10),
    ("T03", "/stats", ["stats"], 10),

    # ── Knowledge / Memory ──
    ("T04", "记住，测试用户叫张三", ["entity_learn", "entity_query", "tech", "auto_guide"], 5),
    ("T05", "测试用户叫什么", ["entity_query", "tech", "auto_guide"], 5),

    # ── Information queries (model needed) ──
    ("T06", "Python是什么", ["auto_guide", "tech", "external"], 20),
    ("T07", "1加1等于几", ["auto_guide", "tech", "external"], 3),

    # ── Multi-step / Complex ──
    ("T08", "Python和Java有什么区别", ["auto_guide", "tech", "external", "analysis"], 30),
    ("T09", "推荐三本编程入门书", ["auto_guide", "tech", "external"], 20),

    # ── English ──
    ("T10", "What is Python", ["auto_guide", "tech", "external"], 20),

    # ── Edge cases ──
    ("T11", "", ["identity", "greeting", "tech", "auto_guide"], 0),
    ("T12", "!!!@@@", ["auto_guide", "tech", "entity_query"], 1),
]

def score_result(case, result):
    tid, inp, accept_intents, min_len = case
    issues = []
    score = 10
    intent = result.get("intent", "?")
    response = str(result.get("response", ""))
    elapsed = result.get("elapsed", 0)

    # Empty input gets a pass on min_len
    if inp and not response:
        issues.append("empty response")
        score -= 5

    if inp and response and len(response) < min_len:
        issues.append(f"too short({len(response)}<{min_len})")
        score -= 2

    if elapsed > 60:
        issues.append(f"timeout({elapsed:.0f}s)")
        score -= 3

    if intent not in accept_intents:
        issues.append(f"intent={intent} not in {accept_intents}")
        score -= 2

    if intent == "error":
        issues.append("error intent")
        score -= 3
    if "模型暂时不可用" in response:
        issues.append("model unavailable in response")
        score -= 5

    passed = score >= 6
    return passed, max(0, score), issues

async def run_tests():
    config = _load_config()
    if not config:
        config = {"ollama": {"key": "ollama", "url": "http://127.0.0.1:11434/v1", "model": "qwen3.5:9b"}}
    
    print("=" * 70)
    print(f"Tiger.M.M NL Execution Test (Auto Route)")
    print(f"Cases: {len(TEST_CASES)} | Model: auto (local + ollama)")
    print("=" * 70)

    t0 = time.time()
    mc = ModelClient(config)
    pipeline = Level4Pipeline(mc, config)
    print(f"Init: {time.time()-t0:.2f}s\n")

    results = []
    passed = 0
    total_score = 0

    for i, case in enumerate(TEST_CASES):
        tid, inp, accept, min_len = case
        desc = tid  # short
        print(f"[{i+1:02d}/{len(TEST_CASES)}] {tid}: {inp[:50]}{'...' if len(inp)>50 else ''}")

        try:
            t_start = time.time()
            result = await asyncio.wait_for(
                pipeline.process(inp, ext_model=None),
                timeout=90.0
            )
            elapsed = time.time() - t_start

            intent = result.get("intent", "?")
            model = result.get("model", "?")
            response = str(result.get("response", ""))
            code_rounds = result.get("code_rounds", 0)

            passed_flag, score, issues = score_result(case, result)

            record = {
                "id": tid, "input": inp[:200],
                "intent": intent, "model": model,
                "elapsed": round(elapsed, 2), "code_rounds": code_rounds,
                "response": response[:300],
                "response_len": len(response),
                "passed": passed_flag, "score": score, "issues": issues,
            }
            results.append(record)

            status = "PASS" if passed_flag else "FAIL"
            print(f"  intent={intent} model={model} {elapsed:.2f}s score={score}/10 {status}")
            if issues:
                for iss in issues:
                    print(f"    ! {iss}")
            if passed_flag:
                passed += 1
            total_score += score

        except asyncio.TimeoutError:
            elapsed = time.time() - t_start
            record = {
                "id": tid, "input": inp[:200],
                "intent": "timeout", "model": "N/A",
                "elapsed": round(elapsed, 2), "code_rounds": 0,
                "response": "", "response_len": 0,
                "passed": False, "score": 0, "issues": ["timeout(>90s)"],
            }
            results.append(record)
            print(f"  TIMEOUT! {elapsed:.0f}s")

        print()

    # Summary
    total_elapsed = time.time() - t0
    n = len(TEST_CASES)
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Total: {n} | Pass: {passed} ({passed*100//n}%) | Fail: {n-passed}")
    print(f"  Score: {total_score}/{n*10} ({total_score*100//(n*10)}%)")
    print(f"  Time: {total_elapsed:.1f}s")

    intent_counts = Counter(r["intent"] for r in results)
    print("\n  Intents:")
    for k, v in intent_counts.most_common():
        print(f"    {k}: {v}")

    avg = sum(r["elapsed"] for r in results) / n
    print(f"  Avg: {avg:.2f}s")

    # Save
    rp = Path(__file__).parent.parent / "data" / "nl_test_ollama.json"
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": "ollama/qwen3.5:9b",
        "total": n, "passed": passed, "failed": n-passed,
        "total_score": total_score, "max_score": n*10,
        "elapsed": round(total_elapsed, 1), "avg_elapsed": round(avg, 2),
        "intents": dict(intent_counts),
        "results": results,
    }
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  Saved: {rp}")

    return results

if __name__ == "__main__":
    asyncio.run(run_tests())
