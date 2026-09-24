"""Tests for risk classification."""
import pytest
from core.risk import RiskClass, classify_command, classify_batch, is_consequential


class TestClassify:
    def test_read(self):
        for cmd in ["ls -la", "cat f", "git status", "echo hi"]:
            assert classify_command(cmd) == RiskClass.READ

    def test_write(self):
        for cmd in ["mkdir x", "rm f", "git add ."]:
            assert classify_command(cmd) == RiskClass.WRITE_LOCAL

    def test_exec(self):
        assert classify_command("python train.py") == RiskClass.EXEC

    def test_external(self):
        for cmd in ["pip install x", "curl https://x.com", "git push"]:
            assert classify_command(cmd) == RiskClass.EXTERNAL

    def test_network(self):
        for cmd in ["flask run", "npm start", "docker ps"]:
            assert classify_command(cmd) == RiskClass.NETWORK

    def test_unknown_is_exec(self):
        assert classify_command("random_tool --flag") == RiskClass.EXEC


class TestBatch:
    def test_all_read(self):
        assert classify_batch(["ls", "cat f"]) == RiskClass.READ

    def test_highest_wins(self):
        assert classify_batch(["ls", "mkdir x", "flask run"]) == RiskClass.NETWORK

    def test_empty(self):
        assert classify_batch([]) == RiskClass.READ


class TestConsequential:
    def test_read_false(self):
        assert not is_consequential(RiskClass.READ)

    def test_others_true(self):
        for r in [RiskClass.WRITE_LOCAL, RiskClass.EXEC, RiskClass.EXTERNAL, RiskClass.NETWORK]:
            assert is_consequential(r)
