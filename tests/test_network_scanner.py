"""Tests for core.network_scanner — network discovery orchestration."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.network_scanner import (
    MDNS_SERVICE_MAP,
    PORT_ADAPTER_MAP,
    PORT_SERVICE_MAP,
    SSDP_DEVICE_MAP,
    DiscoveredService,
    NetworkScanner,
    _fingerprint_http_mdns,
    _get_local_subnet,
    _parse_ssdp_response,
    _extract_ssdp_header,
    _point_to_semantic_name,
    _probe_port,
    generate_spaces_yaml,
)
from sdk.adapter_api.base import InventorySnapshot


# ---------------------------------------------------------------------------
# Unit tests — mapping tables and helpers
# ---------------------------------------------------------------------------


class TestServiceMaps:
    """Verify service → adapter mappings are consistent."""

    def test_mdns_map_has_known_services(self):
        assert "_shelly._tcp.local." in MDNS_SERVICE_MAP
        assert "_hue._tcp.local." in MDNS_SERVICE_MAP
        assert "_esphomelib._tcp.local." in MDNS_SERVICE_MAP
        assert "_matter._tcp.local." in MDNS_SERVICE_MAP

    def test_port_maps_consistent(self):
        """Every port in PORT_SERVICE_MAP has a corresponding adapter mapping."""
        for port in PORT_SERVICE_MAP:
            assert port in PORT_ADAPTER_MAP, f"Port {port} missing adapter mapping"

    def test_ssdp_map_has_hue(self):
        assert "IpBridge" in SSDP_DEVICE_MAP


class TestFingerprinting:
    """Test HTTP mDNS fingerprinting logic."""

    def test_tasmota_detected(self):
        assert _fingerprint_http_mdns({"fw": "Tasmota/15.3.0"}, "device") == "kincony.family"

    def test_esphome_detected(self):
        assert _fingerprint_http_mdns({}, "ESPHome-livingroom") == "esphome.native"

    def test_shelly_detected(self):
        assert _fingerprint_http_mdns({"app": "shelly-plug"}, "ShellyPlug") == "shelly.gen2"

    def test_zigbee2mqtt_detected(self):
        assert _fingerprint_http_mdns({}, "zigbee2mqtt-frontend") == "zigbee2mqtt.http"

    def test_unknown_returns_none(self):
        assert _fingerprint_http_mdns({"vendor": "acme"}, "generic-device") is None


class TestSsdpParsing:
    """Test SSDP response parsing."""

    def test_hue_bridge_detected(self):
        response = "HTTP/1.1 200 OK\r\nSERVER: Linux/3.14 UPnP/1.0 IpBridge/1.0\r\n\r\n"
        assert _parse_ssdp_response(response) == "hue.bridge"

    def test_unknown_ssdp(self):
        response = "HTTP/1.1 200 OK\r\nSERVER: SomeOtherDevice/1.0\r\n\r\n"
        assert _parse_ssdp_response(response) is None

    def test_extract_header(self):
        response = "HTTP/1.1 200 OK\r\nSERVER: TestServer/1.0\r\nLOCATION: http://10.0.0.1/\r\n\r\n"
        assert _extract_ssdp_header(response, "SERVER") == "TestServer/1.0"
        assert _extract_ssdp_header(response, "LOCATION") == "http://10.0.0.1/"
        assert _extract_ssdp_header(response, "MISSING") is None


class TestSubnetDetection:
    """Test local subnet auto-detection."""

    def test_get_local_subnet_returns_cidr_or_none(self):
        result = _get_local_subnet()
        # May return None in CI environments without a default route
        if result is not None:
            assert "/" in result
            assert result.endswith("/24")


# ---------------------------------------------------------------------------
# DiscoveredService dataclass
# ---------------------------------------------------------------------------


class TestDiscoveredService:
    def test_basic_construction(self):
        svc = DiscoveredService(
            protocol="mdns",
            service_type="_shelly._tcp.local.",
            host="192.168.1.50",
            port=80,
            name="Shelly Plug",
            adapter_id="shelly.gen2",
        )
        assert svc.host == "192.168.1.50"
        assert svc.adapter_id == "shelly.gen2"
        assert svc.properties == {}

    def test_defaults(self):
        svc = DiscoveredService(
            protocol="port_scan",
            service_type="tcp:8080",
            host="10.0.0.1",
            port=8080,
        )
        assert svc.name == ""
        assert svc.adapter_id is None
        assert svc.properties == {}


# ---------------------------------------------------------------------------
# NetworkScanner orchestrator
# ---------------------------------------------------------------------------


class TestNetworkScanner:
    """Test the orchestrator (with mocked scan functions)."""

    @pytest.fixture
    def mock_services(self):
        return [
            DiscoveredService(
                protocol="mdns",
                service_type="_shelly._tcp.local.",
                host="192.168.1.10",
                port=80,
                name="Shelly Kitchen",
                adapter_id="shelly.gen2",
            ),
            DiscoveredService(
                protocol="mdns",
                service_type="_hue._tcp.local.",
                host="192.168.1.20",
                port=443,
                name="Hue Bridge",
                adapter_id="hue.bridge",
            ),
            DiscoveredService(
                protocol="port_scan",
                service_type="tcp:8080",
                host="192.168.1.30",
                port=8080,
                name="zigbee2mqtt @ 192.168.1.30",
                adapter_id="zigbee2mqtt.http",
            ),
        ]

    def _patch_all_scans(self):
        """Helper to patch all 4 scan methods."""
        return (
            patch("core.network_scanner.mdns_scan", new_callable=AsyncMock),
            patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock),
            patch("core.network_scanner.port_scan", new_callable=AsyncMock),
            patch("core.network_scanner.http_probe", new_callable=AsyncMock),
        )

    @pytest.mark.asyncio
    async def test_scan_returns_discovered_targets(self, mock_services):
        scanner = NetworkScanner()
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = mock_services[:2]
            mock_ssdp.return_value = []
            mock_port.return_value = mock_services[2:]
            mock_http.return_value = []

            targets = await scanner.scan()

        assert len(targets) == 3
        adapter_ids = {t.adapter_id for t in targets}
        assert adapter_ids == {"shelly.gen2", "hue.bridge", "zigbee2mqtt.http"}

    @pytest.mark.asyncio
    async def test_scan_deduplicates_by_host_and_adapter(self, mock_services):
        """Same host+adapter from different protocols should be deduplicated."""
        scanner = NetworkScanner()

        dup = DiscoveredService(
            protocol="ssdp",
            service_type="upnp",
            host="192.168.1.10",  # same as Shelly from mDNS
            port=80,
            name="Shelly via SSDP",
            adapter_id="shelly.gen2",
        )
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = [mock_services[0]]
            mock_ssdp.return_value = [dup]
            mock_port.return_value = []
            mock_http.return_value = []

            targets = await scanner.scan()

        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_scan_with_adapter_filter(self, mock_services):
        scanner = NetworkScanner()
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = mock_services[:2]
            mock_ssdp.return_value = []
            mock_port.return_value = mock_services[2:]
            mock_http.return_value = []

            targets = await scanner.scan(adapter_filter=["shelly.gen2"])

        assert len(targets) == 1
        assert targets[0].adapter_id == "shelly.gen2"

    @pytest.mark.asyncio
    async def test_scan_with_selective_methods(self):
        scanner = NetworkScanner()
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = []
            mock_ssdp.return_value = []
            mock_port.return_value = []
            mock_http.return_value = []

            await scanner.scan(methods=["mdns"])

        mock_mdns.assert_called_once()
        mock_ssdp.assert_not_called()
        mock_port.assert_not_called()
        mock_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_handles_method_failure(self, mock_services):
        """If one scan method fails, others should still return results."""
        scanner = NetworkScanner()
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.side_effect = Exception("zeroconf crash")
            mock_ssdp.return_value = []
            mock_port.return_value = [mock_services[2]]
            mock_http.return_value = []

            targets = await scanner.scan()

        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_scan_skips_services_without_adapter_id(self):
        scanner = NetworkScanner()

        svc_no_adapter = DiscoveredService(
            protocol="port_scan",
            service_type="tcp:9999",
            host="192.168.1.99",
            port=9999,
            name="unknown",
            adapter_id=None,
        )
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = []
            mock_ssdp.return_value = []
            mock_port.return_value = [svc_no_adapter]
            mock_http.return_value = []

            targets = await scanner.scan()

        assert len(targets) == 0

    @pytest.mark.asyncio
    async def test_scan_and_commission_without_registry(self, mock_services):
        """scan_and_commission without auto_commission just returns targets."""
        scanner = NetworkScanner()
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = mock_services[:1]
            mock_ssdp.return_value = []
            mock_port.return_value = []
            mock_http.return_value = []

            summary = await scanner.scan_and_commission(auto_commission=False)

        assert summary["targets_found"] == 1
        assert len(summary["commissioned"]) == 0

    @pytest.mark.asyncio
    async def test_scan_and_commission_with_registry(self, mock_services):
        """scan_and_commission with auto_commission calls registry."""
        mock_registry = MagicMock()
        mock_adapter = MagicMock()
        mock_registry.get_adapter.return_value = mock_adapter

        commission_result = MagicMock()
        commission_result.status = "ok"
        commission_result.connection_id = "conn_abc123"
        commission_result.diagnostics = {}
        mock_registry.commission_simple = AsyncMock(return_value=commission_result)

        scanner = NetworkScanner(registry=mock_registry)
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = mock_services[:1]
            mock_ssdp.return_value = []
            mock_port.return_value = []
            mock_http.return_value = []

            summary = await scanner.scan_and_commission(auto_commission=True)

        assert summary["targets_found"] == 1
        assert len(summary["commissioned"]) == 1
        assert summary["commissioned"][0]["connection_id"] == "conn_abc123"

    @pytest.mark.asyncio
    async def test_scan_and_commission_handles_adapter_not_found(self, mock_services):
        mock_registry = MagicMock()
        mock_registry.get_adapter.side_effect = KeyError("not found")

        scanner = NetworkScanner(registry=mock_registry)
        p1, p2, p3, p4 = self._patch_all_scans()

        with p1 as mock_mdns, p2 as mock_ssdp, p3 as mock_port, p4 as mock_http:
            mock_mdns.return_value = mock_services[:1]
            mock_ssdp.return_value = []
            mock_port.return_value = []
            mock_http.return_value = []

            summary = await scanner.scan_and_commission(auto_commission=True)

        assert len(summary["errors"]) == 1
        assert "not registered" in summary["errors"][0]["error"]


# ---------------------------------------------------------------------------
# Port probe (unit-level)
# ---------------------------------------------------------------------------


class TestPortProbe:

    @pytest.mark.asyncio
    async def test_probe_closed_port(self):
        """Probing a port that refuses connection returns False."""
        # Use a port that's almost certainly not listening
        result = await _probe_port("127.0.0.1", 59999, timeout=0.5)
        assert result is False


# ---------------------------------------------------------------------------
# mdns_scan without zeroconf
# ---------------------------------------------------------------------------


class TestMdnsScanNoZeroconf:

    @pytest.mark.asyncio
    async def test_returns_empty_without_zeroconf(self):
        from core.network_scanner import mdns_scan
        with patch("core.network_scanner._HAS_ZEROCONF", False):
            results = await mdns_scan(timeout=1.0)
        assert results == []


# ---------------------------------------------------------------------------
# port_scan edge cases
# ---------------------------------------------------------------------------


class TestPortScan:

    @pytest.mark.asyncio
    async def test_invalid_subnet(self):
        from core.network_scanner import port_scan
        results = await port_scan(subnet="not-a-subnet")
        assert results == []

    @pytest.mark.asyncio
    async def test_large_subnet_rejected(self):
        from core.network_scanner import port_scan
        results = await port_scan(subnet="10.0.0.0/16")
        assert results == []

    @pytest.mark.asyncio
    async def test_no_subnet_detected(self):
        from core.network_scanner import port_scan
        with patch("core.network_scanner._get_local_subnet", return_value=None):
            results = await port_scan(subnet=None)
        assert results == []


# ---------------------------------------------------------------------------
# Spaces YAML generator
# ---------------------------------------------------------------------------


class TestSpacesYamlGenerator:

    def _make_snapshot(self):
        return InventorySnapshot(
            connection_id="kincony_abc123",
            devices=[{
                "device_id": "dev_kc868_a4",
                "name": "Tasmota",
                "device_family": "kincony.tasmota",
            }],
            endpoints=[
                {"endpoint_id": "dev_kc868_a4_relay_1", "device_id": "dev_kc868_a4"},
                {"endpoint_id": "dev_kc868_a4_relay_2", "device_id": "dev_kc868_a4"},
                {"endpoint_id": "dev_kc868_a4_dinput_1", "device_id": "dev_kc868_a4"},
                {"endpoint_id": "dev_kc868_a4_analog_1", "device_id": "dev_kc868_a4"},
            ],
            points=[
                {
                    "point_id": "dev_kc868_a4_relay_1_state",
                    "endpoint_id": "dev_kc868_a4_relay_1",
                    "point_class": "switch.state",
                    "value_type": "bool",
                    "writable": True,
                    "native_ref": "relay_1",
                },
                {
                    "point_id": "dev_kc868_a4_relay_2_state",
                    "endpoint_id": "dev_kc868_a4_relay_2",
                    "point_class": "switch.state",
                    "value_type": "bool",
                    "writable": True,
                    "native_ref": "relay_2",
                },
                {
                    "point_id": "dev_kc868_a4_dinput_1_state",
                    "endpoint_id": "dev_kc868_a4_dinput_1",
                    "point_class": "digital_input.state",
                    "value_type": "bool",
                    "writable": False,
                    "native_ref": "digital_input_1",
                },
                {
                    "point_id": "dev_kc868_a4_analog_1_value",
                    "endpoint_id": "dev_kc868_a4_analog_1",
                    "point_class": "analog_input.value",
                    "value_type": "float",
                    "writable": False,
                    "native_ref": "analog_1",
                    "unit": "V",
                },
            ],
        )

    def test_generates_valid_structure(self):
        snapshot = self._make_snapshot()
        comm = {"connection_id": "kincony_abc123", "adapter_id": "kincony.family", "address": "192.168.0.90"}
        result = generate_spaces_yaml([(comm, snapshot)], site_name="test_home")

        assert result["site"] == "test_home"
        assert "spaces" in result
        assert "main" in result["spaces"]
        devices = result["spaces"]["main"]["devices"]
        assert len(devices) == 4

    def test_assigns_correct_capabilities(self):
        snapshot = self._make_snapshot()
        comm = {"connection_id": "kincony_abc123", "adapter_id": "kincony.family", "address": "192.168.0.90"}
        result = generate_spaces_yaml([(comm, snapshot)])

        devices = result["spaces"]["main"]["devices"]
        relay_keys = [k for k in devices if "relay" in k]
        assert len(relay_keys) >= 2

        relay = devices[relay_keys[0]]
        assert "binary_switch" in relay["capabilities"]
        assert relay["ai_access"] == "full"
        assert relay["safety_class"] == "S1"

    def test_assigns_read_only_for_sensors(self):
        snapshot = self._make_snapshot()
        comm = {"connection_id": "kincony_abc123", "adapter_id": "kincony.family", "address": "192.168.0.90"}
        result = generate_spaces_yaml([(comm, snapshot)])

        devices = result["spaces"]["main"]["devices"]
        dinput_keys = [k for k in devices if "digital_input" in k]
        assert len(dinput_keys) >= 1

        dinput = devices[dinput_keys[0]]
        assert dinput["ai_access"] == "read_only"
        assert dinput["safety_class"] == "S0"

    def test_includes_unit_when_present(self):
        snapshot = self._make_snapshot()
        comm = {"connection_id": "kincony_abc123", "adapter_id": "kincony.family", "address": "192.168.0.90"}
        result = generate_spaces_yaml([(comm, snapshot)])

        devices = result["spaces"]["main"]["devices"]
        analog_keys = [k for k in devices if "analog" in k]
        assert len(analog_keys) >= 1
        assert devices[analog_keys[0]]["unit"] == "V"

    def test_multiple_snapshots(self):
        snapshot1 = self._make_snapshot()
        comm1 = {"connection_id": "kincony_abc123", "adapter_id": "kincony.family", "address": "192.168.0.90"}

        snapshot2 = InventorySnapshot(
            connection_id="shelly_def456",
            devices=[{"device_id": "dev_shelly_1", "name": "Shelly Plug"}],
            endpoints=[{"endpoint_id": "dev_shelly_1_switch", "device_id": "dev_shelly_1"}],
            points=[{
                "point_id": "dev_shelly_1_switch_state",
                "endpoint_id": "dev_shelly_1_switch",
                "point_class": "switch.state",
                "value_type": "bool",
                "writable": True,
                "native_ref": "switch_0",
            }],
        )
        comm2 = {"connection_id": "shelly_def456", "adapter_id": "shelly.gen2", "address": "192.168.0.91"}

        result = generate_spaces_yaml([(comm1, snapshot1), (comm2, snapshot2)])
        devices = result["spaces"]["main"]["devices"]
        assert len(devices) == 5  # 4 from KinCony + 1 from Shelly


class TestSemanticNaming:

    def test_native_ref_used(self):
        name = _point_to_semantic_name("switch.state", "relay_1", "ep_id", "tasmota", "kincony")
        assert name == "kincony_relay_1"

    def test_no_duplicate_prefix(self):
        name = _point_to_semantic_name("switch.state", "kincony_relay_1", "ep_id", "tasmota", "kincony")
        assert name == "kincony_relay_1"

    def test_fallback_to_endpoint(self):
        name = _point_to_semantic_name("switch.state", "", "dev_abc_relay_1", "tasmota", "kincony")
        assert "relay" in name

    def test_final_fallback(self):
        name = _point_to_semantic_name("switch.state", "", "", "tasmota", "kincony")
        assert name == "kincony_tasmota"


# ---------------------------------------------------------------------------
# Continuous discovery
# ---------------------------------------------------------------------------


class TestContinuousDiscovery:

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        scanner = NetworkScanner()

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as m1, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as m2, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as m3, \
             patch("core.network_scanner.http_probe", new_callable=AsyncMock) as m4:
            m1.return_value = []
            m2.return_value = []
            m3.return_value = []
            m4.return_value = []

            task = await scanner.start_continuous(interval=0.1)
            await asyncio.sleep(0.3)
            scanner.stop_continuous()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_detects_new_device(self):
        scanner = NetworkScanner()
        found = []

        new_svc = DiscoveredService(
            protocol="mdns",
            service_type="_shelly._tcp.local.",
            host="192.168.1.50",
            port=80,
            name="New Shelly",
            adapter_id="shelly.gen2",
        )

        call_count = 0

        async def mock_mdns(timeout=10.0):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return [new_svc]
            return []

        def on_new(target):
            found.append(target)

        with patch("core.network_scanner.mdns_scan", side_effect=mock_mdns), \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as m2, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as m3, \
             patch("core.network_scanner.http_probe", new_callable=AsyncMock) as m4:
            m2.return_value = []
            m3.return_value = []
            m4.return_value = []

            task = await scanner.start_continuous(
                interval=0.1,
                methods=["mdns"],
                on_new_device=on_new,
            )
            await asyncio.sleep(0.5)
            scanner.stop_continuous()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(found) >= 1
        assert found[0].adapter_id == "shelly.gen2"
