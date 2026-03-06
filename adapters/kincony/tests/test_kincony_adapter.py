"""Tests for the KinCony adapter using a mock Tasmota HTTP server."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from adapters.kincony.adapter import KinConyAdapter
from sdk.adapter_api.base import (
    ConnectionProfile,
    DiscoveryRequest,
    HealthStatus,
    InventorySnapshot,
)


# -- Fixtures --

MOCK_STATUS_0 = {
    "Status": {
        "Module": 0,
        "DeviceName": "KC868-A4 Test",
        "FriendlyName": ["Relay1", "Relay2", "Relay3", "Relay4"],
        "Power": "0000",
    },
    "StatusNET": {
        "Hostname": "tasmota-test",
        "IPAddress": "192.168.1.100",
        "Mac": "AA:BB:CC:DD:EE:FF",
    },
    "StatusFWR": {"Version": "15.3.0"},
    "StatusSTS": {
        "POWER1": "OFF",
        "POWER2": "OFF",
        "POWER3": "OFF",
        "POWER4": "OFF",
        "Wifi": {"SSId": "TestNet", "RSSI": 60, "Signal": -70},
    },
    "StatusSNS": {
        "Time": "2026-03-06T01:00:00",
        "ANALOG": {"A1": 1000, "A2": 2000, "A3": 500, "A4": 3000},
    },
}

MOCK_POWER0 = {"POWER1": "OFF", "POWER2": "ON", "POWER3": "OFF", "POWER4": "OFF"}
MOCK_STATUS = {"Status": {"DeviceName": "KC868-A4 Test"}}


@pytest.fixture
def adapter():
    return KinConyAdapter()


@pytest.fixture
async def connected_adapter(adapter):
    """Return adapter with an active mock connection."""
    with patch(
        "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.ping",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.send_command",
        new_callable=AsyncMock,
        return_value=MOCK_STATUS,
    ):
        profile = ConnectionProfile(
            profile_id="tasmota_http",
            fields={"host": "192.168.1.100", "board_id": "kc868_a4"},
        )
        result = await adapter.commission(None, profile)
        assert result.status == "ok"
        yield adapter, result.connection_id

    # Teardown all connections
    for conn_id in list(adapter._connections.keys()):
        await adapter.teardown(conn_id)


# -- Tests --

class TestKinConyAdapter:
    def test_adapter_id(self, adapter):
        assert adapter.adapter_id == "kincony.family"
        assert adapter.adapter_class == "direct_device"

    def test_connection_templates(self, adapter):
        templates = adapter.connection_templates()
        assert len(templates) == 2
        assert templates[0].required_fields == ["host"]
        assert templates[1].required_fields == ["broker_host", "topic"]

    @pytest.mark.asyncio
    async def test_discover_with_reachable_host(self, adapter):
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.ping",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.device_status",
            new_callable=AsyncMock,
            return_value=MOCK_STATUS_0,
        ), patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.close",
            new_callable=AsyncMock,
        ):
            request = DiscoveryRequest(
                site_id="test",
                methods=["http_probe"],
                scope={"host": "192.168.1.100"},
            )
            targets = await adapter.discover(request)
            assert len(targets) == 1
            assert targets[0].title == "KC868-A4 Test"
            assert targets[0].address == "192.168.1.100"

    @pytest.mark.asyncio
    async def test_commission_success(self, adapter):
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.ping",
            new_callable=AsyncMock,
            return_value=True,
        ):
            profile = ConnectionProfile(
                profile_id="tasmota_http",
                fields={"host": "192.168.1.100"},
            )
            result = await adapter.commission(None, profile)
            assert result.status == "ok"
            assert result.connection_id.startswith("kincony_")

    @pytest.mark.asyncio
    async def test_commission_unreachable(self, adapter):
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.ping",
            new_callable=AsyncMock,
            return_value=False,
        ):
            profile = ConnectionProfile(
                profile_id="tasmota_http",
                fields={"host": "192.168.1.200"},
            )
            result = await adapter.commission(None, profile)
            assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_inventory(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.device_status",
            new_callable=AsyncMock,
            return_value=MOCK_STATUS_0,
        ):
            snapshot = await adapter.inventory(conn_id)
            assert isinstance(snapshot, InventorySnapshot)
            assert len(snapshot.devices) == 1
            assert snapshot.devices[0]["manufacturer"] == "KinCony"

            # 4 relays + 4 digital inputs + 4 analog inputs + IR TX + IR RX = 14
            assert len(snapshot.endpoints) == 14

            # 4 relay points + 4 digital input points + 8 analog (raw+voltage) = 16
            assert len(snapshot.points) == 16

    @pytest.mark.asyncio
    async def test_read_relay_point(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.get_relay_state",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await adapter.read_point(
                conn_id,
                "dev_kc868_a4_192_168_1_100_relay_1_state",
            )
            assert result["value"]["reported"] is True
            assert result["value"]["kind"] == "bool"

    @pytest.mark.asyncio
    async def test_read_analog_point(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.read_analog",
            new_callable=AsyncMock,
            return_value=2048,
        ):
            result = await adapter.read_point(
                conn_id,
                "dev_kc868_a4_192_168_1_100_ainput_1_raw",
            )
            assert result["value"]["reported"] == 2048

    @pytest.mark.asyncio
    async def test_execute_relay_on(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.relay_on",
            new_callable=AsyncMock,
            return_value={"POWER1": "ON"},
        ), patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.get_relay_state",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await adapter.execute(conn_id, {
                "command_id": "cmd_test_1",
                "target": {
                    "device_id": "dev_kc868_a4_192_168_1_100",
                    "endpoint_id": "dev_kc868_a4_192_168_1_100_relay_1",
                },
                "capability": "binary_switch",
                "verb": "set",
                "params": {"value": True},
            })
            assert result["status"] == "succeeded"
            assert result["verified"] is True
            assert result["actual_state"] is True

    @pytest.mark.asyncio
    async def test_execute_relay_toggle(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.relay_toggle",
            new_callable=AsyncMock,
            return_value={"POWER1": "ON"},
        ), patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.get_relay_state",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await adapter.execute(conn_id, {
                "command_id": "cmd_test_2",
                "target": {
                    "device_id": "dev_kc868_a4_192_168_1_100",
                    "endpoint_id": "dev_kc868_a4_192_168_1_100_relay_2",
                },
                "capability": "binary_switch",
                "verb": "toggle",
                "params": {},
            })
            assert result["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_health_healthy(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.ping",
            new_callable=AsyncMock,
            return_value=True,
        ), patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.wifi_signal",
            new_callable=AsyncMock,
            return_value=-70,
        ):
            status = await adapter.health(conn_id)
            assert status.status == "healthy"
            assert status.details["wifi_signal_dbm"] == -70

    @pytest.mark.asyncio
    async def test_health_offline(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.ping",
            new_callable=AsyncMock,
            return_value=False,
        ):
            status = await adapter.health(conn_id)
            assert status.status == "offline"

    @pytest.mark.asyncio
    async def test_teardown(self, connected_adapter):
        adapter, conn_id = connected_adapter
        with patch(
            "adapters.kincony.firmware_profiles.tasmota.TasmotaProfile.close",
            new_callable=AsyncMock,
        ):
            await adapter.teardown(conn_id)
            assert conn_id not in adapter._connections
