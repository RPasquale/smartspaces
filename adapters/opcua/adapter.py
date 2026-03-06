"""OPC UA client adapter.

Connects to OPC UA servers (PLCs, SCADA, HMIs, industrial IoT gateways)
to browse, read, write, and subscribe to nodes. Uses the asyncua library
for the OPC UA binary protocol over TCP.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

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

# OPC UA data type -> canonical mapping
OPCUA_TYPE_MAP = {
    "Boolean": ("bool", ["binary_sensor"]),
    "Int16": ("float", ["analog_input"]),
    "Int32": ("float", ["analog_input"]),
    "Int64": ("float", ["analog_input"]),
    "UInt16": ("float", ["analog_input"]),
    "UInt32": ("float", ["analog_input"]),
    "Float": ("float", ["analog_input"]),
    "Double": ("float", ["analog_input"]),
    "String": ("str", ["binary_sensor"]),
}


class _OpcUaConnection:
    """Wraps an OPC UA client connection.

    In production this would use asyncua for actual OPC UA communication.
    """
    def __init__(self, connection_id: str, endpoint_url: str,
                 username: str | None = None, password: str | None = None):
        self.connection_id = connection_id
        self.endpoint_url = endpoint_url
        self.username = username
        self.password = password
        self.commissioned_at = datetime.now(timezone.utc)
        self._nodes: dict[str, dict[str, Any]] = {}
        self._connected = False

    async def connect(self) -> bool:
        # Real impl: asyncua Client connect
        self._connected = True
        return True

    async def browse(self, node_id: str = "i=85") -> list[dict[str, Any]]:
        # Real impl: browse child nodes
        return list(self._nodes.values())

    async def read_value(self, node_id: str) -> Any:
        node = self._nodes.get(node_id, {})
        return node.get("value")

    async def write_value(self, node_id: str, value: Any) -> None:
        if node_id not in self._nodes:
            self._nodes[node_id] = {}
        self._nodes[node_id]["value"] = value

    async def close(self):
        self._connected = False


class OpcUaAdapter(Adapter):
    adapter_id: str = "opcua.client"
    adapter_class: AdapterClass = "server"

    def __init__(self):
        self._connections: dict[str, _OpcUaConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="OPC UA Server",
                category="server",
                discovery_methods=["manual_url"],
                required_fields=["endpoint_url"],
                secret_fields=["username", "password"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        url = request.scope.get("endpoint_url")
        if url:
            targets.append(DiscoveredTarget(
                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                adapter_id=self.adapter_id,
                native_ref=url,
                title=f"OPC UA @ {url}",
                address=url,
                confidence=0.7,
            ))
        return targets

    async def commission(
        self, target: DiscoveredTarget | None, profile: ConnectionProfile,
    ) -> CommissionResult:
        url = profile.fields.get("endpoint_url") or (target.address if target else None)
        if not url:
            return CommissionResult("", "failed", {"error": "No endpoint_url"})

        username = None
        password = None
        for s in profile.secrets:
            if s.name == "username":
                username = s.handle
            elif s.name == "password":
                password = s.handle

        conn_id = f"opcua_{uuid.uuid4().hex[:8]}"
        conn = _OpcUaConnection(conn_id, url, username, password)
        try:
            await conn.connect()
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        # Preload node map if provided
        node_map = profile.fields.get("node_map", {})
        if isinstance(node_map, dict):
            for nid, info in node_map.items():
                conn._nodes[nid] = info

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"endpoint_url": url})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        # Parse host from endpoint URL for device_id
        url_safe = conn.endpoint_url.replace("://", "_").replace("/", "_").replace(":", "_").replace(".", "_")
        server_id = f"dev_opcua_{url_safe}"

        devices = [{
            "device_id": server_id,
            "native_device_ref": conn.endpoint_url,
            "device_family": "opcua.server",
            "name": f"OPC UA Server",
            "connectivity": {"transport": "opcua_tcp", "address": conn.endpoint_url},
            "safety_class": "S1",
        }]
        endpoints = []
        points = []

        nodes = await conn.browse()
        for node in nodes:
            node_id = node.get("node_id", node.get("NodeId", ""))
            name = node.get("name", node.get("DisplayName", str(node_id)))
            data_type = node.get("data_type", "Double")
            writable = node.get("writable", False)
            unit = node.get("unit")

            vtype, caps = OPCUA_TYPE_MAP.get(data_type, ("str", ["analog_input"]))
            direction = "read_write" if writable else "read"
            safety = "S1" if writable else "S0"

            node_safe = str(node_id).replace(";", "_").replace("=", "_").replace(".", "_")
            ep_id = f"{server_id}_{node_safe}"

            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": server_id,
                "native_endpoint_ref": str(node_id),
                "endpoint_type": "opcua_node",
                "direction": direction,
                "capabilities": caps,
                "polling_mode": "push_preferred_with_poll_verify",
                "safety_class": safety,
            })
            points.append({
                "point_id": f"{ep_id}_value",
                "endpoint_id": ep_id,
                "point_class": f"opcua.{data_type.lower()}",
                "value_type": vtype,
                "unit": unit,
                "readable": True,
                "writable": writable,
                "native_ref": str(node_id),
                "source_protocol": "opcua",
            })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Real impl: OPC UA monitored items / subscriptions
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Extract node_id from native_ref
        node_id = point_id.rsplit("_value", 1)[0].replace(f"dev_opcua_", "")
        value = await conn.read_value(node_id)
        return {
            "point_id": point_id,
            "value": {"kind": "float", "reported": value},
            "quality": {"status": "good" if value is not None else "stale", "source_type": "polled"},
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        target = command.get("target", {})
        params = command.get("params", {})

        node_id = target.get("node_id", "")
        value = params.get("value")

        try:
            await conn.write_value(node_id, value)
            return {"command_id": cmd_id, "status": "succeeded"}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        if conn._connected:
            return HealthStatus("healthy", {"endpoint_url": conn.endpoint_url})
        return HealthStatus("error", {"endpoint_url": conn.endpoint_url, "error": "Not connected"})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _OpcUaConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
