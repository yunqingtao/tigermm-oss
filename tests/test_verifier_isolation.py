"""验证器隔离 — 测试 (2026-09-20 自查事故后加)

事故: `verify_learn_loop.py` 有 5 处 `process(probe=False)` (学习闭环要在真实路径下测),
单独跑时没有隔离变量 → **测试消息真写进了用户的对话历史**
(实测: 一次测量运行写进 16 条, 已按铁律清理: 停引擎→备份→列条目→删+同步计数→rebuild FTS→复检)。
另: 门禁里若有验证器走真实模型调用, 记账会写进用户的 data/model_usage.jsonl,
把"真实用量"顶高 (实测日志出现成组 ollama 行, 时间正落在门禁窗口)。

本文件锁住两条不变量 (静态 + 行为各一层):
  ① 门禁 runner 必须给子进程注入 TMM_USAGE_LOG (用量隔离)
  ② 会走 probe=False 真实路径的验证器, 单跑时必须自我隔离 TMM_SESSION_DB
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent


class TestGateEnvIsolation:
    def test_gate_env_sets_usage_log(self):
        """★ 行为层: _gate_env() 必须含 TMM_USAGE_LOG (指向临时文件)。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("hv", ROOT / "scripts" / "hermes_verify.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        env = m._gate_env()
        for k in ("TMM_SESSION_DB", "TMM_GAP_DB", "TMM_PERCEPTION_FILE", "TMM_USAGE_LOG"):
            assert env.get(k), f"门禁环境缺 {k}"
            assert "tmm_gate_" in env[k], f"{k} 没指向临时沙箱: {env[k]}"
        assert env["TMM_USAGE_LOG"].endswith(".jsonl")

    def test_gate_env_does_not_leak_real_paths(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hv2", ROOT / "scripts" / "hermes_verify.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        env = m._gate_env()
        real_data = str(ROOT / "data")
        for k in ("TMM_SESSION_DB", "TMM_GAP_DB", "TMM_PERCEPTION_FILE", "TMM_USAGE_LOG"):
            assert real_data not in env[k], f"{k} 指向了真实 data/: {env[k]}"


class TestStandaloneVerifiersSelfIsolate:
    REAL_PATH_USERS = ["verify_learn_loop.py"]

    def test_real_path_verifiers_self_isolate(self):
        """★ 走 probe=False 真实路径的验证器, 单跑必须自我隔离会话库。
        (不隔离 = 测试消息写进用户对话历史, 已实际发生过一次)"""
        for name in self.REAL_PATH_USERS:
            src = (ROOT / "scripts" / "verification" / name).read_text(encoding="utf-8")
            assert 'os.environ["TMM_SESSION_DB"]' in src or "os.environ['TMM_SESSION_DB']" in src, \
                f"{name} 没有自我隔离 TMM_SESSION_DB → 单跑会污染用户对话库"
            assert "TMM_USAGE_LOG" in src, f"{name} 没隔离用量日志"
            # 必须是"只在未设时才设" (不能覆盖门禁给的隔离值)
            assert "if not _os.environ.get(\"TMM_SESSION_DB\")" in src

    def test_learn_loop_uses_probe_false_on_purpose(self):
        """锁住意图: 这 5 处 probe=False 是**故意的** (学习闭环必须在真实路径下测),
        所以要做的是隔离, 不是把它们改成 probe=True。"""
        src = (ROOT / "scripts" / "verification" / "verify_learn_loop.py").read_text(encoding="utf-8")
        assert src.count("probe=False") >= 3, "probe=False 的用例被误删了?"
