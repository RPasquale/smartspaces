"""Action History — audit trail for all agent-initiated operations.

Records every read, write, scene activation, and safety decision
with enough context for agents to reason about recent changes,
avoid redundant actions, and answer "what happened?"

Thread-safe ring buffer with configurable max size.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
    READ = "read"
    WRITE = "write"
    SCENE = "scene"
    RULE = "rule"
    SCHEDULE = "schedule"
    CONFIRMATION = "confirmation"
    SAFETY_BLOCK = "safety_block"
    GROUP = "group"
    INTENT = "intent"


class ActionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"


@dataclass(slots=True)
class ActionRecord:
    """A single recorded action."""
    action_id: str
    timestamp: float
    action_type: ActionType
    status: ActionStatus
    device: str | None = None
    space: str | None = None
    action: str | None = None
    value: Any = None
    result: Any = None
    initiator: str = "ai_agent"
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action_id": self.action_id,
            "timestamp": self.timestamp,
            "action_type": self.action_type.value,
            "status": self.status.value,
            "initiator": self.initiator,
        }
        if self.device:
            d["device"] = self.device
        if self.space:
            d["space"] = self.space
        if self.action:
            d["action"] = self.action
        if self.value is not None:
            d["value"] = self.value
        if self.result is not None:
            d["result"] = self.result
        if self.duration_ms is not None:
            d["duration_ms"] = round(self.duration_ms, 2)
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def age_display(self) -> str:
        """Human-readable age string."""
        age = self.age_seconds
        if age < 60:
            return f"{age:.0f}s ago"
        elif age < 3600:
            return f"{age / 60:.0f}m ago"
        elif age < 86400:
            return f"{age / 3600:.1f}h ago"
        return f"{age / 86400:.1f}d ago"


# Default max history size
_DEFAULT_MAX_SIZE = 10_000


class ActionHistory:
    """Thread-safe action history with ring buffer storage.

    Provides recording and querying of all agent-initiated operations.
    """

    def __init__(self, max_size: int = _DEFAULT_MAX_SIZE):
        self._records: deque[ActionRecord] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()
        self._by_device: dict[str, deque[ActionRecord]] = {}
        self._device_max = 500  # max records per device index
        self._counters: dict[str, int] = {
            "total": 0,
            "reads": 0,
            "writes": 0,
            "scenes": 0,
            "blocked": 0,
        }

    def record(
        self,
        action_type: ActionType,
        status: ActionStatus,
        device: str | None = None,
        action: str | None = None,
        value: Any = None,
        result: Any = None,
        initiator: str = "ai_agent",
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActionRecord:
        """Record an action. Thread-safe via GIL for deque append."""
        space = device.split(".")[0] if device and "." in device else None
        rec = ActionRecord(
            action_id=uuid.uuid4().hex[:12],
            timestamp=time.time(),
            action_type=action_type,
            status=status,
            device=device,
            space=space,
            action=action,
            value=value,
            result=result,
            initiator=initiator,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        self._records.append(rec)

        # Update device index
        if device:
            if device not in self._by_device:
                self._by_device[device] = deque(maxlen=self._device_max)
            self._by_device[device].append(rec)

        # Update counters
        self._counters["total"] += 1
        if action_type == ActionType.READ:
            self._counters["reads"] += 1
        elif action_type == ActionType.WRITE:
            self._counters["writes"] += 1
        elif action_type == ActionType.SCENE:
            self._counters["scenes"] += 1
        if status == ActionStatus.BLOCKED:
            self._counters["blocked"] += 1

        return rec

    def query(
        self,
        device: str | None = None,
        space: str | None = None,
        action_type: ActionType | None = None,
        status: ActionStatus | None = None,
        initiator: str | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query action history with filters.

        Args:
            device: Filter by device semantic name
            space: Filter by space name
            action_type: Filter by action type
            status: Filter by action status
            initiator: Filter by who initiated the action
            since: Only return actions after this timestamp
            limit: Maximum number of results (most recent first)
        """
        # Use device index if filtering by device
        if device and device in self._by_device:
            source = self._by_device[device]
        else:
            source = self._records

        results: list[dict[str, Any]] = []
        # Iterate in reverse (most recent first)
        for rec in reversed(source):
            if len(results) >= limit:
                break
            if device and rec.device != device:
                continue
            if space and rec.space != space:
                continue
            if action_type and rec.action_type != action_type:
                continue
            if status and rec.status != status:
                continue
            if initiator and rec.initiator != initiator:
                continue
            if since and rec.timestamp < since:
                continue
            results.append(rec.to_dict())

        return results

    def last_action_for_device(self, device: str) -> ActionRecord | None:
        """Get the most recent action for a specific device."""
        device_records = self._by_device.get(device)
        if device_records:
            return device_records[-1]
        return None

    def recent_summary(self, minutes: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        """Get a human-readable summary of recent actions."""
        since = time.time() - (minutes * 60)
        records = self.query(since=since, limit=limit)
        summary = []
        for rec in records:
            parts = []
            if rec.get("device"):
                parts.append(rec["device"])
            if rec.get("action"):
                parts.append(f"→ {rec['action']}")
            if rec.get("value") is not None:
                parts.append(f"({rec['value']})")
            parts.append(f"[{rec['status']}]")

            # Calculate age
            age = time.time() - rec["timestamp"]
            if age < 60:
                age_str = f"{age:.0f}s ago"
            elif age < 3600:
                age_str = f"{age / 60:.0f}m ago"
            else:
                age_str = f"{age / 3600:.1f}h ago"
            parts.append(age_str)

            summary.append({
                "description": " ".join(parts),
                **rec,
            })
        return summary

    def to_context_prompt(self, minutes: int = 30) -> str:
        """Generate action history context for LLM system prompt injection."""
        records = self.query(since=time.time() - (minutes * 60), limit=15)
        if not records:
            return "No recent actions."

        lines = ["# Recent Actions"]
        for rec in records:
            age = time.time() - rec["timestamp"]
            if age < 60:
                age_str = f"{age:.0f}s ago"
            elif age < 3600:
                age_str = f"{age / 60:.0f}m ago"
            else:
                age_str = f"{age / 3600:.1f}h ago"

            device = rec.get("device", "system")
            action = rec.get("action", rec["action_type"])
            status = rec["status"]
            value = rec.get("value", "")
            value_str = f" = {value}" if value is not None and value != "" else ""

            lines.append(f"- {age_str}: {device} {action}{value_str} [{status}]")
        return "\n".join(lines)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            **self._counters,
            "devices_tracked": len(self._by_device),
            "buffer_size": len(self._records),
            "buffer_capacity": self._records.maxlen,
        }
