"""用量记账 — 测试 (2026-09-20)

守三件事 (每件都有真实用途):
  ★ probe 流量不记账 —— 否则验证/门禁跑的调用把真实用量顶高, 数字就不可信了
  ★ 记账失败绝不影响对话 —— 磁盘满/权限错时, 对话必须照常返回
  ★ 统计口径正确 —— 按模型/按天聚合, 坏行跳过, token 合计对得上

隔离: TMM_USAGE_LOG 指向 tmp (绝不碰 data/model_usage.jsonl)。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import model_client as MC
from tools import usage_stats as US


@pytest.fixture
def ulog(tmp_path, monkeypatch):
    p = tmp_path / "model_usage.jsonl"
    monkeypatch.setenv("TMM_USAGE_LOG", str(p))
    return p


def _row(model="deepseek", ok=True, ts=None, pt=100, ct=50, el=1.0, err=""):
    return {"ts": ts or time.time(), "model": model, "model_id": model, "ok": ok,
            "error": err, "elapsed": el, "prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": pt + ct, "chars_in": 10}


def _write(p, rows):
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


class TestProbeAwareness:
    """★★ 最重要: 探针流量不进账。"""

    def test_record_usage_respects_probe(self, ulog):
        tok = MC.set_probe(True)
        try:
            assert MC.is_probe() is True
            MC.record_usage("deepseek", {"model": "deepseek", "usage": {"total_tokens": 5}}, 0.1, [])
        finally:
            MC.reset_probe(tok)
        assert not ulog.exists(), "probe 状态下竟然记了账"

    def test_records_when_not_probe(self, ulog):
        assert MC.is_probe() is False
        ok = MC.record_usage("deepseek", {"model": "deepseek", "usage": {"total_tokens": 7}}, 0.2,
                             [{"role": "user", "content": "hi"}])
        assert ok is True and ulog.is_file()
        row = json.loads(ulog.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert row["total_tokens"] == 7 and row["ok"] is True and row["chars_in"] == 2

    def test_probe_nested_restores(self, ulog):
        t1 = MC.set_probe(True)
        t2 = MC.set_probe(False)
        MC.reset_probe(t2)
        assert MC.is_probe() is True, "内层恢复后应回到外层状态"
        MC.reset_probe(t1)
        assert MC.is_probe() is False


class TestNeverBreaksCall:
    def test_record_usage_swallows_errors(self, monkeypatch):
        """★ 记账路径出任何错都必须被吞掉 (否则一次磁盘错误会打断对话)。"""
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(MC, "usage_log_path", boom)
        assert MC.record_usage("x", {"usage": {}}, 0.1, []) is False

    def test_generate_wrapper_records_and_returns_same_dict(self, ulog, monkeypatch):
        """★ 包装层不得改变返回值 (调用方拿到的字典必须一模一样)。"""
        sentinel = {"text": "hi", "model": "m", "error": None, "usage": {"total_tokens": 3}}

        async def fake_impl(self, *a, **k):
            return sentinel

        monkeypatch.setattr(MC.ModelClient, "_generate_impl", fake_impl)
        c = MC.ModelClient(config={})
        out = asyncio.run(c.generate("deepseek", [{"role": "user", "content": "hi"}]))
        assert out is sentinel
        assert ulog.is_file(), "正常调用应记一行"

    def test_generate_wrapper_skips_on_probe(self, ulog, monkeypatch):
        async def fake_impl(self, *a, **k):
            return {"text": "", "model": "m", "error": None, "usage": {}}

        monkeypatch.setattr(MC.ModelClient, "_generate_impl", fake_impl)
        c = MC.ModelClient(config={})
        tok = MC.set_probe(True)
        try:
            asyncio.run(c.generate("deepseek", [{"role": "user", "content": "x"}]))
        finally:
            MC.reset_probe(tok)
        assert not ulog.exists()


class TestAggregation:
    def test_empty_log_is_honest(self, ulog):
        r = asyncio.run(US.run())
        assert r["success"] and r["calls"] == 0
        assert "没有用量记录" in r["output"]

    def test_missing_log_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMM_USAGE_LOG", str(tmp_path / "nope.jsonl"))
        r = asyncio.run(US.run())
        assert r["success"] and r["calls"] == 0 and r["log_exists"] is False

    def test_per_model_and_totals(self, ulog):
        _write(ulog, [_row("deepseek", pt=100, ct=50), _row("deepseek", pt=10, ct=5),
                      _row("ollama", pt=1, ct=1, ok=False, err="boom")])
        r = asyncio.run(US.run())
        assert r["calls"] == 3 and r["err"] == 1
        assert r["total_tokens"] == 150 + 15 + 2
        models = {m["name"]: m for m in r["models"]}
        assert models["deepseek"]["calls"] == 2 and models["deepseek"]["total"] == 165
        assert models["ollama"]["err"] == 1
        assert r["models"][0]["name"] == "deepseek", "按调用次数降序"

    def test_bad_lines_skipped_and_counted(self, ulog):
        ulog.write_text('{"ts":1,"model":"a","ok":true,"total_tokens":1}\n'
                        "这不是 json\n"
                        '{"ts":2,"model":"b","ok":true,"total_tokens":2}\n', encoding="utf-8")
        r = asyncio.run(US.run())
        assert r["calls"] == 2 and "2 行" not in r["output"] and "1 行" in r["output"]

    def test_days_filter(self, ulog):
        old = time.time() - 5 * 86400
        _write(ulog, [_row(ts=old), _row(ts=time.time())])
        r = asyncio.run(US.run(action="summary", days=2))
        assert r["calls"] == 1, r["output"][:200]
        r2 = asyncio.run(US.run())
        assert r2["calls"] == 2

    def test_today_action(self, ulog):
        _write(ulog, [_row(ts=time.time() - 5 * 86400), _row(ts=time.time())])
        r = asyncio.run(US.run(action="today"))
        assert r["calls"] == 1

    def test_avg_latency_rendered(self, ulog):
        _write(ulog, [_row(el=1.0), _row(el=3.0)])
        r = asyncio.run(US.run())
        assert r["avg_latency"] == 2.0 and "平均 2.0s" in r["output"]

    def test_no_price_invented(self, ulog):
        """★ 不许编单价 —— 输出里不能出现"元/美元/$"这类金额。"""
        _write(ulog, [_row()])
        r = asyncio.run(US.run())
        assert "说明: 只统计 token 与次数, 不含金额" in r["output"]
        for bad in ("美元", "￥", "$", "元)"):
            assert bad not in r["output"], f"输出里出现了金额暗示: {bad}"

    def test_unknown_action_honest(self, ulog):
        r = asyncio.run(US.run(action="wipe"))
        assert r.get("success") is False and "未知 action" in r.get("error", "")

    def test_read_only(self, ulog):
        """统计不该改动日志 (不轮转不清理)。"""
        _write(ulog, [_row(), _row()])
        before = ulog.read_bytes()
        asyncio.run(US.run())
        assert ulog.read_bytes() == before
