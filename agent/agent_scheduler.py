"""Agent Action Scheduler — time-based automation for AI agents.

Bridges the gap between scenes (instant) and rules (condition-based)
by letting agents schedule actions for specific times:
  "turn off lights at 11pm"
  "dim bedroom in 30 minutes"
  "every day at 7am turn on kitchen light"

Schedules are managed as asyncio tasks with automatic cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class ScheduleType(str, Enum):
    ONE_SHOT = "one_shot"        # Run once at a specific time
    DELAY = "delay"              # Run once after N seconds
    RECURRING = "recurring"      # Run on a cron-like schedule


class ScheduleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class ScheduledAction:
    """A scheduled action to be executed at a specific time."""
    schedule_id: str
    schedule_type: ScheduleType
    status: ScheduleStatus
    device: str | None = None
    action: str | None = None
    value: Any = None
    scene: str | None = None        # Alternative: activate a scene
    created_at: float = field(default_factory=time.time)
    execute_at: float | None = None  # For one_shot and delay
    interval_seconds: float | None = None  # For recurring
    recur_times: list[str] | None = None   # For recurring: ["07:00", "22:00"]
    recur_days: list[int] | None = None    # 0=Mon .. 6=Sun, None=every day
    last_run: float | None = None
    run_count: int = 0
    max_runs: int | None = None      # None = unlimited for recurring
    initiator: str = "ai_agent"
    description: str = ""
    _task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schedule_id": self.schedule_id,
            "schedule_type": self.schedule_type.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "run_count": self.run_count,
            "initiator": self.initiator,
            "description": self.description,
        }
        if self.device:
            d["device"] = self.device
        if self.action:
            d["action"] = self.action
        if self.value is not None:
            d["value"] = self.value
        if self.scene:
            d["scene"] = self.scene
        if self.execute_at:
            d["execute_at"] = self.execute_at
            d["seconds_until"] = max(0, self.execute_at - time.time())
        if self.interval_seconds:
            d["interval_seconds"] = self.interval_seconds
        if self.recur_times:
            d["recur_times"] = self.recur_times
        if self.recur_days is not None:
            d["recur_days"] = self.recur_days
        if self.last_run:
            d["last_run"] = self.last_run
        if self.max_runs is not None:
            d["max_runs"] = self.max_runs
        return d


# Execution callback type: async (device, action, value, scene) -> dict
ExecuteCallback = Callable[..., Awaitable[dict[str, Any]]]


class ActionScheduler:
    """Manages scheduled actions for AI agents.

    Actions are executed via a callback that routes through the
    ToolExecutor, ensuring safety guards are respected.
    """

    def __init__(self, execute_callback: ExecuteCallback | None = None):
        self._schedules: dict[str, ScheduledAction] = {}
        self._execute_fn = execute_callback
        self._lock = asyncio.Lock()

    def set_execute_fn(self, fn: ExecuteCallback) -> None:
        self._execute_fn = fn

    async def schedule_delay(
        self,
        delay_seconds: float,
        device: str | None = None,
        action: str | None = None,
        value: Any = None,
        scene: str | None = None,
        initiator: str = "ai_agent",
        description: str = "",
    ) -> ScheduledAction:
        """Schedule an action to run after a delay."""
        schedule_id = uuid.uuid4().hex[:12]
        execute_at = time.time() + delay_seconds

        sched = ScheduledAction(
            schedule_id=schedule_id,
            schedule_type=ScheduleType.DELAY,
            status=ScheduleStatus.PENDING,
            device=device,
            action=action,
            value=value,
            scene=scene,
            execute_at=execute_at,
            initiator=initiator,
            description=description or f"Run in {delay_seconds:.0f}s",
        )

        async with self._lock:
            self._schedules[schedule_id] = sched

        sched._task = asyncio.create_task(self._run_after_delay(sched, delay_seconds))
        logger.info("Scheduled %s: %s in %.0fs", schedule_id, description, delay_seconds)
        return sched

    async def schedule_at(
        self,
        execute_at: float,
        device: str | None = None,
        action: str | None = None,
        value: Any = None,
        scene: str | None = None,
        initiator: str = "ai_agent",
        description: str = "",
    ) -> ScheduledAction:
        """Schedule an action to run at a specific timestamp."""
        delay = execute_at - time.time()
        if delay < 0:
            # If the time is in the past for today, schedule for tomorrow
            delay += 86400
            execute_at += 86400

        sched = ScheduledAction(
            schedule_id=uuid.uuid4().hex[:12],
            schedule_type=ScheduleType.ONE_SHOT,
            status=ScheduleStatus.PENDING,
            device=device,
            action=action,
            value=value,
            scene=scene,
            execute_at=execute_at,
            initiator=initiator,
            description=description,
        )

        async with self._lock:
            self._schedules[sched.schedule_id] = sched

        sched._task = asyncio.create_task(self._run_after_delay(sched, delay))
        return sched

    async def schedule_recurring(
        self,
        interval_seconds: float,
        device: str | None = None,
        action: str | None = None,
        value: Any = None,
        scene: str | None = None,
        max_runs: int | None = None,
        initiator: str = "ai_agent",
        description: str = "",
    ) -> ScheduledAction:
        """Schedule a recurring action at fixed intervals."""
        sched = ScheduledAction(
            schedule_id=uuid.uuid4().hex[:12],
            schedule_type=ScheduleType.RECURRING,
            status=ScheduleStatus.PENDING,
            device=device,
            action=action,
            value=value,
            scene=scene,
            interval_seconds=interval_seconds,
            max_runs=max_runs,
            initiator=initiator,
            description=description or f"Every {interval_seconds:.0f}s",
        )

        async with self._lock:
            self._schedules[sched.schedule_id] = sched

        sched._task = asyncio.create_task(self._run_recurring(sched))
        return sched

    async def cancel(self, schedule_id: str) -> bool:
        """Cancel a scheduled action. Returns True if cancelled."""
        async with self._lock:
            sched = self._schedules.get(schedule_id)
            if not sched:
                return False
            if sched.status in (ScheduleStatus.COMPLETED, ScheduleStatus.CANCELLED):
                return False
            sched.status = ScheduleStatus.CANCELLED
            if sched._task and not sched._task.done():
                sched._task.cancel()
            logger.info("Cancelled schedule: %s", schedule_id)
            return True

    def list_schedules(
        self,
        status: ScheduleStatus | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """List all schedules with optional filtering."""
        results = []
        for sched in self._schedules.values():
            if active_only and sched.status not in (
                ScheduleStatus.PENDING, ScheduleStatus.RUNNING
            ):
                continue
            if status and sched.status != status:
                continue
            results.append(sched.to_dict())
        return results

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        sched = self._schedules.get(schedule_id)
        return sched.to_dict() if sched else None

    async def cancel_all(self, initiator: str | None = None) -> int:
        """Cancel all schedules. Returns count cancelled."""
        count = 0
        async with self._lock:
            for sched in self._schedules.values():
                if initiator and sched.initiator != initiator:
                    continue
                if sched.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING):
                    sched.status = ScheduleStatus.CANCELLED
                    if sched._task and not sched._task.done():
                        sched._task.cancel()
                    count += 1
        return count

    async def _run_after_delay(self, sched: ScheduledAction, delay: float) -> None:
        """Execute a one-shot or delay schedule."""
        try:
            await asyncio.sleep(delay)
            sched.status = ScheduleStatus.RUNNING
            await self._execute(sched)
            sched.status = ScheduleStatus.COMPLETED
        except asyncio.CancelledError:
            sched.status = ScheduleStatus.CANCELLED
        except Exception as e:
            sched.status = ScheduleStatus.FAILED
            logger.exception("Schedule %s failed: %s", sched.schedule_id, e)

    async def _run_recurring(self, sched: ScheduledAction) -> None:
        """Execute a recurring schedule."""
        try:
            while True:
                if sched.max_runs is not None and sched.run_count >= sched.max_runs:
                    sched.status = ScheduleStatus.COMPLETED
                    break

                await asyncio.sleep(sched.interval_seconds or 60)
                if sched.status == ScheduleStatus.CANCELLED:
                    break

                sched.status = ScheduleStatus.RUNNING
                await self._execute(sched)
                sched.status = ScheduleStatus.PENDING
        except asyncio.CancelledError:
            sched.status = ScheduleStatus.CANCELLED
        except Exception as e:
            sched.status = ScheduleStatus.FAILED
            logger.exception("Recurring schedule %s failed: %s", sched.schedule_id, e)

    async def _execute(self, sched: ScheduledAction) -> None:
        """Execute a scheduled action via the callback."""
        sched.run_count += 1
        sched.last_run = time.time()

        if not self._execute_fn:
            logger.warning("No execute callback set for scheduler")
            return

        if sched.scene:
            await self._execute_fn("activate_scene", {"scene": sched.scene})
        elif sched.device and sched.action:
            args: dict[str, Any] = {"device": sched.device, "action": sched.action}
            if sched.value is not None:
                args["value"] = sched.value
            await self._execute_fn("set_device", args)

        logger.info(
            "Schedule %s executed (run #%d): %s",
            sched.schedule_id, sched.run_count, sched.description,
        )

    @property
    def stats(self) -> dict[str, Any]:
        active = sum(
            1 for s in self._schedules.values()
            if s.status in (ScheduleStatus.PENDING, ScheduleStatus.RUNNING)
        )
        return {
            "total_schedules": len(self._schedules),
            "active": active,
            "completed": sum(1 for s in self._schedules.values() if s.status == ScheduleStatus.COMPLETED),
            "cancelled": sum(1 for s in self._schedules.values() if s.status == ScheduleStatus.CANCELLED),
            "failed": sum(1 for s in self._schedules.values() if s.status == ScheduleStatus.FAILED),
        }
