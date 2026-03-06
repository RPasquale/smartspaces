"""Lutron Caseta / RadioRA adapter via LEAP protocol.

Wraps the Lutron LEAP API (used by Caseta Smart Bridge and
RadioRA 2/3) to inventory, read, and control Lutron devices
including dimmers, switches, shades, and fans.
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

# Lutron device type -> canonical capabilities
LUTRON_DEVICE_MAP = {
    "WallDimmer": (["dimmer", "binary_switch"], "read_write", "S1"),
    "PlugInDimmer": (["dimmer", "binary_switch"], "read_write", "S1"),
    "WallSwitch": (["binary_switch"], "read_write", "S1"),
    "PlugInSwitch": (["binary_switch"], "read_write", "S1"),
    "SerenaHoneycombShade": (["cover"], "read_write", "S1"),
    "SerenaRollerShade": (["cover"], "read_write", "S1"),
    "TriathlonHoneycombShade": (["cover"], "read_write", "S1"),
    "CasetaFanSpeedController": (["fan"], "read_write", "S1"),
    "OccupancySensor": (["binary_sensor"], "read", "S0"),
    "Pico2Button": (["binary_sensor"], "read", "S0"),
    "Pico3Button": (["binary_sensor"], "read", "S0"),
    "Pico4Button": (["binary_sensor"], "read", "S0"),
    "Pico3ButtonRaiseLower": (["binary_sensor"], "read", "S0"),
}


class _LutronConnection:
    def __init__(self, connection_id: str, host: str, port: int = 8081):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        # LEAP normally uses TLS on port 8081; simplified to HTTP proxy for now
        self.base_url = f"https://{host}:{port}"
        self.client = httpx.AsyncClient(timeout=10.0, verify=False)
        self.commissioned_at = datetime.now(timezone.utc)

    async def get(self, path: str) -> Any:
        resp = await self.client.get(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        resp = await self.client.post(f"{self.base_url}{path}", json=data)
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        if not self.client.is_closed:
            await self.client.aclose()


class LutronAdapter(Adapter):
    adapter_id: str = "lutron.caseta"
    adapter_class: AdapterClass = "bridge"

    def __init__(self):
        self._connections: dict[str, _LutronConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Lutron Smart Bridge (LEAP)",
                category="bridge",
                discovery_methods=["mdns", "manual_ip"],
                required_fields=["host"],
                optional_fields=["port"],
                secret_fields=["client_cert", "client_key", "ca_cert"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        host = request.scope.get("host")
        if host:
            try:
                async with httpx.AsyncClient(timeout=5.0, verify=False) as c:
                    resp = await c.get(f"https://{host}:8081/server/1/status/ping")
                    if resp.status_code == 200:
                        targets.append(DiscoveredTarget(
                            discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                            adapter_id=self.adapter_id,
                            native_ref=host,
                            title=f"Lutron Bridge @ {host}",
                            address=host,
                            confidence=0.85,
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
        port = int(profile.fields.get("port", 8081))

        conn_id = f"lutron_{uuid.uuid4().hex[:8]}"
        conn = _LutronConnection(conn_id, host, port)
        try:
            await conn.get("/server/1/status/ping")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "port": port})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        bridge_id = f"dev_lutron_bridge_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": bridge_id,
            "native_device_ref": conn.host,
            "device_family": "lutron.bridge",
            "name": "Lutron Smart Bridge",
            "manufacturer": "Lutron",
            "connectivity": {"transport": "leap", "address": conn.host},
            "safety_class": "S0",
        }]
        endpoints = []
        points = []

        try:
            device_data = await conn.get("/device")
            lutron_devices = device_data.get("Body", {}).get("Devices", [])
        except Exception:
            lutron_devices = []

        for ldev in lutron_devices:
            href = ldev.get("href", "")
            dev_num = href.rsplit("/", 1)[-1] if "/" in href else str(uuid.uuid4().hex[:8])
            dev_type = ldev.get("DeviceType", "Unknown")
            name = ldev.get("Name", f"Lutron {dev_type}")
            serial = ldev.get("SerialNumber", "")
            model = ldev.get("ModelNumber", dev_type)

            dev_id = f"dev_lutron_{dev_num}"
            caps, direction, safety = LUTRON_DEVICE_MAP.get(
                dev_type, (["binary_switch"], "read_write", "S1")
            )

            devices.append({
                "device_id": dev_id,
                "native_device_ref": href,
                "device_family": f"lutron.{dev_type.lower()}",
                "name": name,
                "manufacturer": "Lutron",
                "model": model,
                "serial": serial,
                "connectivity": {"transport": "leap", "address": conn.host},
                "bridge_device_id": bridge_id,
                "safety_class": safety,
            })

            # Each device has zones; create an endpoint per zone
            zones = ldev.get("LocalZones", [])
            if not zones:
                # Single-zone device
                ep_id = f"{dev_id}_zone_0"
                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": dev_id,
                    "native_endpoint_ref": f"{href}/zone/0",
                    "endpoint_type": dev_type.lower(),
                    "direction": direction,
                    "capabilities": caps,
                    "polling_mode": "push_preferred_with_poll_verify",
                    "safety_class": safety,
                })
                points.append({
                    "point_id": f"{ep_id}_level",
                    "endpoint_id": ep_id,
                    "point_class": "level",
                    "value_type": "float",
                    "unit": "%",
                    "readable": direction in ("read", "read_write"),
                    "writable": direction in ("write", "read_write"),
                    "native_ref": f"{href}/zone/0",
                    "source_protocol": "leap",
                })
            else:
                for zi, zone in enumerate(zones):
                    zone_href = zone.get("href", f"{href}/zone/{zi}")
                    ep_id = f"{dev_id}_zone_{zi}"
                    endpoints.append({
                        "endpoint_id": ep_id,
                        "device_id": dev_id,
                        "native_endpoint_ref": zone_href,
                        "endpoint_type": dev_type.lower(),
                        "direction": direction,
                        "capabilities": caps,
                        "polling_mode": "push_preferred_with_poll_verify",
                        "safety_class": safety,
                    })
                    points.append({
                        "point_id": f"{ep_id}_level",
                        "endpoint_id": ep_id,
                        "point_class": "level",
                        "value_type": "float",
                        "unit": "%",
                        "readable": direction in ("read", "read_write"),
                        "writable": direction in ("write", "read_write"),
                        "native_ref": zone_href,
                        "source_protocol": "leap",
                    })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
            raw={"device_count": len(lutron_devices)},
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Parse zone href from point_id
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
        zone_href = target.get("zone_href", "")
        level = params.get("level", params.get("value", 100))

        try:
            result = await conn.post(f"{zone_href}/commandprocessor", {
                "Command": {
                    "CommandType": "GoToLevel",
                    "Parameter": [{"Type": "Level", "Value": level}],
                }
            })
            return {"command_id": cmd_id, "status": "succeeded", "result": result}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        try:
            await conn.get("/server/1/status/ping")
            return HealthStatus("healthy", {"host": conn.host})
        except Exception as e:
            return HealthStatus("error", {"host": conn.host, "error": str(e)})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _LutronConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
