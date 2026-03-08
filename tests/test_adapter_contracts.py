"""Contract test subclasses for all 15 adapters.

Each adapter is tested against the universal AdapterContractSuite to
verify it satisfies the SDK interface contract. Adapters that require
network calls for inventory are tested with partial coverage
(adapter_id, class, templates, discover).
"""

from __future__ import annotations

import pytest
from sdk.adapter_api.contract_tests.test_adapter_contract import AdapterContractSuite
from sdk.adapter_api.base import (
    Adapter, DiscoveryRequest, HealthStatus, InventorySnapshot,
)


# ---------------------------------------------------------------------------
# Base for adapters whose inventory/read need live network — skip those tests
# ---------------------------------------------------------------------------

class OfflineContractSuite:
    """Contract tests that only check offline-safe methods."""

    @pytest.fixture
    def adapter(self) -> Adapter:
        raise NotImplementedError

    def test_has_adapter_id(self, adapter):
        assert isinstance(adapter.adapter_id, str)
        assert len(adapter.adapter_id) > 0

    def test_has_adapter_class(self, adapter):
        assert adapter.adapter_class in (
            "direct_device", "bridge", "network_controller", "bus", "server", "composite"
        )

    def test_has_connection_templates(self, adapter):
        templates = adapter.connection_templates()
        assert isinstance(templates, list)
        assert len(templates) > 0

    async def test_discover_returns_list(self, adapter):
        request = DiscoveryRequest(site_id="test_site", methods=["manual_ip"])
        results = await adapter.discover(request)
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 1. KinCony — needs TasmotaProfile + board dict for _Connection
# ---------------------------------------------------------------------------

class TestKinConyContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.kincony import KinConyAdapter
        return KinConyAdapter()


# ---------------------------------------------------------------------------
# 2. Shelly — inventory calls RPC over HTTP
# ---------------------------------------------------------------------------

class TestShellyContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.shelly import ShellyAdapter
        return ShellyAdapter()


# ---------------------------------------------------------------------------
# 3. MQTT Generic
# ---------------------------------------------------------------------------

class TestMqttGenericContract(AdapterContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.mqtt_generic import MqttGenericAdapter
        a = MqttGenericAdapter()
        from adapters.mqtt_generic.adapter import _MqttConnection
        conn = _MqttConnection("test_conn", "127.0.0.1", 1883)
        a._connections["test_conn"] = conn
        return a

    @pytest.fixture
    def connection_id(self):
        return "test_conn"

    @pytest.fixture
    def sample_point_id(self):
        return "pt_test_topic"


# ---------------------------------------------------------------------------
# 4. Modbus
# ---------------------------------------------------------------------------

class TestModbusContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.modbus import ModbusAdapter
        return ModbusAdapter()


# ---------------------------------------------------------------------------
# 5. Hue — inventory calls HTTP
# ---------------------------------------------------------------------------

class TestHueContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.hue import HueAdapter
        return HueAdapter()


# ---------------------------------------------------------------------------
# 6. ONVIF — inventory calls HTTP
# ---------------------------------------------------------------------------

class TestOnvifContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.onvif import OnvifAdapter
        return OnvifAdapter()


# ---------------------------------------------------------------------------
# 7. ESPHome — inventory calls HTTP
# ---------------------------------------------------------------------------

class TestESPHomeContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.esphome import ESPHomeAdapter
        return ESPHomeAdapter()


# ---------------------------------------------------------------------------
# 8. Zigbee — inventory calls HTTP
# ---------------------------------------------------------------------------

class TestZigbeeContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.zigbee import ZigbeeAdapter
        return ZigbeeAdapter()


# ---------------------------------------------------------------------------
# 9. Z-Wave — inventory calls WebSocket
# ---------------------------------------------------------------------------

class TestZWaveContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.zwave import ZWaveAdapter
        return ZWaveAdapter()


# ---------------------------------------------------------------------------
# 10. Matter — inventory calls HTTP
# ---------------------------------------------------------------------------

class TestMatterContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.matter import MatterAdapter
        return MatterAdapter()


# ---------------------------------------------------------------------------
# 11. Lutron — inventory needs HTTP
# ---------------------------------------------------------------------------

class TestLutronContract(OfflineContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.lutron import LutronAdapter
        return LutronAdapter()


# ---------------------------------------------------------------------------
# 12. KNX — offline-safe inventory
# ---------------------------------------------------------------------------

class TestKnxContract(AdapterContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.knx import KnxAdapter
        a = KnxAdapter()
        from adapters.knx.adapter import _KnxConnection
        conn = _KnxConnection("test_conn", "127.0.0.1")
        conn._group_addresses["1/2/3"] = {
            "name": "Test Light", "dpt": "1.001", "area": "test",
        }
        a._connections["test_conn"] = conn
        return a

    @pytest.fixture
    def connection_id(self):
        return "test_conn"

    @pytest.fixture
    def sample_point_id(self):
        return "dev_knx_test_1_2_3_ep_value"


# ---------------------------------------------------------------------------
# 13. BACnet — offline-safe inventory
# ---------------------------------------------------------------------------

class TestBacnetContract(AdapterContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.bacnet import BacnetAdapter
        a = BacnetAdapter()
        from adapters.bacnet.adapter import _BacnetConnection
        conn = _BacnetConnection("test_conn", "127.0.0.1")
        conn._objects["analogInput:1"] = {"name": "Zone Temp"}
        a._connections["test_conn"] = conn
        return a

    @pytest.fixture
    def connection_id(self):
        return "test_conn"

    @pytest.fixture
    def sample_point_id(self):
        return "dev_bacnet_127_0_0_1_analogInput_1_pv"


# ---------------------------------------------------------------------------
# 14. OPC UA — offline-safe inventory (uses cached nodes)
# ---------------------------------------------------------------------------

class TestOpcUaContract(AdapterContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.opcua import OpcUaAdapter
        a = OpcUaAdapter()
        from adapters.opcua.adapter import _OpcUaConnection
        conn = _OpcUaConnection("test_conn", "opc.tcp://127.0.0.1:4840")
        conn._nodes["ns=2;s=TestNode"] = {
            "node_id": "ns=2;s=TestNode",
            "name": "TestNode",
            "data_type": "Double",
            "writable": False,
        }
        a._connections["test_conn"] = conn
        return a

    @pytest.fixture
    def connection_id(self):
        return "test_conn"

    @pytest.fixture
    def sample_point_id(self):
        return "dev_opcua_opc_tcp___127_0_0_1_4840_ns_2_s_TestNode_value"


# ---------------------------------------------------------------------------
# 15. DNP3 — offline-safe inventory
# ---------------------------------------------------------------------------

class TestDnp3Contract(AdapterContractSuite):
    @pytest.fixture
    def adapter(self):
        from adapters.dnp3 import Dnp3Adapter
        a = Dnp3Adapter()
        from adapters.dnp3.adapter import _Dnp3Connection
        conn = _Dnp3Connection("test_conn", "127.0.0.1", 20000)
        conn._data_map["30:0"] = {"name": "Analog In 0", "value": 1.23}
        a._connections["test_conn"] = conn
        return a

    @pytest.fixture
    def connection_id(self):
        return "test_conn"

    @pytest.fixture
    def sample_point_id(self):
        return "dev_dnp3_127_0_0_1_1_g30_i0_value"
