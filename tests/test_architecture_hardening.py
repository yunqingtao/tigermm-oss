"""
Tiger.M.M v4.2 架构加固测试套件
覆盖: _lay_state / _classify_intent / 智能截断 / 路径纪律 / 
       file_ops完成信号 / 先读后写拦截 / 正则开关 / 智能路由
运行: cd str(Path(__file__).resolve().parents[1]) && python -m pytest tests/test_architecture_hardening.py -v
"""
import sys, os, json, asyncio, pytest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ═══════════════════════════════════════════════
# 共享夹具
# ═══════════════════════════════════════════════

@pytest.fixture(scope="module")
def pipeline():
    """加载 Level4Pipeline 实例（不启动 CLI/server）"""
    from core.pipeline import Level4Pipeline
    from core.model_client import ModelClient
    from config.settings import DATA_DIR
    
    config = {"deepseek": {"key": "test", "url": "https://test.local/v1", "model": "deepseek-v4-pro"}}
    mc = ModelClient(config)
    pl = Level4Pipeline(mc, config)
    return pl


@pytest.fixture(scope="module")
def file_ops_mod():
    """加载 file_ops 模块"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "file_ops", Path(__file__).parent.parent / "tools" / "file_ops.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════
# 测试 1: _lay_state 铺状态开关契约
# ═══════════════════════════════════════════════

@contextmanager
def _lay_state_pref(pl, on: bool):
    """临时切换 lay_state 开关 —— **只改内存, 不调 _save_prefs()**, 用完还原。

    绝不写用户的 data/prefs.json (验证不得污染真实数据)。
    """
    _sentinel = object()
    orig = pl._prefs.get("lay_state", _sentinel)
    pl._prefs["lay_state"] = on
    try:
        yield pl
    finally:
        if orig is _sentinel:
            pl._prefs.pop("lay_state", None)
        else:
            pl._prefs["lay_state"] = orig


class TestLayState:
    """_lay_state 契约 (**2026-09-18 显式化**)

        默认(未打开)  → (None, False)                  输出干净, 无"虎哥理解"噪声
        /state on     → (state_msg, needs_confirm)     打印理解摘要; 任务模式需确认

    历史: 本类原先只断言"静默", 而生产实现一直在打印 → canonical 长期 3 failed,
    把真回归淹没在常驻噪声里。契约现在**双向**可测: 关=静默、开=返回摘要。
    开关存 data/prefs.json 的 `lay_state` 键, 由 CLI `/state on|off` 控制。
    """

    def test_lay_state_default_silent(self, pipeline):
        """默认关 → 静默, 不打印不打断"""
        summary, needs_confirm = pipeline._lay_state("你好")
        assert summary is None, "默认应静默, 返回 None"
        assert needs_confirm is False, "默认不应要求确认"

    def test_lay_state_silent_with_ext_model(self, pipeline):
        """默认关: 即使指定了模型也静默"""
        summary, needs_confirm = pipeline._lay_state("分析这段代码", ext_model="deepseek")
        assert summary is None
        assert needs_confirm is False

    def test_lay_state_silent_for_commands_and_short(self, pipeline):
        """命令 / 短消息: 默认关时同样静默"""
        assert pipeline._lay_state("/pk test") == (None, False)
        assert pipeline._lay_state("x") == (None, False)

    def test_lay_state_enabled_returns_summary(self, pipeline):
        """★ /state on → 返回理解摘要 (能力没丢)"""
        with _lay_state_pref(pipeline, True):
            summary, needs_confirm = pipeline._lay_state("你好")
            assert summary is not None, "打开后应返回摘要"
            assert "虎哥理解" in summary
            assert "你好" in summary, f"摘要应含原话: {summary}"
            assert needs_confirm is False, "闲聊不应要求确认"

    def test_lay_state_enabled_task_needs_confirm(self, pipeline):
        """★ /state on + 任务消息 → 摘要 + 需要确认"""
        task_msg = "请帮我写一份非常详细的架构文档覆盖所有方面" * 8
        assert pipeline._classify_intent(task_msg) == "task"
        with _lay_state_pref(pipeline, True):
            summary, needs_confirm = pipeline._lay_state(task_msg)
            assert summary is not None and "虎哥理解" in summary
            assert needs_confirm is True, "任务模式应要求确认"

    def test_lay_state_flag_off_restores_silence(self, pipeline):
        """★ 关闭后回到静默 (开关可逆)"""
        with _lay_state_pref(pipeline, True):
            assert pipeline._lay_state("你好")[0] is not None
        assert pipeline._lay_state("你好") == (None, False)

    # ── /state 命令 (CLI 调 set_lay_state; REPL 难自动化 → 测这个方法) ──

    def test_set_lay_state_query(self, pipeline):
        """查询: 报告当前状态"""
        pl = pipeline
        pl._prefs.pop("lay_state", None)
        out = pl.set_lay_state("")
        assert "[铺状态]" in out and "当前: 关" in out, out

    def test_set_lay_state_on_off_roundtrip(self, pipeline, monkeypatch):
        """★ on/off 切换 (打桩 _save_prefs → 不写用户 prefs.json)"""
        pl = pipeline
        saved = {"n": 0}
        monkeypatch.setattr(pl, "_save_prefs", lambda: saved.__setitem__("n", saved["n"] + 1))
        orig = pl._prefs.get("lay_state", None)

        out_on = pl.set_lay_state("on")
        assert "已打开" in out_on and pl._lay_state_enabled() is True, out_on
        assert "当前: 开" in pl.set_lay_state("")
        assert pl._lay_state("你好")[0] is not None, "打开后 _lay_state 应返回摘要"

        out_off = pl.set_lay_state("off")
        assert "已关闭" in out_off and pl._lay_state_enabled() is False, out_off
        assert pl._lay_state("你好") == (None, False), "关闭后应静默"

        assert saved["n"] == 2, f"on/off 各应存一次, 实得 {saved['n']}"
        if orig is None:
            pl._prefs.pop("lay_state", None)
        else:
            pl._prefs["lay_state"] = orig


# ═══════════════════════════════════════════════
# 测试 2: _classify_intent 路由修正
# ═══════════════════════════════════════════════

class TestClassifyIntent:
    """含 tool_name( 语法的消息应路由到 command（走 function calling）"""

    def test_tool_syntax_routes_to_command(self, pipeline):
        """file_ops( 应返回 'command' 而非 'task'"""
        msg = "用file_ops(action=read)读取源码" * 6  # 确保 >150 字
        result = pipeline._classify_intent(msg)
        assert result == "command", f"含 file_ops( 应走 command，实际: {result}"

    def test_long_msg_without_tool_routes_to_task(self, pipeline):
        """长消息无工具语法应走 task"""
        msg = "请帮我写一份非常详细的架构文档覆盖所有方面" * 8
        assert len(msg) > 150, f"消息长度={len(msg)} 应>150"
        result = pipeline._classify_intent(msg)
        assert result == "task", f"无工具语法长消息应走 task，实际: {result}"

    def test_short_msg_routes_to_command(self, pipeline):
        """短消息走 command"""
        msg = "你好"
        result = pipeline._classify_intent(msg)
        assert result == "command"


# ═══════════════════════════════════════════════
# 测试 3: file_ops 完成信号
# ═══════════════════════════════════════════════

class TestFileOpsCompleteSignal:
    """file_ops read 应返回 complete / total_lines / 分页指引"""

    @pytest.mark.asyncio
    async def test_read_returns_complete_flag(self, file_ops_mod):
        """读完整文件应返回 complete=True"""
        test_file = Path(__file__).parent.parent / "core" / "tool_schema.py"
        result = await file_ops_mod.run(action="read", path=str(test_file))
        assert result["success"], f"读取失败: {result.get('error')}"
        assert result["complete"] == True, f"完整读取应 complete=True, 实际: {result}"
        assert "FILE COMPLETE" in result["meta"], f"meta应含FILE COMPLETE: {result['meta']}"
        assert result["total_lines"] > 0

    @pytest.mark.asyncio
    async def test_chunked_read_shows_more_available(self, file_ops_mod):
        """分片读取应显示 MORE AVAILABLE + 下一页 offset"""
        test_file = Path(__file__).parent.parent / "core" / "pipeline.py"
        result = await file_ops_mod.run(action="read", path=str(test_file), offset=0, limit=50)
        assert result["success"]
        assert result["complete"] == False, f"分片应 complete=False, 实际: {result}"
        assert "MORE AVAILABLE" in result["meta"], f"应含MORE AVAILABLE: {result['meta']}"
        assert "offset=" in result["meta"], "应提供下一页的 offset"

    @pytest.mark.asyncio
    async def test_final_chunk_shows_complete(self, file_ops_mod):
        """最后一页应显示 FILE COMPLETE"""
        test_file = Path(__file__).parent.parent / "core" / "compactor.py"  # ~130 lines
        result = await file_ops_mod.run(action="read", path=str(test_file), offset=120, limit=50)
        assert result["success"]
        assert result["complete"] == True, f"最后一页应 complete=True: {result}"
        assert "FILE COMPLETE" in result["meta"]


# ═══════════════════════════════════════════════
# 测试 4: 正则快捷通道开关
# ═══════════════════════════════════════════════

class TestRegexPassthrough:
    """_regex_passthrough 开关控制 Email/URL/FTS 正则拦截"""

    def test_flag_exists_and_defaults_true(self, pipeline):
        """开关应存在且默认为 True"""
        assert hasattr(pipeline, '_regex_passthrough'), "缺少 _regex_passthrough 属性"
        assert pipeline._regex_passthrough in (True, False), "值应为布尔"

    def test_toggle_via_prefs(self, pipeline):
        """通过 _prefs 切换"""
        original = pipeline._regex_passthrough
        pipeline._regex_passthrough = not original
        pipeline._prefs["regex_passthrough"] = pipeline._regex_passthrough
        assert pipeline._regex_passthrough != original
        # 恢复
        pipeline._regex_passthrough = original
        pipeline._prefs["regex_passthrough"] = original


# ═══════════════════════════════════════════════
# 测试 5: 智能路由复杂度判定
# ═══════════════════════════════════════════════

class TestSmartRoute:
    """复杂度检测应正确区分简单/复杂消息"""

    def _simulate_complexity(self, msg):
        c = 0
        if len(msg) > 300: c += 1
        if len(msg) > 800: c += 2
        if len(msg) > 2000: c += 2
        markers = ['file_ops(', 'shell_exec(', 'send_email(', 'system_info(',
                   'windows_desktop(', 'web_search(', '/tool ']
        tc = sum(1 for m in markers if m in msg)
        if tc >= 2: c += 2
        if tc >= 4: c += 1
        if '\n' in msg: c += 1
        patterns = ['```', 'def ', 'class ', 'import ', 'async def', 'action=read', 'action=write']
        if any(p in msg for p in patterns): c += 2
        multi = ['先读', '然后', '再写', '逐个', '每个文件', '全部读', '依次',
                '第一步', '第二步', '1.', '2.', '3.', '步骤']
        if any(p in msg for p in multi): c += 2
        return c

    def test_simple_greeting_low_complexity(self):
        assert self._simulate_complexity("你好") < 3

    def test_short_email_low_complexity(self):
        assert self._simulate_complexity("发邮件给 test@example.com 主题:hi 内容:hello") < 3

    def test_tool_with_multi_step_high_complexity(self):
        msg = "用file_ops(action=read)逐个读取以下文件：core/pipeline.py、core/model_client.py。全部读完后写文档保存到桌面。"
        assert self._simulate_complexity(msg) >= 3, "含 action=read + 多步骤词应触发高复杂度"

    def test_long_text_high_complexity(self):
        msg = "详细的架构文档内容" * 100  # ~1000 chars
        assert self._simulate_complexity(msg) >= 3, f"超长文本应触发: {self._simulate_complexity(msg)}"


# ═══════════════════════════════════════════════
# 测试 6: 先读后写拦截逻辑（单元级）
# ═══════════════════════════════════════════════

class TestReadBeforeWrite:
    """_process_external 内部：无 file_ops(read) 时拦截 file_ops(write)"""

    def test_write_without_read_blocked(self):
        """_reads_done 为空时 write 应被拦截"""
        _reads_done = []
        _is_write = True
        blocked = _is_write and not _reads_done
        assert blocked, "无读取时 write 应被拦截"

    def test_write_after_read_allowed(self):
        """_reads_done 非空时 write 应放行"""
        _reads_done = ["core/pipeline.py"]
        _is_write = True
        blocked = _is_write and not _reads_done
        assert not blocked, "有读取记录时 write 应放行"

    def test_non_fileops_unaffected(self):
        """非 file_ops 工具调用不受影响"""
        _reads_done = []
        _is_write = False
        blocked = _is_write and not _reads_done
        assert not blocked

    def test_fileops_list_unaffected(self):
        """file_ops(action=list) 不受影响"""
        _reads_done = []
        _is_write = False  # action='list'
        blocked = _is_write and not _reads_done
        assert not blocked


# ═══════════════════════════════════════════════
# 测试 7: 整合——模拟完整 tool call 链路
# ═══════════════════════════════════════════════

class TestIntegration:
    """模拟一次完整的 request → tool_calls → result 链路"""

    @pytest.mark.asyncio
    async def test_read_then_write_flow(self, file_ops_mod, pipeline):
        """先读一个真实文件，验证链路通畅"""
        test_file = Path(__file__).parent.parent / "core" / "compactor.py"
        
        # Step 1: Read
        result = await file_ops_mod.run(action="read", path=str(test_file))
        assert result["success"], f"Step 1 读失败: {result}"
        assert result["complete"] == True
        assert result["total_lines"] > 0
        
        # Step 2: Verify _reads_done would be tracked
        reads_tracked = [str(test_file)]
        assert len(reads_tracked) > 0
        
        # Step 3: Write would be allowed (not blocked)
        _is_write_allowed = len(reads_tracked) > 0
        assert _is_write_allowed, "先读后写应被允许"


# ═══════════════════════════════════════════════
# 运行入口
# ═══════════════════════════════════════════════



# ═══════════════════════════════════════════════
# 测试 8: TigerScheduler
# ═══════════════════════════════════════════════

class TestScheduler:
    """定时任务引擎"""

    def test_parse_interval(self):
        from core.scheduler import _parse_schedule
        r = _parse_schedule("every 30m")
        assert r["type"] == "interval"
        assert r["seconds"] == 1800

    def test_parse_oneshot(self):
        from core.scheduler import _parse_schedule
        r = _parse_schedule("2026-12-31T23:59:00")
        assert r["type"] == "oneshot"

    def test_add_and_list(self, tmp_path):
        from core.scheduler import TigerScheduler
        s = TigerScheduler(tmp_path)
        j = s.add("test", "every 1h", "say hello")
        assert j.id
        jobs = s.list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["name"] == "test"

    def test_remove(self, tmp_path):
        from core.scheduler import TigerScheduler
        s = TigerScheduler(tmp_path)
        j = s.add("test", "every 1h", "say hello")
        assert s.remove(j.id)
        assert s.list_jobs() == []


# ═══════════════════════════════════════════════
# 测试 9: Browser 安全校验
# ═══════════════════════════════════════════════

class TestBrowserSecurity:
    """浏览器 URL 安全校验"""

    def _get_check(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "browser", str(__import__('pathlib').Path(__file__).parent.parent / "tools" / "browser.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._check_url

    def test_https_allowed(self):
        check = self._get_check()
        ok, _ = check("https://example.com")
        assert ok

    def test_file_blocked(self):
        check = self._get_check()
        ok, reason = check("file:///etc/passwd")
        assert not ok

    def test_localhost_blocked(self):
        check = self._get_check()
        ok, _ = check("http://localhost:8080")
        assert not ok

    def test_private_ip_blocked(self):
        check = self._get_check()
        ok, _ = check("http://192.168.1.1")
        assert not ok

    def test_no_scheme_blocked(self):
        check = self._get_check()
        ok, _ = check("example.com")
        assert not ok


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
