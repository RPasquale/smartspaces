"""Integration tests for the Engine lifecycle.

Tests the full Engine start → register → connect → poll → stop flow
using the in-memory event bus and an in-memory SQLite state store.
"""

from __future__ import annotations

import asyncio

import pytest

from core.engine import Engine
from adapters.kincony import KinConyAdapter
from adapters.shelly import ShellyAdapter
from core.event_bus import EventBus
from core.state_store import StateStore


class TestEngineLifecycle:
    """Verify Engine start/stop without a real server."""

    @pytest.fixture
    def engine(self):
        return Engine(db_path=":memory:", default_poll_interval=1.0)

    async def test_start_stop(self, engine):
        await engine.start(restore_connections=False)
        assert engine._started is True
        await engine.stop()
        assert engine._started is False

    async def test_double_start_is_idempotent(self, engine):
        await engine.start(restore_connections=False)
        await engine.start(restore_connections=False)  # should not raise
        assert engine._started is True
        await engine.stop()

    async def test_double_stop_is_idempotent(self, engine):
        await engine.start(restore_connections=False)
        await engine.stop()
        await engine.stop()  # should not raise
        assert engine._started is False

    async def test_event_bus_wired(self, engine):
        await engine.start(restore_connections=False)
        # Event bus should be running
        assert engine.event_bus._running is True
        await engine.stop()

    async def test_point_reported_persisted(self, engine):
        """When a point.reported event is published, the state store should persist it."""
        await engine.start(restore_connections=False)
        await engine.event_bus.publish({
            "type": "point.reported",
            "point_id": "test_pt_1",
            "value": 42.0,
            "quality": "good",
        })
        # Give dispatch loop time to process
        await asyncio.sleep(0.1)
        # Verify state store received the value
        row = await engine.state_store.get_point_value("test_pt_1")
        assert row is not None
        await engine.stop()


class TestEngineWithAdapters:
    """Register adapters and verify registry integration."""

    @pytest.fixture
    def engine(self):
        return Engine(db_path=":memory:")

    def test_register_adapter(self, engine):
        adapter = KinConyAdapter()
        engine.register_adapter(adapter)
        assert "kincony.family" in engine.registry._adapters

    def test_register_multiple_adapters(self, engine):
        engine.register_adapter(KinConyAdapter())
        engine.register_adapter(ShellyAdapter())
        assert len(engine.registry._adapters) >= 2

    async def test_list_adapters_after_start(self, engine):
        engine.register_adapter(KinConyAdapter())
        await engine.start(restore_connections=False)
        adapters = engine.registry.list_adapters()
        assert any(a["adapter_id"] == "kincony.family" for a in adapters)
        await engine.stop()


class TestEngineCreateApi:
    """Verify Engine.create_api() produces a working FastAPI app."""

    @pytest.fixture
    def engine(self):
        return Engine(db_path=":memory:")

    def test_create_api_returns_app(self, engine):
        engine.register_adapter(KinConyAdapter())
        app = engine.create_api()
        assert app is not None
        # Should have routes
        assert len(app.routes) > 0

    def test_create_api_with_cors(self, engine):
        engine.register_adapter(KinConyAdapter())
        app = engine.create_api(cors_origins=["http://localhost:3000"])
        assert app is not None


class TestEngineCLI:
    """Test CLI argument parsing."""

    def test_build_parser_defaults(self):
        from core.engine import _build_parser
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.host == "0.0.0.0"
        assert args.port == 8000
        assert args.db_path == "state.db"
        assert args.log_format == "text"
        assert args.no_restore is False

    def test_build_parser_custom(self):
        from core.engine import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "--host", "127.0.0.1",
            "--port", "9090",
            "--db-path", "/tmp/test.db",
            "--log-format", "json",
            "--log-level", "DEBUG",
            "--no-restore",
            "--event-bus", "redis",
            "--redis-url", "redis://myhost:6380",
        ])
        assert args.host == "127.0.0.1"
        assert args.port == 9090
        assert args.event_bus == "redis"
        assert args.redis_url == "redis://myhost:6380"
