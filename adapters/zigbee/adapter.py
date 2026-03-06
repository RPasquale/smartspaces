"""Zigbee adapter via Zigbee2MQTT bridge.

Wraps Zigbee2MQTT's MQTT API and HTTP frontend to inventory, read,
and control Zigbee devices. Controller-implementation-agnostic —
works with any Zigbee2MQTT-compatible coordinator.
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
from sdk.adapter_api.errors import InvalidTargetError, UnreachableError

# Zigbee device type -> canonical capabilities
Z2M_CAP_MAP = {
    "light": (["dimmer", "light_color", "binary_switch"], "S1"),
    "switch": (["binary_switch"], "S1"),
    "plug": (["binary_switch", "meter_power"], "S1"),
    "sensor": (["temperature_sensor", "humidity_sensor", "analog_input"], "S0"),
    "lock": (["lock"], "S2"),
    "cover": (["cover"], "S1"),
    "fan": (["fan"], "S1"),
    "climate": (["thermostat", "climate_setpoint"], "S2"),
}


class _Z2MConnection:
    def __init__(self, connection_id: str, host: str, port: int = 8080):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.client = httpx.AsyncClient(timeout=10.0)
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


class ZigbeeAdapter(Adapter):
    adapter_id: str = "zigbee.z2m"
    adapter_class: AdapterClass = "network_controller"

    def __init__(self):
        self._connections: dict[str, _Z2MConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Zigbee2MQTT",
                category="mesh_network",
                discovery_methods=["http_probe", "manual_ip"],
                required_fields=["host"],
                optional_fields=["port"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        if "http_probe" in request.methods:
            host = request.scope.get("host")
            port = request.scope.get("port", 8080)
            if host:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        resp = await c.get(f"http://{host}:{port}/api/health")
                        if resp.status_code == 200:
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=f"{host}:{port}",
                                title=f"Zigbee2MQTT @ {host}",
                                address=host,
                                confidence=0.9,
                            ))
                except Exception:
                    pass
        return targets

    async def commission(self, target: DiscoveredTarget | None, profile: ConnectionProfile) -> CommissionResult:
        host = profile.fields.get("host") or (target.address if target else None)
        if not host:
            return CommissionResult("", "failed", {"error": "No host"})
        port = int(profile.fields.get("port", 8080))

        conn_id = f"z2m_{uuid.uuid4().hex[:8]}"
        conn = _Z2MConnection(conn_id, host, port)
        try:
            await conn.get("/api/health")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "port": port})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)
        z2m_devices = await conn.get("/api/devices")

        # Coordinator device
        coord_id = f"dev_z2m_coordinator_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": coord_id,
            "native_device_ref": f"{conn.host}:{conn.port}",
            "device_family": "zigbee.coordinator",
            "name": "Zigbee2MQTT Coordinator",
            "connectivity": {"transport": "http", "address": conn.host},
            "safety_class": "S0",
        }]
        endpoints = []
        points = []

        for zdev in z2m_devices:
            if zdev.get("type") == "Coordinator":
                continue

            ieee = zdev.get("ieee_address", "")
            friendly = zdev.get("friendly_name", ieee)
            dev_id = f"dev_z2m_{ieee.replace(':', '_')}"
            definition = zdev.get("definition") or {}
            vendor = definition.get("vendor", "Unknown")
            model = definition.get("model", "Unknown")
            desc = definition.get("description", "")

            devices.append({
                "device_id": dev_id,
                "native_device_ref": ieee,
                "device_family": "zigbee.device",
                "name": friendly,
                "manufacturer": vendor,
                "model": model,
                "firmware": {"version": zdev.get("software_build_id", "")},
                "connectivity": {"transport": "zigbee", "address": ieee},
                "bridge_device_id": coord_id,
                "safety_class": "S1",
            })

            # Map exposes to endpoints/points
            exposes = definition.get("exposes", [])
            for expose in exposes:
                etype = expose.get("type", "")
                features = expose.get("features", [])
                if not features and "property" in expose:
                    features = [expose]

                # Single-feature expose
                if "property" in expose and not features:
                    features = [expose]

                for feat in features:
                    prop = feat.get("property", feat.get("name", ""))
                    if not prop:
                        continue

                    ep_id = f"{dev_id}_{prop}"
                    access = feat.get("access", 1)
                    readable = bool(access & 1)
                    writable = bool(access & 2)
                    direction = "read_write" if writable else "read"
                    vtype = {"numeric": "float", "binary": "bool", "enum": "str"}.get(feat.get("type", ""), "str")
                    unit = feat.get("unit")

                    caps, safety = Z2M_CAP_MAP.get(etype, (["binary_sensor"], "S0"))

                    endpoints.append({
                        "endpoint_id": ep_id,
                        "device_id": dev_id,
                        "native_endpoint_ref": prop,
                        "endpoint_type": etype or "generic",
                        "direction": direction,
                        "capabilities": caps,
                        "polling_mode": "push_preferred_with_poll_verify",
                        "safety_class": safety,
                    })
                    points.append({
                        "point_id": f"{ep_id}_value",
                        "endpoint_id": ep_id,
                        "point_class": f"{prop}",
                        "value_type": vtype,
                        "unit": unit,
                        "readable": readable,
                        "writable": writable,
                        "native_ref": prop,
                        "source_protocol": "zigbee2mqtt",
                    })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
            raw={"device_count": len(z2m_devices)},
        )

    async def subscribe(self, connection_id: str, point_ids: list[str] | None = None) -> AsyncIterator[dict[str, Any]]:
        # Real impl would subscribe to MQTT topics
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Extract friendly name and property from Z2M API
        # For now, use the device state endpoint
        # point_id format: dev_z2m_{ieee}_{property}_value
        parts = point_id.rsplit("_value", 1)[0].split("_")
        # This is a simplified implementation
        return {
            "point_id": point_id,
            "value": None,
            "quality": {"status": "stale", "source_type": "polled"},
            "error": "Use MQTT subscription for real-time Zigbee state",
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        endpoint_id = command.get("target", {}).get("endpoint_id", "")
        params = command.get("params", {})

        # Extract friendly name — Z2M uses friendly_name for set commands
        # endpoint_id: dev_z2m_{ieee}_{property}
        try:
            result = await conn.post("/api/device/set", {
                "id": endpoint_id,
                "state": params,
            })
            return {"command_id": cmd_id, "status": "succeeded", "result": result}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        try:
            info = await conn.get("/api/health")
            return HealthStatus("healthy", {"host": conn.host, "info": info})
        except Exception as e:
            return HealthStatus("error", {"host": conn.host, "error": str(e)})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _Z2MConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
