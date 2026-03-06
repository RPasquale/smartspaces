"""Protocol-agnostic contract tests for adapter compliance.

Any adapter can import and run these tests to verify it satisfies the
SDK contract. Subclass AdapterContractSuite and provide a configured
adapter instance via the `make_adapter` fixture.

Usage in an adapter's test suite:

    from sdk.adapter_api.contract_tests.test_adapter_contract import AdapterContractSuite

    class TestMyAdapter(AdapterContractSuite):
        @pytest.fixture
        def adapter(self):
            return MyAdapter(...)

        @pytest.fixture
        def connection_id(self):
            return "test_conn_1"

        @pytest.fixture
        def sample_point_id(self):
            return "pt_relay1_state"
"""

from __future__ import annotations

import pytest

from sdk.adapter_api.base import (
    Adapter,
    CommissionResult,
    ConnectionProfile,
    DiscoveryRequest,
    HealthStatus,
    InventorySnapshot,
)


class AdapterContractSuite:
    """Base contract test suite. Adapters subclass this and provide fixtures."""

    @pytest.fixture
    def adapter(self) -> Adapter:
        raise NotImplementedError("Provide an adapter fixture")

    @pytest.fixture
    def connection_id(self) -> str:
        raise NotImplementedError("Provide a connection_id fixture")

    @pytest.fixture
    def sample_point_id(self) -> str:
        raise NotImplementedError("Provide a sample_point_id fixture")

    def test_has_adapter_id(self, adapter: Adapter):
        assert hasattr(adapter, "adapter_id")
        assert isinstance(adapter.adapter_id, str)
        assert len(adapter.adapter_id) > 0

    def test_has_adapter_class(self, adapter: Adapter):
        assert hasattr(adapter, "adapter_class")
        assert adapter.adapter_class in (
            "direct_device", "bridge", "network_controller", "bus", "server", "composite"
        )

    def test_has_connection_templates(self, adapter: Adapter):
        templates = adapter.connection_templates()
        assert isinstance(templates, list)

    @pytest.mark.asyncio
    async def test_discover_returns_list(self, adapter: Adapter):
        request = DiscoveryRequest(site_id="test_site", methods=["manual_ip"])
        results = await adapter.discover(request)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_health_returns_status(self, adapter: Adapter, connection_id: str):
        status = await adapter.health(connection_id)
        assert isinstance(status, HealthStatus)
        assert status.status in ("healthy", "degraded", "offline", "error")

    @pytest.mark.asyncio
    async def test_inventory_returns_snapshot(self, adapter: Adapter, connection_id: str):
        snapshot = await adapter.inventory(connection_id)
        assert isinstance(snapshot, InventorySnapshot)
        assert isinstance(snapshot.devices, list)
        assert isinstance(snapshot.endpoints, list)
        assert isinstance(snapshot.points, list)

    @pytest.mark.asyncio
    async def test_inventory_devices_have_required_fields(
        self, adapter: Adapter, connection_id: str
    ):
        snapshot = await adapter.inventory(connection_id)
        for dev in snapshot.devices:
            assert "device_id" in dev
            assert "native_device_ref" in dev
            assert "device_family" in dev

    @pytest.mark.asyncio
    async def test_inventory_endpoints_have_required_fields(
        self, adapter: Adapter, connection_id: str
    ):
        snapshot = await adapter.inventory(connection_id)
        for ep in snapshot.endpoints:
            assert "endpoint_id" in ep
            assert "device_id" in ep
            assert "capabilities" in ep

    @pytest.mark.asyncio
    async def test_inventory_points_have_required_fields(
        self, adapter: Adapter, connection_id: str
    ):
        snapshot = await adapter.inventory(connection_id)
        for pt in snapshot.points:
            assert "point_id" in pt
            assert "endpoint_id" in pt
            assert "point_class" in pt
            assert "value_type" in pt
            assert "readable" in pt

    @pytest.mark.asyncio
    async def test_read_point_returns_dict(
        self, adapter: Adapter, connection_id: str, sample_point_id: str
    ):
        result = await adapter.read_point(connection_id, sample_point_id)
        assert isinstance(result, dict)
        assert "value" in result or "error" in result

    @pytest.mark.asyncio
    async def test_execute_returns_dict(self, adapter: Adapter, connection_id: str):
        command = {
            "command_id": "test_cmd_1",
            "target": {"device_id": "test", "endpoint_id": "test"},
            "capability": "binary_switch",
            "verb": "set",
            "params": {"value": True},
        }
        result = await adapter.execute(connection_id, command)
        assert isinstance(result, dict)
        assert "status" in result
