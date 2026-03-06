"""DNP3 master adapter.

Connects to DNP3 outstations (RTUs, IEDs, meters) used in utility
SCADA and industrial control. Supports reading analog/binary inputs,
counters, and writing analog/binary outputs via DNP3/TCP.
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

# DNP3 object group -> canonical capabilities
DNP3_GROUP_MAP = {
    1: (["binary_input"], "read", "S0", "binary_input"),
    2: (["binary_input"], "read", "S0", "binary_input_event"),
    10: (["binary_output"], "read_write", "S3", "binary_output"),
    12: (["binary_output"], "write", "S3", "crob"),
    20: (["counter"], "read", "S0", "counter"),
    21: (["counter"], "read", "S0", "frozen_counter"),
    30: (["analog_input"], "read", "S0", "analog_input"),
    32: (["analog_input"], "read", "S0", "analog_input_event"),
    40: (["analog_output"], "read_write", "S3", "analog_output"),
    41: (["analog_output"], "write", "S3", "analog_output_command"),
}


class _Dnp3Connection:
    """Wraps a DNP3 master-outstation TCP connection.

    In production this would use pydnp3 or opendnp3 for the actual
    DNP3 application layer.
    """
    def __init__(self, connection_id: str, host: str, port: int = 20000,
                 outstation_addr: int = 1, master_addr: int = 0):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.outstation_addr = outstation_addr
        self.master_addr = master_addr
        self.commissioned_at = datetime.now(timezone.utc)
        self._data_map: dict[str, dict[str, Any]] = {}
        self._connected = False

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def integrity_poll(self) -> dict[str, Any]:
        # Real impl: class 0/1/2/3 poll
        return self._data_map

    async def read_point(self, group: int, variation: int, index: int) -> Any:
        key = f"{group}:{index}"
        return self._data_map.get(key, {}).get("value")

    async def direct_operate(self, group: int, variation: int,
                             index: int, value: Any) -> bool:
        key = f"{group}:{index}"
        if key not in self._data_map:
            self._data_map[key] = {}
        self._data_map[key]["value"] = value
        return True

    async def close(self):
        self._connected = False


class Dnp3Adapter(Adapter):
    adapter_id: str = "dnp3.master"
    adapter_class: AdapterClass = "bus"

    def __init__(self):
        self._connections: dict[str, _Dnp3Connection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="DNP3 TCP Outstation",
                category="bus",
                discovery_methods=["manual_ip"],
                required_fields=["host", "outstation_address"],
                optional_fields=["port", "master_address", "point_map"],
                supports_auto_inventory=False,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        host = request.scope.get("host")
        if host:
            targets.append(DiscoveredTarget(
                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                adapter_id=self.adapter_id,
                native_ref=host,
                title=f"DNP3 Outstation @ {host}",
                address=host,
                confidence=0.5,
            ))
        return targets

    async def commission(
        self, target: DiscoveredTarget | None, profile: ConnectionProfile,
    ) -> CommissionResult:
        host = profile.fields.get("host") or (target.address if target else None)
        if not host:
            return CommissionResult("", "failed", {"error": "No host"})

        port = int(profile.fields.get("port", 20000))
        outstation_addr = int(profile.fields.get("outstation_address", 1))
        master_addr = int(profile.fields.get("master_address", 0))

        conn_id = f"dnp3_{uuid.uuid4().hex[:8]}"
        conn = _Dnp3Connection(conn_id, host, port, outstation_addr, master_addr)
        try:
            await conn.connect()
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        # Preload point map if provided
        point_map = profile.fields.get("point_map", [])
        if isinstance(point_map, list):
            for pt in point_map:
                group = pt.get("group", 30)
                index = pt.get("index", 0)
                conn._data_map[f"{group}:{index}"] = pt

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {
            "host": host, "port": port,
            "outstation_address": outstation_addr,
        })

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        dev_id = f"dev_dnp3_{conn.host.replace('.', '_')}_{conn.outstation_addr}"
        devices = [{
            "device_id": dev_id,
            "native_device_ref": f"{conn.host}:{conn.outstation_addr}",
            "device_family": "dnp3.outstation",
            "name": f"DNP3 Outstation {conn.outstation_addr}",
            "connectivity": {
                "transport": "dnp3_tcp",
                "address": conn.host,
                "outstation_address": conn.outstation_addr,
            },
            "safety_class": "S3",
        }]
        endpoints = []
        points = []

        for key, pt_info in conn._data_map.items():
            group, index = key.split(":")
            group = int(group)
            index = int(index)
            name = pt_info.get("name", f"G{group}I{index}")

            caps, direction, safety, ptype = DNP3_GROUP_MAP.get(
                group, (["analog_input"], "read", "S0", "unknown")
            )

            is_analog = group in (30, 32, 40, 41)
            vtype = "float" if is_analog else "bool"
            unit = pt_info.get("unit")

            ep_id = f"{dev_id}_g{group}_i{index}"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": dev_id,
                "native_endpoint_ref": f"g{group}v0i{index}",
                "endpoint_type": ptype,
                "direction": direction,
                "capabilities": caps,
                "polling_mode": "poll",
                "safety_class": safety,
            })
            points.append({
                "point_id": f"{ep_id}_value",
                "endpoint_id": ep_id,
                "point_class": f"dnp3.{ptype}",
                "value_type": vtype,
                "unit": unit,
                "readable": direction in ("read", "read_write"),
                "writable": direction in ("write", "read_write"),
                "native_ref": f"{group}:{index}",
                "source_protocol": "dnp3",
            })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Real impl: unsolicited responses from outstation
        conn = self._get_conn(connection_id)
        data = await conn.integrity_poll()
        for key, pt in data.items():
            group, index = key.split(":")
            yield {
                "type": "point.reported",
                "point_id": f"dev_dnp3_{conn.host.replace('.', '_')}_{conn.outstation_addr}_g{group}_i{index}_value",
                "value": {"kind": "float", "reported": pt.get("value")},
                "quality": {"status": "good", "source_type": "polled"},
            }

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Parse group and index from point_id
        # Format: dev_dnp3_..._g30_i0_value
        parts = point_id.rsplit("_value", 1)[0]
        g_part = ""
        i_part = ""
        for seg in parts.split("_"):
            if seg.startswith("g") and seg[1:].isdigit():
                g_part = seg[1:]
            elif seg.startswith("i") and seg[1:].isdigit():
                i_part = seg[1:]

        if g_part and i_part:
            value = await conn.read_point(int(g_part), 0, int(i_part))
            return {
                "point_id": point_id,
                "value": {"kind": "float", "reported": value},
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

        group = int(target.get("group", 12))
        variation = int(target.get("variation", 1))
        index = int(target.get("index", 0))
        value = params.get("value")

        try:
            ok = await conn.direct_operate(group, variation, index, value)
            status = "succeeded" if ok else "failed"
            return {"command_id": cmd_id, "status": status}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        if conn._connected:
            return HealthStatus("healthy", {
                "host": conn.host,
                "outstation_address": conn.outstation_addr,
            })
        return HealthStatus("error", {"host": conn.host, "error": "Not connected"})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _Dnp3Connection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
