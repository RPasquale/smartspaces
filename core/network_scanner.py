"""Network scanner — discovers devices on the local network.

Scans using mDNS/DNS-SD, SSDP, async port probing, and HTTP fingerprinting,
then maps discovered services to SmartSpaces adapter IDs. Provides one-shot
scanning, auto-commissioning, spaces.yaml generation, and background
continuous discovery.

Requires ``zeroconf`` for mDNS (``pip install 'smartspaces[discovery]'``).
SSDP, port scanning, and HTTP probing use only the standard library + httpx.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sdk.adapter_api.base import DiscoveredTarget, InventorySnapshot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# mDNS service type → adapter mapping
# ---------------------------------------------------------------------------

MDNS_SERVICE_MAP: dict[str, str] = {
    "_shelly._tcp.local.": "shelly.gen2",
    "_esphomelib._tcp.local.": "esphome.native",
    "_hue._tcp.local.": "hue.bridge",
    "_leap._tcp.local.": "lutron.caseta",
    "_matter._tcp.local.": "matter.python",
    "_matterc._udp.local.": "matter.python",
}

# Generic HTTP services need fingerprinting
_HTTP_SERVICE = "_http._tcp.local."

# Well-known ports for services without mDNS
PORT_SERVICE_MAP: dict[int, str] = {
    8080: "zigbee2mqtt",       # Zigbee2MQTT web UI
    3000: "zwave-js",          # Z-Wave JS UI
    5580: "matter-server",     # python-matter-server
}

# Port → adapter ID (for confirmed services)
PORT_ADAPTER_MAP: dict[int, str] = {
    8080: "zigbee2mqtt.http",
    3000: "zwave.jsui",
    5580: "matter.python",
}

# SSDP target → adapter ID
SSDP_DEVICE_MAP: dict[str, str] = {
    "IpBridge": "hue.bridge",
    "urn:schemas-upnp-org:device:Basic:1": "hue.bridge",
}

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredService:
    """Raw service found on the network before adapter mapping."""
    protocol: str           # "mdns", "ssdp", "port_scan", "ws_discovery"
    service_type: str       # e.g. "_shelly._tcp.local." or "tcp:8080"
    host: str               # IP address
    port: int
    name: str = ""          # device/service name
    properties: dict[str, Any] = field(default_factory=dict)
    adapter_id: str | None = None  # resolved adapter ID


# ---------------------------------------------------------------------------
# mDNS scanner (requires zeroconf)
# ---------------------------------------------------------------------------

try:
    from zeroconf import IPVersion
    from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
    _HAS_ZEROCONF = True
except ImportError:
    _HAS_ZEROCONF = False


async def mdns_scan(timeout: float = 10.0) -> list[DiscoveredService]:
    """Scan for devices via mDNS/DNS-SD.

    Browses all service types in MDNS_SERVICE_MAP plus generic HTTP.
    Returns discovered services with adapter IDs pre-mapped.
    """
    if not _HAS_ZEROCONF:
        logger.debug("zeroconf not installed, skipping mDNS scan")
        return []

    results: list[DiscoveredService] = []
    found_names: set[str] = set()
    discovered_service_names: list[tuple[str, str]] = []  # (service_type, name)

    service_types = list(MDNS_SERVICE_MAP.keys()) + [_HTTP_SERVICE]

    def _on_service_state_change(zeroconf, service_type, name, state_change):
        """Handler called by ServiceBrowser when services are found."""
        discovered_service_names.append((service_type, name))

    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        browsers = []
        for stype in service_types:
            browser = AsyncServiceBrowser(
                azc.zeroconf, stype, handlers=[_on_service_state_change]
            )
            browsers.append(browser)

        # Let scanning run for the timeout period
        await asyncio.sleep(timeout)

        # Resolve details for each discovered service
        for stype, name in discovered_service_names:
            if name in found_names:
                continue
            found_names.add(name)

            info = AsyncServiceInfo(stype, name)
            await info.async_request(azc.zeroconf, timeout=3000)

            addresses = info.parsed_scoped_addresses(IPVersion.V4Only)
            if not addresses:
                continue

            host = addresses[0]
            port = info.port or 80
            props = {}
            if info.properties:
                for k, v in info.properties.items():
                    key = k.decode() if isinstance(k, bytes) else str(k)
                    val = v.decode() if isinstance(v, bytes) else str(v)
                    props[key] = val

            svc_name = info.name or name
            adapter_id = MDNS_SERVICE_MAP.get(stype)

            # For generic HTTP, try to fingerprint from properties
            if stype == _HTTP_SERVICE and adapter_id is None:
                adapter_id = _fingerprint_http_mdns(props, svc_name)

            if adapter_id:
                results.append(DiscoveredService(
                    protocol="mdns",
                    service_type=stype,
                    host=host,
                    port=port,
                    name=svc_name,
                    properties=props,
                    adapter_id=adapter_id,
                ))

        # Cancel browsers
        for browser in browsers:
            await browser.async_cancel()

    except Exception:
        logger.exception("mDNS scan error")
    finally:
        await azc.async_close()

    logger.info("mDNS scan found %d services", len(results))
    return results


def _fingerprint_http_mdns(properties: dict[str, Any], name: str) -> str | None:
    """Try to identify the adapter from generic HTTP mDNS TXT records."""
    name_lower = name.lower()
    all_vals = " ".join(str(v) for v in properties.values()).lower()
    combined = f"{name_lower} {all_vals}"

    if "tasmota" in combined:
        return "kincony.family"
    if "esphome" in combined:
        return "esphome.native"
    if "shelly" in combined:
        return "shelly.gen2"
    if "zigbee2mqtt" in combined:
        return "zigbee2mqtt.http"
    return None


# ---------------------------------------------------------------------------
# SSDP scanner (no external dependency)
# ---------------------------------------------------------------------------

_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900
_SSDP_MX = 5

_SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: {mx}\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
)


async def ssdp_scan(timeout: float = 8.0) -> list[DiscoveredService]:
    """Scan for devices via SSDP/UPnP multicast discovery."""
    results: list[DiscoveredService] = []
    seen_hosts: set[str] = set()

    try:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0)

        msg = _SSDP_MSEARCH.format(mx=_SSDP_MX).encode()
        sock.sendto(msg, (_SSDP_ADDR, _SSDP_PORT))

        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                data, addr = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: sock.recvfrom(4096)),
                    timeout=max(0.1, deadline - loop.time()),
                )
            except (asyncio.TimeoutError, OSError):
                break

            host = addr[0]
            if host in seen_hosts:
                continue
            seen_hosts.add(host)

            response = data.decode(errors="replace")
            adapter_id = _parse_ssdp_response(response)
            if adapter_id:
                # Extract a friendly name from the response
                name = _extract_ssdp_header(response, "SERVER") or f"SSDP device @ {host}"
                results.append(DiscoveredService(
                    protocol="ssdp",
                    service_type="upnp",
                    host=host,
                    port=80,
                    name=name,
                    properties={"raw_headers": response[:500]},
                    adapter_id=adapter_id,
                ))

        sock.close()
    except Exception:
        logger.debug("SSDP scan error", exc_info=True)

    logger.info("SSDP scan found %d services", len(results))
    return results


def _parse_ssdp_response(response: str) -> str | None:
    """Match an SSDP response to a known adapter."""
    upper = response.upper()
    for keyword, adapter_id in SSDP_DEVICE_MAP.items():
        if keyword.upper() in upper:
            return adapter_id
    return None


def _extract_ssdp_header(response: str, header: str) -> str | None:
    """Extract an HTTP-style header value from an SSDP response."""
    for line in response.split("\r\n"):
        if line.upper().startswith(header.upper() + ":"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------------------
# Port scanner (async TCP connect probes)
# ---------------------------------------------------------------------------


def _get_local_subnet() -> str | None:
    """Detect the local subnet by inspecting the default route interface."""
    try:
        # Connect to a public IP (doesn't send data) to find our local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        # Assume /24 for typical home/office networks
        network = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return str(network)
    except Exception:
        return None


async def _probe_port(host: str, port: int, timeout: float = 1.5) -> bool:
    """Try to open a TCP connection to host:port."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
        return False


async def port_scan(
    subnet: str | None = None,
    ports: list[int] | None = None,
    timeout: float = 20.0,
    concurrency: int = 100,
) -> list[DiscoveredService]:
    """Scan a subnet for known service ports.

    Probes well-known ports (Zigbee2MQTT, Z-Wave JS, etc.) across
    the local /24 subnet using async TCP connect.
    """
    if subnet is None:
        subnet = _get_local_subnet()
        if subnet is None:
            logger.debug("Could not detect local subnet, skipping port scan")
            return []

    if ports is None:
        ports = list(PORT_SERVICE_MAP.keys())

    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError:
        logger.warning("Invalid subnet: %s", subnet)
        return []

    # Skip networks larger than /22 (1024 hosts) to avoid excessive scanning
    if network.num_addresses > 1024:
        logger.warning("Subnet %s too large (>1024 hosts), skipping port scan", subnet)
        return []

    results: list[DiscoveredService] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def _check(host_str: str, port: int) -> DiscoveredService | None:
        async with semaphore:
            if await _probe_port(host_str, port, timeout=1.5):
                service_name = PORT_SERVICE_MAP.get(port, f"tcp:{port}")
                adapter_id = PORT_ADAPTER_MAP.get(port)
                return DiscoveredService(
                    protocol="port_scan",
                    service_type=f"tcp:{port}",
                    host=host_str,
                    port=port,
                    name=f"{service_name} @ {host_str}",
                    adapter_id=adapter_id,
                )
        return None

    # Build task list
    tasks = []
    for host_addr in network.hosts():
        host_str = str(host_addr)
        for port in ports:
            tasks.append(_check(host_str, port))

    # Run with overall timeout
    try:
        done = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
        for result in done:
            if isinstance(result, DiscoveredService):
                results.append(result)
    except asyncio.TimeoutError:
        logger.warning("Port scan timed out after %.0fs", timeout)

    logger.info("Port scan found %d services on %s", len(results), subnet)
    return results


# ---------------------------------------------------------------------------
# HTTP probe — identifies Tasmota/Shelly/ESPHome devices on port 80
# ---------------------------------------------------------------------------

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

# Known HTTP fingerprint endpoints
_HTTP_FINGERPRINTS = [
    # (path, response_key, adapter_id, device_name_key)
    ("/cm?cmnd=Status%200", "Status", "kincony.family", "DeviceName"),
    ("/rpc/Shelly.GetDeviceInfo", "id", "shelly.gen2", "id"),
]


async def http_probe(
    subnet: str | None = None,
    timeout: float = 20.0,
    concurrency: int = 50,
) -> list[DiscoveredService]:
    """Probe hosts on port 80 for known HTTP device APIs (Tasmota, Shelly, etc.)."""
    if not _HAS_HTTPX:
        logger.debug("httpx not available, skipping HTTP probe")
        return []

    if subnet is None:
        subnet = _get_local_subnet()
        if subnet is None:
            return []

    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError:
        return []

    if network.num_addresses > 1024:
        return []

    results: list[DiscoveredService] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def _check_host(host_str: str) -> None:
        async with semaphore:
            # First check if port 80 is open
            if not await _probe_port(host_str, 80, timeout=1.0):
                return

            # Try each fingerprint endpoint
            async with httpx.AsyncClient(timeout=3.0) as client:
                for path, resp_key, adapter_id, name_key in _HTTP_FINGERPRINTS:
                    try:
                        resp = await client.get(f"http://{host_str}{path}")
                        if resp.status_code == 200:
                            data = resp.json()
                            if resp_key in data:
                                # Extract device name
                                name = host_str
                                if resp_key in data and isinstance(data[resp_key], dict):
                                    name = data[resp_key].get(name_key, host_str)
                                elif name_key in data:
                                    name = data[name_key]

                                props = {}
                                # For Tasmota, extract useful info
                                if adapter_id == "kincony.family" and "Status" in data:
                                    status = data["Status"]
                                    name = status.get("DeviceName", host_str)
                                    props["topic"] = status.get("Topic", "")
                                    props["module"] = str(status.get("Module", ""))
                                    props["friendly_names"] = str(
                                        status.get("FriendlyName", [])
                                    )

                                results.append(DiscoveredService(
                                    protocol="http_probe",
                                    service_type=f"http:{path.split('?')[0]}",
                                    host=host_str,
                                    port=80,
                                    name=f"{name} @ {host_str}",
                                    properties=props,
                                    adapter_id=adapter_id,
                                ))
                                return  # Found a match, stop checking
                    except Exception:
                        continue

    tasks = [_check_host(str(addr)) for addr in network.hosts()]

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("HTTP probe timed out after %.0fs", timeout)

    logger.info("HTTP probe found %d devices on %s", len(results), subnet)
    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class NetworkScanner:
    """Orchestrates network discovery across multiple protocols.

    Runs mDNS, SSDP, and port scanning in parallel and maps results
    to SmartSpaces adapter IDs.
    """

    def __init__(self, registry: Any = None):
        self._registry = registry

    async def scan(
        self,
        methods: list[str] | None = None,
        subnet: str | None = None,
        timeout: float = 15.0,
        adapter_filter: list[str] | None = None,
    ) -> list[DiscoveredTarget]:
        """Run a full network scan and return discovered targets.

        Args:
            methods: Discovery protocols to use. Default: all available.
                     Options: "mdns", "ssdp", "port_scan"
            subnet: Subnet for port scanning (e.g. "192.168.1.0/24").
                    Auto-detected if None.
            timeout: Maximum scan duration in seconds.
            adapter_filter: Only return results for these adapter IDs.

        Returns:
            List of DiscoveredTarget ready for commissioning.
        """
        if methods is None:
            methods = ["mdns", "ssdp", "port_scan", "http_probe"]

        # Launch scans in parallel
        tasks: dict[str, asyncio.Task] = {}
        if "mdns" in methods:
            tasks["mdns"] = asyncio.create_task(mdns_scan(timeout=timeout))
        if "ssdp" in methods:
            tasks["ssdp"] = asyncio.create_task(ssdp_scan(timeout=timeout))
        if "port_scan" in methods:
            tasks["port_scan"] = asyncio.create_task(
                port_scan(subnet=subnet, timeout=timeout)
            )
        if "http_probe" in methods:
            tasks["http_probe"] = asyncio.create_task(
                http_probe(subnet=subnet, timeout=timeout)
            )

        all_services: list[DiscoveredService] = []
        for name, task in tasks.items():
            try:
                result = await task
                all_services.extend(result)
            except Exception:
                logger.exception("Scan method %s failed", name)

        # Deduplicate by (host, adapter_id)
        seen: set[tuple[str, str]] = set()
        targets: list[DiscoveredTarget] = []

        for svc in all_services:
            if svc.adapter_id is None:
                continue
            key = (svc.host, svc.adapter_id)
            if key in seen:
                continue
            seen.add(key)

            if adapter_filter and svc.adapter_id not in adapter_filter:
                continue

            targets.append(DiscoveredTarget(
                discovery_id=f"scan_{uuid.uuid4().hex[:8]}",
                adapter_id=svc.adapter_id,
                native_ref=svc.host,
                title=svc.name,
                address=svc.host,
                fingerprint={
                    "protocol": svc.protocol,
                    "service_type": svc.service_type,
                    "port": svc.port,
                    **svc.properties,
                },
                confidence=0.9 if svc.protocol == "mdns" else 0.7,
            ))

        logger.info(
            "Network scan complete: %d services found, %d unique targets",
            len(all_services), len(targets),
        )
        return targets

    async def scan_and_commission(
        self,
        methods: list[str] | None = None,
        subnet: str | None = None,
        timeout: float = 15.0,
        auto_commission: bool = False,
    ) -> dict[str, Any]:
        """Scan the network and optionally auto-commission discovered devices.

        Returns a summary of what was found and what was commissioned.
        """
        targets = await self.scan(
            methods=methods, subnet=subnet, timeout=timeout
        )

        summary: dict[str, Any] = {
            "targets_found": len(targets),
            "targets": [],
            "commissioned": [],
            "errors": [],
        }

        for target in targets:
            target_info = {
                "adapter_id": target.adapter_id,
                "title": target.title,
                "address": target.address,
                "confidence": target.confidence,
                "protocol": target.fingerprint.get("protocol", "unknown"),
            }
            summary["targets"].append(target_info)

            if auto_commission and self._registry:
                try:
                    # Check if this adapter is registered
                    adapter = self._registry.get_adapter(target.adapter_id)
                except Exception:
                    summary["errors"].append({
                        "address": target.address,
                        "adapter_id": target.adapter_id,
                        "error": f"Adapter {target.adapter_id} not registered",
                    })
                    continue

                # Build commission fields from discovery
                fields = {"host": target.address}
                port = target.fingerprint.get("port")
                if port and port not in (80, 443):
                    fields["port"] = port

                try:
                    result = await self._registry.commission_simple(
                        target.adapter_id,
                        "default",
                        fields,
                    )
                    if result.status == "ok":
                        summary["commissioned"].append({
                            "connection_id": result.connection_id,
                            "adapter_id": target.adapter_id,
                            "address": target.address,
                        })
                        logger.info(
                            "Auto-commissioned %s @ %s → %s",
                            target.adapter_id, target.address,
                            result.connection_id,
                        )
                    else:
                        summary["errors"].append({
                            "address": target.address,
                            "adapter_id": target.adapter_id,
                            "error": str(result.diagnostics),
                        })
                except Exception as e:
                    summary["errors"].append({
                        "address": target.address,
                        "adapter_id": target.adapter_id,
                        "error": str(e),
                    })

        return summary

    async def scan_commission_and_generate(
        self,
        methods: list[str] | None = None,
        subnet: str | None = None,
        timeout: float = 15.0,
        output_path: str | Path = "spaces_auto.yaml",
        site_name: str = "my_home",
    ) -> dict[str, Any]:
        """Full auto-setup: scan, commission, inventory, and generate spaces.yaml.

        This is the one-call solution: discovers devices on the network,
        commissions them, inventories all their points, and writes a
        spaces.yaml mapping every endpoint to a semantic name.
        """
        summary = await self.scan_and_commission(
            methods=methods, subnet=subnet, timeout=timeout,
            auto_commission=True,
        )

        if not self._registry or not summary["commissioned"]:
            summary["spaces_yaml"] = None
            return summary

        # Inventory all commissioned connections
        all_snapshots: list[tuple[dict, InventorySnapshot]] = []
        for comm in summary["commissioned"]:
            try:
                snapshot = await self._registry.inventory(comm["connection_id"])
                all_snapshots.append((comm, snapshot))
            except Exception as e:
                logger.warning("Inventory failed for %s: %s", comm["connection_id"], e)

        # Generate spaces.yaml
        spaces_data = generate_spaces_yaml(all_snapshots, site_name=site_name)
        output = Path(output_path)
        output.write_text(yaml.dump(spaces_data, default_flow_style=False, sort_keys=False))
        logger.info("Generated %s with %d spaces", output, len(spaces_data.get("spaces", {})))
        summary["spaces_yaml"] = str(output)
        summary["spaces_data"] = spaces_data

        return summary

    async def start_continuous(
        self,
        interval: float = 60.0,
        methods: list[str] | None = None,
        subnet: str | None = None,
        on_new_device: Any = None,
    ) -> asyncio.Task:
        """Start background continuous discovery.

        Periodically rescans and calls on_new_device(target) for each
        newly-discovered device.

        Returns the background task (cancel it to stop).
        """
        self._known_hosts: set[tuple[str, str]] = set()
        self._continuous_running = True

        # Seed known hosts from initial scan
        try:
            initial = await self.scan(methods=methods, subnet=subnet, timeout=15.0)
            for t in initial:
                self._known_hosts.add((t.address, t.adapter_id))
        except Exception:
            logger.debug("Initial continuous scan failed", exc_info=True)

        async def _loop():
            while self._continuous_running:
                await asyncio.sleep(interval)
                try:
                    targets = await self.scan(
                        methods=methods or ["http_probe", "mdns"],
                        subnet=subnet,
                        timeout=15.0,
                    )
                    for t in targets:
                        key = (t.address, t.adapter_id)
                        if key not in self._known_hosts:
                            self._known_hosts.add(key)
                            logger.info(
                                "New device discovered: %s (%s) at %s",
                                t.title, t.adapter_id, t.address,
                            )
                            if on_new_device:
                                try:
                                    result = on_new_device(t)
                                    if asyncio.iscoroutine(result):
                                        await result
                                except Exception:
                                    logger.exception("on_new_device callback failed")
                except Exception:
                    logger.debug("Continuous scan cycle failed", exc_info=True)

        task = asyncio.create_task(_loop())
        logger.info("Continuous discovery started (interval=%.0fs)", interval)
        return task

    def stop_continuous(self) -> None:
        """Signal the continuous discovery loop to stop."""
        self._continuous_running = False


# ---------------------------------------------------------------------------
# Spaces YAML generator
# ---------------------------------------------------------------------------

# Adapter ID -> human-friendly device family name
_ADAPTER_FAMILY_NAMES: dict[str, str] = {
    "kincony.family": "kincony",
    "shelly.gen2": "shelly",
    "esphome.native": "esphome",
    "hue.bridge": "hue",
    "zigbee2mqtt.http": "zigbee",
    "zwave.jsui": "zwave",
    "matter.python": "matter",
    "lutron.caseta": "lutron",
    "mqtt.generic": "mqtt",
    "modbus.tcp": "modbus",
    "onvif.camera": "camera",
    "knx.tunnel": "knx",
    "bacnet.ip": "bacnet",
    "opcua.client": "opcua",
    "dnp3.tcp": "dnp3",
}

# Point class -> (capabilities, ai_access, safety_class)
_POINT_CLASS_DEFAULTS: dict[str, tuple[list[str], str, str]] = {
    "switch.state": (["binary_switch"], "full", "S1"),
    "relay.state": (["binary_switch"], "full", "S1"),
    "dimmer.level": (["dimmer", "binary_switch"], "full", "S1"),
    "digital_input.state": (["binary_sensor"], "read_only", "S0"),
    "analog_input.value": (["analog_input"], "read_only", "S0"),
    "temperature.value": (["temperature_sensor"], "read_only", "S0"),
    "humidity.value": (["humidity_sensor"], "read_only", "S0"),
    "cover.position": (["cover"], "confirm_required", "S2"),
    "lock.state": (["lock", "door_lock"], "blocked", "S3"),
    "ir.transmitter": (["ir_transmitter"], "full", "S1"),
    "button.event": (["binary_sensor"], "read_only", "S0"),
    "dac.output": (["analog_output"], "full", "S1"),
}


def generate_spaces_yaml(
    snapshots: list[tuple[dict, InventorySnapshot]],
    site_name: str = "my_home",
    default_space: str = "main",
) -> dict[str, Any]:
    """Generate a spaces.yaml dict from commissioned inventory snapshots.

    Groups endpoints by device, creates semantic names from the point metadata,
    and assigns sensible defaults for capabilities, ai_access, and safety_class.
    """
    spaces: dict[str, dict] = {}
    space_devices: dict[str, dict] = {}

    for comm_info, snapshot in snapshots:
        conn_id = comm_info.get("connection_id", "")
        adapter_id = comm_info.get("adapter_id", "")
        address = comm_info.get("address", "")
        family = _ADAPTER_FAMILY_NAMES.get(adapter_id, adapter_id.split(".")[0])

        # Use device name from inventory, or derive from adapter family
        for device in snapshot.devices:
            device_name = device.get("name", family).lower().replace(" ", "_")
            device_id = device.get("device_id", "")

            # Find all points for this device
            device_endpoints = [
                ep for ep in snapshot.endpoints
                if ep.get("device_id") == device_id
            ]
            device_points = [
                pt for pt in snapshot.points
                if any(pt.get("endpoint_id") == ep.get("endpoint_id")
                       for ep in device_endpoints)
            ]

            # Create a space for this device (using address-based grouping)
            space_key = default_space
            if not space_devices.get(space_key):
                space_devices[space_key] = {}

            for point in device_points:
                point_id = point.get("point_id", "")
                point_class = point.get("point_class", "")
                endpoint_id = point.get("endpoint_id", "")
                value_type = point.get("value_type", "str")
                unit = point.get("unit")
                writable = point.get("writable", False)
                native_ref = point.get("native_ref", "")

                # Generate semantic device name from the point
                dev_key = _point_to_semantic_name(
                    point_class, native_ref, endpoint_id, device_name, family
                )

                # Look up defaults
                caps, ai_access, safety = _POINT_CLASS_DEFAULTS.get(
                    point_class, (["binary_switch"] if writable else ["binary_sensor"],
                                  "full" if writable else "read_only",
                                  "S1" if writable else "S0")
                )

                device_entry: dict[str, Any] = {
                    "point_id": point_id,
                    "connection_id": conn_id,
                    "endpoint_id": endpoint_id,
                    "device_id": device_id,
                    "capabilities": caps,
                    "ai_access": ai_access,
                    "safety_class": safety,
                    "value_type": value_type,
                }
                if unit:
                    device_entry["unit"] = unit

                space_devices[space_key][dev_key] = device_entry

    # Build the final YAML structure
    for space_key, devices in space_devices.items():
        spaces[space_key] = {
            "display_name": space_key.replace("_", " ").title(),
            "devices": devices,
        }

    return {"site": site_name, "spaces": spaces}


def _point_to_semantic_name(
    point_class: str, native_ref: str, endpoint_id: str,
    device_name: str, family: str,
) -> str:
    """Generate a human-friendly semantic name from point metadata."""
    # Try to extract a meaningful name from point_class
    # e.g. "switch.state" -> "relay_1" using native_ref like "relay_1"
    if native_ref:
        # Clean up native ref to be a valid identifier
        clean = native_ref.lower().replace(" ", "_").replace("-", "_")
        clean = clean.replace(".", "_")
        # Prefix with family if not already
        if not clean.startswith(family):
            return f"{family}_{clean}"
        return clean

    # Fall back to endpoint_id-based naming
    if endpoint_id:
        # Strip the device prefix from endpoint_id
        parts = endpoint_id.split("_")
        # Take the last meaningful parts
        if len(parts) > 3:
            return "_".join(parts[-3:])
        return endpoint_id.replace(".", "_")

    return f"{family}_{device_name}"
