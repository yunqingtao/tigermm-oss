"""技能加载的稳健性 — 测试 (2026-09-20)

对照 OpenClaw(WorkBuddy) 补技能时**真踩出来的**四类坑, 都是"静默/误导"型:

  ① 漏写 frontmatter 结束的 `---` → 技能被**静默跳过**, 作者以为生效了
  ② YAML 语法错 (中文里带 ASCII ": " 就会炸, 如 `只做三件事: 认同哪些`) →
     旧代码吞掉异常只留 fm={} → 日志报"无有效 frontmatter", **真因被掩盖**
     (我因此先怀疑 CRLF, 白查一轮)
  ③ CRLF 换行 → 必须能正常解析 (Windows 上用记事本改一下就是 CRLF)
  ④ 触发词与自然说法差一个字 (我写"给电脑做体检", 用户说"给电脑做个体检")
     → 技能永远接不到; 这类只能靠"自然说法实测"发现 (见 verify_skill_gaps.py)

本文件守 ①②③ (纯加载层), ④ 由留档验证器覆盖。
"""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.skill_loader import SkillWorkflowEngine

GOOD = """---
name: demo-skill
description: "演示技能"
tags: [演示]
triggers: [演示一下]
params:
  path:
    type: path
    required: false
    desc: 位置
steps:
  - id: s1
    tool: shell_exec
    input:
      command: echo hi
---
# demo
"""

NO_CLOSE = GOOD.rsplit("---", 2)[0]                      # 去掉结尾的 ---
YAML_BAD = GOOD.replace('description: "演示技能"',
                        "description: 演示技能, 只做三件事: 认同哪些、反对哪些")
CRLF = GOOD.replace("\n", "\r\n")
BOM = "\ufeff" + GOOD


def _eng(tmp_path, text, name="demo-skill"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(text.encode("utf-8"))
    e = SkillWorkflowEngine(skills_dir=tmp_path)
    e.scan()
    return e


class TestFrontmatterRobustness:
    def test_good_skill_loads(self, tmp_path):
        e = _eng(tmp_path, GOOD)
        assert "demo-skill" in e.get_index()
        assert e.get_dag("demo-skill").steps

    def test_crlf_loads(self, tmp_path):
        """★ Windows 记事本改过的技能是 CRLF —— 必须照常加载。"""
        e = _eng(tmp_path, CRLF)
        assert "demo-skill" in e.get_index(), "CRLF 的 SKILL.md 被丢弃了"

    def test_no_closing_marker_skipped(self, tmp_path):
        """★ 漏写结束 --- → 跳过 (这是"静默丢弃"的主因, 跳过本身正确)。"""
        e = _eng(tmp_path, NO_CLOSE)
        assert "demo-skill" not in e.get_index()

    def test_bom_does_not_crash(self, tmp_path):
        """带 BOM 不该把扫描搞崩 (Windows 编辑器常加 BOM)。"""
        e = _eng(tmp_path, BOM)
        assert isinstance(e.get_index(), dict)


class TestEofAfterClosingMarker:
    """★ 文件**正好以 --- 结束** (后面没有换行/正文) 也必须能加载。

    实测踩到: 照 OpenClaw 风格补的 5 个技能全是"frontmatter 写完就到底"
    → `---` 个数=2、YAML 也合法, 但旧正则要求结束标记后必须有换行+正文
    → 5 个技能**全部被静默丢弃** (报"无有效 frontmatter", 看不出问题在哪)。
    """

    def test_file_ending_at_marker_loads(self, tmp_path):
        text = GOOD.rstrip("\n")
        text = text[:text.rindex("---") + 3]
        assert text.endswith("---") and not text.endswith("\n")
        e = _eng(tmp_path, text)
        assert "demo-skill" in e.get_index(), "以 --- 结尾的技能被丢弃了"
        assert e.get_dag("demo-skill").steps

    def test_no_body_but_trailing_newline_loads(self, tmp_path):
        text = GOOD[:GOOD.rindex("---") + 3] + "\n"
        assert "demo-skill" in _eng(tmp_path, text).get_index()

    def test_body_still_parsed_when_present(self, tmp_path):
        idx = _eng(tmp_path, GOOD).get_index()["demo-skill"]
        assert "demo" in (idx.body or ""), "有正文时应正常取到正文"


class TestYamlErrorIsDiagnosed:
    def test_yaml_error_logs_real_reason(self, tmp_path, caplog):
        """★★ YAML 语法错必须报**真因** (含 YAML 错误文本), 不能只说"无 frontmatter"。

        实测教训: 真因是标量里有 ": " 让 YAML 炸, 而旧日志把它说成
        "无有效 YAML frontmatter (必须以 --- 开头/结束)" —— 完全误导, 白查一轮。
        """
        with caplog.at_level(logging.WARNING, logger="core.skill_loader"):
            _eng(tmp_path, YAML_BAD)
        joined = " ".join(r.message for r in caplog.records)
        assert "语法错误" in joined, f"没报 YAML 语法错误: {joined[:220]}"
        assert ("mapping values" in joined) or ("line" in joined), f"没带 YAML 细节: {joined[:220]}"

    def test_missing_frontmatter_hint_differs(self, tmp_path, caplog):
        """两种失败必须能分辨 —— 否则又回到"分不清真因"。"""
        with caplog.at_level(logging.WARNING, logger="core.skill_loader"):
            _eng(tmp_path, "# 就是一篇普通 markdown\n没有 frontmatter\n")
        joined = " ".join(r.message for r in caplog.records)
        assert "frontmatter" in joined
        assert "语法错误" not in joined


class TestRealSkillsLoad:
    """★ 真实仓库: 磁盘上的技能都要能加载, 且 0 解析错误。"""

    # 别的验证器会在 tmm_skills/ 下**故意**放夹具 (verify_skill_dag_layer 放的
    # `test-copy-file` 与 `_broken_no_fm`) —— 夹具不参与"真技能都必须加载"的判断。
    FIXTURE_PREFIXES = ("_", "test-")

    def test_all_real_skills_load(self):
        # ★ 不用 get_skill_index(): 它是**进程级单例**, 任何先跑过的测试若传了临时目录,
        #   单例会绑到那个临时目录 → 本测试在全量套件里假红 (实测踩到)。
        #   直接建默认引擎 (skills_dir 缺省 = 仓库 tmm_skills/) 就与全局状态无关。
        from core.skill_loader import SKILLS_DIR, SkillWorkflowEngine
        eng = SkillWorkflowEngine()
        idx = eng.get_index()
        disk = sorted(p.parent.name for p in Path(SKILLS_DIR).glob("*/SKILL.md"))
        real = [n for n in disk if not n.startswith(self.FIXTURE_PREFIXES)]
        missing = [n for n in real if n not in idx]
        assert not missing, f"真技能没加载: {missing} (frontmatter/YAML 有问题)"
        errs = {n: (eng.get_dag(n).parse_errors if eng.get_dag(n) else ["无DAG"]) for n in idx if n in real}
        assert not {k: v for k, v in errs.items() if v}, errs
        assert len(real) >= 17, f"真技能只有 {len(real)} 个?" 

    def test_broken_fixture_must_not_load(self):
        """★ 夹具 `_broken_no_fm`(故意写坏) 在场时必须**不被加载** —— 反向锁定 skip 行为。"""
        from core.skill_loader import SKILLS_DIR, SkillWorkflowEngine
        broken = Path(SKILLS_DIR) / "_broken_no_fm"
        if not broken.is_dir():
            pytest.skip("夹具不在场 (只有 verify_skill_dag_layer 跑的时候才建)")
        idx = SkillWorkflowEngine().get_index()
        assert "_broken_no_fm" not in idx, "坏夹具竟然被加载了 —— 说明跳过逻辑没了"

    def test_new_skills_present(self):
        from core.skill_loader import SkillWorkflowEngine
        idx = SkillWorkflowEngine().get_index()
        for n in ("summarize-web", "transcribe-audio", "video-frames",
                  "second-opinion", "pc-checkup"):
            assert n in idx, f"{n} 没加载"
