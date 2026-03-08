"""Redis-backed async event bus.

Drop-in replacement for the in-process EventBus that uses Redis Pub/Sub
for cross-process event distribution.  Maintains the same public interface
(start, stop, subscribe, unsubscribe, publish, publish_nowait, stats, pending)
so callers need not change.

Requires ``redis[hiredis]`` (``pip install redis[hiredis]``).

Channel naming: ``smartspaces:{event_type}``
    e.g. ``smartspaces:point.reported``, ``smartspaces:command.succeeded``

Pattern subscriptions use Redis PSUBSCRIBE:
    ``smartspaces:point.*`` matches ``smartspaces:point.reported``

The wildcard ``*`` subscribes to ``smartspaces:*`` (all events).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

try:
    import redis.asyncio as aioredis
    _HAS_REDIS = True
except ImportError:
    aioredis = None  # type: ignore[assignment]
    _HAS_REDIS = False

from core.metrics import METRICS

logger = logging.getLogger(__name__)

# Subscriber callback type — same as in-process bus
Subscriber = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

_CHANNEL_PREFIX = "smartspaces:"

# Reconnection parameters
_RECONNECT_BASE_DELAY = 1.0   # seconds
_RECONNECT_MAX_DELAY = 30.0   # seconds
_RECONNECT_FACTOR = 2.0


def _channel_for(event_type: str) -> str:
    """Map an event type to a Redis channel name."""
    return f"{_CHANNEL_PREFIX}{event_type}"


def _pattern_for(event_type: str) -> str:
    """Map a subscription pattern (e.g. ``point.*``) to a Redis PSUBSCRIBE pattern."""
    return f"{_CHANNEL_PREFIX}{event_type}"


def _is_pattern(event_type: str) -> bool:
    """Return True if *event_type* contains glob meta-characters."""
    return any(ch in event_type for ch in ("*", "?", "["))


class RedisEventBus:
    """Async event bus backed by Redis Pub/Sub.

    Follows the same interface as :class:`core.event_bus.EventBus`.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        max_queue_size: int = 10_000,
    ):
        self._redis_url = redis_url
        self._max_queue_size = max_queue_size

        # Local subscriber registry (callbacks live in this process)
        self._subscribers: dict[str, list[Subscriber]] = defaultdict(list)
        self._wildcard_subscribers: list[Subscriber] = []
        # Track which patterns use PSUBSCRIBE vs SUBSCRIBE
        self._pattern_types: set[str] = set()
        self._exact_types: set[str] = set()

        # Internal queue for publish_nowait support and back-pressure
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max_queue_size,
        )

        self._running = False
        self._publish_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None

        # Redis connections (separate for pub and sub per Redis requirement)
        self._pub_client: aioredis.Redis | None = None
        self._sub_client: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None

        self._stats = {"published": 0, "dispatched": 0, "errors": 0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Redis and start background tasks."""
        if not _HAS_REDIS:
            raise ImportError(
                "The 'redis' library is required for RedisEventBus. "
                "Install it with: pip install 'smartspaces[redis]'"
            )
        if self._running:
            return

        self._running = True

        # Create separate Redis clients for publishing and subscribing
        self._pub_client = aioredis.from_url(
            self._redis_url,
            decode_responses=True,
            retry_on_error=[ConnectionError, TimeoutError],
        )
        self._sub_client = aioredis.from_url(
            self._redis_url,
            decode_responses=True,
            retry_on_error=[ConnectionError, TimeoutError],
        )

        # Verify connectivity (fast-fail on misconfiguration)
        try:
            await self._pub_client.ping()
        except Exception:
            logger.error("RedisEventBus: cannot reach Redis at %s", self._redis_url)
            raise

        self._pubsub = self._sub_client.pubsub()

        # Re-subscribe any callbacks that were registered before start()
        await self._sync_subscriptions()

        # Background tasks
        self._publish_task = asyncio.create_task(self._publish_loop())
        self._listener_task = asyncio.create_task(self._listen_loop())

        logger.info("RedisEventBus started (url=%s)", self._redis_url)

    async def stop(self) -> None:
        """Drain the publish queue, cancel tasks, and disconnect."""
        self._running = False

        # Cancel background tasks
        for task in (self._publish_task, self._listener_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._publish_task = None
        self._listener_task = None

        # Close pubsub and clients
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.punsubscribe()
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

        for client in (self._pub_client, self._sub_client):
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

        self._pub_client = None
        self._sub_client = None

        logger.info(
            "RedisEventBus stopped (published=%d dispatched=%d errors=%d)",
            self._stats["published"],
            self._stats["dispatched"],
            self._stats["errors"],
        )

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, callback: Subscriber) -> None:
        """Register *callback* for events matching *event_type*.

        Patterns are supported:
            * ``"*"`` — all events
            * ``"point.*"`` — any event whose type starts with ``point.``
            * ``"point.reported"`` — exact match

        Subscriptions registered before :meth:`start` are honoured once
        the bus connects to Redis.
        """
        if event_type == "*":
            self._wildcard_subscribers.append(callback)
        else:
            self._subscribers[event_type].append(callback)
            if _is_pattern(event_type):
                self._pattern_types.add(event_type)
            else:
                self._exact_types.add(event_type)

        # If we are already running, update Redis subscriptions immediately
        if self._running and self._pubsub is not None:
            asyncio.ensure_future(self._sync_subscriptions())

    def unsubscribe(self, event_type: str, callback: Subscriber) -> None:
        """Remove a previously registered subscription."""
        if event_type == "*":
            self._wildcard_subscribers = [
                s for s in self._wildcard_subscribers if s is not callback
            ]
        else:
            subs = self._subscribers[event_type]
            self._subscribers[event_type] = [s for s in subs if s is not callback]
            # If no callbacks remain for this type, clean up sets
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]
                self._pattern_types.discard(event_type)
                self._exact_types.discard(event_type)

        if self._running and self._pubsub is not None:
            asyncio.ensure_future(self._sync_subscriptions())

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, event: dict[str, Any]) -> None:
        """Publish an event.  Blocks if the internal queue is full."""
        await self._queue.put(event)
        self._stats["published"] += 1
        METRICS.events_published_total.labels(
            event_type=event.get("type", "unknown"),
        ).inc()
        METRICS.event_queue_depth.set(self._queue.qsize())

    def publish_nowait(self, event: dict[str, Any]) -> bool:
        """Non-blocking publish.  Returns ``False`` if the queue is full."""
        try:
            self._queue.put_nowait(event)
            self._stats["published"] += 1
            METRICS.events_published_total.labels(
                event_type=event.get("type", "unknown"),
            ).inc()
            METRICS.event_queue_depth.set(self._queue.qsize())
            return True
        except asyncio.QueueFull:
            logger.warning(
                "RedisEventBus queue full, dropping event type=%s",
                event.get("type"),
            )
            return False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Internal — publish loop
    # ------------------------------------------------------------------

    async def _publish_loop(self) -> None:
        """Drain the internal queue and PUBLISH each event to Redis."""
        delay = _RECONNECT_BASE_DELAY
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            event_type = event.get("type", "unknown")
            channel = _channel_for(event_type)
            payload = json.dumps(event, default=str)

            try:
                if self._pub_client is not None:
                    await self._pub_client.publish(channel, payload)
                delay = _RECONNECT_BASE_DELAY  # reset on success
            except (ConnectionError, TimeoutError, aioredis.ConnectionError) as exc:
                logger.warning(
                    "RedisEventBus publish failed (channel=%s): %s — "
                    "requeuing and retrying in %.1fs",
                    channel,
                    exc,
                    delay,
                )
                # Put the event back so it is not lost
                try:
                    self._queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.error(
                        "RedisEventBus queue full after publish failure, "
                        "event lost: type=%s",
                        event_type,
                    )
                await asyncio.sleep(delay)
                delay = min(delay * _RECONNECT_FACTOR, _RECONNECT_MAX_DELAY)
                await self._try_reconnect_publisher()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("RedisEventBus unexpected publish error")

            METRICS.event_queue_depth.set(self._queue.qsize())

    # ------------------------------------------------------------------
    # Internal — listen loop (incoming messages from Redis)
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Read messages from Redis PubSub and dispatch to local callbacks."""
        delay = _RECONNECT_BASE_DELAY
        while self._running:
            try:
                await self._dispatch_messages()
                delay = _RECONNECT_BASE_DELAY
            except (ConnectionError, TimeoutError, aioredis.ConnectionError) as exc:
                logger.warning(
                    "RedisEventBus listener disconnected: %s — "
                    "reconnecting in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * _RECONNECT_FACTOR, _RECONNECT_MAX_DELAY)
                await self._try_reconnect_subscriber()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("RedisEventBus listener error")
                await asyncio.sleep(delay)
                delay = min(delay * _RECONNECT_FACTOR, _RECONNECT_MAX_DELAY)

    async def _dispatch_messages(self) -> None:
        """Inner dispatch loop — reads one message at a time."""
        if self._pubsub is None:
            await asyncio.sleep(1.0)
            return

        while self._running:
            msg = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if msg is None:
                continue

            msg_type = msg.get("type", "")
            if msg_type not in ("message", "pmessage"):
                continue

            raw_channel: str = msg.get("channel", "")
            data: str = msg.get("data", "")

            # Strip prefix to recover event_type
            if raw_channel.startswith(_CHANNEL_PREFIX):
                event_type = raw_channel[len(_CHANNEL_PREFIX):]
            else:
                event_type = raw_channel

            # Deserialise
            try:
                event = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "RedisEventBus: non-JSON payload on channel %s",
                    raw_channel,
                )
                continue

            # Collect matching local callbacks
            targets: list[Subscriber] = list(self._wildcard_subscribers)

            # Exact match
            targets.extend(self._subscribers.get(event_type, []))

            # Pattern matches (e.g. "point.*")
            for pattern in self._pattern_types:
                if fnmatch.fnmatch(event_type, pattern):
                    targets.extend(self._subscribers.get(pattern, []))

            for callback in targets:
                try:
                    await callback(event)
                    self._stats["dispatched"] += 1
                    METRICS.events_dispatched_total.inc()
                except Exception:
                    self._stats["errors"] += 1
                    METRICS.events_errors_total.inc()
                    logger.exception(
                        "RedisEventBus subscriber error for event type=%s",
                        event_type,
                    )

    # ------------------------------------------------------------------
    # Internal — subscription synchronisation
    # ------------------------------------------------------------------

    async def _sync_subscriptions(self) -> None:
        """Ensure the Redis PubSub object mirrors the local registry."""
        if self._pubsub is None:
            return

        try:
            # Always listen on the wildcard pattern if there are wildcard subs
            if self._wildcard_subscribers:
                await self._pubsub.psubscribe(_pattern_for("*"))
            else:
                try:
                    await self._pubsub.punsubscribe(_pattern_for("*"))
                except Exception:
                    pass

            # Exact channels
            for event_type in self._exact_types:
                await self._pubsub.subscribe(_channel_for(event_type))

            # Pattern channels
            for event_type in self._pattern_types:
                await self._pubsub.psubscribe(_pattern_for(event_type))

        except Exception:
            logger.exception("RedisEventBus: failed to sync subscriptions")

    # ------------------------------------------------------------------
    # Internal — reconnection helpers
    # ------------------------------------------------------------------

    async def _try_reconnect_publisher(self) -> None:
        """Attempt to re-create the publisher Redis client."""
        try:
            if self._pub_client is not None:
                try:
                    await self._pub_client.aclose()
                except Exception:
                    pass
            self._pub_client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                retry_on_error=[ConnectionError, TimeoutError],
            )
            await self._pub_client.ping()
            logger.info("RedisEventBus publisher reconnected")
        except Exception as exc:
            logger.warning("RedisEventBus publisher reconnect failed: %s", exc)

    async def _try_reconnect_subscriber(self) -> None:
        """Attempt to re-create the subscriber Redis client and PubSub."""
        try:
            if self._pubsub is not None:
                try:
                    await self._pubsub.close()
                except Exception:
                    pass
            if self._sub_client is not None:
                try:
                    await self._sub_client.aclose()
                except Exception:
                    pass

            self._sub_client = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
                retry_on_error=[ConnectionError, TimeoutError],
            )
            self._pubsub = self._sub_client.pubsub()
            await self._sync_subscriptions()
            logger.info("RedisEventBus subscriber reconnected")
        except Exception as exc:
            logger.warning("RedisEventBus subscriber reconnect failed: %s", exc)
