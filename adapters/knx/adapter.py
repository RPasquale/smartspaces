"""KNX/IP adapter.

Connects to a KNX/IP gateway via tunneling protocol to read and
control KNX group addresses. Supports lights, blinds, sensors,
HVAC, and metering devices on the KNX bus.
"""

from __future__ import annotations

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
    """Wraps a KNX/IP tunneling connection.

    In production this would use xknx or knxpy for actual KNX/IP tunneling.
    This implementation provides the HTTP management layer.
    """
    def __init__(self, connection_id: str, host: str, port: int = 3671):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.commissioned_at = datetime.now(timezone.utc)
        self._group_addresses: dict[str, dict[str, Any]] = {}
        self._connected = False

    async def connect(self) -> bool:
        # Real impl: xknx tunnel connection
        self._connected = True
        return True

    async def read_group(self, group_address: str) -> Any:
        return self._group_addresses.get(group_address, {}).get("value")

    async def write_group(self, group_address: str, value: Any) -> None:
        if group_address not in self._group_addresses:
            self._group_addresses[group_address] = {}
        self._group_addresses[group_address]["value"] = value

    async def close(self):
        self._connected = False


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
