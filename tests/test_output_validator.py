"""output_validator: check_evidence, negation filter, evidence types."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
from core.output_validator import check_evidence


class TestCompletionClaims:
    """Detect completion claims (已修复/已完成/etc)."""

    def test_done_claim(self):
        r = check_evidence("已修复该问题")
        assert r["has_claim"]

    def test_fixed_claim(self):
        r = check_evidence("fixed the bug")
        assert r["has_claim"]

    def test_hao_le_claim(self):
        r = check_evidence("好了，搞定了")
        assert r["has_claim"]


class TestNegationFilter:
    """Negated claims should NOT trigger."""

    def test_not_done(self):
        r = check_evidence("还没修复完")
        assert not r["has_claim"]

    def test_mei_yong(self):
        r = check_evidence("修好了但没用")
        assert not r["has_claim"]

    def test_bu_suan(self):
        r = check_evidence("完成了但不算数")
        assert not r["has_claim"]


class TestEvidenceDetection:
    """Valid evidence patterns detected."""

    def test_md5(self):
        r = check_evidence("已修复，MD5: d41d8cd98f00b204e9800998ecf8427e")
        assert r["has_evidence"]

    def test_file_path(self):
        r = check_evidence("已写好 core/pipeline.py")
        assert r["has_evidence"]

    def test_line_count_ascii(self):
        r = check_evidence("已修复，125 lines changed")
        assert r["has_evidence"]

    def test_pass_count(self):
        r = check_evidence("已完成，29 passed")
        assert r["has_evidence"]


class TestNoClaimNoProblem:
    """Messages without completion claims pass through."""

    def test_plain_response(self):
        r = check_evidence("虎哥在此，有事你说")
        assert r["passed"]

    def test_question(self):
        r = check_evidence("你需要什么帮助？")
        assert r["passed"]
