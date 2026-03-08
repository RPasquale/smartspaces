"""KNX/IP adapter.

Connects to a KNX/IP gateway via tunneling protocol to read and
control KNX group addresses. Supports lights, blinds, sensors,
HVAC, and metering devices on the KNX bus.

Requires the ``xknx`` library for real KNX/IP tunneling.
Install via: pip install 'smartspaces[knx]'
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from sdk.adapter_api.base import (
    Adapter,
    AdapterClass,
    CommissionResult,
    ConnectionProfile,
    ConnectionTemplate,
    DiscoveredTarget,
    DiscoveryRequest,
    HealthStatus,
    InventorySnapshot,
)
from sdk.adapter_api.errors import UnreachableError

# ---------------------------------------------------------------------------
# Optional dependency: xknx
# ---------------------------------------------------------------------------
try:
    import xknx as _xknx_mod
    from xknx import XKNX
    from xknx.core import ValueReader
    from xknx.dpt import DPTArray, DPTBinary
    from xknx.io import ConnectionConfig, ConnectionType
    from xknx.telegram import GroupAddress, Telegram
    from xknx.telegram.apci import GroupValueRead, GroupValueWrite

    _HAS_XKNX = True
except ImportError:
    _HAS_XKNX = False

log = logging.getLogger(__name__)

# KNX DPT (Datapoint Type) -> canonical capabilities
KNX_DPT_MAP = {
    "1.001": (["binary_switch"], "read_write", "S1", "bool", None),         # Switch
    "1.008": (["cover"], "read_write", "S1", "bool", None),                 # Up/Down
    "1.009": (["binary_switch"], "read_write", "S1", "bool", None),         # Open/Close
    "3.007": (["dimmer"], "read_write", "S1", "float", "%"),                # Dimming control
    "5.001": (["dimmer"], "read_write", "S1", "float", "%"),                # Percentage
    "5.004": (["dimmer"], "read_write", "S1", "float", "%"),                # Percentage 0-255
    "9.001": (["temperature_sensor"], "read", "S0", "float", "°C"),         # Temperature
    "9.007": (["humidity_sensor"], "read", "S0", "float", "%"),             # Humidity
    "9.004": (["analog_input"], "read", "S0", "float", "lux"),             # Illuminance
    "9.006": (["analog_input"], "read", "S0", "float", "Pa"),              # Pressure
    "12.001": (["meter_power"], "read", "S0", "float", "Wh"),              # Counter
    "13.010": (["meter_power"], "read", "S0", "float", "Wh"),              # Active energy
    "14.056": (["analog_input"], "read", "S0", "float", "W"),              # Power
    "20.102": (["thermostat"], "read_write", "S2", "str", None),           # HVAC mode
}


class _KnxConnection:
    """Wraps a KNX/IP tunneling connection using the *xknx* library.

    If ``xknx`` is not installed the constructor succeeds but
    :meth:`connect` raises :class:`ImportError` with installation
    instructions so that the adapter fails fast at commission time.
    """

    def __init__(self, connection_id: str, host: str, port: int = 3671):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.commissioned_at = datetime.now(timezone.utc)
        self._group_addresses: dict[str, dict[str, Any]] = {}
        self._connected = False
        self._xknx: Any | None = None  # XKNX instance when connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open a KNX/IP tunneling connection to the gateway."""
        if not _HAS_XKNX:
            raise ImportError(
                "The 'xknx' library is required for the KNX adapter. "
                "Install it with: pip install 'smartspaces[knx]'"
            )

        connection_config = ConnectionConfig(
            connection_type=ConnectionType.TUNNELING,
            gateway_ip=self.host,
            gateway_port=self.port,
        )
        self._xknx = XKNX(connection_config=connection_config)
        await self._xknx.start()
        self._connected = True
        log.info("KNX connection %s established to %s:%s",
                 self.connection_id, self.host, self.port)
        return True

    async def close(self) -> None:
        """Gracefully shut down the tunneling connection."""
        if self._xknx is not None:
            try:
                await self._xknx.stop()
            except Exception:
                log.debug("Error stopping xknx instance", exc_info=True)
            self._xknx = None
        self._connected = False

    # ------------------------------------------------------------------
    # Read / Write
    # ------------------------------------------------------------------

    async def read_group(self, group_address: str) -> Any:
        """Read the current value of a group address from the KNX bus.

        Sends a *GroupValueRead* telegram and waits for the response.
        Falls back to the local cache when the bus does not respond.
        """
        if not self._connected or self._xknx is None:
            return self._group_addresses.get(group_address, {}).get("value")

        try:
            ga = GroupAddress(group_address)
            # ValueReader sends a GroupRead and waits for the response
            value_reader = ValueReader(self._xknx, ga, timeout_in_seconds=2.0)
            telegram = await value_reader.read()
            if telegram is not None and telegram.payload is not None:
                raw = telegram.payload.value
                # Cache locally
                if group_address not in self._group_addresses:
                    self._group_addresses[group_address] = {}
                self._group_addresses[group_address]["value"] = raw
                return raw
        except asyncio.TimeoutError:
            log.warning("KNX read timeout for GA %s", group_address)
        except Exception:
            log.warning("KNX read failed for GA %s", group_address, exc_info=True)

        # Fallback to cached value
        return self._group_addresses.get(group_address, {}).get("value")

    async def write_group(self, group_address: str, value: Any) -> None:
        """Write *value* to a group address on the KNX bus.

        Constructs a ``GroupValueWrite`` telegram and sends it through
        the active tunneling connection.
        """
        if not self._connected or self._xknx is None:
            # Offline cache-only write
            if group_address not in self._group_addresses:
                self._group_addresses[group_address] = {}
            self._group_addresses[group_address]["value"] = value
            return

        ga = GroupAddress(group_address)

        # Build the payload — single-bit booleans go as DPTBinary,
        # everything else as DPTArray.
        if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
            payload = GroupValueWrite(DPTBinary(int(value)))
        elif isinstance(value, (list, tuple, bytes)):
            payload = GroupValueWrite(DPTArray(value))
        elif isinstance(value, int):
            payload = GroupValueWrite(DPTArray(value))
        else:
            payload = GroupValueWrite(DPTArray(value))

        telegram = Telegram(destination_address=ga, payload=payload)
        await self._xknx.telegrams.put(telegram)

        # Update local cache
        if group_address not in self._group_addresses:
            self._group_addresses[group_address] = {}
        self._group_addresses[group_address]["value"] = value


class KnxAdapter(Adapter):
    adapter_id: str = "knx.ip"
    adapter_class: AdapterClass = "bus"

    def __init__(self):
        self._connections: dict[str, _KnxConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="KNX/IP Tunneling Gateway",
                category="bus",
                discovery_methods=["knx_search", "manual_ip"],
                required_fields=["host"],
                optional_fields=["port", "group_address_map"],
                supports_auto_inventory=False,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        host = request.scope.get("host")
        if host:
            # KNX/IP gateways respond to KNXnet/IP search on UDP 3671
            # Simplified: attempt TCP connection check
            try:
                async with httpx.AsyncClient(timeout=3.0) as c:
                    # Some KNX/IP gateways have a web interface
                    resp = await c.get(f"http://{host}/")
                    if resp.status_code == 200:
                        targets.append(DiscoveredTarget(
                            discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                            adapter_id=self.adapter_id,
                            native_ref=host,
                            title=f"KNX/IP Gateway @ {host}",
                            address=host,
                            confidence=0.6,
                        ))
            except Exception:
                pass
        return targets

    async def commission(
        self, target: DiscoveredTarget | None, profile: ConnectionProfile,
    ) -> CommissionResult:
        host = profile.fields.get("host") or (target.address if target else None)
        if not host:
            return CommissionResult("", "failed", {"error": "No host"})
        port = int(profile.fields.get("port", 3671))

        conn_id = f"knx_{uuid.uuid4().hex[:8]}"
        conn = _KnxConnection(conn_id, host, port)
        try:
            await conn.connect()
        except ImportError as exc:
            return CommissionResult("", "failed", {"error": str(exc)})
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        # Load group address map if provided
        ga_map = profile.fields.get("group_address_map", {})
        if isinstance(ga_map, dict):
            for ga, info in ga_map.items():
                conn._group_addresses[ga] = info

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "port": port})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        gw_id = f"dev_knx_gateway_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": gw_id,
            "native_device_ref": conn.host,
            "device_family": "knx.gateway",
            "name": f"KNX/IP Gateway {conn.host}",
            "manufacturer": "KNX",
            "connectivity": {"transport": "knx_tunneling", "address": conn.host},
            "safety_class": "S0",
        }]
        endpoints = []
        points = []

        # Build inventory from configured group addresses
        for ga, info in conn._group_addresses.items():
            ga_safe = ga.replace("/", "_")
            name = info.get("name", f"GA {ga}")
            dpt = info.get("dpt", "1.001")
            device_area = info.get("area", "default")

            caps, direction, safety, vtype, unit = KNX_DPT_MAP.get(
                dpt, (["binary_sensor"], "read", "S0", "str", None)
            )

            dev_id = f"dev_knx_{device_area}_{ga_safe}"
            devices.append({
                "device_id": dev_id,
                "native_device_ref": ga,
                "device_family": "knx.device",
                "name": name,
                "connectivity": {"transport": "knx_tunneling", "address": ga},
                "bridge_device_id": gw_id,
                "safety_class": safety,
            })

            ep_id = f"{dev_id}_ep"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": dev_id,
                "native_endpoint_ref": ga,
                "endpoint_type": dpt.split(".")[0],
                "direction": direction,
                "capabilities": caps,
                "polling_mode": "push_preferred_with_poll_verify",
                "safety_class": safety,
            })
            points.append({
                "point_id": f"{ep_id}_value",
                "endpoint_id": ep_id,
                "point_class": f"knx.{dpt}",
                "value_type": vtype,
                "unit": unit,
                "readable": direction in ("read", "read_write"),
                "writable": direction in ("write", "read_write"),
                "native_ref": ga,
                "source_protocol": "knx",
            })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Extract group address from point_id
        # point_id format: dev_knx_{area}_{ga}_ep_value
        ga = self._extract_ga(point_id)
        if ga:
            value = await conn.read_group(ga)
            return {
                "point_id": point_id,
                "value": {"kind": "str", "reported": value},
                "quality": {"status": "good" if value is not None else "stale", "source_type": "polled"},
            }
        return {
            "point_id": point_id,
            "value": None,
            "quality": {"status": "stale", "source_type": "polled"},
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        target = command.get("target", {})
        params = command.get("params", {})
        ga = target.get("group_address", "")
        value = params.get("value")

        try:
            await conn.write_group(ga, value)
            return {"command_id": cmd_id, "status": "succeeded"}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        if conn._connected:
            return HealthStatus("healthy", {"host": conn.host})
        return HealthStatus("error", {"host": conn.host, "error": "Not connected"})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _KnxConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn

    def _extract_ga(self, point_id: str) -> str | None:
        """Extract group address from a point_id like dev_knx_area_1_2_3_ep_value."""
        # GA was stored as 1/2/3 -> 1_2_3 in point_id
        parts = point_id.split("_ep_")[0].replace("dev_knx_", "")
        # Remove area prefix (everything before first digit group)
        segments = parts.split("_")
        # Try to find 3 consecutive numeric segments (group address)
        for i in range(len(segments) - 2):
            if segments[i].isdigit() and segments[i + 1].isdigit() and segments[i + 2].isdigit():
                return f"{segments[i]}/{segments[i + 1]}/{segments[i + 2]}"
        return None
