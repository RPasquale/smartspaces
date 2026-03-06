"""Z-Wave adapter via Z-Wave JS server.

Wraps the Z-Wave JS WebSocket/REST API to inventory, read,
and control Z-Wave devices through any compatible Z-Wave
USB stick (e.g. Aeotec Z-Stick, Zooz ZST10).
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

# Z-Wave command class -> canonical capabilities
ZWAVE_CC_MAP = {
    "Binary Switch": (["binary_switch"], "read_write", "S1"),
    "Multilevel Switch": (["dimmer", "binary_switch"], "read_write", "S1"),
    "Color Switch": (["dimmer", "light_color"], "read_write", "S1"),
    "Binary Sensor": (["binary_sensor"], "read", "S0"),
    "Multilevel Sensor": (["analog_input"], "read", "S0"),
    "Meter": (["meter_power"], "read", "S0"),
    "Thermostat Mode": (["thermostat"], "read_write", "S2"),
    "Thermostat Setpoint": (["climate_setpoint"], "read_write", "S2"),
    "Door Lock": (["lock"], "read_write", "S2"),
    "Barrier Operator": (["cover"], "read_write", "S1"),
    "Fan Mode": (["fan"], "read_write", "S1"),
    "Notification": (["binary_sensor"], "read", "S0"),
    "Battery": (["battery_level"], "read", "S0"),
}


class _ZWaveConnection:
    def __init__(self, connection_id: str, host: str, port: int = 3000):
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


class ZWaveAdapter(Adapter):
    adapter_id: str = "zwave.zwave_js"
    adapter_class: AdapterClass = "network_controller"

    def __init__(self):
        self._connections: dict[str, _ZWaveConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Z-Wave JS Server",
                category="network_controller",
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
            port = request.scope.get("port", 3000)
            if host:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        resp = await c.get(f"http://{host}:{port}/")
                        if resp.status_code == 200:
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=f"{host}:{port}",
                                title=f"Z-Wave JS @ {host}",
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
        port = int(profile.fields.get("port", 3000))

        conn_id = f"zwave_{uuid.uuid4().hex[:8]}"
        conn = _ZWaveConnection(conn_id, host, port)
        try:
            await conn.get("/")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "port": port})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)
        nodes = await conn.get("/nodes")
        if isinstance(nodes, dict):
            nodes = nodes.get("nodes", nodes.get("result", []))

        controller_id = f"dev_zwave_controller_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": controller_id,
            "native_device_ref": f"{conn.host}:{conn.port}",
            "device_family": "zwave.controller",
            "name": "Z-Wave JS Controller",
            "connectivity": {"transport": "http", "address": conn.host},
            "safety_class": "S0",
        }]
        endpoints = []
        points = []

        for node in nodes:
            node_id = node.get("nodeId", node.get("id", 0))
            if node.get("isControllerNode"):
                continue

            dev_id = f"dev_zwave_node_{node_id}"
            name = node.get("name") or node.get("label") or f"Node {node_id}"
            manufacturer = node.get("manufacturer", "Unknown")
            product = node.get("productLabel", node.get("product", "Unknown"))

            devices.append({
                "device_id": dev_id,
                "native_device_ref": str(node_id),
                "device_family": "zwave.device",
                "name": name,
                "manufacturer": manufacturer,
                "model": product,
                "firmware": {"version": node.get("firmwareVersion", "")},
                "connectivity": {"transport": "zwave", "address": str(node_id)},
                "bridge_device_id": controller_id,
                "safety_class": "S1",
            })

            # Parse command classes into endpoints/points
            values = node.get("values", {})
            if isinstance(values, dict):
                values = list(values.values())

            for val in values:
                cc_name = val.get("commandClassName", "")
                prop = val.get("propertyName", val.get("property", ""))
                if not prop or cc_name in ("Version", "Manufacturer Specific", "Association"):
                    continue

                caps, direction, safety = ZWAVE_CC_MAP.get(
                    cc_name, (["binary_sensor"], "read", "S0")
                )

                ep_id = f"{dev_id}_{cc_name.replace(' ', '_').lower()}_{prop}"
                readable = val.get("readable", True)
                writable = val.get("writeable", False)
                if writable:
                    direction = "read_write"

                meta = val.get("metadata", {})
                unit = meta.get("unit")
                vtype = "float" if meta.get("type") == "number" else "str"

                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": dev_id,
                    "native_endpoint_ref": f"{cc_name}/{prop}",
                    "endpoint_type": cc_name.replace(" ", "_").lower(),
                    "direction": direction,
                    "capabilities": caps,
                    "polling_mode": "push_preferred_with_poll_verify",
                    "safety_class": safety,
                })
                points.append({
                    "point_id": f"{ep_id}_value",
                    "endpoint_id": ep_id,
                    "point_class": f"{cc_name.replace(' ', '_').lower()}.{prop}",
                    "value_type": vtype,
                    "unit": unit,
                    "readable": readable,
                    "writable": writable,
                    "native_ref": f"{node_id}/{cc_name}/{prop}",
                    "source_protocol": "zwave_js",
                })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
            raw={"node_count": len(nodes)},
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Real impl would use Z-Wave JS WebSocket events
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Parse node_id, command class, property from native_ref stored in point
        return {
            "point_id": point_id,
            "value": None,
            "quality": {"status": "stale", "source_type": "polled"},
            "error": "Use WebSocket subscription for real-time Z-Wave state",
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        params = command.get("params", {})
        target = command.get("target", {})

        try:
            result = await conn.post("/node/setValue", {
                "nodeId": target.get("node_id"),
                "commandClass": target.get("command_class"),
                "property": target.get("property"),
                "value": params.get("value"),
            })
            return {"command_id": cmd_id, "status": "succeeded", "result": result}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        try:
            await conn.get("/")
            return HealthStatus("healthy", {"host": conn.host})
        except Exception as e:
            return HealthStatus("error", {"host": conn.host, "error": str(e)})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _ZWaveConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
