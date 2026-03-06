"""KinCony adapter — reference implementation of the adapter SDK.

Supports multiple KinCony boards and firmware personalities.
Currently implements the Tasmota firmware profile for the KC868-A4.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from adapters.kincony.firmware_profiles.tasmota import TasmotaProfile
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

BOARDS_DIR = Path(__file__).parent / "boards"


def _load_board(board_id: str) -> dict[str, Any]:
    path = BOARDS_DIR / f"{board_id}.yaml"
    if not path.exists():
        raise InvalidTargetError(f"Unknown board: {board_id}")
    with path.open() as f:
        return yaml.safe_load(f)


class _Connection:
    """Internal state for an active connection."""

    def __init__(
        self,
        connection_id: str,
        profile: TasmotaProfile,
        board: dict[str, Any],
        host: str,
    ):
        self.connection_id = connection_id
        self.profile = profile
        self.board = board
        self.host = host
        self.commissioned_at = datetime.now(timezone.utc)


class KinConyAdapter(Adapter):
    """Adapter for KinCony ESP32 relay boards running Tasmota firmware."""

    adapter_id: str = "kincony.family"
    adapter_class: AdapterClass = "direct_device"

    def __init__(self):
        self._connections: dict[str, _Connection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="KinCony Tasmota (HTTP)",
                category="relay_controller",
                discovery_methods=["http_probe", "manual_ip"],
                required_fields=["host"],
                optional_fields=["board_id"],
                secret_fields=["password"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
                risk_level="low",
            ),
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="KinCony Tasmota (MQTT)",
                category="relay_controller",
                discovery_methods=["mqtt_discovery"],
                required_fields=["broker_host", "topic"],
                secret_fields=["mqtt_username", "mqtt_password"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
                risk_level="low",
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets: list[DiscoveredTarget] = []

        if "http_probe" in request.methods:
            host = request.scope.get("host")
            if host:
                profile = TasmotaProfile(host)
                try:
                    reachable = await profile.ping()
                    if reachable:
                        status = await profile.device_status()
                        dev_name = status.get("Status", {}).get("DeviceName", "Unknown")
                        targets.append(DiscoveredTarget(
                            discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                            adapter_id=self.adapter_id,
                            native_ref=host,
                            title=dev_name,
                            address=host,
                            fingerprint={
                                "firmware": "tasmota",
                                "status": status.get("Status", {}),
                            },
                            confidence=0.95,
                            suggested_profile_id="tasmota_http",
                        ))
                finally:
                    await profile.close()

        return targets

    async def commission(
        self,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        host = profile.fields.get("host")
        if not host:
            if target and target.address:
                host = target.address
            else:
                return CommissionResult(
                    connection_id="",
                    status="failed",
                    diagnostics={"error": "No host provided"},
                )

        board_id = profile.fields.get("board_id", "kc868_a4")
        password = None
        for secret in profile.secrets:
            if secret.name == "password":
                password = secret.handle

        board = _load_board(board_id)
        tasmota = TasmotaProfile(host, password=password)

        try:
            reachable = await tasmota.ping()
        except Exception as e:
            await tasmota.close()
            return CommissionResult(
                connection_id="",
                status="failed",
                diagnostics={"error": str(e)},
            )

        if not reachable:
            await tasmota.close()
            return CommissionResult(
                connection_id="",
                status="failed",
                diagnostics={"error": f"Device at {host} is not reachable"},
            )

        conn_id = f"kincony_{uuid.uuid4().hex[:8]}"
        self._connections[conn_id] = _Connection(
            connection_id=conn_id,
            profile=tasmota,
            board=board,
            host=host,
        )

        return CommissionResult(
            connection_id=conn_id,
            status="ok",
            diagnostics={"host": host, "board": board_id},
        )

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_connection(connection_id)
        board = conn.board
        board_id = board["board_id"]
        host = conn.host

        # Fetch live device info
        try:
            status = await conn.profile.device_status()
        except Exception:
            status = {}

        device_name = status.get("Status", {}).get("DeviceName", board["display_name"])
        mac = status.get("StatusNET", {}).get("Mac", "")
        fw_version = status.get("StatusFWR", {}).get("Version", "")

        device_id = f"dev_{board_id}_{host.replace('.', '_')}"
        device = {
            "device_id": device_id,
            "native_device_ref": host,
            "device_family": f"kincony.{board_id}",
            "name": device_name,
            "manufacturer": "KinCony",
            "model": board["display_name"],
            "firmware": {"version": fw_version, "personality": "tasmota"},
            "hardware": {"mcu": board["mcu"], "flash_mb": board["flash_mb"]},
            "connectivity": {"transport": "http", "address": host},
            "safety_class": board.get("default_safety_class", "S2"),
            "bridge_device_id": None,
        }

        endpoints = []
        points = []

        # Relays
        for i in range(1, board["relays"] + 1):
            ep_id = f"{device_id}_relay_{i}"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": device_id,
                "native_endpoint_ref": f"Power{i}",
                "endpoint_type": "relay_channel",
                "direction": "read_write",
                "capabilities": ["relay_output", "binary_switch"],
                "traits": {"supports_toggle": True, "supports_pulse": True},
                "polling_mode": "poll_preferred_with_event_assist",
                "safety_class": board.get("default_safety_class", "S2"),
            })
            points.append({
                "point_id": f"{ep_id}_state",
                "endpoint_id": ep_id,
                "point_class": "switch.state",
                "value_type": "bool",
                "unit": None,
                "readable": True,
                "writable": True,
                "event_driven": False,
                "native_ref": f"POWER{i}",
                "source_protocol": "tasmota_http",
                "semantic_tags": ["relay", "switch"],
            })

        # Digital inputs
        for i in range(1, board["digital_inputs"] + 1):
            ep_id = f"{device_id}_dinput_{i}"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": device_id,
                "native_endpoint_ref": f"Switch{i}",
                "endpoint_type": "digital_input_channel",
                "direction": "read",
                "capabilities": ["digital_input", "binary_sensor"],
                "traits": {"opto_isolated": True, "voltage_range": "5-24V DC"},
                "polling_mode": "poll_only",
                "safety_class": "S0",
            })
            points.append({
                "point_id": f"{ep_id}_state",
                "endpoint_id": ep_id,
                "point_class": "digital_input.state",
                "value_type": "bool",
                "unit": None,
                "readable": True,
                "writable": False,
                "event_driven": False,
                "native_ref": f"Switch{i}",
                "source_protocol": "tasmota_http",
                "semantic_tags": ["digital_input", "binary_sensor"],
            })

        # Analog inputs
        for i in range(1, board["analog_inputs"] + 1):
            ep_id = f"{device_id}_ainput_{i}"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": device_id,
                "native_endpoint_ref": f"A{i}",
                "endpoint_type": "analog_input_channel",
                "direction": "read",
                "capabilities": ["analog_input"],
                "traits": {
                    "resolution_bits": board.get("input_specs", {}).get(
                        "analog_resolution_bits", 12
                    ),
                    "voltage_range": "0-3.3V",
                },
                "polling_mode": "poll_only",
                "safety_class": "S0",
            })
            points.extend([
                {
                    "point_id": f"{ep_id}_raw",
                    "endpoint_id": ep_id,
                    "point_class": "analog_input.raw",
                    "value_type": "int",
                    "unit": None,
                    "readable": True,
                    "writable": False,
                    "event_driven": False,
                    "native_ref": f"ANALOG.A{i}",
                    "source_protocol": "tasmota_http",
                    "semantic_tags": ["analog_input"],
                },
                {
                    "point_id": f"{ep_id}_voltage",
                    "endpoint_id": ep_id,
                    "point_class": "analog_input.voltage",
                    "value_type": "float",
                    "unit": "V",
                    "readable": True,
                    "writable": False,
                    "event_driven": False,
                    "native_ref": f"ANALOG.A{i}",
                    "source_protocol": "tasmota_http",
                    "semantic_tags": ["analog_input", "voltage"],
                },
            ])

        # IR transmitter
        if board.get("ir_tx"):
            ep_id = f"{device_id}_ir_tx"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": device_id,
                "native_endpoint_ref": "IRsend",
                "endpoint_type": "ir_transmitter",
                "direction": "write",
                "capabilities": ["ir_transmit"],
                "traits": {"protocols": ["NEC", "Sony", "RC5", "RC6", "Samsung", "LG"]},
                "polling_mode": "push_only",
                "safety_class": "S1",
            })

        # IR receiver
        if board.get("ir_rx"):
            ep_id = f"{device_id}_ir_rx"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": device_id,
                "native_endpoint_ref": "IRrecv",
                "endpoint_type": "ir_receiver",
                "direction": "event_only",
                "capabilities": ["binary_sensor"],
                "traits": {},
                "polling_mode": "push_only",
                "safety_class": "S0",
            })

        raw_status = {}
        try:
            raw_status = await conn.profile.device_status()
        except Exception:
            pass

        return InventorySnapshot(
            connection_id=connection_id,
            devices=[device],
            endpoints=endpoints,
            points=points,
            raw=raw_status,
        )

    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Tasmota HTTP doesn't support push subscriptions.
        # For real-time events, use MQTT or WebSocket transport.
        # This is a polling fallback that yields once per call.
        conn = self._get_connection(connection_id)
        try:
            relay_states = await conn.profile.relay_status()
            for key, value in relay_states.items():
                point_id = f"dev_{conn.board['board_id']}_{conn.host.replace('.', '_')}_relay_{key[-1]}_state"
                if point_ids is None or point_id in point_ids:
                    yield {
                        "event_id": f"evt_{uuid.uuid4().hex[:8]}",
                        "type": "point.reported",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "point_id": point_id,
                        "value": {"kind": "bool", "reported": value == "ON", "unit": None},
                        "quality": {"status": "good", "source_type": "polled"},
                        "raw": {key: value},
                    }
        except Exception:
            pass

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_connection(connection_id)

        # Parse point_id to determine what to read
        if "_relay_" in point_id and point_id.endswith("_state"):
            relay_num = self._extract_index(point_id, "_relay_")
            state = await conn.profile.get_relay_state(relay_num)
            return {
                "point_id": point_id,
                "value": {"kind": "bool", "reported": state, "unit": None},
                "quality": {"status": "good", "source_type": "polled"},
                "raw": {f"POWER{relay_num}": "ON" if state else "OFF"},
                "read_at": datetime.now(timezone.utc).isoformat(),
            }

        if "_ainput_" in point_id and point_id.endswith("_raw"):
            ch = self._extract_index(point_id, "_ainput_")
            raw_val = await conn.profile.read_analog(ch)
            return {
                "point_id": point_id,
                "value": {"kind": "int", "reported": raw_val, "unit": None},
                "quality": {"status": "good", "source_type": "polled"},
                "raw": {f"ANALOG.A{ch}": raw_val},
                "read_at": datetime.now(timezone.utc).isoformat(),
            }

        if "_ainput_" in point_id and point_id.endswith("_voltage"):
            ch = self._extract_index(point_id, "_ainput_")
            voltage = await conn.profile.read_analog_voltage(ch)
            return {
                "point_id": point_id,
                "value": {"kind": "float", "reported": round(voltage, 4), "unit": "V"},
                "quality": {"status": "good", "source_type": "polled"},
                "raw": {f"ANALOG.A{ch}": int(voltage * 4095 / 3.3)},
                "read_at": datetime.now(timezone.utc).isoformat(),
            }

        if "_dinput_" in point_id and point_id.endswith("_state"):
            # Digital inputs require Status 10 or rules-based reading
            status = await conn.profile.send_command("Status 10")
            switch_states = status.get("StatusSNS", {})
            ch = self._extract_index(point_id, "_dinput_")
            key = f"Switch{ch}"
            val = switch_states.get(key, "OFF")
            return {
                "point_id": point_id,
                "value": {"kind": "bool", "reported": val == "ON", "unit": None},
                "quality": {"status": "good", "source_type": "polled"},
                "raw": {key: val},
                "read_at": datetime.now(timezone.utc).isoformat(),
            }

        raise InvalidTargetError(f"Unknown point: {point_id}")

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_connection(connection_id)

        target = command.get("target", {})
        endpoint_id = target.get("endpoint_id", "")
        verb = command.get("verb", "set")
        params = command.get("params", {})
        command_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")

        # Relay commands
        if "relay" in endpoint_id:
            relay_num = self._extract_index(endpoint_id, "_relay_")

            if verb == "set":
                value = params.get("value", True)
                if value:
                    result = await conn.profile.relay_on(relay_num)
                else:
                    result = await conn.profile.relay_off(relay_num)
            elif verb == "toggle":
                result = await conn.profile.relay_toggle(relay_num)
            elif verb == "pulse":
                duration_ds = params.get("duration_deciseconds", 10)
                await conn.profile.set_pulse_time(relay_num, duration_ds)
                result = await conn.profile.relay_on(relay_num)
            else:
                return {
                    "command_id": command_id,
                    "status": "failed",
                    "error": f"Unsupported verb: {verb}",
                }

            # Verify after write
            actual_state = await conn.profile.get_relay_state(relay_num)

            return {
                "command_id": command_id,
                "status": "succeeded",
                "verified": True,
                "result": result,
                "actual_state": actual_state,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }

        # IR transmit commands
        if "ir_tx" in endpoint_id:
            protocol = params.get("protocol", "NEC")
            bits = params.get("bits", 32)
            data = params.get("data", "0x00000000")
            result = await conn.profile.ir_send(protocol, bits, data)
            return {
                "command_id": command_id,
                "status": "succeeded",
                "verified": False,
                "result": result,
                "executed_at": datetime.now(timezone.utc).isoformat(),
            }

        return {
            "command_id": command_id,
            "status": "failed",
            "error": f"Unknown endpoint: {endpoint_id}",
        }

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_connection(connection_id)
        try:
            reachable = await conn.profile.ping()
            if reachable:
                signal = await conn.profile.wifi_signal()
                return HealthStatus(
                    status="healthy",
                    details={
                        "host": conn.host,
                        "wifi_signal_dbm": signal,
                        "commissioned_at": conn.commissioned_at.isoformat(),
                    },
                )
            return HealthStatus(status="offline", details={"host": conn.host})
        except Exception as e:
            return HealthStatus(status="error", details={"host": conn.host, "error": str(e)})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.profile.close()

    # -- Helpers --

    def _get_connection(self, connection_id: str) -> _Connection:
        conn = self._connections.get(connection_id)
        if not conn:
            raise UnreachableError(f"No active connection: {connection_id}")
        return conn

    @staticmethod
    def _extract_index(text: str, marker: str) -> int:
        """Extract numeric index after a marker in a point/endpoint ID."""
        idx = text.index(marker) + len(marker)
        num_str = ""
        for ch in text[idx:]:
            if ch.isdigit():
                num_str += ch
            else:
                break
        return int(num_str) if num_str else 1
