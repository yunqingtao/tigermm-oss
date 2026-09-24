"""Tests for InboxStore."""
import pytest
from core.inbox import InboxStore, RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS


class TestInbox:
    def test_auto_approve_read(self, inbox):
        item = inbox.create_approval("s1", ["ls", "cat f"])
        assert item.state == "resolved"

    def test_write_needs_approval(self, inbox):
        item = inbox.create_approval("s1", ["mkdir x"])
        assert item.state == "pending"

    def test_resolve(self, inbox):
        item = inbox.create_approval("s1", ["mkdir x"])
        assert inbox.resolve(item.id, RESOLVE_ALLOW)
        assert inbox.get(item.id).resolution == "allow"

    def test_first_wins(self, inbox):
        item = inbox.create_approval("s1", ["mkdir x"])
        inbox.resolve(item.id, RESOLVE_ALLOW)
        assert not inbox.resolve(item.id, RESOLVE_DENY)

    def test_list_pending(self, inbox):
        inbox.create_approval("s1", ["mkdir a"])
        inbox.create_approval("s2", ["mkdir b"])
        assert len(inbox.list_pending()) == 2
        assert len(inbox.list_pending(session_id="s1")) == 1

    def test_session_allow(self, inbox):
        inbox.allow_session("always")
        assert inbox.is_session_allowed("always")
        inbox.revoke_session("always")
        assert not inbox.is_session_allowed("always")

    def test_stats(self, inbox):
        inbox.create_approval("s1", ["ls"])
        inbox.create_approval("s1", ["mkdir"])
        s = inbox.stats()
        assert s["pending"] == 1
        assert s["resolved"] == 1

    @pytest.mark.asyncio
    async def test_wait_timeout(self, inbox):
        item = inbox.create_approval("s1", ["mkdir"], timeout=1)
        r = await inbox.wait_for(item.id, timeout=1)
        assert r == "deny"
