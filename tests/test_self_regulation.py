"""Tests for self_regulation module."""
import pytest
from core.self_regulation import ModuleBoundary


class TestSelfRegulation:
    def test_enum_members(self):
        members = list(ModuleBoundary)
        assert len(members) > 0

    def test_enum_has_values(self):
        values = [m.value for m in ModuleBoundary]
        assert "knowledge" in values or "reasoning" in values

    def test_enum_iterable(self):
        names = [m.name for m in ModuleBoundary]
        assert len(names) > 0

    def test_enum_by_value(self):
        first = list(ModuleBoundary)[0]
        assert isinstance(first.value, str)
