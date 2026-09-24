"""
Tiger.M.M Natural Language Execution Test Suite.
Tests the full pipeline: natural language input → intent routing → tool execution → response.
"""
import asyncio, json, time, sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pipeline import Level4Pipeline
from core.model_client import ModelClient
from config.settings import DATA_DIR

TEST_CASES = [
    # ── Category 1: Greetings & Simple ──
    ("T01", "简单问候", "你好",
     ["greeting", "identity", "entity_query", "agent_guide", "auto_guide", None], 5, 5.0),
    ("T02", "英文问候", "Hello",
     ["greeting", "identity", "entity_query", "agent_guide", "auto_guide", None], 3, 5.0),
    ("T03", "询问身份", "你是谁",
     ["identity", "greeting", "entity_query", "auto_guide", None], 10, 5.0),

    # ── Category 2: Commands ──
    ("T04", "/stats命令", "/stats",
     ["stats"], 10, 3.0),
    ("T05", "/tool路由", "/tool system_info",
     ["tool", "error"], 5, 5.0),

    # ── Category 3: Knowledge / Memory ──
    ("T06", "记住事实", "记住，我的名字叫小王",
     ["entity_learn", "entity_query", "agent_guide", "auto_guide", None], 5, 5.0),
    ("T07", "查询记忆", "我叫什么名字",
     ["entity_query", "agent_guide", "auto_guide", None], 5, 5.0),

    # ── Category 4: Search & Information ──
    ("T08", "网页搜索", "搜索Python最新版本",
     ["search", "web_search", "agent_guide", "auto_guide", None], 20, 30.0),

    # ── Category 5: System / Desktop ──
    ("T09", "系统时间", "现在几点",
     ["agent_guide", "auto_guide", "entity_query", None], 5, 5.0),

    # ── Category 6: Task / Code ──
    ("T10", "简单计算", "1+1等于几",
     ["agent_guide", "auto_guide", "knowledge_exec", None], 3, 10.0),

    # ── Category 7: Multi-step / Complex ──
    ("T11", "分析型查询", "Python和JavaScript有什么区别",
     ["agent_guide", "auto_guide", "analysis", "external", None], 30, 30.0),

    # ── Category 8: English queries ──
    ("T12", "英文查询", "what is the weather today",
     ["agent_guide", "auto_guide", "entity_query", None], 5, 10.0),

    # ── Category 9: Edge Cases ──
    ("T13", "空输入", "",
     ["identity", "greeting", "agent_guide", "auto_guide", None], 0, 3.0),
    ("T14", "特殊字符", "!!!@#$%",
     ["agent_guide", "auto_guide", "entity_query", None], 1, 5.0),
    ("T15", "超长输入", "请帮我分析一下" + "人工智能的发展趋势。" * 20,
     ["analysis", "agent_guide", "auto_guide", "external", "task", None], 20, 60.0),

    # ── Category 10: Plugin routing ──
    ("T16", "天气查询(含城市)", "北京天气怎么样",
     ["plugin:openmeteo", "agent_guide", "auto_guide", "geocode", None], 10, 15.0),

    # ── Category 11: File / Data ──
    ("T17", "文件操作意图", "帮我打开桌面上的文件",
     ["agent_guide", "auto_guide", "entity_query", "desktop", None], 10, 10.0),
]

def score_result(case, result):
    tid, desc, inp, expected_intents, min_len, max_elapsed = case
    issues = []
    score = 10
    intent = result.get("intent", "?")
    response = result.get("response", "")
    elapsed = result.get("elapsed", 0)

    if inp and not response:
        issues.append("empty response")
        score -= 5

    if inp and response and len(str(response)) < min_len:
        issues.append(f"response too short({len(str(response))}<{min_len})")
        score -= 2

    if elapsed > max_elapsed:
        issues.append(f"timeout({elapsed:.1f}s>{max_elapsed}s)")
        score -= 2

    if intent is not None and expected_intents != [None]:
        acceptable = [e for e in expected_intents if e is not None]
        if acceptable and intent not in acceptable:
            issues.append(f"intent mismatch({intent} not in {acceptable})")
            score -= 2

    if intent == "error":
        issues.append("error status")
        score -= 3
    if "Traceback" in str(response):
        issues.append("traceback in response")
        score -= 3

    passed = score >= 6
    return passed, max(0, score), issues

async def run_tests():
    # Use the same config loading as main.py
    from main import _load_config
    config = _load_config()
    if not config:
        print("WARNING: No model config found! Check keys.json")
        config = {"deepseek": {"model": "deepseek-v4-pro", "provider": "deepseek"}}

    print("=" * 70)
    print("Tiger.M.M Natural Language Execution Test")
    print(f"Cases: {len(TEST_CASES)} | Models: {list(config.keys())}")
    print("=" * 70)

    t0 = time.time()
    mc = ModelClient(config)
    pipeline = Level4Pipeline(mc, config)
    print(f"Pipeline init: {time.time()-t0:.2f}s\n")

    results = []
    passed_count = 0
    total_score = 0

    for i, case in enumerate(TEST_CASES):
        tid, desc, inp, expected, min_len, max_elapsed = case
        print(f"[{i+1:02d}/{len(TEST_CASES)}] {tid} {desc}")
        inp_display = inp[:60] + ('...' if len(inp) > 60 else '')
        print(f"  Input: {inp_display}")

        try:
            t_start = time.time()
            result = await asyncio.wait_for(
                pipeline.process(inp, ext_model=None),
                timeout=60.0
            )
            elapsed = time.time() - t_start

            intent = result.get("intent", "?")
            model = result.get("model", "?")
            response = result.get("response", "")
            code_rounds = result.get("code_rounds", 0)

            passed, score, issues = score_result(case, result)

            record = {
                "id": tid, "description": desc, "input": inp[:200],
                "intent": intent, "model": model,
                "elapsed": round(elapsed, 3), "code_rounds": code_rounds,
                "response_preview": str(response)[:200],
                "response_len": len(str(response)),
                "passed": passed, "score": score, "issues": issues,
            }
            results.append(record)

            status = "OK" if passed else "FAIL"
            print(f"  intent={intent} | model={model} | {elapsed:.2f}s | score={score}/10 | {status}")
            for issue in issues:
                print(f"    ! {issue}")
            if passed:
                passed_count += 1
            total_score += score

        except asyncio.TimeoutError:
            elapsed = time.time() - t_start
            record = {
                "id": tid, "description": desc, "input": inp[:200],
                "intent": "timeout", "model": "N/A",
                "elapsed": round(elapsed, 3), "code_rounds": 0,
                "response_preview": "", "response_len": 0,
                "passed": False, "score": 0, "issues": ["timeout(>60s)"],
            }
            results.append(record)
            print(f"  TIMEOUT! ({elapsed:.1f}s)")

        print()

    # Summary
    total_elapsed = time.time() - t0
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    n = len(TEST_CASES)
    print(f"  Total: {n} | Passed: {passed_count} ({passed_count*100//n}%) | Failed: {n-passed_count}")
    print(f"  Score: {total_score}/{n*10} ({total_score*100//(n*10)}%)")
    print(f"  Elapsed: {total_elapsed:.1f}s")

    intent_counts = Counter(r["intent"] for r in results)
    print("\n  Intent Distribution:")
    for intent, count in intent_counts.most_common():
        print(f"    {intent}: {count}")

    model_counts = Counter(r["model"] for r in results)
    print("  Model Distribution:")
    for model, count in model_counts.most_common():
        print(f"    {model}: {count}")

    avg_elapsed = sum(r["elapsed"] for r in results) / len(results)
    print(f"  Avg Response: {avg_elapsed:.2f}s")

    # Save report
    report_path = Path(__file__).parent.parent / "data" / "nl_test_results.json"
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": n, "passed": passed_count, "failed": n-passed_count,
        "total_score": total_score, "max_score": n*10,
        "elapsed_total": round(total_elapsed, 1),
        "avg_elapsed": round(avg_elapsed, 3),
        "intent_distribution": dict(intent_counts),
        "model_distribution": dict(model_counts),
        "results": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  Report saved: {report_path}")

    return results

if __name__ == "__main__":
    asyncio.run(run_tests())
