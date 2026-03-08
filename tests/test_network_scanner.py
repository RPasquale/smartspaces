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
    _probe_port,
)


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

    @pytest.mark.asyncio
    async def test_scan_returns_discovered_targets(self, mock_services):
        scanner = NetworkScanner()

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = mock_services[:2]
            mock_ssdp.return_value = []
            mock_port.return_value = mock_services[2:]

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

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = [mock_services[0]]
            mock_ssdp.return_value = [dup]
            mock_port.return_value = []

            targets = await scanner.scan()

        assert len(targets) == 1

    @pytest.mark.asyncio
    async def test_scan_with_adapter_filter(self, mock_services):
        scanner = NetworkScanner()

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = mock_services[:2]
            mock_ssdp.return_value = []
            mock_port.return_value = mock_services[2:]

            targets = await scanner.scan(adapter_filter=["shelly.gen2"])

        assert len(targets) == 1
        assert targets[0].adapter_id == "shelly.gen2"

    @pytest.mark.asyncio
    async def test_scan_with_selective_methods(self):
        scanner = NetworkScanner()

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = []
            mock_ssdp.return_value = []
            mock_port.return_value = []

            await scanner.scan(methods=["mdns"])

        mock_mdns.assert_called_once()
        mock_ssdp.assert_not_called()
        mock_port.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_handles_method_failure(self, mock_services):
        """If one scan method fails, others should still return results."""
        scanner = NetworkScanner()

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.side_effect = Exception("zeroconf crash")
            mock_ssdp.return_value = []
            mock_port.return_value = [mock_services[2]]

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

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = []
            mock_ssdp.return_value = []
            mock_port.return_value = [svc_no_adapter]

            targets = await scanner.scan()

        assert len(targets) == 0

    @pytest.mark.asyncio
    async def test_scan_and_commission_without_registry(self, mock_services):
        """scan_and_commission without auto_commission just returns targets."""
        scanner = NetworkScanner()

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = mock_services[:1]
            mock_ssdp.return_value = []
            mock_port.return_value = []

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

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = mock_services[:1]
            mock_ssdp.return_value = []
            mock_port.return_value = []

            summary = await scanner.scan_and_commission(auto_commission=True)

        assert summary["targets_found"] == 1
        assert len(summary["commissioned"]) == 1
        assert summary["commissioned"][0]["connection_id"] == "conn_abc123"

    @pytest.mark.asyncio
    async def test_scan_and_commission_handles_adapter_not_found(self, mock_services):
        mock_registry = MagicMock()
        mock_registry.get_adapter.side_effect = KeyError("not found")

        scanner = NetworkScanner(registry=mock_registry)

        with patch("core.network_scanner.mdns_scan", new_callable=AsyncMock) as mock_mdns, \
             patch("core.network_scanner.ssdp_scan", new_callable=AsyncMock) as mock_ssdp, \
             patch("core.network_scanner.port_scan", new_callable=AsyncMock) as mock_port:

            mock_mdns.return_value = mock_services[:1]
            mock_ssdp.return_value = []
            mock_port.return_value = []

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
