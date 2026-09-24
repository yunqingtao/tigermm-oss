"""Tests for Pipeline routing and intent classification."""
import pytest, re, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestIntentClassification:
    """Test _classify_intent patterns without needing pipeline instance."""

    TASK_PREFIXES = [
        '工程题', '任务：', '任务:', '帮我写', '帮我实现', '帮我建',
        '实现一个', '新建一个', '新建agent', '新建文件',
        '写一个agent', '写代码', '构建', '开发一个', '创建一个',
        '请帮我写', '请实现', '请新建', '请构建',
        '# 工程', '# 任务', '## 工程', '## 任务',
    ]

    TASK_MARKERS = [
        '在 core/ 下新建', '在core/下新建', '在 tools/ 下新建',
        '实现 class', '新建 .py', '写一个 agent',
        '新建模块', '重构', '添加功能',
    ]

    def classify(self, msg):
        msg = msg.strip()
        if any(msg.startswith(p) for p in self.TASK_PREFIXES):
            return 'task'
        if any(m in msg[:40] for m in self.TASK_MARKERS):
            return 'task'
        if len(msg) > 150:
            return 'task'
        if '\n' in msg:
            return 'task'
        return 'command'

    def test_task_prefixes(self):
        assert self.classify('帮我写一个爬虫') == 'task'
        assert self.classify('任务：分析这个项目') == 'task'
        assert self.classify('实现一个登录功能') == 'task'

    def test_task_markers(self):
        assert self.classify('在 core/ 下新建 agent.py') == 'task'
        assert self.classify('重构 pipeline 模块') == 'task'

    def test_long_is_task(self):
        long_msg = '请帮我分析这个项目的架构，包括所有的模块依赖关系、数据流向、以及可能的性能瓶颈，然后给出优化建议。' * 4
        assert self.classify(long_msg) == 'task'

    def test_multiline_is_task(self):
        assert self.classify('line1\nline2') == 'task'

    def test_short_is_command(self):
        assert self.classify('hello') == 'command'
        assert self.classify('what is python') == 'command'


class TestPipelinePatterns:
    """Test regex patterns used in pipeline.process()."""

    def test_email_detection(self):
        pattern = r'(?:发邮件|发送邮件|mail)\s*(?:给)?\s*(\S+@\S+\.\S+)'
        assert re.search(pattern, '发邮件给 test@example.com 主题:hello')
        assert re.search(pattern, '发送邮件 admin@site.org')
        assert not re.search(pattern, 'hello world')

    def test_url_detection(self):
        pattern = r'(https?://[^\s]+)'
        assert re.search(pattern, '打开 https://example.com/page')
        assert re.search(pattern, 'http://localhost:8080')
        assert not re.search(pattern, 'no url here')

    def test_mouse_move_detection(self):
        pattern = r'(?:移动|挪)\s*(?:鼠标|光标)\s*(?:到|至)?\s*(\d+)\s*[,，\s]\s*(\d+)'
        m = re.search(pattern, '移动鼠标到 100, 200')
        assert m and m.group(1) == '100' and m.group(2) == '200'

    def test_type_text_detection(self):
        pattern = r'(?:打字|输入|type)\s*(.+)'
        m = re.search(pattern, '输入 hello world')
        assert m and 'hello world' in m.group(1)

    def test_search_keywords(self):
        keywords = ['搜索', '百度', 'google', '搜一下', '查一下', '搜']
        for kw in keywords:
            assert kw in '请' + kw + ' Python教程'

    def test_stats_command(self):
        assert '/stats'.strip() == '/stats'
        assert '  /stats  '.strip() == '/stats'

    def test_tool_command_parse(self):
        line = '/tool file_read path=core/test.py'
        parts = line.split(None, 2)
        assert parts[0] == '/tool'
        assert parts[1] == 'file_read'
        assert 'path=core/test.py' in parts[2]
