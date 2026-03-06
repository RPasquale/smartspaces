"""Polling scheduler for adapters that don't support push events.

Runs periodic read cycles on poll-only points, respects per-point
polling intervals, and publishes results through the event bus.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core.event_bus import EventBus
from core.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass
class PollTarget:
    """A point that needs periodic polling."""
    connection_id: str
    point_id: str
    interval_sec: float = 30.0
    last_polled: float = 0.0
    consecutive_errors: int = 0
    max_errors: int = 5


class Scheduler:
    """Periodic polling scheduler for poll-only adapter points."""

    def __init__(
        self,
        event_bus: EventBus,
        state_store: StateStore,
        default_interval: float = 30.0,
        tick_interval: float = 1.0,
    ):
        self.event_bus = event_bus
        self.state_store = state_store
        self.default_interval = default_interval
        self.tick_interval = tick_interval
        self._targets: dict[str, PollTarget] = {}  # point_id -> PollTarget
        self._read_fn: Any = None  # Set by engine: async fn(connection_id, point_id) -> dict
        self._running = False
        self._task: asyncio.Task | None = None
        self._stats = {"polls": 0, "successes": 0, "errors": 0}

    def set_read_fn(self, fn: Any) -> None:
        """Set the function used to read points. Typically registry.read_point."""
        self._read_fn = fn

    def add_target(
        self,
        connection_id: str,
        point_id: str,
        interval_sec: float | None = None,
    ) -> None:
        """Add a point to the polling schedule."""
        self._targets[point_id] = PollTarget(
            connection_id=connection_id,
            point_id=point_id,
            interval_sec=interval_sec or self.default_interval,
        )

    def add_targets_from_inventory(
        self,
        connection_id: str,
        points: list[dict[str, Any]],
        interval_sec: float | None = None,
    ) -> None:
        """Add all readable points from an inventory snapshot to the schedule."""
        for pt in points:
            if pt.get("readable", True) and not pt.get("event_driven", False):
                self.add_target(connection_id, pt["point_id"], interval_sec)

    def remove_target(self, point_id: str) -> None:
        """Remove a point from the polling schedule."""
        self._targets.pop(point_id, None)

    def remove_connection(self, connection_id: str) -> None:
        """Remove all polling targets for a connection."""
        to_remove = [pid for pid, t in self._targets.items() if t.connection_id == connection_id]
        for pid in to_remove:
            del self._targets[pid]

    async def start(self) -> None:
        """Start the polling loop."""
        if self._running:
            return
        if not self._read_fn:
            raise RuntimeError("Scheduler requires a read function. Call set_read_fn() first.")
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Scheduler started with %d targets", len(self._targets))

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scheduler stopped (polls=%d ok=%d err=%d)",
                     self._stats["polls"], self._stats["successes"], self._stats["errors"])

    async def _poll_loop(self) -> None:
        """Main polling loop. Checks which targets are due and reads them."""
        while self._running:
            try:
                now = time.monotonic()
                due = [
                    t for t in self._targets.values()
                    if (now - t.last_polled) >= t.interval_sec
                    and t.consecutive_errors < t.max_errors
                ]

                if due:
                    # Poll up to 10 targets concurrently
                    batch = due[:10]
                    tasks = [self._poll_one(t) for t in batch]
                    await asyncio.gather(*tasks, return_exceptions=True)

                await asyncio.sleep(self.tick_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler loop error")
                await asyncio.sleep(self.tick_interval)

    async def _poll_one(self, target: PollTarget) -> None:
        """Poll a single point."""
        self._stats["polls"] += 1
        target.last_polled = time.monotonic()

        try:
            result = await self._read_fn(target.connection_id, target.point_id)
            target.consecutive_errors = 0
            self._stats["successes"] += 1
        except Exception as e:
            target.consecutive_errors += 1
            self._stats["errors"] += 1
            if target.consecutive_errors >= target.max_errors:
                logger.warning(
                    "Point %s reached max poll errors (%d), suspending",
                    target.point_id, target.max_errors,
                )
                self.event_bus.publish_nowait({
                    "type": "point.quality_changed",
                    "point_id": target.point_id,
                    "connection_id": target.connection_id,
                    "quality": {"status": "bad", "source_type": "polled", "comm_lost": True},
                })

    def reset_errors(self, point_id: str) -> None:
        """Reset error count for a point (e.g. after reconnection)."""
        target = self._targets.get(point_id)
        if target:
            target.consecutive_errors = 0

    def reset_all_errors(self, connection_id: str) -> None:
        """Reset all error counts for a connection."""
        for t in self._targets.values():
            if t.connection_id == connection_id:
                t.consecutive_errors = 0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            **self._stats,
            "active_targets": sum(1 for t in self._targets.values() if t.consecutive_errors < t.max_errors),
            "suspended_targets": sum(1 for t in self._targets.values() if t.consecutive_errors >= t.max_errors),
            "total_targets": len(self._targets),
        }

    @property
    def targets(self) -> list[dict[str, Any]]:
        return [
            {
                "point_id": t.point_id,
                "connection_id": t.connection_id,
                "interval_sec": t.interval_sec,
                "consecutive_errors": t.consecutive_errors,
                "suspended": t.consecutive_errors >= t.max_errors,
            }
            for t in self._targets.values()
        ]
