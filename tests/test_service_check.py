"""tools/service_check.py — 服务/端口存活检查测试 (2026-09-20)

要点:
  ★ 真起一个本地监听 socket → 必须报"通"; 关掉的端口 → 必须报"不通"
    (不测真 8800/11434: 那要求引擎在跑, 会把"引擎没开"误判成工具坏)
  ★ 不通时**不许卡住** (超时压短; 这是它存在的意义之一)
  ★ 只读: 跑完不留文件, 不动配置
  ★ extra 参数能临时加端口, 且去重 (同端口不重复探)
"""
import asyncio
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import service_check as SC


def _free_port():
    """拿一个当前没人用的端口号。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def listener():
    """起一个真监听 socket (不 accept, 只用来让 connect_ex 成功)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    yield s.getsockname()[1]
    s.close()


class TestTcpProbe:
    def test_listening_port_reports_up(self, listener):
        ok, ms, err = SC.tcp_probe(listener)
        assert ok is True and ms is not None and ms >= 0, (ok, ms, err)

    def test_closed_port_reports_down(self):
        p = _free_port()
        ok, ms, err = SC.tcp_probe(p)
        assert ok is False and ms is None and err

    def test_does_not_hang_on_blackhole(self):
        """★ 打不通的端口必须快速返回 (超时压短), 否则一轮对话被卡死。"""
        p = _free_port()
        t0 = time.time()
        SC.tcp_probe(p, timeout=0.3)
        assert time.time() - t0 < 3, "探测超时没生效"


class TestRunOutput:
    def test_ports_action_reports_real_listener(self, listener):
        r = asyncio.run(SC.run(action="ports", extra=f"测试服务:{listener}"))
        assert r["success"]
        mine = [s for s in r["services"] if s["port"] == listener]
        assert mine and mine[0]["up"] is True, r["output"]
        assert "✓" in r["output"] and "测试服务" in r["output"]

    def test_closed_port_shown_as_down(self):
        p = _free_port()
        r = asyncio.run(SC.run(action="ports", extra=f"空端口:{p}"))
        assert r["success"] and r["down"] >= 1
        assert "✗" in r["output"]

    def test_extra_ports_deduped(self, listener):
        r = asyncio.run(SC.run(action="ports", extra=f"甲:{listener},乙:{listener}"))
        ports = [s["port"] for s in r["services"]]
        assert ports.count(listener) == 1, "同端口应去重"

    def test_extra_bad_format_ignored(self, listener):
        r = asyncio.run(SC.run(action="ports", extra=f"坏的:abc,好的:{listener}"))
        assert r["success"]
        assert any(s["port"] == listener and s["up"] for s in r["services"])

    def test_port_without_name_accepted(self, listener):
        r = asyncio.run(SC.run(action="ports", extra=str(listener)))
        assert r["success"] and any(s["port"] == listener for s in r["services"])

    def test_default_services_always_checked(self):
        """默认必须含 8800/11434 —— 那是 TMM 的两个命根子服务。"""
        r = asyncio.run(SC.run(action="ports"))
        ports = {s["port"] for s in r["services"]}
        assert {8800, 11434} <= ports, ports

    def test_unknown_action_honest(self):
        r = asyncio.run(SC.run(action="restart_everything"))
        assert r.get("success") is False and "未知 action" in r.get("error", "")

    def test_timeout_clamped(self):
        r = asyncio.run(SC.run(action="ports", timeout=999))
        assert r["success"]          # 不该因为超大超时值炸掉


class TestSideEffects:
    def test_no_files_written(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        before = set(p.name for p in tmp_path.iterdir())
        asyncio.run(SC.run(action="ports"))
        assert set(p.name for p in tmp_path.iterdir()) == before


class TestHttpDigest:
    def test_tmm_digest_renders_uptime(self):
        txt = SC._svc_digest("TMM 引擎", 8800, "/stats", {"uptime": 600, "errors": 0})
        assert "10 分钟" in txt and "错误 0" in txt

    def test_ollama_digest_counts_models(self):
        txt = SC._svc_digest("ollama", 11434, "/api/tags", {"models": [1, 2, 3]})
        assert "3 个模型" in txt

    def test_unknown_shape_falls_back(self):
        assert SC._svc_digest("别的", 1, "/x", {"weird": 1}) == "(有响应)"
