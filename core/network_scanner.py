"""Network scanner — discovers devices on the local network.

Scans using mDNS/DNS-SD, SSDP, and async port probing, then maps
discovered services to SmartSpaces adapter IDs. Provides both
one-shot scanning and a background continuous discovery mode.

Requires ``zeroconf`` for mDNS (``pip install 'smartspaces[discovery]'``).
SSDP and port scanning use only the standard library.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

from sdk.adapter_api.base import DiscoveredTarget

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
    from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf, IPVersion
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

    service_types = list(MDNS_SERVICE_MAP.keys()) + [_HTTP_SERVICE]

    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        browsers = []
        for stype in service_types:
            browser = AsyncServiceBrowser(
                azc.zeroconf, stype, handlers=[]
            )
            browsers.append((stype, browser))

        # Let scanning run for the timeout period
        await asyncio.sleep(timeout)

        # Collect results from each browser
        for stype, browser in browsers:
            # Get discovered service names from the zeroconf cache
            names = list(azc.zeroconf.cache.names())
            for name in names:
                if name in found_names:
                    continue
                if not name.endswith(stype):
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
        for _, browser in browsers:
            browser.cancel()

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
            methods = ["mdns", "ssdp", "port_scan"]

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
