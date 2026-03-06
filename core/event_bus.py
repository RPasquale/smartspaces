"""In-process async event bus.

Provides publish/subscribe for canonical event envelopes flowing between
adapters, the state store, the scheduler, and external consumers.

Designed to be swappable — start in-process, replace with Redis/NATS later
without changing adapter or consumer code.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Subscriber callback type: async fn(event_dict) -> None
Subscriber = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """Async in-process pub/sub event bus."""

    def __init__(self, max_queue_size: int = 10_000):
        self._subscribers: dict[str, list[Subscriber]] = defaultdict(list)
        self._wildcard_subscribers: list[Subscriber] = []
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        self._running = False
        self._dispatch_task: asyncio.Task | None = None
        self._stats = {"published": 0, "dispatched": 0, "errors": 0}

    async def start(self) -> None:
        """Start the background dispatch loop."""
        if self._running:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("EventBus started")

    async def stop(self) -> None:
        """Stop the dispatch loop and drain remaining events."""
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None
        logger.info("EventBus stopped (published=%d dispatched=%d errors=%d)",
                     self._stats["published"], self._stats["dispatched"], self._stats["errors"])

    def subscribe(self, event_type: str, callback: Subscriber) -> None:
        """Subscribe to a specific event type.

        Args:
            event_type: Event type to match (e.g. "point.reported", "command.succeeded").
                       Use "*" for all events.
            callback: Async function called with the event dict.
        """
        if event_type == "*":
            self._wildcard_subscribers.append(callback)
        else:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Subscriber) -> None:
        """Remove a subscription."""
        if event_type == "*":
            self._wildcard_subscribers = [s for s in self._wildcard_subscribers if s is not callback]
        else:
            self._subscribers[event_type] = [
                s for s in self._subscribers[event_type] if s is not callback
            ]

    async def publish(self, event: dict[str, Any]) -> None:
        """Publish an event to the bus.

        Events are queued and dispatched asynchronously. If the queue is full,
        this will block until space is available.
        """
        await self._queue.put(event)
        self._stats["published"] += 1

    def publish_nowait(self, event: dict[str, Any]) -> bool:
        """Non-blocking publish. Returns False if queue is full."""
        try:
            self._queue.put_nowait(event)
            self._stats["published"] += 1
            return True
        except asyncio.QueueFull:
            logger.warning("EventBus queue full, dropping event type=%s", event.get("type"))
            return False

    async def _dispatch_loop(self) -> None:
        """Background loop that pulls events from the queue and dispatches them."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            event_type = event.get("type", "")
            targets = list(self._wildcard_subscribers)
            targets.extend(self._subscribers.get(event_type, []))

            # Also match prefix subscribers (e.g. "point.*" matches "point.reported")
            prefix = event_type.split(".")[0] + ".*" if "." in event_type else ""
            if prefix:
                targets.extend(self._subscribers.get(prefix, []))

            for callback in targets:
                try:
                    await callback(event)
                    self._stats["dispatched"] += 1
                except Exception:
                    self._stats["errors"] += 1
                    logger.exception("EventBus subscriber error for event type=%s", event_type)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def pending(self) -> int:
        return self._queue.qsize()
