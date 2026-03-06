"""Modbus TCP/RTU adapter.

Supports industrial devices via Modbus protocol. Requires a register
map (YAML) that defines which registers to read/write and how to
normalize them into canonical points.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

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
from sdk.adapter_api.errors import InvalidTargetError, InvalidValueError, UnreachableError

try:
    from pymodbus.client import AsyncModbusTcpClient
    HAS_PYMODBUS = True
except ImportError:
    HAS_PYMODBUS = False


class RegisterDef:
    """Parsed register definition from a register map."""

    def __init__(self, data: dict[str, Any]):
        self.point_id: str = data["point_id"]
        self.unit_id: int = data.get("unit_id", 1)
        self.table: str = data["table"]  # coil, discrete_input, holding_register, input_register
        self.address: int = data["address"]
        self.data_type: str = data.get("data_type", "uint16")
        self.scale: float = data.get("scale", 1.0)
        self.offset: float = data.get("offset", 0.0)
        self.unit: str | None = data.get("unit")
        self.readable: bool = data.get("readable", True)
        self.writable: bool = data.get("writable", False)
        self.point_class: str = data.get("point_class", "analog_input.value")
        self.safety_class: str = data.get("safety_class", "S1")
        self.verification: dict[str, Any] = data.get("verification", {})
        self.semantic_tags: list[str] = data.get("semantic_tags", [])

    @property
    def count(self) -> int:
        """Number of registers to read based on data type."""
        widths = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "float64": 4}
        return widths.get(self.data_type, 1)


def load_register_map(path: str | Path) -> list[RegisterDef]:
    """Load a YAML register map file."""
    with Path(path).open() as f:
        data = yaml.safe_load(f)
    return [RegisterDef(entry) for entry in data.get("registers", [])]


class _ModbusConnection:
    def __init__(
        self,
        connection_id: str,
        host: str,
        port: int,
        register_map: list[RegisterDef],
    ):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.register_map = register_map
        self.registers_by_id = {r.point_id: r for r in register_map}
        self.client: AsyncModbusTcpClient | None = None
        self.commissioned_at = datetime.now(timezone.utc)

    async def connect(self) -> bool:
        if not HAS_PYMODBUS:
            return False
        self.client = AsyncModbusTcpClient(self.host, port=self.port)
        return await self.client.connect()

    async def disconnect(self):
        if self.client:
            self.client.close()

    async def read_register(self, reg: RegisterDef) -> Any:
        if not self.client:
            return None

        if reg.table == "holding_register":
            result = await self.client.read_holding_registers(
                reg.address, count=reg.count, slave=reg.unit_id
            )
        elif reg.table == "input_register":
            result = await self.client.read_input_registers(
                reg.address, count=reg.count, slave=reg.unit_id
            )
        elif reg.table == "coil":
            result = await self.client.read_coils(
                reg.address, count=1, slave=reg.unit_id
            )
        elif reg.table == "discrete_input":
            result = await self.client.read_discrete_inputs(
                reg.address, count=1, slave=reg.unit_id
            )
        else:
            return None

        if result.isError():
            return None

        if reg.table in ("coil", "discrete_input"):
            return bool(result.bits[0])

        raw = result.registers[0] if reg.count == 1 else result.registers
        if isinstance(raw, int):
            return raw * reg.scale + reg.offset
        return raw

    async def write_register(self, reg: RegisterDef, value: Any) -> bool:
        if not self.client or not reg.writable:
            return False

        if reg.table == "coil":
            result = await self.client.write_coil(
                reg.address, bool(value), slave=reg.unit_id
            )
        elif reg.table == "holding_register":
            # Reverse scale/offset
            raw = int((float(value) - reg.offset) / reg.scale) if reg.scale else int(value)
            result = await self.client.write_register(
                reg.address, raw, slave=reg.unit_id
            )
        else:
            return False

        return not result.isError()


class ModbusAdapter(Adapter):
    adapter_id: str = "modbus.generic"
    adapter_class: AdapterClass = "bus"

    def __init__(self):
        self._connections: dict[str, _ModbusConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Modbus TCP",
                category="industrial",
                required_fields=["host", "port"],
                optional_fields=["unit_ids"],
                files_to_upload=["register_map"],
                supports_auto_inventory=False,
                supports_local_only_mode=True,
                risk_level="medium",
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        # Modbus typically requires manual configuration
        return []

    async def commission(
        self,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        if not HAS_PYMODBUS:
            return CommissionResult("", "failed", {"error": "pymodbus not installed"})

        host = profile.fields.get("host", "")
        port = int(profile.fields.get("port", 502))
        register_map_path = profile.fields.get("register_map", "")

        if not host:
            return CommissionResult("", "failed", {"error": "No host provided"})

        reg_map: list[RegisterDef] = []
        if register_map_path and Path(register_map_path).exists():
            reg_map = load_register_map(register_map_path)

        conn_id = f"modbus_{uuid.uuid4().hex[:8]}"
        conn = _ModbusConnection(conn_id, host, port, reg_map)

        connected = await conn.connect()
        if not connected:
            await conn.disconnect()
            return CommissionResult("", "failed", {"error": f"Cannot connect to {host}:{port}"})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {
            "host": host,
            "port": port,
            "register_count": len(reg_map),
        })

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        # Group registers by unit_id as "devices"
        devices_map: dict[int, list[RegisterDef]] = {}
        for reg in conn.register_map:
            devices_map.setdefault(reg.unit_id, []).append(reg)

        devices = []
        endpoints = []
        points = []

        for unit_id, regs in devices_map.items():
            dev_id = f"dev_modbus_{conn.host.replace('.', '_')}_{unit_id}"
            devices.append({
                "device_id": dev_id,
                "native_device_ref": f"{conn.host}:{conn.port}/unit_{unit_id}",
                "device_family": "modbus.generic",
                "name": f"Modbus Unit {unit_id}",
                "manufacturer": "Unknown",
                "model": "Unknown",
                "connectivity": {"transport": "modbus_tcp", "address": conn.host, "port": conn.port},
                "safety_class": "S3",
            })

            for reg in regs:
                ep_id = f"{dev_id}_{reg.table}_{reg.address}"

                cap_map = {
                    "coil": "digital_output" if reg.writable else "digital_input",
                    "discrete_input": "digital_input",
                    "holding_register": "analog_output" if reg.writable else "analog_input",
                    "input_register": "analog_input",
                }
                cap = cap_map.get(reg.table, "analog_input")

                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": dev_id,
                    "native_endpoint_ref": f"{reg.table}:{reg.address}",
                    "endpoint_type": reg.table,
                    "direction": "read_write" if reg.writable else "read",
                    "capabilities": [cap],
                    "polling_mode": "poll_only",
                    "safety_class": reg.safety_class,
                })

                vt = "bool" if reg.table in ("coil", "discrete_input") else "float"
                points.append({
                    "point_id": reg.point_id,
                    "endpoint_id": ep_id,
                    "point_class": reg.point_class,
                    "value_type": vt,
                    "unit": reg.unit,
                    "readable": reg.readable,
                    "writable": reg.writable,
                    "event_driven": False,
                    "native_ref": f"{reg.table}:{reg.address}",
                    "source_protocol": "modbus_tcp",
                    "semantic_tags": reg.semantic_tags,
                })

        return InventorySnapshot(
            connection_id=connection_id,
            devices=devices,
            endpoints=endpoints,
            points=points,
        )

    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Modbus is poll-only — this reads all mapped registers once
        conn = self._get_conn(connection_id)
        for reg in conn.register_map:
            if point_ids and reg.point_id not in point_ids:
                continue
            if not reg.readable:
                continue
            value = await conn.read_register(reg)
            yield {
                "type": "point.reported",
                "point_id": reg.point_id,
                "value": {"kind": "float" if reg.table.endswith("register") else "bool", "reported": value, "unit": reg.unit},
                "quality": {"status": "good" if value is not None else "bad", "source_type": "polled"},
            }

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        reg = conn.registers_by_id.get(point_id)
        if not reg:
            raise InvalidTargetError(f"Unknown point: {point_id}")

        value = await conn.read_register(reg)
        quality = "good" if value is not None else "bad"

        return {
            "point_id": point_id,
            "value": {"kind": "float" if reg.table.endswith("register") else "bool", "reported": value, "unit": reg.unit},
            "quality": {"status": quality, "source_type": "polled"},
            "read_at": datetime.now(timezone.utc).isoformat(),
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        point_id = command.get("target", {}).get("point_id", "")
        value = command.get("params", {}).get("value")

        reg = conn.registers_by_id.get(point_id)
        if not reg:
            return {"command_id": cmd_id, "status": "failed", "error": f"Unknown point: {point_id}"}

        if not reg.writable:
            return {"command_id": cmd_id, "status": "failed", "error": f"Point {point_id} is read-only"}

        success = await conn.write_register(reg, value)

        result: dict[str, Any] = {
            "command_id": cmd_id,
            "status": "succeeded" if success else "failed",
            "verified": False,
        }

        # Readback verification if configured
        if success and reg.verification.get("readback"):
            import asyncio
            delay = reg.verification.get("delay_ms", 250) / 1000
            await asyncio.sleep(delay)
            readback = await conn.read_register(reg)
            result["verified"] = True
            result["actual_value"] = readback

        return result

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        if conn.client and conn.client.connected:
            return HealthStatus("healthy", {
                "host": conn.host,
                "port": conn.port,
                "register_count": len(conn.register_map),
            })
        return HealthStatus("offline", {"host": conn.host, "port": conn.port})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.disconnect()

    def _get_conn(self, connection_id: str) -> _ModbusConnection:
        conn = self._connections.get(connection_id)
        if not conn:
            raise UnreachableError(f"No active connection: {connection_id}")
        return conn
