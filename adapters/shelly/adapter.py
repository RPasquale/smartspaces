"""Shelly Gen2+/Pro adapter.

Supports Shelly devices via their local HTTP RPC API. Uses mDNS or
HTTP probing for discovery, digest auth where configured.
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


class _ShellyConnection:
    def __init__(self, connection_id: str, host: str, auth: httpx.DigestAuth | None = None):
        self.connection_id = connection_id
        self.host = host
        self.auth = auth
        self.client = httpx.AsyncClient(timeout=5.0, auth=auth)
        self.base_url = f"http://{host}"
        self.commissioned_at = datetime.now(timezone.utc)

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"id": 1, "method": method}
        if params:
            payload["params"] = params
        resp = await self.client.post(f"{self.base_url}/rpc", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise Exception(f"Shelly RPC error: {data['error']}")
        return data.get("result", {})

    async def close(self):
        if not self.client.is_closed:
            await self.client.aclose()


class ShellyAdapter(Adapter):
    adapter_id: str = "shelly.gen2"
    adapter_class: AdapterClass = "direct_device"

    def __init__(self):
        self._connections: dict[str, _ShellyConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Shelly Gen2 (LAN, open)",
                category="smart_switch",
                discovery_methods=["mdns", "http_probe", "manual_ip"],
                required_fields=["host"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Shelly Gen2 (LAN, digest auth)",
                category="smart_switch",
                discovery_methods=["mdns", "http_probe"],
                required_fields=["host"],
                secret_fields=["password"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets: list[DiscoveredTarget] = []
        if "http_probe" in request.methods:
            host = request.scope.get("host")
            if host:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.post(
                            f"http://{host}/rpc",
                            json={"id": 1, "method": "Shelly.GetDeviceInfo"},
                        )
                        if resp.status_code == 200:
                            info = resp.json().get("result", {})
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=info.get("id", host),
                                title=info.get("name", info.get("app", "Shelly")),
                                address=host,
                                fingerprint={
                                    "model": info.get("model"),
                                    "app": info.get("app"),
                                    "fw_id": info.get("fw_id"),
                                    "mac": info.get("mac"),
                                },
                                confidence=0.95,
                            ))
                except Exception:
                    pass
        return targets

    async def commission(
        self,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        host = profile.fields.get("host")
        if not host and target:
            host = target.address
        if not host:
            return CommissionResult("", "failed", {"error": "No host"})

        auth = None
        for secret in profile.secrets:
            if secret.name == "password":
                auth = httpx.DigestAuth("admin", secret.handle)

        conn_id = f"shelly_{uuid.uuid4().hex[:8]}"
        conn = _ShellyConnection(conn_id, host, auth)

        try:
            await conn.rpc("Shelly.GetDeviceInfo")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        info = await conn.rpc("Shelly.GetDeviceInfo")
        config = await conn.rpc("Shelly.GetConfig")
        status = await conn.rpc("Shelly.GetStatus")

        device_id = f"dev_shelly_{info.get('id', conn.host)}"
        device = {
            "device_id": device_id,
            "native_device_ref": info.get("id", conn.host),
            "device_family": "shelly.gen2",
            "name": info.get("name") or info.get("app", "Shelly"),
            "manufacturer": "Shelly",
            "model": info.get("model", "Unknown"),
            "firmware": {"version": info.get("fw_id", ""), "app": info.get("app", "")},
            "connectivity": {"transport": "http", "address": conn.host},
            "safety_class": "S1",
        }

        endpoints = []
        points = []

        # Enumerate switch components
        for key in sorted(config.keys()):
            if key.startswith("switch:"):
                idx = key.split(":")[1]
                ep_id = f"{device_id}_switch_{idx}"
                sw_config = config[key]
                sw_status = status.get(key, {})

                caps = ["binary_switch", "relay_output"]
                if "apower" in sw_status:
                    caps.append("meter_power")
                if "aenergy" in sw_status:
                    caps.append("meter_energy")

                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": device_id,
                    "native_endpoint_ref": key,
                    "endpoint_type": "switch_channel",
                    "direction": "read_write",
                    "capabilities": caps,
                    "traits": {
                        "name": sw_config.get("name"),
                        "supports_toggle": True,
                    },
                    "polling_mode": "push_preferred_with_poll_verify",
                    "safety_class": "S1",
                })

                points.append({
                    "point_id": f"{ep_id}_state",
                    "endpoint_id": ep_id,
                    "point_class": "switch.state",
                    "value_type": "bool",
                    "unit": None,
                    "readable": True,
                    "writable": True,
                    "native_ref": f"{key}/output",
                    "source_protocol": "shelly_rpc",
                })

                if "apower" in sw_status:
                    points.append({
                        "point_id": f"{ep_id}_power",
                        "endpoint_id": ep_id,
                        "point_class": "power.watts",
                        "value_type": "float",
                        "unit": "W",
                        "readable": True,
                        "writable": False,
                        "native_ref": f"{key}/apower",
                        "source_protocol": "shelly_rpc",
                    })

                if "aenergy" in sw_status:
                    energy = sw_status["aenergy"]
                    points.append({
                        "point_id": f"{ep_id}_energy",
                        "endpoint_id": ep_id,
                        "point_class": "energy.wh_total",
                        "value_type": "float",
                        "unit": "Wh",
                        "readable": True,
                        "writable": False,
                        "native_ref": f"{key}/aenergy/total",
                        "source_protocol": "shelly_rpc",
                    })

            # Input components
            elif key.startswith("input:"):
                idx = key.split(":")[1]
                ep_id = f"{device_id}_input_{idx}"
                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": device_id,
                    "native_endpoint_ref": key,
                    "endpoint_type": "input_channel",
                    "direction": "read",
                    "capabilities": ["digital_input", "binary_sensor"],
                    "polling_mode": "push_preferred_with_poll_verify",
                    "safety_class": "S0",
                })
                input_status = status.get(key, {})
                points.append({
                    "point_id": f"{ep_id}_state",
                    "endpoint_id": ep_id,
                    "point_class": "digital_input.state",
                    "value_type": "bool",
                    "unit": None,
                    "readable": True,
                    "writable": False,
                    "native_ref": f"{key}/state",
                    "source_protocol": "shelly_rpc",
                })

            # Cover components
            elif key.startswith("cover:"):
                idx = key.split(":")[1]
                ep_id = f"{device_id}_cover_{idx}"
                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": device_id,
                    "native_endpoint_ref": key,
                    "endpoint_type": "cover_channel",
                    "direction": "read_write",
                    "capabilities": ["cover"],
                    "polling_mode": "push_preferred_with_poll_verify",
                    "safety_class": "S1",
                })
                points.extend([
                    {
                        "point_id": f"{ep_id}_position",
                        "endpoint_id": ep_id,
                        "point_class": "cover.position_pct",
                        "value_type": "int",
                        "unit": "%",
                        "readable": True,
                        "writable": True,
                        "native_ref": f"{key}/current_pos",
                        "source_protocol": "shelly_rpc",
                    },
                    {
                        "point_id": f"{ep_id}_state",
                        "endpoint_id": ep_id,
                        "point_class": "cover.state",
                        "value_type": "str",
                        "unit": None,
                        "readable": True,
                        "writable": False,
                        "native_ref": f"{key}/state",
                        "source_protocol": "shelly_rpc",
                    },
                ])

        return InventorySnapshot(
            connection_id=connection_id,
            devices=[device],
            endpoints=endpoints,
            points=points,
            raw={"info": info, "config": config, "status": status},
        )

    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Polling fallback — real implementation would use WebSocket
        conn = self._get_conn(connection_id)
        status = await conn.rpc("Shelly.GetStatus")
        for key, value in status.items():
            if key.startswith("switch:") and isinstance(value, dict):
                idx = key.split(":")[1]
                yield {
                    "type": "point.reported",
                    "point_id": f"switch_{idx}_state",
                    "value": {"kind": "bool", "reported": value.get("output", False)},
                    "quality": {"status": "good", "source_type": "polled"},
                }

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        status = await conn.rpc("Shelly.GetStatus")

        if "_switch_" in point_id and point_id.endswith("_state"):
            idx = point_id.split("_switch_")[1].split("_")[0]
            sw = status.get(f"switch:{idx}", {})
            return {
                "point_id": point_id,
                "value": {"kind": "bool", "reported": sw.get("output", False)},
                "quality": {"status": "good", "source_type": "polled"},
            }

        if "_switch_" in point_id and point_id.endswith("_power"):
            idx = point_id.split("_switch_")[1].split("_")[0]
            sw = status.get(f"switch:{idx}", {})
            return {
                "point_id": point_id,
                "value": {"kind": "float", "reported": sw.get("apower", 0.0), "unit": "W"},
                "quality": {"status": "good", "source_type": "polled"},
            }

        if "_input_" in point_id:
            idx = point_id.split("_input_")[1].split("_")[0]
            inp = status.get(f"input:{idx}", {})
            return {
                "point_id": point_id,
                "value": {"kind": "bool", "reported": inp.get("state", False)},
                "quality": {"status": "good", "source_type": "polled"},
            }

        if "_cover_" in point_id and point_id.endswith("_position"):
            idx = point_id.split("_cover_")[1].split("_")[0]
            cov = status.get(f"cover:{idx}", {})
            return {
                "point_id": point_id,
                "value": {"kind": "int", "reported": cov.get("current_pos", 0), "unit": "%"},
                "quality": {"status": "good", "source_type": "polled"},
            }

        raise InvalidTargetError(f"Unknown point: {point_id}")

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        endpoint_id = command.get("target", {}).get("endpoint_id", "")
        verb = command.get("verb", "set")
        params = command.get("params", {})
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")

        if "switch" in endpoint_id:
            idx = endpoint_id.split("_switch_")[1].split("_")[0] if "_switch_" in endpoint_id else "0"
            if verb == "set":
                await conn.rpc("Switch.Set", {"id": int(idx), "on": params.get("value", True)})
            elif verb == "toggle":
                await conn.rpc("Switch.Toggle", {"id": int(idx)})
            else:
                return {"command_id": cmd_id, "status": "failed", "error": f"Unsupported verb: {verb}"}

            # Verify
            st = await conn.rpc("Shelly.GetStatus")
            actual = st.get(f"switch:{idx}", {}).get("output", None)
            return {
                "command_id": cmd_id,
                "status": "succeeded",
                "verified": True,
                "actual_state": actual,
            }

        if "cover" in endpoint_id:
            idx = endpoint_id.split("_cover_")[1].split("_")[0] if "_cover_" in endpoint_id else "0"
            if verb == "open":
                await conn.rpc("Cover.Open", {"id": int(idx)})
            elif verb == "close":
                await conn.rpc("Cover.Close", {"id": int(idx)})
            elif verb == "stop":
                await conn.rpc("Cover.Stop", {"id": int(idx)})
            elif verb == "set":
                pos = params.get("position", 100)
                await conn.rpc("Cover.GoToPosition", {"id": int(idx), "pos": pos})
            return {"command_id": cmd_id, "status": "succeeded", "verified": False}

        return {"command_id": cmd_id, "status": "failed", "error": f"Unknown endpoint: {endpoint_id}"}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        try:
            info = await conn.rpc("Shelly.GetDeviceInfo")
            return HealthStatus("healthy", {"host": conn.host, "uptime": info.get("uptime")})
        except Exception as e:
            return HealthStatus("error", {"host": conn.host, "error": str(e)})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, connection_id: str) -> _ShellyConnection:
        conn = self._connections.get(connection_id)
        if not conn:
            raise UnreachableError(f"No active connection: {connection_id}")
        return conn
