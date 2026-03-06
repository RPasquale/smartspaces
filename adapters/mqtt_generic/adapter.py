"""Generic MQTT adapter.

Bridges any MQTT-connected device into the canonical model.
Supports auto-discovery via retained discovery payloads (HA-style)
and manual topic mapping.
"""

from __future__ import annotations

import asyncio
import json
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
from sdk.adapter_api.errors import InvalidTargetError, UnreachableError

try:
    import paho.mqtt.client as mqtt_client
    HAS_PAHO = True
except ImportError:
    HAS_PAHO = False


class _MqttConnection:
    def __init__(
        self,
        connection_id: str,
        broker_host: str,
        broker_port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        discovery_prefix: str = "homeassistant",
    ):
        self.connection_id = connection_id
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.discovery_prefix = discovery_prefix
        self.commissioned_at = datetime.now(timezone.utc)

        self._client: mqtt_client.Client | None = None
        self._discovered: dict[str, dict[str, Any]] = {}
        self._state_cache: dict[str, Any] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._connected = False

        if HAS_PAHO:
            self._client = mqtt_client.Client(
                callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
                client_id=f"adapter_{connection_id}",
            )
            if username:
                self._client.username_pw_set(username, password)
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self._connected = True
        client.subscribe(f"{self.discovery_prefix}/#")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = msg.payload.decode(errors="replace")

        # Discovery messages
        if topic.startswith(self.discovery_prefix + "/"):
            parts = topic.split("/")
            if len(parts) >= 4 and parts[-1] == "config":
                component = parts[1]
                node_id = parts[2] if len(parts) == 5 else ""
                object_id = parts[-2]
                unique_key = f"{component}/{node_id}/{object_id}"
                if isinstance(payload, dict):
                    self._discovered[unique_key] = {
                        "component": component,
                        "node_id": node_id,
                        "object_id": object_id,
                        "config": payload,
                    }
            return

        # State messages
        self._state_cache[topic] = payload
        try:
            self._event_queue.put_nowait({
                "type": "point.reported",
                "topic": topic,
                "value": payload,
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
        except asyncio.QueueFull:
            pass

    async def connect(self) -> bool:
        if not self._client:
            return False
        try:
            self._client.connect(self.broker_host, self.broker_port, keepalive=60)
            self._client.loop_start()
            # Wait for connection
            for _ in range(50):
                if self._connected:
                    return True
                await asyncio.sleep(0.1)
            return self._connected
        except Exception:
            return False

    async def disconnect(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    async def publish(self, topic: str, payload: Any, retain: bool = False) -> bool:
        if not self._client or not self._connected:
            return False
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        result = self._client.publish(topic, payload, retain=retain)
        return result.rc == 0

    async def subscribe_topic(self, topic: str):
        if self._client and self._connected:
            self._client.subscribe(topic)


class MqttGenericAdapter(Adapter):
    adapter_id: str = "mqtt.generic"
    adapter_class: AdapterClass = "bus"

    def __init__(self):
        self._connections: dict[str, _MqttConnection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="MQTT Broker (auto-discovery)",
                category="message_bus",
                discovery_methods=["mqtt_discovery"],
                required_fields=["broker_host"],
                optional_fields=["broker_port", "discovery_prefix"],
                secret_fields=["username", "password"],
                supports_auto_inventory=True,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        # MQTT discovery happens post-commission by subscribing to discovery topics
        return []

    async def commission(
        self,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        if not HAS_PAHO:
            return CommissionResult("", "failed", {"error": "paho-mqtt not installed"})

        broker = profile.fields.get("broker_host", "")
        port = int(profile.fields.get("broker_port", 1883))
        prefix = profile.fields.get("discovery_prefix", "homeassistant")

        username = None
        password = None
        for secret in profile.secrets:
            if secret.name == "username":
                username = secret.handle
            elif secret.name == "password":
                password = secret.handle

        conn_id = f"mqtt_{uuid.uuid4().hex[:8]}"
        conn = _MqttConnection(conn_id, broker, port, username, password, prefix)

        connected = await conn.connect()
        if not connected:
            await conn.disconnect()
            return CommissionResult("", "failed", {"error": f"Cannot connect to {broker}:{port}"})

        # Allow time for discovery messages
        await asyncio.sleep(2)

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {
            "broker": broker,
            "port": port,
            "discovered_count": len(conn._discovered),
        })

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        devices = []
        endpoints = []
        points = []
        seen_devices: set[str] = set()

        for key, disc in conn._discovered.items():
            config = disc["config"]
            node_id = disc.get("node_id", "")
            component = disc["component"]
            object_id = disc["object_id"]

            # Build device from discovery config
            dev_info = config.get("device", {}) if isinstance(config, dict) else {}
            dev_id = dev_info.get("identifiers", [node_id])[0] if isinstance(dev_info.get("identifiers"), list) else node_id or object_id
            dev_id_canon = f"dev_mqtt_{dev_id}"

            if dev_id_canon not in seen_devices:
                seen_devices.add(dev_id_canon)
                devices.append({
                    "device_id": dev_id_canon,
                    "native_device_ref": dev_id,
                    "device_family": "mqtt.generic",
                    "name": dev_info.get("name", dev_id),
                    "manufacturer": dev_info.get("manufacturer", "Unknown"),
                    "model": dev_info.get("model", "Unknown"),
                    "firmware": {"version": dev_info.get("sw_version", "")},
                    "connectivity": {"transport": "mqtt", "address": conn.broker_host},
                    "safety_class": "S1",
                })

            # Endpoint
            ep_id = f"{dev_id_canon}_{component}_{object_id}"
            cap_map = {
                "switch": ["binary_switch"],
                "light": ["dimmer", "light_color"],
                "binary_sensor": ["binary_sensor"],
                "sensor": ["analog_input"],
                "cover": ["cover"],
                "fan": ["fan"],
                "climate": ["thermostat"],
                "lock": ["lock"],
            }
            caps = cap_map.get(component, [component])

            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": dev_id_canon,
                "native_endpoint_ref": key,
                "endpoint_type": component,
                "direction": "read_write" if component in ("switch", "light", "cover", "fan", "climate", "lock") else "read",
                "capabilities": caps,
                "polling_mode": "push_only",
                "safety_class": "S1",
            })

            # Points from discovery config
            state_topic = config.get("state_topic", config.get("stat_t", ""))
            command_topic = config.get("command_topic", config.get("cmd_t", ""))

            if state_topic:
                points.append({
                    "point_id": f"{ep_id}_state",
                    "endpoint_id": ep_id,
                    "point_class": f"{component}.state",
                    "value_type": "str",
                    "unit": config.get("unit_of_measurement"),
                    "readable": True,
                    "writable": bool(command_topic),
                    "event_driven": True,
                    "native_ref": state_topic,
                    "source_protocol": "mqtt",
                })
                # Subscribe to state topic
                await conn.subscribe_topic(state_topic)

        return InventorySnapshot(
            connection_id=connection_id,
            devices=devices,
            endpoints=endpoints,
            points=points,
            raw={"discovered": {k: v["config"] for k, v in conn._discovered.items()}},
        )

    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        conn = self._get_conn(connection_id)
        while True:
            try:
                event = await asyncio.wait_for(conn._event_queue.get(), timeout=30)
                yield event
            except asyncio.TimeoutError:
                yield {"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)

        # Find the point's state topic from inventory
        for key, disc in conn._discovered.items():
            config = disc["config"]
            state_topic = config.get("state_topic", config.get("stat_t", ""))
            if state_topic and state_topic in conn._state_cache:
                if point_id.endswith("_state"):
                    return {
                        "point_id": point_id,
                        "value": {"kind": "str", "reported": conn._state_cache[state_topic]},
                        "quality": {"status": "good", "source_type": "device_push"},
                    }

        return {
            "point_id": point_id,
            "value": None,
            "quality": {"status": "stale", "source_type": "polled"},
            "error": "No cached state available",
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        endpoint_id = command.get("target", {}).get("endpoint_id", "")
        params = command.get("params", {})

        # Find command topic for this endpoint
        for key, disc in conn._discovered.items():
            ep_match = f"_{disc['component']}_{disc['object_id']}"
            if endpoint_id.endswith(ep_match):
                config = disc["config"]
                cmd_topic = config.get("command_topic", config.get("cmd_t", ""))
                if cmd_topic:
                    payload = params.get("payload", params.get("value", ""))
                    if isinstance(payload, bool):
                        payload = config.get("payload_on", "ON") if payload else config.get("payload_off", "OFF")
                    success = await conn.publish(cmd_topic, str(payload))
                    return {
                        "command_id": cmd_id,
                        "status": "succeeded" if success else "failed",
                        "topic": cmd_topic,
                        "payload": payload,
                    }

        return {"command_id": cmd_id, "status": "failed", "error": f"No command topic for {endpoint_id}"}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        if conn._connected:
            return HealthStatus("healthy", {
                "broker": conn.broker_host,
                "discovered_devices": len(conn._discovered),
                "cached_states": len(conn._state_cache),
            })
        return HealthStatus("offline", {"broker": conn.broker_host})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.disconnect()

    def _get_conn(self, connection_id: str) -> _MqttConnection:
        conn = self._connections.get(connection_id)
        if not conn:
            raise UnreachableError(f"No active connection: {connection_id}")
        return conn
