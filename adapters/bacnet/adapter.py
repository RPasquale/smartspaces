"""BACnet/IP adapter.

Connects to BACnet devices for building automation — AHUs, VAVs,
chillers, boilers, meters, and general-purpose controllers. Uses
the BACnet/IP protocol for reading and writing object properties.
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

# BACnet object type -> canonical capabilities
BACNET_OBJ_MAP = {
    "analogInput": (["analog_input"], "read", "S0"),
    "analogOutput": (["analog_output"], "read_write", "S1"),
    "analogValue": (["analog_input"], "read_write", "S1"),
    "binaryInput": (["binary_sensor"], "read", "S0"),
    "binaryOutput": (["binary_switch"], "read_write", "S1"),
    "binaryValue": (["binary_switch"], "read_write", "S1"),
    "multiStateInput": (["binary_sensor"], "read", "S0"),
    "multiStateOutput": (["binary_switch"], "read_write", "S1"),
    "multiStateValue": (["binary_switch"], "read_write", "S1"),
    "loop": (["thermostat", "climate_setpoint"], "read_write", "S2"),
    "schedule": (["schedule"], "read_write", "S1"),
    "trendLog": (["analog_input"], "read", "S0"),
}

# BACnet engineering units (subset)
BACNET_UNITS = {
    62: "°C", 64: "°F", 98: "%", 19: "kW", 18: "W",
    46: "Pa", 132: "kWh", 95: "m³/h", 85: "kg", 73: "L/s",
}


class _BacnetConnection:
    """Wraps a BACnet/IP connection.

    In production this would use BAC0 or bacpypes for the actual BACnet stack.
    """
    def __init__(self, connection_id: str, host: str, port: int = 47808,
                 device_instance: int | None = None):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.device_instance = device_instance
        self.commissioned_at = datetime.now(timezone.utc)
        self._objects: dict[str, dict[str, Any]] = {}
        self._connected = False

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def read_property(self, obj_type: str, obj_instance: int, prop: str = "presentValue") -> Any:
        key = f"{obj_type}:{obj_instance}"
        obj = self._objects.get(key, {})
        return obj.get(prop)

    async def write_property(self, obj_type: str, obj_instance: int,
                             prop: str, value: Any, priority: int = 16) -> None:
        key = f"{obj_type}:{obj_instance}"
        if key not in self._objects:
            self._objects[key] = {}
        self._objects[key][prop] = value

    async def who_is(self) -> list[dict[str, Any]]:
        # Real impl: send WhoIs and collect IAm responses
        return []

    async def close(self):
        self._connected = False


class BacnetAdapter(Adapter):
    adapter_id: str = "bacnet.ip"
    adapter_class: AdapterClass = "bus"

    def __init__(self):
        self._connections: dict[str, _BacnetConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="BACnet/IP Device",
                category="bus",
                discovery_methods=["whois", "manual_ip"],
                required_fields=["host"],
                optional_fields=["port", "device_instance", "object_list"],
                supports_auto_inventory=True,
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
                title=f"BACnet Device @ {host}",
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
        port = int(profile.fields.get("port", 47808))
        dev_inst = profile.fields.get("device_instance")
        if dev_inst is not None:
            dev_inst = int(dev_inst)

        conn_id = f"bacnet_{uuid.uuid4().hex[:8]}"
        conn = _BacnetConnection(conn_id, host, port, dev_inst)
        try:
            await conn.connect()
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        # Preload object list if provided
        obj_list = profile.fields.get("object_list", [])
        if isinstance(obj_list, list):
            for obj in obj_list:
                key = f"{obj.get('type', 'analogInput')}:{obj.get('instance', 0)}"
                conn._objects[key] = obj

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "port": port})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        dev_id = f"dev_bacnet_{conn.host.replace('.', '_')}"
        if conn.device_instance is not None:
            dev_id += f"_{conn.device_instance}"

        devices = [{
            "device_id": dev_id,
            "native_device_ref": conn.host,
            "device_family": "bacnet.device",
            "name": f"BACnet Device {conn.device_instance or conn.host}",
            "connectivity": {"transport": "bacnet_ip", "address": conn.host},
            "safety_class": "S1",
        }]
        endpoints = []
        points = []

        for key, obj_info in conn._objects.items():
            obj_type, obj_inst = key.split(":", 1)
            obj_inst_int = int(obj_inst)
            name = obj_info.get("name", f"{obj_type} {obj_inst}")
            unit_code = obj_info.get("units", 0)
            unit = BACNET_UNITS.get(unit_code)

            caps, direction, safety = BACNET_OBJ_MAP.get(
                obj_type, (["analog_input"], "read", "S0")
            )

            ep_id = f"{dev_id}_{obj_type}_{obj_inst}"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": dev_id,
                "native_endpoint_ref": f"{obj_type},{obj_inst}",
                "endpoint_type": obj_type,
                "direction": direction,
                "capabilities": caps,
                "polling_mode": "poll" if direction == "read" else "push_preferred_with_poll_verify",
                "safety_class": safety,
            })
            points.append({
                "point_id": f"{ep_id}_pv",
                "endpoint_id": ep_id,
                "point_class": f"bacnet.{obj_type}.presentValue",
                "value_type": "float" if "analog" in obj_type.lower() else "str",
                "unit": unit,
                "readable": True,
                "writable": direction in ("write", "read_write"),
                "native_ref": f"{obj_type},{obj_inst},presentValue",
                "source_protocol": "bacnet",
            })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Real impl: BACnet COV subscriptions
        yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Parse obj_type, obj_instance from native_ref
        # point_id: dev_bacnet_..._analogInput_1_pv
        parts = point_id.rsplit("_pv", 1)[0]
        segments = parts.replace(f"dev_bacnet_{conn.host.replace('.', '_')}_", "").split("_")

        obj_type = segments[0] if segments else "analogInput"
        obj_inst = int(segments[1]) if len(segments) > 1 else 0

        value = await conn.read_property(obj_type, obj_inst)
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

        obj_type = target.get("object_type", "analogOutput")
        obj_inst = int(target.get("object_instance", 0))
        prop = params.get("property", "presentValue")
        value = params.get("value")
        priority = int(params.get("priority", 16))

        try:
            await conn.write_property(obj_type, obj_inst, prop, value, priority)
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

    def _get_conn(self, cid: str) -> _BacnetConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
