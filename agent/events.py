"""Real-time Event Streaming — SSE bridge between EventBus and AI agents.

Manages Server-Sent Event connections, dispatching device state changes,
action results, rule triggers, and safety events to connected clients
with per-client filtering.

Usage (FastAPI):
    manager = EventStreamManager()
    manager.bind_event_bus(event_bus)

    @app.get("/api/agent/events")
    async def sse(request: Request):
        return manager.sse_response(filters={"spaces": ["living_room"]})
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """Types of events dispatched to SSE clients."""
    DEVICE_STATE_CHANGE = "device_state_change"
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    RULE_TRIGGERED = "rule_triggered"
    SCENE_ACTIVATED = "scene_activated"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_RESOLVED = "confirmation_resolved"
    SAFETY_BLOCKED = "safety_blocked"
    DEVICE_ONLINE = "device_online"
    DEVICE_OFFLINE = "device_offline"
    SCHEDULE_FIRED = "schedule_fired"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A single event dispatched to SSE clients."""
    type: EventType
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    device: str | None = None
    space: str | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def sse_format(self) -> str:
        """Format as an SSE message."""
        payload = {
            "type": self.type.value,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            **self.data,
        }
        if self.device:
            payload["device"] = self.device
        if self.space:
            payload["space"] = self.space
        data_str = json.dumps(payload, default=str)
        return f"id: {self.event_id}\nevent: {self.type.value}\ndata: {data_str}\n\n"


@dataclass
class ClientFilter:
    """Filters applied to a single SSE client connection."""
    spaces: set[str] | None = None
    devices: set[str] | None = None
    event_types: set[EventType] | None = None

    def matches(self, event: AgentEvent) -> bool:
        """Check if an event passes this client's filters."""
        if self.event_types and event.type not in self.event_types:
            if event.type != EventType.HEARTBEAT:
                return False

        if self.spaces and event.space and event.space not in self.spaces:
            return False

        if self.devices and event.device and event.device not in self.devices:
            return False

        return True


@dataclass
class _ClientConnection:
    """Internal state for a connected SSE client."""
    client_id: str
    queue: asyncio.Queue[AgentEvent]
    filters: ClientFilter
    connected_at: float = field(default_factory=time.time)
    events_sent: int = 0
    events_dropped: int = 0


# Maximum events buffered per client before dropping oldest
_MAX_CLIENT_QUEUE = 256

# Heartbeat interval in seconds
_HEARTBEAT_INTERVAL = 15.0


class EventStreamManager:
    """Manages SSE connections and dispatches events to connected clients.

    Thread-safe: all mutations go through asyncio primitives.
    """

    def __init__(self, max_queue_per_client: int = _MAX_CLIENT_QUEUE):
        self._clients: dict[str, _ClientConnection] = {}
        self._max_queue = max_queue_per_client
        self._lock = asyncio.Lock()
        self._total_events_dispatched: int = 0
        self._total_events_dropped: int = 0

    async def connect(
        self,
        client_id: str | None = None,
        filters: ClientFilter | None = None,
    ) -> tuple[str, asyncio.Queue[AgentEvent]]:
        """Register a new SSE client. Returns (client_id, queue)."""
        cid = client_id or uuid.uuid4().hex[:16]
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self._max_queue)
        conn = _ClientConnection(
            client_id=cid,
            queue=queue,
            filters=filters or ClientFilter(),
        )
        async with self._lock:
            self._clients[cid] = conn
        logger.info("SSE client connected: %s (total: %d)", cid, len(self._clients))
        return cid, queue

    async def disconnect(self, client_id: str) -> None:
        """Remove a client connection."""
        async with self._lock:
            conn = self._clients.pop(client_id, None)
        if conn:
            logger.info(
                "SSE client disconnected: %s (sent=%d, dropped=%d)",
                client_id, conn.events_sent, conn.events_dropped,
            )

    async def dispatch(self, event: AgentEvent) -> int:
        """Dispatch an event to all matching clients. Returns number of clients reached."""
        dispatched = 0
        async with self._lock:
            clients = list(self._clients.values())

        for conn in clients:
            if not conn.filters.matches(event):
                continue
            try:
                conn.queue.put_nowait(event)
                conn.events_sent += 1
                dispatched += 1
            except asyncio.QueueFull:
                # Drop oldest event to make room
                try:
                    conn.queue.get_nowait()
                    conn.events_dropped += 1
                    self._total_events_dropped += 1
                except asyncio.QueueEmpty:
                    pass
                try:
                    conn.queue.put_nowait(event)
                    conn.events_sent += 1
                    dispatched += 1
                except asyncio.QueueFull:
                    pass

        self._total_events_dispatched += 1
        return dispatched

    async def event_generator(self, client_id: str) -> AsyncGenerator[str, None]:
        """Async generator yielding SSE-formatted strings for StreamingResponse."""
        async with self._lock:
            conn = self._clients.get(client_id)
        if not conn:
            return

        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        conn.queue.get(), timeout=_HEARTBEAT_INTERVAL
                    )
                    yield event.sse_format()
                except asyncio.TimeoutError:
                    heartbeat = AgentEvent(
                        type=EventType.HEARTBEAT,
                        data={"connected_clients": len(self._clients)},
                    )
                    yield heartbeat.sse_format()
        except asyncio.CancelledError:
            pass
        finally:
            await self.disconnect(client_id)

    def emit(self, event_type: EventType, data: dict[str, Any],
             device: str | None = None, space: str | None = None) -> None:
        """Fire-and-forget event dispatch (schedules on the running loop)."""
        event = AgentEvent(type=event_type, data=data, device=device, space=space)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.dispatch(event))
        except RuntimeError:
            pass  # No running loop — skip (e.g. during testing)

    def bind_event_bus(self, event_bus: Any) -> None:
        """Subscribe to core EventBus topics and forward as SSE events."""

        async def _on_point_reported(event: dict[str, Any]) -> None:
            device = event.get("semantic_name", event.get("point_id", ""))
            space = device.split(".")[0] if "." in device else None
            await self.dispatch(AgentEvent(
                type=EventType.DEVICE_STATE_CHANGE,
                data={
                    "point_id": event.get("point_id"),
                    "value": event.get("value"),
                    "previous": event.get("previous"),
                    "quality": event.get("quality"),
                },
                device=device,
                space=space,
            ))

        async def _on_command_result(event: dict[str, Any]) -> None:
            status = event.get("status", "")
            etype = EventType.ACTION_EXECUTED if status == "succeeded" else EventType.ACTION_FAILED
            device = event.get("semantic_name", event.get("device", ""))
            space = device.split(".")[0] if "." in device else None
            await self.dispatch(AgentEvent(
                type=etype,
                data={
                    "command_id": event.get("command_id"),
                    "action": event.get("action"),
                    "status": status,
                    "value": event.get("value"),
                },
                device=device,
                space=space,
            ))

        event_bus.subscribe("point.reported", _on_point_reported)
        event_bus.subscribe("command.result", _on_command_result)

    @property
    def connected_count(self) -> int:
        return len(self._clients)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "connected_clients": len(self._clients),
            "total_events_dispatched": self._total_events_dispatched,
            "total_events_dropped": self._total_events_dropped,
            "clients": [
                {
                    "client_id": c.client_id,
                    "connected_at": c.connected_at,
                    "events_sent": c.events_sent,
                    "events_dropped": c.events_dropped,
                }
                for c in self._clients.values()
            ],
        }


def parse_sse_filters(
    spaces: str | None = None,
    devices: str | None = None,
    types: str | None = None,
) -> ClientFilter:
    """Parse query string parameters into a ClientFilter."""
    return ClientFilter(
        spaces=set(spaces.split(",")) if spaces else None,
        devices=set(devices.split(",")) if devices else None,
        event_types={EventType(t) for t in types.split(",")} if types else None,
    )
