"""Tests for the in-process event bus."""

import asyncio

import pytest

from core.event_bus import EventBus


@pytest.fixture
async def bus():
    b = EventBus()
    await b.start()
    yield b
    await b.stop()


class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test.event", handler)
        await bus.publish({"type": "test.event", "data": "hello"})
        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0]["data"] == "hello"

    @pytest.mark.asyncio
    async def test_wildcard_subscriber(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("*", handler)
        await bus.publish({"type": "point.reported", "value": 1})
        await bus.publish({"type": "command.succeeded", "value": 2})
        await asyncio.sleep(0.1)

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_prefix_subscriber(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("point.*", handler)
        await bus.publish({"type": "point.reported", "x": 1})
        await bus.publish({"type": "point.quality_changed", "x": 2})
        await bus.publish({"type": "command.succeeded", "x": 3})
        await asyncio.sleep(0.1)

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test.event", handler)
        await bus.publish({"type": "test.event"})
        await asyncio.sleep(0.1)
        assert len(received) == 1

        bus.unsubscribe("test.event", handler)
        await bus.publish({"type": "test.event"})
        await asyncio.sleep(0.1)
        assert len(received) == 1  # No new event

    @pytest.mark.asyncio
    async def test_publish_nowait(self, bus):
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("fast", handler)
        ok = bus.publish_nowait({"type": "fast", "val": 42})
        assert ok is True
        await asyncio.sleep(0.1)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_stats(self, bus):
        await bus.publish({"type": "a"})
        await bus.publish({"type": "b"})
        await asyncio.sleep(0.1)
        assert bus.stats["published"] == 2

    @pytest.mark.asyncio
    async def test_subscriber_error_doesnt_crash_bus(self, bus):
        good_received = []

        async def bad_handler(event):
            raise ValueError("boom")

        async def good_handler(event):
            good_received.append(event)

        bus.subscribe("test", bad_handler)
        bus.subscribe("test", good_handler)
        await bus.publish({"type": "test"})
        await asyncio.sleep(0.1)

        assert len(good_received) == 1
        assert bus.stats["errors"] >= 1
