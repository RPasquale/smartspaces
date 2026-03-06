"""Tests for the engine — full integration of all core components."""

from unittest.mock import AsyncMock

import pytest

from core.engine import Engine
from sdk.adapter_api.base import (
    CommissionResult,
    ConnectionTemplate,
    HealthStatus,
    InventorySnapshot,
)


class FakeAdapter:
    adapter_id = "test.engine"
    adapter_class = "direct_device"

    def __init__(self):
        self.discover = AsyncMock(return_value=[])
        self.commission = AsyncMock(return_value=CommissionResult("eng_conn_1", "ok"))
        self.inventory = AsyncMock(return_value=InventorySnapshot(
            connection_id="eng_conn_1",
            devices=[{"device_id": "dev_eng_1", "native_device_ref": "1.2.3.4", "device_family": "test"}],
            endpoints=[{"endpoint_id": "ep_eng_1", "device_id": "dev_eng_1", "capabilities": ["switch"]}],
            points=[{
                "point_id": "pt_eng_1", "endpoint_id": "ep_eng_1",
                "point_class": "switch.state", "value_type": "bool",
                "readable": True, "event_driven": False,
            }],
        ))
        self.read_point = AsyncMock(return_value={
            "point_id": "pt_eng_1",
            "value": {"kind": "bool", "reported": True},
            "quality": {"status": "good"},
        })
        self.execute = AsyncMock(return_value={"command_id": "c1", "status": "succeeded"})
        self.health = AsyncMock(return_value=HealthStatus("healthy"))
        self.teardown = AsyncMock()

    def connection_templates(self):
        return [ConnectionTemplate(adapter_id="test.engine", display_name="Test Engine")]


@pytest.fixture
async def engine(tmp_path):
    e = Engine(db_path=tmp_path / "engine_test.db", default_poll_interval=60)
    e.register_adapter(FakeAdapter())
    await e.start()
    yield e
    await e.stop()


class TestEngine:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, tmp_path):
        e = Engine(db_path=tmp_path / "start_stop.db")
        await e.start()
        assert e._started is True
        await e.stop()
        assert e._started is False

    @pytest.mark.asyncio
    async def test_register_adapter(self, engine):
        adapters = engine.registry.list_adapters()
        assert len(adapters) == 1
        assert adapters[0]["adapter_id"] == "test.engine"

    @pytest.mark.asyncio
    async def test_quick_connect(self, engine):
        conn_id = await engine.quick_connect("test.engine", "test", {"host": "1.2.3.4"})
        assert conn_id == "eng_conn_1"

        # Device should be persisted
        devices = await engine.state_store.list_devices("eng_conn_1")
        assert len(devices) == 1

        # Point should be scheduled for polling
        assert engine.scheduler.stats["total_targets"] == 1

    @pytest.mark.asyncio
    async def test_read_point_persists(self, engine):
        await engine.quick_connect("test.engine", "test", {"host": "1.2.3.4"})
        result = await engine.registry.read_point("eng_conn_1", "pt_eng_1")
        assert result["value"]["reported"] is True

        val = await engine.state_store.get_point_value("pt_eng_1")
        assert val is not None

    @pytest.mark.asyncio
    async def test_execute_command(self, engine):
        await engine.quick_connect("test.engine", "test", {"host": "1.2.3.4"})
        result = await engine.registry.execute("eng_conn_1", {
            "command_id": "cmd_test",
            "target": {"device_id": "dev_eng_1", "endpoint_id": "ep_eng_1"},
            "capability": "binary_switch",
            "verb": "set",
            "params": {"value": True},
        })
        assert result["status"] == "succeeded"

        # Audit should have entries
        log = await engine.state_store.get_audit_log(limit=10)
        assert len(log) >= 2

    @pytest.mark.asyncio
    async def test_health_check(self, engine):
        await engine.quick_connect("test.engine", "test", {})
        statuses = await engine.registry.health_all()
        assert "eng_conn_1" in statuses
        assert statuses["eng_conn_1"].status == "healthy"

    @pytest.mark.asyncio
    async def test_teardown_cleans_up(self, engine):
        await engine.quick_connect("test.engine", "test", {})
        await engine.registry.teardown("eng_conn_1")
        adapters = engine.registry.list_adapters()
        assert len(adapters[0]["connections"]) == 0

    @pytest.mark.asyncio
    async def test_event_bus_receives_events(self, engine):
        received = []

        async def handler(event):
            received.append(event)

        engine.event_bus.subscribe("*", handler)
        await engine.quick_connect("test.engine", "test", {})

        import asyncio
        await asyncio.sleep(0.2)

        # Should have connection + inventory events at minimum
        event_types = [e.get("type") for e in received]
        assert "connection.state_changed" in event_types
        assert "device.inventory_changed" in event_types

    @pytest.mark.asyncio
    async def test_create_api(self, engine):
        app = engine.create_api()
        # Will be None if fastapi not installed, or a FastAPI app if it is
        # Either way, should not error
        if app is not None:
            assert hasattr(app, "routes")
