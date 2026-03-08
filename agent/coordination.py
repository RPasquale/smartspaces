"""Multi-agent Coordination — lease-based device locking.

Prevents concurrent agents from fighting over the same device.
Agent A acquires a lease on a device or group, gets exclusive write
access for the lease duration. Other agents are blocked with a clear
error explaining who holds the lease.

Leases expire automatically. Higher-priority agents can preempt
lower-priority leases.

Thread-safe via asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Lease constraints
MIN_LEASE_SECONDS = 5.0
MAX_LEASE_SECONDS = 300.0
DEFAULT_LEASE_SECONDS = 30.0


@dataclass(slots=True)
class DeviceLease:
    """An active lease granting exclusive write access to a device."""
    lease_id: str
    device_name: str
    agent_id: str
    priority: int
    acquired_at: float
    expires_at: float
    reason: str = ""

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "device_name": self.device_name,
            "agent_id": self.agent_id,
            "priority": self.priority,
            "acquired_at": self.acquired_at,
            "remaining_seconds": round(self.remaining_seconds, 1),
            "is_expired": self.is_expired,
            "reason": self.reason,
        }


class DeviceCoordinator:
    """Lease-based coordination for multi-agent device access.

    Usage:
        coord = DeviceCoordinator()

        # Agent acquires exclusive access
        lease = await coord.acquire("living_room.light", "agent_1", duration=30)

        # Check before writing
        ok, reason = coord.check_write("living_room.light", "agent_1")
        if not ok:
            print(reason)  # "Device leased by agent_2 (15.3s remaining)"

        # Release when done
        await coord.release(lease.lease_id, "agent_1")
    """

    def __init__(self):
        self._leases: dict[str, DeviceLease] = {}  # device_name -> lease
        self._lock = asyncio.Lock()
        self._stats = {
            "acquired": 0,
            "released": 0,
            "expired": 0,
            "preempted": 0,
            "denied": 0,
        }

    async def acquire(
        self,
        device_name: str,
        agent_id: str,
        duration: float = DEFAULT_LEASE_SECONDS,
        priority: int = 0,
        reason: str = "",
    ) -> DeviceLease | None:
        """Acquire exclusive write access to a device.

        Returns the lease on success, None if blocked by a higher-priority agent.
        """
        duration = max(MIN_LEASE_SECONDS, min(duration, MAX_LEASE_SECONDS))

        async with self._lock:
            self._cleanup_expired()

            existing = self._leases.get(device_name)
            if existing and not existing.is_expired:
                if existing.agent_id == agent_id:
                    # Same agent — extend the lease
                    existing.expires_at = time.monotonic() + duration
                    return existing

                if priority <= existing.priority:
                    # Lower or equal priority — denied
                    self._stats["denied"] += 1
                    return None

                # Higher priority — preempt
                logger.info(
                    "Lease preempted: %s took %s from %s (priority %d > %d)",
                    agent_id, device_name, existing.agent_id,
                    priority, existing.priority,
                )
                self._stats["preempted"] += 1

            now = time.monotonic()
            lease = DeviceLease(
                lease_id=uuid.uuid4().hex[:12],
                device_name=device_name,
                agent_id=agent_id,
                priority=priority,
                acquired_at=now,
                expires_at=now + duration,
                reason=reason,
            )
            self._leases[device_name] = lease
            self._stats["acquired"] += 1

            logger.info(
                "Lease acquired: %s → %s (%.0fs, priority=%d)",
                agent_id, device_name, duration, priority,
            )
            return lease

    async def release(self, lease_id: str, agent_id: str) -> bool:
        """Release a lease. Returns True if released."""
        async with self._lock:
            for device_name, lease in list(self._leases.items()):
                if lease.lease_id == lease_id:
                    if lease.agent_id != agent_id:
                        return False  # Can't release another agent's lease
                    del self._leases[device_name]
                    self._stats["released"] += 1
                    return True
        return False

    async def release_device(self, device_name: str, agent_id: str) -> bool:
        """Release a lease by device name."""
        async with self._lock:
            lease = self._leases.get(device_name)
            if lease and lease.agent_id == agent_id:
                del self._leases[device_name]
                self._stats["released"] += 1
                return True
        return False

    async def release_all(self, agent_id: str) -> int:
        """Release all leases held by an agent. Returns count released."""
        count = 0
        async with self._lock:
            for device_name in list(self._leases.keys()):
                if self._leases[device_name].agent_id == agent_id:
                    del self._leases[device_name]
                    count += 1
            self._stats["released"] += count
        return count

    def check_write(self, device_name: str, agent_id: str) -> tuple[bool, str]:
        """Check if an agent can write to a device.

        Returns (allowed, reason).
        Does NOT acquire a lease — just checks.
        """
        self._cleanup_expired()

        lease = self._leases.get(device_name)
        if not lease or lease.is_expired:
            return True, "ok"

        if lease.agent_id == agent_id:
            return True, "ok"

        return False, (
            f"Device '{device_name}' is leased by agent '{lease.agent_id}' "
            f"({lease.remaining_seconds:.1f}s remaining)"
        )

    def get_lease(self, device_name: str) -> DeviceLease | None:
        """Get the active lease for a device, if any."""
        self._cleanup_expired()
        lease = self._leases.get(device_name)
        if lease and not lease.is_expired:
            return lease
        return None

    def list_leases(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """List all active leases, optionally filtered by agent."""
        self._cleanup_expired()
        results = []
        for lease in self._leases.values():
            if lease.is_expired:
                continue
            if agent_id and lease.agent_id != agent_id:
                continue
            results.append(lease.to_dict())
        return results

    def _cleanup_expired(self) -> None:
        """Remove expired leases."""
        expired = [
            name for name, lease in self._leases.items()
            if lease.is_expired
        ]
        for name in expired:
            del self._leases[name]
            self._stats["expired"] += 1

    @property
    def stats(self) -> dict[str, Any]:
        self._cleanup_expired()
        return {
            **self._stats,
            "active_leases": len(self._leases),
        }
