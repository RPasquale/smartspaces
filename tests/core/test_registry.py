"""Tests for the adapter registry."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.event_bus import EventBus
from core.registry import AdapterRegistry
from core.state_store import StateStore
from sdk.adapter_api.base import (
    CommissionResult,
    ConnectionProfile,
    ConnectionTemplate,
    HealthStatus,
    InventorySnapshot,
)


class FakeAdapter:
    """Minimal adapter for testing the registry."""

    adapter_id = "test.fake"
    adapter_class = "direct_device"

    def __init__(self):
        self.discover = AsyncMock(return_value=[])
        self.commission = AsyncMock(return_value=CommissionResult("conn_test_1", "ok"))
        self.inventory = AsyncMock(return_value=InventorySnapshot(
            connection_id="conn_test_1",
            devices=[{"device_id": "dev_1", "native_device_ref": "1.2.3.4", "device_family": "test"}],
            endpoints=[{"endpoint_id": "ep_1", "device_id": "dev_1", "capabilities": ["switch"]}],
            points=[{"point_id": "pt_1", "endpoint_id": "ep_1", "point_class": "switch.state",
                     "value_type": "bool", "readable": True}],
        ))
        self.read_point = AsyncMock(return_value={
            "point_id": "pt_1",
            "value": {"kind": "bool", "reported": True},
            "quality": {"status": "good"},
        })
        self.execute = AsyncMock(return_value={"command_id": "cmd_1", "status": "succeeded"})
        self.health = AsyncMock(return_value=HealthStatus("healthy", {"latency": 10}))
        self.teardown = AsyncMock()

    def connection_templates(self):
        return [ConnectionTemplate(adapter_id="test.fake", display_name="Test")]


@pytest.fixture
async def bus():
    b = EventBus()
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
async def store(tmp_path):
    s = StateStore(db_path=tmp_path / "test.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def registry(bus, store):
    return AdapterRegistry(bus, store)


@pytest.fixture
def adapter():
    return FakeAdapter()


class TestAdapterRegistry:
    def test_register_and_list(self, registry, adapter):
        registry.register(adapter)
        adapters = registry.list_adapters()
        assert len(adapters) == 1
        assert adapters[0]["adapter_id"] == "test.fake"

    def test_get_adapter(self, registry, adapter):
        registry.register(adapter)
        a = registry.get_adapter("test.fake")
        assert a is adapter

    def test_get_unknown_adapter_raises(self, registry):
        with pytest.raises(Exception):
            registry.get_adapter("nonexistent")

    @pytest.mark.asyncio
    async def test_commission(self, registry, adapter):
        registry.register(adapter)
        profile = ConnectionProfile(profile_id="test", fields={"host": "1.2.3.4"})
        result = await registry.commission("test.fake", None, profile)
        assert result.status == "ok"
        assert result.connection_id == "conn_test_1"

    @pytest.mark.asyncio
    async def test_commission_simple(self, registry, adapter):
        registry.register(adapter)
        result = await registry.commission_simple("test.fake", "test", {"host": "1.2.3.4"})
        assert result.status == "ok"

    @pytest.mark.asyncio
    async def test_inventory_persists(self, registry, adapter, store):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {"host": "1.2.3.4"})
        snapshot = await registry.inventory("conn_test_1")

        assert len(snapshot.devices) == 1

        # Check state store has the device
        devices = await store.list_devices("conn_test_1")
        assert len(devices) == 1
        assert devices[0]["device_id"] == "dev_1"

    @pytest.mark.asyncio
    async def test_read_point_persists_value(self, registry, adapter, store):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {})
        await registry.inventory("conn_test_1")

        result = await registry.read_point("conn_test_1", "pt_1")
        assert result["value"]["reported"] is True

        # Check state store has the value
        val = await store.get_point_value("pt_1")
        assert val is not None
        assert val["value"]["reported"] is True

    @pytest.mark.asyncio
    async def test_execute_audits(self, registry, adapter, store):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {})

        command = {
            "command_id": "cmd_test",
            "target": {"device_id": "dev_1", "endpoint_id": "ep_1"},
            "capability": "binary_switch",
            "verb": "set",
            "params": {"value": True},
            "context": {"initiator": "test"},
        }
        result = await registry.execute("conn_test_1", command)
        assert result["status"] == "succeeded"

        # Check audit log
        log = await store.get_audit_log(limit=10)
        assert len(log) >= 2  # accepted + succeeded

    @pytest.mark.asyncio
    async def test_health(self, registry, adapter):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {})
        status = await registry.health("conn_test_1")
        assert status.status == "healthy"

    @pytest.mark.asyncio
    async def test_health_all(self, registry, adapter):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {})
        statuses = await registry.health_all()
        assert "conn_test_1" in statuses
        assert statuses["conn_test_1"].status == "healthy"

    @pytest.mark.asyncio
    async def test_teardown(self, registry, adapter):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {})
        await registry.teardown("conn_test_1")
        adapter.teardown.assert_called_once_with("conn_test_1")

    @pytest.mark.asyncio
    async def test_teardown_all(self, registry, adapter):
        registry.register(adapter)
        await registry.commission_simple("test.fake", "test", {})
        await registry.teardown_all()
        adapter.teardown.assert_called()
