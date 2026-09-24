"""Tests for ProactiveThinker (minimal — heavy deps)."""
import pytest
from core.proactive import ProactiveThinker


class TestProactiveThinker:
    def test_class_exists(self):
        assert ProactiveThinker is not None
