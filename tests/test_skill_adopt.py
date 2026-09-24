"""技能"取形式重编" (core/skill_adopt.py) 的测试。

守四组:
  A ★★ 抽象化 —— 外部实现细节 (URL/路径/代码/工具名/安装命令) 必须被剥掉
  B 动作序列 —— 只认抽象动作词, 且按原文出现顺序排 (保留编排形式)
  C ★ 安全闸 —— 许可证不合格 (copyleft/无) → blocked, 不采用
  D 描述渲染 —— 交给工厂的描述里**不得**夹带外部工具名/URL/路径
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.skill_adopt import (ACTION_WORDS, AdoptPlan, abstract_actions, assess_external,
                             describe_form, parse_skill_md, render_plan)

# 真实形态的外部技能文本样本 (模拟别人仓库里的 SKILL.md)
EXTERNAL = """# Web Report Generator

Fetch data from an API endpoint, summarize it with an LLM, and write a report.

## Steps
1. fetch: requests.get('https://api.example.com/v1/data') -> save to D:/work/data.json
2. parse: extract fields from the JSON payload
3. summarize: prompt the model to write a summary
4. write: save the result to ./output/report.md

## Install
pip install requests pandas

## Script
```python
import requests, pandas as pd
df = pd.read_csv('https://cdn.example.com/sales.csv')
open('C:/tmp/out.csv','w').write(df.to_csv())
```

See also: https://github.com/someone/web-report for docs.
"""


# ───────────────────── A. ★★ 抽象化 ─────────────────────

class TestAbstract:
    def test_urls_stripped(self):
        acts, cap = abstract_actions(EXTERNAL)
        joined = " ".join(acts) + " " + cap
        assert "http" not in joined.lower()
        assert "api.example.com" not in joined

    def test_paths_stripped(self):
        _, cap = abstract_actions(EXTERNAL)
        assert "D:/work" not in cap and "C:/tmp" not in cap and "./output" not in cap

    def test_capability_has_no_code(self):
        _, cap = abstract_actions(EXTERNAL)
        assert "```" not in cap and "import " not in cap

    def test_capability_reasonable(self):
        _, cap = abstract_actions(EXTERNAL)
        assert cap and len(cap) <= 120, cap

    def test_strip_helper_removes_install(self):
        from core.skill_adopt import _strip_externals
        s = _strip_externals("先 pip install requests 再跑 requests.get('http://x')")
        assert "pip install" not in s and "http://x" not in s

    def test_file_names_stripped(self):
        from core.skill_adopt import _strip_externals
        s = _strip_externals("读 data.json 写 out.py")
        assert "data.json" not in s and "out.py" not in s

    def test_empty_input_safe(self):
        acts, cap = abstract_actions("")
        assert acts == [] and cap == ""


# ───────────────────── B. 动作序列 ─────────────────────

class TestActions:
    def test_recognizes_core_actions(self):
        acts, _ = abstract_actions(EXTERNAL)
        for want in ("fetch", "parse", "summarize", "write"):
            assert want in acts, f"缺 {want}: {acts}"

    def test_order_follows_source(self):
        """★ 步骤顺序 = 它的编排形式, 必须按原文出现顺序保留。"""
        acts, _ = abstract_actions(EXTERNAL)
        assert acts.index("fetch") < acts.index("summarize") < acts.index("write"), acts

    def test_only_abstract_words(self):
        acts, _ = abstract_actions(EXTERNAL)
        assert set(acts) <= set(ACTION_WORDS), acts

    def test_dedup(self):
        acts, _ = abstract_actions("search 一次, 再 search 一次, 然后 write")
        assert acts.count("search") == 1

    def test_limit(self):
        acts, _ = abstract_actions(" ".join(ACTION_WORDS.keys()) * 3, limit=3)
        assert len(acts) <= 3

    def test_chinese_actions_recognized(self):
        acts, _ = abstract_actions("先搜索资料, 再生成提纲, 最后写入文件")
        assert "search" in acts and "generate" in acts and "write" in acts, acts

    def test_no_action_no_crash(self):
        acts, cap = abstract_actions("这是一段没有任何动作词的说明文字而已")
        assert acts == [] and isinstance(cap, str)

    def test_pure(self):
        assert abstract_actions(EXTERNAL) == abstract_actions(EXTERNAL)


# ───────────────────── C. ★ 安全闸 ─────────────────────

class TestAssess:
    def _meta(self, **kw):
        m = {"name": "web-report", "license": "MIT", "source": "github",
             "text": EXTERNAL}
        m.update(kw)
        return m

    def test_mit_allowed(self):
        p = assess_external(self._meta())
        assert not p.blocked and p.license_class == "ok", p

    def test_no_license_blocked(self):
        p = assess_external(self._meta(license=""))
        assert p.blocked and "不采用" in " ".join(p.reasons), p

    def test_copyleft_blocked(self):
        """copyleft: 连形式也不做 (避免把 GPL 的编排当成"我们的"实现)。"""
        p = assess_external(self._meta(license="GPL-3.0"))
        assert p.blocked, p

    def test_unknown_license_blocked(self):
        p = assess_external(self._meta(license="Weird Custom 1.0"))
        assert p.blocked

    def test_archived_is_warning_not_block(self):
        p = assess_external(self._meta(archived=True))
        assert not p.blocked and any("archived" in w for w in p.warnings), p

    def test_typosquat_warned(self):
        # ★ 注意: "requests" 本身就是知名包 → 不是混淆; 要真的近似名才该预警
        p = assess_external(self._meta(name="requestz"))
        assert any("近似" in w for w in p.warnings), p

    def test_exact_popular_name_not_warned(self):
        p = assess_external(self._meta(name="requests"))
        assert not any("近似" in w for w in p.warnings), p

    def test_thin_material_warned(self):
        p = assess_external(self._meta(text="什么都没说"))
        assert any("没从素材里识别出" in w for w in p.warnings), p

    def test_plan_carries_form(self):
        p = assess_external(self._meta())
        assert p.steps_hint and p.capability and not p.blocked

    def test_pure(self):
        m = self._meta()
        a, b = assess_external(m), assess_external(m)
        assert (a.blocked, a.steps_hint, a.capability) == (b.blocked, b.steps_hint, b.capability)


# ───────────────────── D. 描述渲染 ─────────────────────

class TestDescribe:
    def test_description_has_no_external_details(self):
        """★★ 交给工厂的描述里不得出现外部 URL/路径/工具名 —— 否则就是把别人的带进来。"""
        p = assess_external({"name": "web-report", "license": "MIT", "text": EXTERNAL})
        d = describe_form(p.capability, p.steps_hint)
        low = d.lower()
        for bad in ("http", "requests.get", "api.example", "d:/work", "./output",
                    "pip install", "```"):
            assert bad not in low, f"描述里混进了外部细节: {bad}\n{d}"

    def test_description_has_actions(self):
        p = assess_external({"name": "x", "license": "MIT", "text": EXTERNAL})
        d = describe_form(p.capability, p.steps_hint)
        assert "fetch" in d and "write" in d, d

    def test_description_asks_for_our_style(self):
        """要明确要求"我们自己的风格" —— 触发词具体/参数非必填/路径规范。"""
        d = describe_form("做点事", ["search", "write"])
        assert "我们自己风格的" in d or "我们自己" in d
        assert "触发词" in d and "非必填" in d and "$params.path" in d

    def test_empty_ok(self):
        assert isinstance(describe_form("", []), str)

    def test_pure(self):
        assert describe_form("a", ["b"]) == describe_form("a", ["b"])

    def test_render_plan_blocked_shows_reason(self):
        p = assess_external({"name": "x", "license": "", "text": EXTERNAL})
        txt = render_plan(p)
        assert "不采用" in txt and "许可证" in txt

    def test_render_plan_ok_shows_next_step(self):
        p = assess_external({"name": "x", "license": "MIT", "text": EXTERNAL})
        txt = render_plan(p)
        assert "可重编" in txt and "步骤形式" in txt and "skill-adopt" in txt

# ───────────────────── E. 能力提取的噪声过滤 (实测驱动) ─────────────────────

class TestCapabilityNoise:
    """★ 实测来由: 拿真实仓库跑, README 第一行是徽章 `[![xxx](url)`,
    提取结果变成 "[![<file>](<url>" —— 垃圾。必须显式跳过图片/徽章/链接行。"""

    BADGE_README = """[![Build](https://img.shields.io/badge/build-passing.svg)](https://ci.example.com)
[![License: MIT](https://img.shields.io/badge/license-MIT.svg)](https://opensource.org)

# Awesome Tools

A toolkit that searches web pages and writes a summary report.
"""

    def test_badge_not_picked_as_capability(self):
        _, cap = abstract_actions(self.BADGE_README)
        assert "![" not in cap and "](<url>)" not in cap, cap
        assert "shields.io" not in cap

    def test_real_description_picked(self):
        _, cap = abstract_actions(self.BADGE_README)
        assert "toolkit" in cap.lower() or "summary" in cap.lower(), cap

    def test_link_only_line_skipped(self):
        _, cap = abstract_actions("[docs](<url>)\n\n其实就是把网页抓下来写成报告。")
        assert "其实就是把网页抓下来写成报告" in cap, cap

    def test_summary_preferred_over_readme(self):
        """仓库简介比 README 首行可靠 → 优先用简介当能力描述。"""
        p = assess_external({"name": "x", "license": "MIT",
                             "summary": "搜索网页并生成摘要报告",
                             "text": self.BADGE_README})
        assert p.capability == "搜索网页并生成摘要报告", p.capability

    def test_actions_from_summary_when_readme_empty(self):
        """README 是空壳时, 简介里的动作词也要能抽出来。"""
        p = assess_external({"name": "x", "license": "MIT",
                             "summary": "search the web then write a report", "text": ""})
        assert "search" in p.steps_hint and "write" in p.steps_hint, p.steps_hint

    def test_summary_shown_when_readme_differs(self):
        p = assess_external({"name": "x", "license": "MIT",
                             "summary": "完全不同的简介内容在这里",
                             "text": self.BADGE_README})
        assert p.capability == "完全不同的简介内容在这里"

# ───────────────────── F. 真 SKILL.md 解析 (最准的一档) ─────────────────────

SKILL_MD = """---
name: web-summary
description: "抓取网页正文并生成摘要归档成 Word"
tags: [网页摘要, 抓取, 归档]
triggers: [摘要这个网页, 把文章存下来]
platforms: [windows, linux]
requires_tools: [firecrawl, llm, tiger_office]
params:
  url:
    type: text
    required: false
steps:
  - id: fetch
    tool: firecrawl
    input:
      url: $message
  - id: sum
    tool: llm
    input:
      prompt: 摘要 $steps.fetch.text
  - id: save
    tool: tiger_office
    input:
      content: $steps.sum.text
---

# web-summary

抓网页 → 摘要 → 存 Word。
"""


class TestParseSkillMd:
    def test_detects_skill_md(self):
        r = parse_skill_md(SKILL_MD)
        assert r["is_skill_md"] is True, r

    def test_description_extracted(self):
        r = parse_skill_md(SKILL_MD)
        assert "抓取网页正文" in r["description"], r

    def test_tools_extracted(self):
        r = parse_skill_md(SKILL_MD)
        assert r["tools"][:3] == ["firecrawl", "llm", "tiger_office"], r["tools"]

    def test_steps_abstracted_from_tools(self):
        """★ 步骤 = 抽象动作 (由工具名映射), 不保留外部工具名。"""
        r = parse_skill_md(SKILL_MD)
        assert len(r["steps"]) == 3, r["steps"]
        assert all(s in ACTION_WORDS or s == "llm" for s in r["steps"]), r["steps"]

    def test_tags_extracted(self):
        r = parse_skill_md(SKILL_MD)
        assert "网页摘要" in r["tags"], r["tags"]

    def test_non_skill_md(self):
        assert parse_skill_md("# 就是个 README\n\n没 frontmatter").get("is_skill_md") is False

    def test_empty(self):
        assert parse_skill_md("")["is_skill_md"] is False

    def test_assess_prefers_skill_md(self):
        """有真 SKILL.md → 用它当形式 (而不是 README 抽词)。"""
        p = assess_external({"name": "x", "license": "MIT", "text": SKILL_MD,
                             "summary": "外部简介", "paths_found": ["skills/x/SKILL.md"]})
        assert p.capability == "抓取网页正文并生成摘要归档成 Word", p.capability
        assert len(p.steps_hint) == 3, p.steps_hint
        assert any("SKILL.md" in w for w in p.warnings), p.warnings

    def test_parse_is_pure(self):
        assert parse_skill_md(SKILL_MD) == parse_skill_md(SKILL_MD)
