"""Tests for advanced Agent Gateway features — events, intent, groups, history, etc."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent.spaces import SpaceRegistry
from agent.safety import AISafetyGuard, SafetyConfig
from agent.scenes import SceneEngine
from agent.tools import ToolExecutor
from agent.events import EventStreamManager, EventType, AgentEvent, ClientFilter
from agent.groups import GroupRegistry
from agent.history import ActionHistory, ActionType, ActionStatus
from agent.intent import IntentResolver, IntentCategory
from agent.coordination import DeviceCoordinator
from agent.agent_scheduler import ActionScheduler
from agent.analytics import EnergyComfortAnalyzer
from agent.suggestions import ActionSuggester
from agent.discovery import CapabilityDescriber


# -- Shared fixtures --

SPACES_DATA = {
    "site": "test_home",
    "spaces": {
        "living_room": {
            "display_name": "Living Room",
            "devices": {
                "ceiling_light": {
                    "point_id": "pt_relay_1",
                    "connection_id": "conn_1",
                    "endpoint_id": "ep_1",
                    "device_id": "dev_1",
                    "capabilities": ["binary_switch"],
                    "ai_access": "full",
                    "safety_class": "S1",
                },
                "lamp": {
                    "point_id": "pt_dimmer_1",
                    "connection_id": "conn_1",
                    "capabilities": ["binary_switch", "dimmer"],
                    "ai_access": "full",
                },
                "temperature": {
                    "point_id": "pt_temp_1",
                    "connection_id": "conn_1",
                    "capabilities": ["temperature_sensor"],
                    "ai_access": "read_only",
                    "unit": "°C",
                    "value_type": "float",
                },
                "fan": {
                    "point_id": "pt_relay_2",
                    "connection_id": "conn_1",
                    "capabilities": ["fan", "binary_switch"],
                    "ai_access": "full",
                },
            },
        },
        "bedroom": {
            "display_name": "Bedroom",
            "devices": {
                "main_light": {
                    "point_id": "pt_hue_1",
                    "connection_id": "conn_2",
                    "capabilities": ["dimmer", "light_color"],
                    "ai_access": "full",
                },
            },
        },
        "garage": {
            "display_name": "Garage",
            "devices": {
                "door": {
                    "point_id": "pt_cover_1",
                    "connection_id": "conn_3",
                    "capabilities": ["cover"],
                    "ai_access": "confirm_required",
                    "safety_class": "S2",
                },
            },
        },
    },
}

SCENES_DATA = {
    "scenes": {
        "movie_mode": {
            "display_name": "Movie Mode",
            "tags": ["entertainment", "evening"],
            "actions": [
                {"device": "living_room.ceiling_light", "action": "off"},
                {"device": "living_room.fan", "action": "on"},
            ],
        },
        "goodnight": {
            "display_name": "Goodnight",
            "tags": ["sleep"],
            "actions": [
                {"device": "living_room.ceiling_light", "action": "off"},
                {"device": "bedroom.main_light", "action": "off"},
            ],
        },
    },
    "rules": {},
}

GROUPS_DATA = {
    "groups": {
        "all_lights": {
            "display_name": "All Lights",
            "match": {"capability": ["binary_switch", "dimmer"]},
        },
        "custom_set": {
            "display_name": "Custom Set",
            "members": ["living_room.fan", "bedroom.main_light"],
        },
    },
}


@pytest.fixture
def spaces():
    r = SpaceRegistry()
    r.load_dict(SPACES_DATA)
    return r


@pytest.fixture
def safety(spaces):
    config = SafetyConfig(max_writes_per_minute=50, cooldown_seconds=0.01, require_readback=False)
    return AISafetyGuard(spaces, config)


@pytest.fixture
def scenes():
    e = SceneEngine()
    e.load_dict(SCENES_DATA)
    return e


@pytest.fixture
def executor(spaces, safety, scenes):
    async def mock_read(conn_id, point_id):
        return {"point_id": point_id, "value": True, "quality": {"status": "good"}}

    async def mock_execute(conn_id, command):
        return {"command_id": command.get("command_id"), "status": "succeeded"}

    return ToolExecutor(spaces, safety, scenes, mock_read, mock_execute)


# ====================================================================
# Event Streaming Tests
# ====================================================================

class TestEventStreamManager:
    async def test_connect_and_disconnect(self):
        mgr = EventStreamManager()
        cid, queue = await mgr.connect()
        assert mgr.connected_count == 1
        await mgr.disconnect(cid)
        assert mgr.connected_count == 0

    async def test_dispatch_reaches_client(self):
        mgr = EventStreamManager()
        cid, queue = await mgr.connect()
        event = AgentEvent(
            type=EventType.DEVICE_STATE_CHANGE,
            data={"value": True},
            device="living_room.light",
            space="living_room",
        )
        reached = await mgr.dispatch(event)
        assert reached == 1
        received = queue.get_nowait()
        assert received.type == EventType.DEVICE_STATE_CHANGE
        await mgr.disconnect(cid)

    async def test_filter_by_space(self):
        mgr = EventStreamManager()
        filters = ClientFilter(spaces={"bedroom"})
        cid, queue = await mgr.connect(filters=filters)

        # Living room event should not reach
        event1 = AgentEvent(type=EventType.DEVICE_STATE_CHANGE, data={}, space="living_room")
        await mgr.dispatch(event1)
        assert queue.empty()

        # Bedroom event should reach
        event2 = AgentEvent(type=EventType.DEVICE_STATE_CHANGE, data={}, space="bedroom")
        await mgr.dispatch(event2)
        assert not queue.empty()
        await mgr.disconnect(cid)

    async def test_filter_by_device(self):
        mgr = EventStreamManager()
        filters = ClientFilter(devices={"living_room.fan"})
        cid, queue = await mgr.connect(filters=filters)

        await mgr.dispatch(AgentEvent(type=EventType.DEVICE_STATE_CHANGE, data={}, device="living_room.light"))
        assert queue.empty()

        await mgr.dispatch(AgentEvent(type=EventType.DEVICE_STATE_CHANGE, data={}, device="living_room.fan"))
        assert not queue.empty()
        await mgr.disconnect(cid)

    async def test_filter_by_event_type(self):
        mgr = EventStreamManager()
        filters = ClientFilter(event_types={EventType.ACTION_EXECUTED})
        cid, queue = await mgr.connect(filters=filters)

        await mgr.dispatch(AgentEvent(type=EventType.DEVICE_STATE_CHANGE, data={}))
        assert queue.empty()

        await mgr.dispatch(AgentEvent(type=EventType.ACTION_EXECUTED, data={}))
        assert not queue.empty()
        await mgr.disconnect(cid)

    async def test_sse_format(self):
        event = AgentEvent(
            type=EventType.DEVICE_STATE_CHANGE,
            data={"value": True},
            device="light",
        )
        sse = event.sse_format()
        assert "event: device_state_change" in sse
        assert "data:" in sse
        assert "id:" in sse

    async def test_stats(self):
        mgr = EventStreamManager()
        cid, _ = await mgr.connect()
        await mgr.dispatch(AgentEvent(type=EventType.HEARTBEAT, data={}))
        stats = mgr.stats
        assert stats["connected_clients"] == 1
        assert stats["total_events_dispatched"] == 1
        await mgr.disconnect(cid)


# ====================================================================
# Group Registry Tests
# ====================================================================

class TestGroupRegistry:
    def test_auto_groups(self, spaces):
        groups = GroupRegistry(spaces)
        group_list = groups.list_groups()
        # Should auto-generate groups for capabilities and spaces
        names = {g["name"] for g in group_list}
        assert "all_binary_switch" in names
        assert "all_living_room" in names

    def test_load_custom_groups(self, spaces):
        groups = GroupRegistry(spaces)
        groups.load_dict(GROUPS_DATA)
        assert groups.resolve_name("all_lights") is not None
        assert groups.resolve_name("custom_set") is not None

    def test_resolve_by_capability(self, spaces):
        groups = GroupRegistry(spaces)
        groups.load_dict(GROUPS_DATA)
        members = groups.resolve("all_lights")
        names = {m.semantic_name for m in members}
        assert "living_room.ceiling_light" in names
        assert "living_room.lamp" in names

    def test_resolve_explicit_members(self, spaces):
        groups = GroupRegistry(spaces)
        groups.load_dict(GROUPS_DATA)
        members = groups.resolve("custom_set")
        names = {m.semantic_name for m in members}
        assert "living_room.fan" in names
        assert "bedroom.main_light" in names

    def test_writable_members(self, spaces):
        groups = GroupRegistry(spaces)
        writable = groups.get_writable_members("all_living_room")
        names = {m.semantic_name for m in writable}
        # temperature is read_only so excluded
        assert "living_room.temperature" not in names
        assert "living_room.ceiling_light" in names

    def test_add_group(self, spaces):
        groups = GroupRegistry(spaces)
        groups.add_group("test", "Test Group", members=["living_room.fan"])
        assert groups.resolve_name("test") is not None
        members = groups.resolve("test")
        assert len(members) == 1

    def test_remove_group(self, spaces):
        groups = GroupRegistry(spaces)
        groups.add_group("temp", "Temp", members=[])
        assert groups.remove_group("temp")
        assert not groups.remove_group("nonexistent")

    def test_find_groups_for_device(self, spaces):
        groups = GroupRegistry(spaces)
        found = groups.find_groups_for_device("living_room.ceiling_light")
        assert len(found) >= 1


# ====================================================================
# Action History Tests
# ====================================================================

class TestActionHistory:
    def test_record_and_query(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="living_room.light", action="on")
        results = h.query(limit=10)
        assert len(results) == 1
        assert results[0]["device"] == "living_room.light"

    def test_query_by_device(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="living_room.light", action="on")
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="bedroom.light", action="off")
        results = h.query(device="living_room.light")
        assert len(results) == 1

    def test_query_by_space(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="living_room.light", action="on")
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="bedroom.light", action="off")
        results = h.query(space="living_room")
        assert len(results) == 1

    def test_query_by_status(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="a.b")
        h.record(ActionType.WRITE, ActionStatus.BLOCKED, device="c.d")
        results = h.query(status=ActionStatus.BLOCKED)
        assert len(results) == 1

    def test_recent_summary(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="living_room.light", action="on")
        summary = h.recent_summary(minutes=5)
        assert len(summary) == 1
        assert "living_room.light" in summary[0]["description"]

    def test_stats(self):
        h = ActionHistory()
        h.record(ActionType.READ, ActionStatus.SUCCEEDED, device="a.b")
        h.record(ActionType.WRITE, ActionStatus.BLOCKED, device="c.d")
        stats = h.stats
        assert stats["total"] == 2
        assert stats["reads"] == 1
        assert stats["blocked"] == 1

    def test_to_context_prompt(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="living_room.light", action="on")
        prompt = h.to_context_prompt()
        assert "living_room.light" in prompt

    def test_last_action_for_device(self):
        h = ActionHistory()
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="x.y", action="on")
        h.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="x.y", action="off")
        last = h.last_action_for_device("x.y")
        assert last is not None
        assert last.action == "off"


# ====================================================================
# Intent Resolver Tests
# ====================================================================

class TestIntentResolver:
    @pytest.fixture
    def resolver(self, spaces, scenes):
        groups = GroupRegistry(spaces)
        return IntentResolver(spaces, groups, scenes)

    def test_turn_on_device(self, resolver):
        result = resolver.resolve("turn on the ceiling light")
        assert result.category == IntentCategory.CONTROL
        assert len(result.tool_calls) >= 1
        assert result.tool_calls[0]["args"]["action"] == "on"

    def test_turn_off_device(self, resolver):
        result = resolver.resolve("switch off the fan")
        assert result.category == IntentCategory.CONTROL
        assert result.tool_calls[0]["args"]["action"] == "off"

    def test_query_temperature(self, resolver):
        result = resolver.resolve("what's the temperature?")
        assert result.category == IntentCategory.QUERY

    def test_activate_scene(self, resolver):
        result = resolver.resolve("activate movie mode")
        assert result.category == IntentCategory.SCENE
        assert result.tool_calls[0]["args"]["scene"] == "movie_mode"

    def test_environment_cooler(self, resolver):
        result = resolver.resolve("make it cooler in the living room")
        assert result.category == IntentCategory.ENVIRONMENT
        assert len(result.tool_calls) >= 1

    def test_group_all_off(self, resolver):
        result = resolver.resolve("turn off all lights")
        assert result.category == IntentCategory.GROUP

    def test_schedule_delay(self, resolver):
        result = resolver.resolve("turn off the ceiling light in 30 minutes")
        assert result.category == IntentCategory.SCHEDULE
        assert result.extracted_entities.get("time", {}).get("type") == "delay"

    def test_schedule_absolute(self, resolver):
        result = resolver.resolve("turn on the fan at 7pm")
        assert result.category == IntentCategory.SCHEDULE

    def test_meta_list_rooms(self, resolver):
        result = resolver.resolve("list all rooms")
        assert result.category == IntentCategory.META

    def test_dim_with_value(self, resolver):
        result = resolver.resolve("dim the lamp to 50")
        assert result.category == IntentCategory.CONTROL
        tc = result.tool_calls[0]
        assert tc["args"]["action"] == "set"

    def test_unknown_intent(self, resolver):
        result = resolver.resolve("xyzzy")
        assert result.category == IntentCategory.UNKNOWN


# ====================================================================
# Device Coordinator Tests
# ====================================================================

class TestDeviceCoordinator:
    async def test_acquire_and_release(self):
        coord = DeviceCoordinator()
        lease = await coord.acquire("light", "agent_1", duration=10)
        assert lease is not None
        assert lease.agent_id == "agent_1"
        released = await coord.release(lease.lease_id, "agent_1")
        assert released

    async def test_block_second_agent(self):
        coord = DeviceCoordinator()
        await coord.acquire("light", "agent_1", duration=10)
        lease2 = await coord.acquire("light", "agent_2", duration=10)
        assert lease2 is None  # Blocked

    async def test_preempt_with_higher_priority(self):
        coord = DeviceCoordinator()
        await coord.acquire("light", "agent_1", duration=10, priority=0)
        lease2 = await coord.acquire("light", "agent_2", duration=10, priority=5)
        assert lease2 is not None
        assert lease2.agent_id == "agent_2"

    async def test_same_agent_extends(self):
        coord = DeviceCoordinator()
        lease1 = await coord.acquire("light", "agent_1", duration=10)
        lease2 = await coord.acquire("light", "agent_1", duration=30)
        assert lease2 is not None
        assert lease2.lease_id == lease1.lease_id

    async def test_check_write(self):
        coord = DeviceCoordinator()
        await coord.acquire("light", "agent_1", duration=10)
        ok1, _ = coord.check_write("light", "agent_1")
        assert ok1
        ok2, reason = coord.check_write("light", "agent_2")
        assert not ok2
        assert "agent_1" in reason

    async def test_release_all(self):
        coord = DeviceCoordinator()
        await coord.acquire("light", "agent_1")
        await coord.acquire("fan", "agent_1")
        count = await coord.release_all("agent_1")
        assert count == 2

    async def test_list_leases(self):
        coord = DeviceCoordinator()
        await coord.acquire("light", "agent_1")
        leases = coord.list_leases()
        assert len(leases) == 1

    async def test_stats(self):
        coord = DeviceCoordinator()
        await coord.acquire("light", "agent_1")
        stats = coord.stats
        assert stats["acquired"] == 1
        assert stats["active_leases"] == 1


# ====================================================================
# Action Scheduler Tests
# ====================================================================

class TestActionScheduler:
    async def test_schedule_delay(self):
        executed = []
        async def callback(tool, args):
            executed.append((tool, args))
            return {"status": "ok"}

        sched = ActionScheduler(execute_callback=callback)
        action = await sched.schedule_delay(0.05, device="light", action="off")
        assert action.schedule_id
        await asyncio.sleep(0.15)
        assert len(executed) == 1
        assert executed[0][1]["device"] == "light"

    async def test_cancel_schedule(self):
        executed = []
        async def callback(tool, args):
            executed.append(True)
            return {}

        sched = ActionScheduler(execute_callback=callback)
        action = await sched.schedule_delay(0.5, device="light", action="off")
        cancelled = await sched.cancel(action.schedule_id)
        assert cancelled
        await asyncio.sleep(0.6)
        assert len(executed) == 0

    async def test_list_schedules(self):
        sched = ActionScheduler()
        await sched.schedule_delay(10.0, device="light", action="off")
        schedules = sched.list_schedules(active_only=True)
        assert len(schedules) >= 1
        await sched.cancel_all()

    async def test_stats(self):
        sched = ActionScheduler()
        await sched.schedule_delay(10.0, device="light", action="off")
        stats = sched.stats
        assert stats["active"] >= 1
        await sched.cancel_all()


# ====================================================================
# Analytics Tests
# ====================================================================

class TestAnalytics:
    def test_compute_with_no_state(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        snap = analyzer.compute()
        assert snap.total_power_watts == 0
        assert snap.active_device_count == 0

    def test_power_estimation(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.ceiling_light", True)
        analyzer.update_state("living_room.fan", True)
        snap = analyzer.compute()
        assert snap.total_power_watts > 0
        assert snap.active_device_count == 2

    def test_temperature_tracking(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.temperature", 25.0)
        snap = analyzer.compute()
        assert snap.avg_temperature == 25.0
        assert snap.comfort_score is not None
        assert 0 <= snap.comfort_score <= 1

    def test_comfort_assessment(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.temperature", 22.0)
        snap = analyzer.compute()
        assert "Ideal" in snap.comfort_assessment

    def test_hot_assessment(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.temperature", 32.0)
        snap = analyzer.compute()
        assert "hot" in snap.comfort_assessment.lower()

    def test_to_context_prompt(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.ceiling_light", True)
        analyzer.update_state("living_room.temperature", 26.0)
        prompt = analyzer.to_context_prompt()
        assert "Power" in prompt or "Comfort" in prompt

    def test_custom_power_estimate(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.set_power_estimate("living_room.fan", 150.0)
        analyzer.update_state("living_room.fan", True)
        snap = analyzer.compute()
        assert snap.power_by_device.get("living_room.fan") == 150.0

    def test_to_dict(self, spaces):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.ceiling_light", True)
        d = analyzer.compute().to_dict()
        assert "power" in d
        assert "comfort" in d


# ====================================================================
# Suggestions Tests
# ====================================================================

class TestSuggestions:
    def test_suggest_returns_list(self, spaces, scenes):
        analyzer = EnergyComfortAnalyzer(spaces)
        history = ActionHistory()
        suggester = ActionSuggester(spaces, scenes, history, analyzer)
        suggestions = suggester.suggest()
        assert isinstance(suggestions, list)

    def test_temperature_suggestion(self, spaces, scenes):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.temperature", 32.0)
        suggester = ActionSuggester(spaces, scenes, None, analyzer)
        suggestions = suggester.suggest()
        # Should suggest turning on fan
        high_priority = [s for s in suggestions if s["priority"] == "high"]
        assert len(high_priority) >= 1

    def test_dismiss(self, spaces, scenes):
        analyzer = EnergyComfortAnalyzer(spaces)
        analyzer.update_state("living_room.temperature", 35.0)
        suggester = ActionSuggester(spaces, scenes, None, analyzer)
        suggestions = suggester.suggest()
        if suggestions:
            sid = suggestions[0]["suggestion_id"]
            suggester.dismiss(sid)
            # Should not appear again
            suggestions2 = suggester.suggest()
            ids = {s["suggestion_id"] for s in suggestions2}
            assert sid not in ids


# ====================================================================
# Capability Describer Tests
# ====================================================================

class TestCapabilityDescriber:
    def test_describe_switch(self, spaces):
        describer = CapabilityDescriber(spaces)
        desc = describer.describe("living_room.ceiling_light")
        assert desc["type"] == "on/off switch"
        assert "on" in desc["available_actions"]
        assert "summary" in desc

    def test_describe_sensor(self, spaces):
        describer = CapabilityDescriber(spaces)
        desc = describer.describe("living_room.temperature")
        assert desc["is_read_only"]
        assert desc["ai_access"] == "read_only"
        assert desc["unit"] == "°C"

    def test_describe_unknown(self, spaces):
        describer = CapabilityDescriber(spaces)
        desc = describer.describe("nonexistent")
        assert "error" in desc

    def test_describe_all(self, spaces):
        describer = CapabilityDescriber(spaces)
        all_desc = describer.describe_all()
        assert len(all_desc) >= 5

    def test_describe_by_space(self, spaces):
        describer = CapabilityDescriber(spaces)
        lr_desc = describer.describe_all(space="living_room")
        assert len(lr_desc) == 4

    def test_to_context_prompt(self, spaces):
        describer = CapabilityDescriber(spaces)
        prompt = describer.to_context_prompt()
        assert "Living Room" in prompt
        assert "on/off switch" in prompt

    def test_with_state(self, spaces):
        analytics = EnergyComfortAnalyzer(spaces)
        analytics.update_state("living_room.ceiling_light", True)
        describer = CapabilityDescriber(spaces, analytics)
        desc = describer.describe("living_room.ceiling_light", include_state=True)
        assert desc["current_state"] is not None
        assert "ON" in desc["summary"]


# ====================================================================
# Integration: ToolExecutor with advanced components
# ====================================================================

class TestToolExecutorAdvanced:
    @pytest.fixture
    def full_executor(self, executor, spaces, scenes):
        groups = GroupRegistry(spaces)
        groups.load_dict(GROUPS_DATA)
        executor.groups = groups
        executor.history = ActionHistory()
        executor.scheduler = ActionScheduler(execute_callback=executor.call)
        executor.analytics = EnergyComfortAnalyzer(spaces)
        executor.coordinator = DeviceCoordinator()
        executor.intent_resolver = IntentResolver(spaces, groups, scenes)
        executor.suggester = ActionSuggester(spaces, scenes, executor.history, executor.analytics)
        executor.describer = CapabilityDescriber(spaces, executor.analytics)
        return executor

    async def test_list_groups(self, full_executor):
        result = await full_executor.call("list_groups", {})
        assert "groups" in result

    async def test_set_group(self, full_executor):
        result = await full_executor.call("set_group", {
            "group": "custom_set",
            "action": "on",
        })
        assert result["total_devices"] == 2

    async def test_get_history(self, full_executor):
        full_executor.history.record(ActionType.WRITE, ActionStatus.SUCCEEDED, device="x.y")
        result = await full_executor.call("get_history", {"minutes": 5})
        assert "history" in result

    async def test_resolve_intent(self, full_executor):
        result = await full_executor.call("resolve_intent", {
            "text": "turn on the ceiling light",
        })
        assert result["category"] == "control"
        assert len(result["tool_calls"]) >= 1

    async def test_resolve_and_execute(self, full_executor):
        result = await full_executor.call("resolve_intent", {
            "text": "turn on the ceiling light",
            "execute": True,
        })
        assert "execution_results" in result

    async def test_acquire_and_release_lock(self, full_executor):
        result = await full_executor.call("acquire_lock", {
            "device": "living_room.ceiling_light",
            "agent_id": "test_agent",
        })
        assert result["acquired"]

        result2 = await full_executor.call("release_lock", {
            "device": "living_room.ceiling_light",
            "agent_id": "test_agent",
        })
        assert result2["released"]

    async def test_get_analytics(self, full_executor):
        result = await full_executor.call("get_analytics", {})
        assert "power" in result
        assert "comfort" in result

    async def test_get_suggestions(self, full_executor):
        result = await full_executor.call("get_suggestions", {})
        assert "suggestions" in result

    async def test_describe_device(self, full_executor):
        result = await full_executor.call("describe_device", {
            "device": "living_room.ceiling_light",
        })
        assert "summary" in result

    async def test_schedule_action(self, full_executor):
        result = await full_executor.call("schedule_action", {
            "delay_seconds": 100,
            "device": "living_room.ceiling_light",
            "action": "off",
        })
        assert "schedule_id" in result
        # Cleanup
        await full_executor.scheduler.cancel(result["schedule_id"])

    async def test_list_and_cancel_schedules(self, full_executor):
        result = await full_executor.call("schedule_action", {
            "delay_seconds": 100,
            "device": "living_room.fan",
            "action": "off",
        })
        sid = result["schedule_id"]
        listing = await full_executor.call("list_schedules", {"active_only": True})
        assert any(s["schedule_id"] == sid for s in listing["schedules"])

        cancel = await full_executor.call("cancel_schedule", {"schedule_id": sid})
        assert cancel["cancelled"]
