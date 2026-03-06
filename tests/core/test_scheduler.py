"""Tests for the polling scheduler."""

import asyncio

import pytest

from core.event_bus import EventBus
from core.scheduler import Scheduler
from core.state_store import StateStore


@pytest.fixture
async def bus():
    b = EventBus()
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
async def store(tmp_path):
    s = StateStore(db_path=tmp_path / "test.db")
    await s.open()
    yield s
    await s.close()


class TestScheduler:
    @pytest.mark.asyncio
    async def test_add_and_list_targets(self, bus, store):
        sched = Scheduler(bus, store, default_interval=10)
        sched.add_target("conn_1", "pt_1", interval_sec=5)
        sched.add_target("conn_1", "pt_2", interval_sec=15)
        assert sched.stats["total_targets"] == 2

    @pytest.mark.asyncio
    async def test_add_targets_from_inventory(self, bus, store):
        sched = Scheduler(bus, store)
        points = [
            {"point_id": "pt_1", "readable": True, "event_driven": False},
            {"point_id": "pt_2", "readable": True, "event_driven": True},  # Skipped
            {"point_id": "pt_3", "readable": False},  # Skipped
            {"point_id": "pt_4", "readable": True, "event_driven": False},
        ]
        sched.add_targets_from_inventory("conn_1", points)
        assert sched.stats["total_targets"] == 2  # Only pt_1 and pt_4

    @pytest.mark.asyncio
    async def test_remove_target(self, bus, store):
        sched = Scheduler(bus, store)
        sched.add_target("conn_1", "pt_1")
        sched.remove_target("pt_1")
        assert sched.stats["total_targets"] == 0

    @pytest.mark.asyncio
    async def test_remove_connection(self, bus, store):
        sched = Scheduler(bus, store)
        sched.add_target("conn_1", "pt_1")
        sched.add_target("conn_1", "pt_2")
        sched.add_target("conn_2", "pt_3")
        sched.remove_connection("conn_1")
        assert sched.stats["total_targets"] == 1

    @pytest.mark.asyncio
    async def test_poll_calls_read_fn(self, bus, store):
        read_calls = []

        async def mock_read(conn_id, point_id):
            read_calls.append((conn_id, point_id))
            return {"value": True}

        sched = Scheduler(bus, store, default_interval=0.1, tick_interval=0.05)
        sched.set_read_fn(mock_read)
        sched.add_target("conn_1", "pt_1", interval_sec=0.1)

        await sched.start()
        await asyncio.sleep(0.3)
        await sched.stop()

        assert len(read_calls) >= 1
        assert read_calls[0] == ("conn_1", "pt_1")

    @pytest.mark.asyncio
    async def test_error_suspension(self, bus, store):
        call_count = 0

        async def failing_read(conn_id, point_id):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("down")

        sched = Scheduler(bus, store, default_interval=0.05, tick_interval=0.02)
        sched.set_read_fn(failing_read)
        sched.add_target("conn_1", "pt_1", interval_sec=0.05)

        # Lower max errors for test speed
        sched._targets["pt_1"].max_errors = 3

        await sched.start()
        await asyncio.sleep(0.5)
        await sched.stop()

        assert sched.stats["errors"] >= 3
        assert sched.stats["suspended_targets"] == 1

    @pytest.mark.asyncio
    async def test_reset_errors(self, bus, store):
        sched = Scheduler(bus, store)
        sched.add_target("conn_1", "pt_1")
        sched._targets["pt_1"].consecutive_errors = 5
        sched.reset_errors("pt_1")
        assert sched._targets["pt_1"].consecutive_errors == 0

    @pytest.mark.asyncio
    async def test_reset_all_errors(self, bus, store):
        sched = Scheduler(bus, store)
        sched.add_target("conn_1", "pt_1")
        sched.add_target("conn_1", "pt_2")
        sched.add_target("conn_2", "pt_3")
        sched._targets["pt_1"].consecutive_errors = 3
        sched._targets["pt_2"].consecutive_errors = 5
        sched._targets["pt_3"].consecutive_errors = 2
        sched.reset_all_errors("conn_1")
        assert sched._targets["pt_1"].consecutive_errors == 0
        assert sched._targets["pt_2"].consecutive_errors == 0
        assert sched._targets["pt_3"].consecutive_errors == 2  # Unchanged
