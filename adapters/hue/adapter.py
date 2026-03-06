"""Philips Hue adapter.

Bridge-based adapter that discovers and controls Hue lights, sensors,
and scenes via the Hue Bridge v2 (CLIP) API.
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


class _HueConnection:
    def __init__(self, connection_id: str, host: str, api_key: str):
        self.connection_id = connection_id
        self.host = host
        self.api_key = api_key
        self.base_url = f"https://{host}/clip/v2"
        self.client = httpx.AsyncClient(
            timeout=10.0,
            verify=False,  # Hue bridge uses self-signed cert
            headers={"hue-application-key": api_key},
        )
        self.commissioned_at = datetime.now(timezone.utc)

    async def get(self, path: str) -> dict[str, Any]:
        resp = await self.client.get(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json()

    async def put(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = await self.client.put(f"{self.base_url}{path}", json=data)
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        if not self.client.is_closed:
            await self.client.aclose()


class HueAdapter(Adapter):
    adapter_id: str = "hue.bridge"
    adapter_class: AdapterClass = "bridge"

    def __init__(self):
        self._connections: dict[str, _HueConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="Philips Hue Bridge",
                category="lighting",
                discovery_methods=["mdns", "http_probe", "manual_ip"],
                required_fields=["host"],
                secret_fields=["api_key"],
                physical_actions=["Press link button on Hue Bridge"],
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
                    async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                        resp = await client.get(f"https://{host}/api/0/config")
                        if resp.status_code == 200:
                            config = resp.json()
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=config.get("bridgeid", host),
                                title=config.get("name", "Hue Bridge"),
                                address=host,
                                fingerprint={
                                    "model": config.get("modelid"),
                                    "sw_version": config.get("swversion"),
                                    "api_version": config.get("apiversion"),
                                },
                                confidence=0.9,
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

        api_key = None
        for secret in profile.secrets:
            if secret.name == "api_key":
                api_key = secret.handle

        if not api_key:
            # Attempt link-button pairing
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    resp = await client.post(
                        f"https://{host}/api",
                        json={"devicetype": "physical_space_adapter#instance", "generateclientkey": True},
                    )
                    data = resp.json()
                    if isinstance(data, list) and data:
                        entry = data[0]
                        if "success" in entry:
                            api_key = entry["success"].get("username")
            except Exception as e:
                return CommissionResult("", "failed", {"error": f"Pairing failed: {e}"})

        if not api_key:
            return CommissionResult("", "failed", {
                "error": "No API key. Press link button on bridge and retry.",
                "action_required": "press_link_button",
            })

        conn_id = f"hue_{uuid.uuid4().hex[:8]}"
        conn = _HueConnection(conn_id, host, api_key)

        try:
            await conn.get("/resource/bridge")
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        lights_resp = await conn.get("/resource/light")
        rooms_resp = await conn.get("/resource/room")
        scenes_resp = await conn.get("/resource/scene")

        lights = lights_resp.get("data", [])
        rooms = rooms_resp.get("data", [])
        scenes = scenes_resp.get("data", [])

        # Bridge device
        bridge_id = f"dev_hue_bridge_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": bridge_id,
            "native_device_ref": conn.host,
            "device_family": "hue.bridge",
            "name": "Hue Bridge",
            "manufacturer": "Signify",
            "model": "BSB002",
            "connectivity": {"transport": "https", "address": conn.host},
            "safety_class": "S0",
            "bridge_device_id": None,
        }]

        endpoints = []
        points = []

        for light in lights:
            light_id = light["id"]
            owner = light.get("owner", {})
            dev_id = f"dev_hue_{light_id}"

            metadata = light.get("metadata", {})
            name = metadata.get("name", f"Light {light_id[:8]}")

            devices.append({
                "device_id": dev_id,
                "native_device_ref": light_id,
                "device_family": "hue.light",
                "name": name,
                "manufacturer": "Signify",
                "model": metadata.get("archetype", "unknown"),
                "connectivity": {"transport": "hue_zigbee", "address": light_id},
                "safety_class": "S1",
                "bridge_device_id": bridge_id,
            })

            # Determine capabilities
            caps = ["binary_switch"]
            has_dimming = "dimming" in light
            has_color = "color" in light
            has_color_temp = "color_temperature" in light

            if has_dimming:
                caps.append("dimmer")
            if has_color:
                caps.append("light_color")

            ep_id = f"{dev_id}_light"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": dev_id,
                "native_endpoint_ref": f"light/{light_id}",
                "endpoint_type": "light",
                "direction": "read_write",
                "capabilities": caps,
                "polling_mode": "push_preferred_with_poll_verify",
                "safety_class": "S1",
            })

            # On/off point
            on_state = light.get("on", {}).get("on", False)
            points.append({
                "point_id": f"{ep_id}_on",
                "endpoint_id": ep_id,
                "point_class": "switch.state",
                "value_type": "bool",
                "readable": True,
                "writable": True,
                "native_ref": f"light/{light_id}/on",
                "source_protocol": "hue_clip_v2",
            })

            # Brightness point
            if has_dimming:
                points.append({
                    "point_id": f"{ep_id}_brightness",
                    "endpoint_id": ep_id,
                    "point_class": "dimmer.level",
                    "value_type": "float",
                    "unit": "%",
                    "readable": True,
                    "writable": True,
                    "native_ref": f"light/{light_id}/dimming/brightness",
                    "source_protocol": "hue_clip_v2",
                })

            # Color temperature point
            if has_color_temp:
                points.append({
                    "point_id": f"{ep_id}_color_temp",
                    "endpoint_id": ep_id,
                    "point_class": "light_color.mirek",
                    "value_type": "int",
                    "unit": "mirek",
                    "readable": True,
                    "writable": True,
                    "native_ref": f"light/{light_id}/color_temperature/mirek",
                    "source_protocol": "hue_clip_v2",
                })

            # Color XY point
            if has_color:
                points.append({
                    "point_id": f"{ep_id}_color_xy",
                    "endpoint_id": ep_id,
                    "point_class": "light_color.xy",
                    "value_type": "dict",
                    "readable": True,
                    "writable": True,
                    "native_ref": f"light/{light_id}/color/xy",
                    "source_protocol": "hue_clip_v2",
                })

        return InventorySnapshot(
            connection_id=connection_id,
            devices=devices,
            endpoints=endpoints,
            points=points,
            raw={
                "lights_count": len(lights),
                "rooms_count": len(rooms),
                "scenes_count": len(scenes),
            },
        )

    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Hue v2 supports SSE on /eventstream — not implemented in this version
        # Polling fallback
        conn = self._get_conn(connection_id)
        lights = (await conn.get("/resource/light")).get("data", [])
        for light in lights:
            on = light.get("on", {}).get("on", False)
            yield {
                "type": "point.reported",
                "point_id": f"dev_hue_{light['id']}_light_on",
                "value": {"kind": "bool", "reported": on},
                "quality": {"status": "good", "source_type": "polled"},
            }

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)

        # Extract light ID from point_id
        # Format: dev_hue_{light_id}_light_{property}
        if "_light_on" in point_id:
            light_id = self._extract_light_id(point_id)
            resp = await conn.get(f"/resource/light/{light_id}")
            light = resp.get("data", [{}])[0]
            return {
                "point_id": point_id,
                "value": {"kind": "bool", "reported": light.get("on", {}).get("on", False)},
                "quality": {"status": "good", "source_type": "polled"},
            }

        if "_light_brightness" in point_id:
            light_id = self._extract_light_id(point_id)
            resp = await conn.get(f"/resource/light/{light_id}")
            light = resp.get("data", [{}])[0]
            return {
                "point_id": point_id,
                "value": {"kind": "float", "reported": light.get("dimming", {}).get("brightness", 0), "unit": "%"},
                "quality": {"status": "good", "source_type": "polled"},
            }

        raise InvalidTargetError(f"Unknown point: {point_id}")

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        endpoint_id = command.get("target", {}).get("endpoint_id", "")
        verb = command.get("verb", "set")
        params = command.get("params", {})

        if "_light" in endpoint_id:
            light_id = self._extract_light_id(endpoint_id)
            payload: dict[str, Any] = {}

            if verb == "set":
                if "value" in params:
                    payload["on"] = {"on": bool(params["value"])}
                if "brightness" in params:
                    payload["dimming"] = {"brightness": float(params["brightness"])}
                if "color_temp" in params:
                    payload["color_temperature"] = {"mirek": int(params["color_temp"])}
                if "color_xy" in params:
                    payload["color"] = {"xy": params["color_xy"]}
            elif verb == "toggle":
                resp = await conn.get(f"/resource/light/{light_id}")
                current = resp.get("data", [{}])[0].get("on", {}).get("on", False)
                payload["on"] = {"on": not current}
            elif verb == "dim":
                payload["dimming"] = {"brightness": float(params.get("level", 50))}

            if payload:
                await conn.put(f"/resource/light/{light_id}", payload)
                return {"command_id": cmd_id, "status": "succeeded", "verified": False}

        return {"command_id": cmd_id, "status": "failed", "error": f"Unknown target: {endpoint_id}"}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        try:
            resp = await conn.get("/resource/bridge")
            bridges = resp.get("data", [])
            if bridges:
                return HealthStatus("healthy", {"host": conn.host})
            return HealthStatus("degraded", {"host": conn.host})
        except Exception as e:
            return HealthStatus("error", {"host": conn.host, "error": str(e)})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, connection_id: str) -> _HueConnection:
        conn = self._connections.get(connection_id)
        if not conn:
            raise UnreachableError(f"No active connection: {connection_id}")
        return conn

    @staticmethod
    def _extract_light_id(text: str) -> str:
        """Extract Hue light UUID from a point/endpoint ID."""
        # dev_hue_{uuid}_light_...
        parts = text.split("_")
        # UUID is between "hue" and "light"
        hue_idx = parts.index("hue") if "hue" in parts else -1
        light_idx = parts.index("light") if "light" in parts else -1
        if hue_idx >= 0 and light_idx > hue_idx:
            return "-".join(parts[hue_idx + 1 : light_idx])
        return ""
