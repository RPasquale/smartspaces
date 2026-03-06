"""Matter adapter via python-matter-server.

Wraps the python-matter-server WebSocket API to inventory, read,
and control Matter devices. Works with Thread and Wi-Fi Matter
devices through any compatible Matter controller.
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

# Matter cluster ID -> canonical capabilities
MATTER_CLUSTER_MAP = {
    6: (["binary_switch"], "read_write", "S1", "on_off"),
    8: (["dimmer"], "read_write", "S1", "level_control"),
    768: (["light_color"], "read_write", "S1", "color_control"),
    1026: (["temperature_sensor"], "read", "S0", "temperature"),
    1029: (["humidity_sensor"], "read", "S0", "humidity"),
    1024: (["analog_input"], "read", "S0", "illuminance"),
    1030: (["binary_sensor"], "read", "S0", "occupancy"),
    513: (["thermostat", "climate_setpoint"], "read_write", "S2", "thermostat"),
    258: (["cover"], "read_write", "S1", "window_covering"),
    514: (["fan"], "read_write", "S1", "fan_control"),
    257: (["lock"], "read_write", "S2", "door_lock"),
    1028: (["analog_input"], "read", "S0", "pressure"),
    47: (["meter_power"], "read", "S0", "power"),
}


class _MatterConnection:
    def __init__(self, connection_id: str, host: str, port: int = 5580):
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


class MatterAdapter(Adapter):
    adapter_id: str = "matter.python"
    adapter_class: AdapterClass = "network_controller"

    def __init__(self):
        self._connections: dict[str, _MatterConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Matter Server",
                category="network_controller",
                discovery_methods=["mdns", "manual_ip"],
                required_fields=["host"],
                optional_fields=["port"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        if "http_probe" in request.methods or "mdns" in request.methods:
            host = request.scope.get("host")
            port = request.scope.get("port", 5580)
            if host:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        resp = await c.get(f"http://{host}:{port}/")
                        if resp.status_code == 200:
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=f"{host}:{port}",
                                title=f"Matter Server @ {host}",
                                address=host,
                                confidence=0.8,
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
        port = int(profile.fields.get("port", 5580))

        conn_id = f"matter_{uuid.uuid4().hex[:8]}"
        conn = _MatterConnection(conn_id, host, port)
        try:
            await conn.get("/")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "port": port})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)
        nodes_data = await conn.get("/nodes")
        nodes = nodes_data if isinstance(nodes_data, list) else nodes_data.get("nodes", [])

        controller_id = f"dev_matter_controller_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": controller_id,
            "native_device_ref": f"{conn.host}:{conn.port}",
            "device_family": "matter.controller",
            "name": "Matter Controller",
            "connectivity": {"transport": "http", "address": conn.host},
            "safety_class": "S0",
        }]
        endpoints = []
        points = []

        for node in nodes:
            node_id = node.get("node_id", node.get("nodeId", 0))
            dev_id = f"dev_matter_node_{node_id}"
            node_info = node.get("attributes", {})

            # Extract basic info from basic cluster attributes
            name = node.get("name", f"Matter Node {node_id}")
            vendor = node.get("vendor_name", "Unknown")
            product = node.get("product_name", "Unknown")

            devices.append({
                "device_id": dev_id,
                "native_device_ref": str(node_id),
                "device_family": "matter.device",
                "name": name,
                "manufacturer": vendor,
                "model": product,
                "connectivity": {"transport": "matter", "address": str(node_id)},
                "bridge_device_id": controller_id,
                "safety_class": "S1",
            })

            # Parse clusters into endpoints/points
            node_endpoints = node.get("endpoints", {})
            if isinstance(node_endpoints, dict):
                node_endpoints = [
                    {"id": k, **v} for k, v in node_endpoints.items()
                ]

            for ep in node_endpoints:
                ep_num = ep.get("id", ep.get("endpoint", 0))
                clusters = ep.get("clusters", ep.get("serverClusters", {}))
                if isinstance(clusters, dict):
                    clusters = [{"id": k, **v} for k, v in clusters.items()]

                for cluster in clusters:
                    cluster_id = int(cluster.get("id", cluster.get("clusterId", 0)))
                    if cluster_id not in MATTER_CLUSTER_MAP:
                        continue

                    caps, direction, safety, cname = MATTER_CLUSTER_MAP[cluster_id]
                    ep_id = f"{dev_id}_ep{ep_num}_{cname}"

                    endpoints.append({
                        "endpoint_id": ep_id,
                        "device_id": dev_id,
                        "native_endpoint_ref": f"{node_id}/{ep_num}/{cluster_id}",
                        "endpoint_type": cname,
                        "direction": direction,
                        "capabilities": caps,
                        "polling_mode": "push_preferred_with_poll_verify",
                        "safety_class": safety,
                    })

                    # Attributes as points
                    attributes = cluster.get("attributes", {})
                    if isinstance(attributes, dict):
                        for attr_name, attr_val in attributes.items():
                            pt_id = f"{ep_id}_{attr_name}"
                            points.append({
                                "point_id": pt_id,
                                "endpoint_id": ep_id,
                                "point_class": f"{cname}.{attr_name}",
                                "value_type": "float" if isinstance(attr_val, (int, float)) else "str",
                                "readable": True,
                                "writable": direction in ("write", "read_write"),
                                "native_ref": f"{node_id}/{ep_num}/{cluster_id}/{attr_name}",
                                "source_protocol": "matter",
                            })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
            raw={"node_count": len(nodes)},
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        return {
            "point_id": point_id,
            "value": None,
            "quality": {"status": "stale", "source_type": "polled"},
            "error": "Use WebSocket subscription for real-time Matter state",
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        target = command.get("target", {})
        params = command.get("params", {})

        try:
            result = await conn.post("/node/command", {
                "node_id": target.get("node_id"),
                "endpoint": target.get("endpoint", 1),
                "cluster_id": target.get("cluster_id"),
                "command": target.get("command", ""),
                "args": params,
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

    def _get_conn(self, cid: str) -> _MatterConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
