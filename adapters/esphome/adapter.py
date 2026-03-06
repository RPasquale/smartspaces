"""ESPHome adapter.

Connects to ESPHome nodes via their native API or MQTT fallback.
Treats ESPHome as a first-class endpoint ecosystem with rich
composition support (many entity types per node).
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


class _ESPHomeConnection:
    def __init__(self, connection_id: str, host: str, port: int = 6053, password: str | None = None):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.password = password
        # For now, use the ESPHome REST API (requires web_server component)
        self.api_url = f"http://{host}"
        self.client = httpx.AsyncClient(timeout=10.0)
        self.commissioned_at = datetime.now(timezone.utc)

    async def get(self, path: str) -> Any:
        resp = await self.client.get(f"{self.api_url}{path}")
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        resp = await self.client.post(f"{self.api_url}{path}", json=data or {})
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}

    async def close(self):
        if not self.client.is_closed:
            await self.client.aclose()


ENTITY_CAP_MAP = {
    "switch": (["binary_switch"], "read_write", "S1"),
    "light": (["dimmer", "light_color"], "read_write", "S1"),
    "binary_sensor": (["binary_sensor"], "read", "S0"),
    "sensor": (["analog_input"], "read", "S0"),
    "fan": (["fan"], "read_write", "S1"),
    "cover": (["cover"], "read_write", "S1"),
    "climate": (["thermostat", "climate_setpoint"], "read_write", "S2"),
    "number": (["setpoint"], "read_write", "S1"),
    "select": (["hvac_mode"], "read_write", "S1"),
    "button": (["momentary_output"], "write", "S1"),
    "lock": (["lock"], "read_write", "S2"),
    "text_sensor": (["binary_sensor"], "read", "S0"),
}


class ESPHomeAdapter(Adapter):
    adapter_id: str = "esphome.native"
    adapter_class: AdapterClass = "direct_device"

    def __init__(self):
        self._connections: dict[str, _ESPHomeConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="ESPHome Node (HTTP/REST)",
                category="smart_device",
                discovery_methods=["mdns", "http_probe", "manual_ip"],
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
                        resp = await client.get(f"http://{host}/")
                        if resp.status_code == 200 and "esphome" in resp.text.lower():
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=host,
                                title=f"ESPHome @ {host}",
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

        password = None
        for s in profile.secrets:
            if s.name == "password":
                password = s.handle

        conn_id = f"esphome_{uuid.uuid4().hex[:8]}"
        conn = _ESPHomeConnection(conn_id, host, password=password)
        try:
            await conn.get("/")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)
        device_id = f"dev_esphome_{conn.host.replace('.', '_')}"

        devices = [{
            "device_id": device_id,
            "native_device_ref": conn.host,
            "device_family": "esphome.node",
            "name": f"ESPHome {conn.host}",
            "manufacturer": "ESPHome",
            "connectivity": {"transport": "http", "address": conn.host},
            "safety_class": "S1",
        }]

        endpoints = []
        points = []

        # ESPHome REST API serves entities at typed endpoints
        for entity_type, (caps, direction, safety) in ENTITY_CAP_MAP.items():
            try:
                entities = await conn.get(f"/{entity_type}")
                if not isinstance(entities, list):
                    continue
            except Exception:
                continue

            for entity in entities:
                eid = entity.get("id", entity.get("object_id", ""))
                name = entity.get("name", eid)
                ep_id = f"{device_id}_{entity_type}_{eid}"

                endpoints.append({
                    "endpoint_id": ep_id,
                    "device_id": device_id,
                    "native_endpoint_ref": f"{entity_type}/{eid}",
                    "endpoint_type": entity_type,
                    "direction": direction,
                    "capabilities": caps,
                    "traits": {"name": name},
                    "polling_mode": "push_preferred_with_poll_verify",
                    "safety_class": safety,
                })

                # State point
                points.append({
                    "point_id": f"{ep_id}_state",
                    "endpoint_id": ep_id,
                    "point_class": f"{entity_type}.state",
                    "value_type": "str",
                    "unit": entity.get("unit_of_measurement"),
                    "readable": direction in ("read", "read_write"),
                    "writable": direction in ("write", "read_write"),
                    "native_ref": f"{entity_type}/{eid}",
                    "source_protocol": "esphome_rest",
                })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # ESPHome native API supports event stream; REST fallback is poll
        conn = self._get_conn(connection_id)
        try:
            for etype in ("switch", "sensor", "binary_sensor"):
                entities = await conn.get(f"/{etype}")
                if isinstance(entities, list):
                    for e in entities:
                        yield {
                            "type": "point.reported",
                            "point_id": f"dev_esphome_{conn.host.replace('.', '_')}_{etype}_{e.get('id', '')}_state",
                            "value": {"kind": "str", "reported": e.get("state", e.get("value"))},
                            "quality": {"status": "good", "source_type": "polled"},
                        }
        except Exception:
            pass

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Parse entity type and id from point_id
        parts = point_id.replace(f"dev_esphome_{conn.host.replace('.', '_')}_", "").rsplit("_state", 1)[0]
        # Find entity_type and eid
        for etype in ENTITY_CAP_MAP:
            prefix = f"{etype}_"
            if parts.startswith(prefix):
                eid = parts[len(prefix):]
                try:
                    entity = await conn.get(f"/{etype}/{eid}")
                    return {
                        "point_id": point_id,
                        "value": {"kind": "str", "reported": entity.get("state", entity.get("value"))},
                        "quality": {"status": "good", "source_type": "polled"},
                    }
                except Exception:
                    break
        raise InvalidTargetError(f"Unknown point: {point_id}")

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        endpoint_id = command.get("target", {}).get("endpoint_id", "")
        verb = command.get("verb", "set")
        params = command.get("params", {})

        # Parse entity type and id
        base = endpoint_id.replace(f"dev_esphome_{conn.host.replace('.', '_')}_", "")
        for etype in ENTITY_CAP_MAP:
            prefix = f"{etype}_"
            if base.startswith(prefix):
                eid = base[len(prefix):]
                action = "turn_on" if params.get("value", True) else "turn_off"
                if verb == "toggle":
                    action = "toggle"
                try:
                    await conn.post(f"/{etype}/{eid}/{action}")
                    return {"command_id": cmd_id, "status": "succeeded", "verified": False}
                except Exception as e:
                    return {"command_id": cmd_id, "status": "failed", "error": str(e)}

        return {"command_id": cmd_id, "status": "failed", "error": f"Unknown endpoint: {endpoint_id}"}

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

    def _get_conn(self, cid: str) -> _ESPHomeConnection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
