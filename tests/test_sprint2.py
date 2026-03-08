"""Tests for Sprint 2 features.

Covers: audit log retention, Pydantic request models for agent endpoints.
"""

from __future__ import annotations

import pytest


# -- P1.5: Audit log retention --

class TestAuditLogRetention:
    """Tests for audit log prune_audit_log method."""

    @pytest.fixture
    async def store(self):
        from core.state_store import StateStore
        s = StateStore(db_path=":memory:")
        await s.open()
        yield s
        await s.close()

    async def test_prune_by_age(self, store):
        """Old audit entries should be deleted."""
        from datetime import datetime, timezone, timedelta
        # Insert an old entry directly
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        await store.db.execute(
            "INSERT INTO audit_log (timestamp, event_type) VALUES (?, ?)",
            (old_ts, "old_event"),
        )
        await store.db.commit()

        # Insert a recent entry
        await store.audit(event_type="recent_event")

        deleted = await store.prune_audit_log(max_age_days=30)
        assert deleted >= 1

        entries = await store.get_audit_log(limit=100)
        assert all(e["event_type"] != "old_event" for e in entries)
        assert any(e["event_type"] == "recent_event" for e in entries)

    async def test_prune_by_count(self, store):
        """When exceeding max_rows, oldest entries should be removed."""
        for i in range(10):
            await store.audit(event_type=f"event_{i}")

        deleted = await store.prune_audit_log(max_age_days=365, max_rows=5)
        assert deleted >= 5

        entries = await store.get_audit_log(limit=100)
        assert len(entries) <= 5

    async def test_prune_nothing_to_delete(self, store):
        """When nothing is old or over limit, should return 0."""
        await store.audit(event_type="fresh")
        deleted = await store.prune_audit_log(max_age_days=30, max_rows=1_000_000)
        assert deleted == 0


# -- P2.2: Pydantic request models --

class TestPydanticRequestModels:
    """Verify agent endpoints use proper Pydantic models."""

    def _get_api_source(self):
        import core.api as api_mod
        return open(api_mod.__file__).read()

    def test_agent_state_uses_model(self):
        src = self._get_api_source()
        assert "AgentStateRequest" in src
        assert "req: AgentStateRequest" in src

    def test_agent_set_uses_model(self):
        src = self._get_api_source()
        assert "AgentSetRequest" in src
        assert "req: AgentSetRequest" in src

    def test_agent_intent_uses_model(self):
        src = self._get_api_source()
        assert "AgentIntentRequest" in src
        assert "req: AgentIntentRequest" in src

    def test_agent_lock_uses_model(self):
        src = self._get_api_source()
        assert "AgentLockRequest" in src
        assert "req: AgentLockRequest" in src

    def test_agent_schedule_uses_model(self):
        src = self._get_api_source()
        assert "AgentScheduleRequest" in src
        assert "req: AgentScheduleRequest" in src

    def test_models_are_pydantic(self):
        """All agent request models should be Pydantic BaseModel subclasses."""
        src = self._get_api_source()
        model_names = [
            "AgentStateRequest", "AgentSetRequest", "AgentSpaceSummaryRequest",
            "AgentSceneRequest", "AgentCreateSceneRequest", "AgentCreateRuleRequest",
            "AgentIntentRequest", "AgentCreateGroupRequest", "AgentSetGroupRequest",
            "AgentScheduleRequest", "AgentLockRequest", "AgentReleaseRequest",
        ]
        for name in model_names:
            assert f"class {name}(BaseModel)" in src, f"{name} not found as BaseModel"

    def test_no_raw_dict_in_agent_post_endpoints(self):
        """No agent POST endpoint should accept raw dict[str, Any] as body."""
        src = self._get_api_source()
        # Find all agent POST handler signatures
        import re
        # Match "async def agent_*(...req: dict..."
        matches = re.findall(r'async def agent_\w+\(req: dict', src)
        assert len(matches) == 0, f"Found agent endpoints still using raw dict: {matches}"
