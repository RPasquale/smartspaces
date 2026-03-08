"""Tests for the Redis event bus module.

These tests verify the RedisEventBus interface without requiring a real
Redis server — they test the class structure, channel naming, and
pattern matching logic.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.event_bus_redis import (
    RedisEventBus,
    _channel_for,
    _is_pattern,
    _pattern_for,
    _CHANNEL_PREFIX,
)


class TestChannelNaming:
    """Test the channel naming helper functions."""

    def test_channel_for_simple(self):
        assert _channel_for("point.reported") == "smartspaces:point.reported"

    def test_channel_for_nested(self):
        assert _channel_for("command.succeeded") == "smartspaces:command.succeeded"

    def test_pattern_for_wildcard(self):
        assert _pattern_for("point.*") == "smartspaces:point.*"

    def test_pattern_for_all(self):
        assert _pattern_for("*") == "smartspaces:*"

    def test_is_pattern_star(self):
        assert _is_pattern("point.*") is True

    def test_is_pattern_question(self):
        assert _is_pattern("point.?") is True

    def test_is_pattern_bracket(self):
        assert _is_pattern("point.[abc]") is True

    def test_is_pattern_exact(self):
        assert _is_pattern("point.reported") is False


class TestRedisEventBusInit:
    """Test RedisEventBus initialization (no Redis required)."""

    def test_default_init(self):
        bus = RedisEventBus()
        assert bus._redis_url == "redis://localhost:6379"
        assert bus._max_queue_size == 10_000
        assert bus._running is False

    def test_custom_init(self):
        bus = RedisEventBus(
            redis_url="redis://custom:6380",
            max_queue_size=5_000,
        )
        assert bus._redis_url == "redis://custom:6380"
        assert bus._max_queue_size == 5_000

    def test_stats_initial(self):
        bus = RedisEventBus()
        assert bus.stats == {"published": 0, "dispatched": 0, "errors": 0}

    def test_pending_initial(self):
        bus = RedisEventBus()
        assert bus.pending == 0


class TestRedisEventBusSubscribe:
    """Test subscribe/unsubscribe logic (local registry, no Redis)."""

    def test_subscribe_exact(self):
        bus = RedisEventBus()

        async def handler(e):
            pass

        bus.subscribe("point.reported", handler)
        assert "point.reported" in bus._subscribers
        assert handler in bus._subscribers["point.reported"]
        assert "point.reported" in bus._exact_types

    def test_subscribe_pattern(self):
        bus = RedisEventBus()

        async def handler(e):
            pass

        bus.subscribe("point.*", handler)
        assert "point.*" in bus._pattern_types

    def test_subscribe_wildcard(self):
        bus = RedisEventBus()

        async def handler(e):
            pass

        bus.subscribe("*", handler)
        assert handler in bus._wildcard_subscribers

    def test_unsubscribe_exact(self):
        bus = RedisEventBus()

        async def handler(e):
            pass

        bus.subscribe("point.reported", handler)
        bus.unsubscribe("point.reported", handler)
        assert "point.reported" not in bus._subscribers

    def test_unsubscribe_wildcard(self):
        bus = RedisEventBus()

        async def handler(e):
            pass

        bus.subscribe("*", handler)
        bus.unsubscribe("*", handler)
        assert handler not in bus._wildcard_subscribers


class TestRedisEventBusPublishNowait:
    """Test publish_nowait without a running bus."""

    def test_publish_nowait_queues(self):
        bus = RedisEventBus()
        bus._running = True
        result = bus.publish_nowait({"type": "test.event", "data": "hello"})
        assert result is True
        assert bus.stats["published"] == 1
        assert bus.pending == 1

    def test_publish_nowait_full_queue(self):
        bus = RedisEventBus(max_queue_size=1)
        bus._running = True
        bus.publish_nowait({"type": "first"})
        result = bus.publish_nowait({"type": "second"})
        assert result is False
        assert bus.stats["published"] == 1  # only first counted


class TestRedisEventBusEngineIntegration:
    """Test that Engine supports Redis event bus selection."""

    def test_engine_default_memory_bus(self):
        from core.engine import Engine
        engine = Engine(db_path=":memory:")
        from core.event_bus import EventBus
        assert isinstance(engine.event_bus, EventBus)

    def test_engine_redis_bus_flag(self):
        """Engine with event_bus='redis' should create a RedisEventBus."""
        from core.engine import Engine
        engine = Engine(db_path=":memory:", event_bus="redis")
        assert isinstance(engine.event_bus, RedisEventBus)
