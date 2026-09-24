"""Tests for Rich theme module."""
import pytest
from core.theme import gold, red, green, dim, console


class TestTheme:
    def test_colors_produce_output(self):
        assert gold("T") == "[gold]T[/gold]"
        assert red("E") == "[red]E[/red]"
        assert green("S") == "[green]S[/green]"
        assert dim("T") == "[dim]T[/dim]"

    def test_console_exists(self):
        assert console is not None

    def test_empty_returns_tags(self):
        assert gold("") == "[gold][/gold]"
        assert red("") == "[red][/red]"

    def test_unicode(self):
        result = gold("你好")
        assert "你好" in result
        assert "[gold]" in result
