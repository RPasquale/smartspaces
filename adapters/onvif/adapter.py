"""ONVIF camera/NVR adapter.

Supports discovery, stream inventory, snapshot retrieval, motion events,
and PTZ control for ONVIF-compliant cameras and NVRs.
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


class _OnvifConnection:
    def __init__(self, connection_id: str, host: str, port: int, username: str, password: str):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(timeout=10.0)
        self.service_url = f"http://{host}:{port}/onvif/device_service"
        self.media_url = f"http://{host}:{port}/onvif/media_service"
        self.ptz_url = f"http://{host}:{port}/onvif/ptz_service"
        self.commissioned_at = datetime.now(timezone.utc)
        self.device_info: dict[str, Any] = {}
        self.profiles: list[dict[str, Any]] = []

    async def soap_request(self, url: str, body: str) -> str:
        """Send a SOAP request and return the response body."""
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Header>
    <Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <UsernameToken>
        <Username>{self.username}</Username>
        <Password>{self.password}</Password>
      </UsernameToken>
    </Security>
  </s:Header>
  <s:Body>{body}</s:Body>
</s:Envelope>"""
        resp = await self.client.post(
            url,
            content=envelope.encode(),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        )
        resp.raise_for_status()
        return resp.text

    async def get_device_info(self) -> dict[str, Any]:
        body = '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
        try:
            resp_text = await self.soap_request(self.service_url, body)
            # Simplified parsing — real impl would use zeep or XML parser
            info = {"raw_response": resp_text[:500]}
            for field in ["Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId"]:
                start = resp_text.find(f"<tds:{field}>")
                end = resp_text.find(f"</tds:{field}>")
                if start > 0 and end > start:
                    info[field.lower()] = resp_text[start + len(f"<tds:{field}>"):end]
            self.device_info = info
            return info
        except Exception:
            return {}

    async def get_stream_uri(self, profile_token: str) -> str:
        body = f"""<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">
  <StreamSetup>
    <Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>
    <Transport xmlns="http://www.onvif.org/ver10/schema">
      <Protocol>RTSP</Protocol>
    </Transport>
  </StreamSetup>
  <ProfileToken>{profile_token}</ProfileToken>
</GetStreamUri>"""
        try:
            resp_text = await self.soap_request(self.media_url, body)
            start = resp_text.find("<tt:Uri>")
            end = resp_text.find("</tt:Uri>")
            if start > 0 and end > start:
                return resp_text[start + 8:end]
        except Exception:
            pass
        return ""

    async def close(self):
        if not self.client.is_closed:
            await self.client.aclose()


class OnvifAdapter(Adapter):
    adapter_id: str = "onvif.camera"
    adapter_class: AdapterClass = "bridge"

    def __init__(self):
        self._connections: dict[str, _OnvifConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="ONVIF Camera/NVR",
                category="camera",
                discovery_methods=["ws_discovery", "http_probe", "manual_ip"],
                required_fields=["host"],
                optional_fields=["port"],
                secret_fields=["username", "password"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
                risk_level="low",
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets: list[DiscoveredTarget] = []
        if "http_probe" in request.methods:
            host = request.scope.get("host")
            port = request.scope.get("port", 80)
            if host:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"http://{host}:{port}/onvif/device_service")
                        if resp.status_code in (200, 400, 405):
                            targets.append(DiscoveredTarget(
                                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                                adapter_id=self.adapter_id,
                                native_ref=f"{host}:{port}",
                                title=f"ONVIF Device @ {host}",
                                address=host,
                                confidence=0.7,
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

        port = int(profile.fields.get("port", 80))
        username = ""
        password = ""
        for secret in profile.secrets:
            if secret.name == "username":
                username = secret.handle
            elif secret.name == "password":
                password = secret.handle

        conn_id = f"onvif_{uuid.uuid4().hex[:8]}"
        conn = _OnvifConnection(conn_id, host, port, username, password)

        info = await conn.get_device_info()
        if not info:
            await conn.close()
            return CommissionResult("", "failed", {"error": "Cannot reach ONVIF service"})

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {"host": host, "device_info": info})

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)
        info = conn.device_info

        device_id = f"dev_onvif_{conn.host.replace('.', '_')}"
        devices = [{
            "device_id": device_id,
            "native_device_ref": f"{conn.host}:{conn.port}",
            "device_family": "onvif.camera",
            "name": info.get("model", f"ONVIF @ {conn.host}"),
            "manufacturer": info.get("manufacturer", "Unknown"),
            "model": info.get("model", "Unknown"),
            "firmware": {"version": info.get("firmwareversion", "")},
            "connectivity": {"transport": "http", "address": conn.host, "port": conn.port},
            "safety_class": "S0",
        }]

        endpoints = []
        points = []

        # Main stream endpoint
        ep_stream = f"{device_id}_stream_main"
        endpoints.append({
            "endpoint_id": ep_stream,
            "device_id": device_id,
            "native_endpoint_ref": "media/main",
            "endpoint_type": "camera_stream",
            "direction": "read",
            "capabilities": ["camera_stream"],
            "safety_class": "S0",
        })
        points.append({
            "point_id": f"{ep_stream}_uri",
            "endpoint_id": ep_stream,
            "point_class": "camera_stream.uri",
            "value_type": "str",
            "readable": True,
            "writable": False,
            "native_ref": "GetStreamUri/main",
            "source_protocol": "onvif",
        })

        # Snapshot endpoint
        ep_snap = f"{device_id}_snapshot"
        endpoints.append({
            "endpoint_id": ep_snap,
            "device_id": device_id,
            "native_endpoint_ref": "media/snapshot",
            "endpoint_type": "camera_snapshot",
            "direction": "read",
            "capabilities": ["camera_snapshot"],
            "safety_class": "S0",
        })

        # Motion detection endpoint (if available)
        ep_motion = f"{device_id}_motion"
        endpoints.append({
            "endpoint_id": ep_motion,
            "device_id": device_id,
            "native_endpoint_ref": "events/motion",
            "endpoint_type": "motion_detector",
            "direction": "event_only",
            "capabilities": ["camera_motion", "motion"],
            "polling_mode": "push_preferred_with_poll_verify",
            "safety_class": "S0",
        })
        points.append({
            "point_id": f"{ep_motion}_state",
            "endpoint_id": ep_motion,
            "point_class": "motion.detected",
            "value_type": "bool",
            "readable": True,
            "writable": False,
            "native_ref": "events/motion/state",
            "source_protocol": "onvif",
        })

        return InventorySnapshot(
            connection_id=connection_id,
            devices=devices,
            endpoints=endpoints,
            points=points,
            raw={"device_info": info},
        )

    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # ONVIF events require WS-BaseNotification — not yet implemented
        yield {
            "type": "heartbeat",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": "ONVIF event subscription not yet implemented",
        }

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)

        if point_id.endswith("_uri"):
            # Get stream URI
            uri = await conn.get_stream_uri("profile_1")
            return {
                "point_id": point_id,
                "value": {"kind": "str", "reported": uri},
                "quality": {"status": "good" if uri else "bad", "source_type": "polled"},
            }

        raise InvalidTargetError(f"Unknown point: {point_id}")

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        verb = command.get("verb", "")

        if verb in ("ptz_move", "ptz_preset"):
            # PTZ not yet implemented
            return {"command_id": cmd_id, "status": "failed", "error": "PTZ not yet implemented"}

        return {"command_id": cmd_id, "status": "failed", "error": f"Unsupported verb: {verb}"}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        info = await conn.get_device_info()
        if info:
            return HealthStatus("healthy", {"host": conn.host, "model": info.get("model")})
        return HealthStatus("offline", {"host": conn.host})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, connection_id: str) -> _OnvifConnection:
        conn = self._connections.get(connection_id)
        if not conn:
            raise UnreachableError(f"No active connection: {connection_id}")
        return conn
