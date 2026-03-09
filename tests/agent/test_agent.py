"""Tests for the Agent Gateway — spaces, safety, scenes, tools, MCP."""

from __future__ import annotations

import asyncio
import time

import pytest

from agent.spaces import SpaceRegistry
from agent.safety import AISafetyGuard, SafetyConfig
from agent.scenes import SceneEngine
from agent.tools import ToolExecutor, ToolGenerator
from agent.mcp_server import MCPServer, create_mcp_server


# -- Fixtures --

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
                    "endpoint_id": "ep_2",
                    "device_id": "dev_1",
                    "capabilities": ["fan", "binary_switch"],
                    "ai_access": "full",
                },
            },
        },
        "garage": {
            "display_name": "Garage",
            "devices": {
                "door": {
                    "point_id": "pt_cover_1",
                    "connection_id": "conn_2",
                    "capabilities": ["cover"],
                    "ai_access": "confirm_required",
                    "safety_class": "S2",
                },
            },
        },
        "front_door": {
            "display_name": "Front Door",
            "devices": {
                "lock": {
                    "point_id": "pt_lock_1",
                    "connection_id": "conn_3",
                    "capabilities": ["lock", "door_lock"],
                    "ai_access": "blocked",
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
            "actions": [
                {"device": "living_room.ceiling_light", "action": "off"},
                {"device": "living_room.fan", "action": "on"},
            ],
        },
    },
    "rules": {
        "auto_cooling": {
            "display_name": "Auto Cooling",
            "condition": {"device": "living_room.temperature", "operator": ">", "value": 28},
            "actions": [{"device": "living_room.fan", "action": "on"}],
            "cooldown_sec": 0.1,
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
    config = SafetyConfig(
        max_writes_per_minute=5,
        cooldown_seconds=0.1,
        require_readback=False,
    )
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
# Space Registry Tests
# ====================================================================

class TestSpaceRegistry:
    def test_load_spaces(self, spaces):
        assert spaces.site_name == "test_home"
        assert len(spaces.spaces) == 3
        assert "living_room" in spaces.spaces

    def test_list_spaces(self, spaces):
        result = spaces.list_spaces()
        assert len(result) == 3
        lr = next(s for s in result if s["name"] == "living_room")
        assert lr["device_count"] == 3

    def test_get_by_semantic_name(self, spaces):
        dev = spaces.get("living_room.ceiling_light")
        assert dev is not None
        assert dev.point_id == "pt_relay_1"
        assert dev.ai_access == "full"

    def test_get_by_point_id(self, spaces):
        dev = spaces.get_by_point_id("pt_relay_1")
        assert dev is not None
        assert dev.semantic_name == "living_room.ceiling_light"

    def test_resolve_fuzzy(self, spaces):
        assert spaces.resolve_name("ceiling_light") is not None
        assert spaces.resolve_name("living_room.ceiling_light") is not None
        assert spaces.resolve_name("nonexistent") is None

    def test_list_devices_by_space(self, spaces):
        devs = spaces.list_devices(space="living_room")
        assert len(devs) == 3

    def test_list_devices_by_capability(self, spaces):
        devs = spaces.list_devices(capability="temperature_sensor")
        assert len(devs) == 1
        assert devs[0]["name"] == "living_room.temperature"

    def test_to_context_prompt(self, spaces):
        prompt = spaces.to_context_prompt()
        assert "Living Room" in prompt
        assert "ceiling_light" in prompt
        assert "binary_switch" in prompt


# ====================================================================
# AI Safety Guard Tests
# ====================================================================

class TestAISafetyGuard:
    def test_read_allowed(self, safety):
        ok, _ = safety.check_read("living_room.ceiling_light")
        assert ok

    def test_read_blocked_device(self, safety):
        ok, reason = safety.check_read("front_door.lock")
        assert not ok
        assert "blocked" in reason

    def test_read_unknown_device(self, safety):
        ok, _ = safety.check_read("nonexistent")
        assert not ok

    def test_write_allowed(self, safety):
        ok, _ = safety.check_write("living_room.ceiling_light", "on")
        assert ok

    def test_write_read_only_blocked(self, safety):
        ok, reason = safety.check_write("living_room.temperature", "set", 25)
        assert not ok
        assert "read-only" in reason

    def test_write_blocked_device(self, safety):
        ok, reason = safety.check_write("front_door.lock", "unlock")
        assert not ok
        assert "blocked" in reason

    def test_write_blocked_capability(self, safety):
        # lock capability is in blocked list
        ok, reason = safety.check_write("front_door.lock", "unlock")
        assert not ok

    def test_write_confirm_required(self, safety):
        ok, reason = safety.check_write("garage.door", "open")
        assert not ok
        assert reason.startswith("CONFIRM:")

    def test_rate_limiting(self, safety):
        # First write should pass
        ok1, _ = safety.check_write("living_room.ceiling_light", "on")
        assert ok1
        safety.record_write("living_room.ceiling_light")

        # Immediate second write should be rate-limited (cooldown)
        ok2, reason = safety.check_write("living_room.ceiling_light", "off")
        assert not ok2
        assert "Cooldown" in reason

    def test_confirmation_workflow(self, safety):
        req = safety.request_confirmation("conf_1", "garage.door", "open")
        assert req["status"] == "pending"

        pending = safety.list_pending_confirmations()
        assert len(pending) == 1

        approved = safety.approve_confirmation("conf_1")
        assert approved is not None
        assert approved["status"] == "approved"

        assert len(safety.list_pending_confirmations()) == 0

    def test_stats(self, safety):
        safety.check_read("living_room.ceiling_light")
        safety.check_write("front_door.lock", "unlock")
        stats = safety.stats
        assert stats["checks"] == 2
        assert stats["blocked"] >= 1


# ====================================================================
# Scene Engine Tests
# ====================================================================

class TestSceneEngine:
    def test_load_scenes(self, scenes):
        assert len(scenes.scenes) == 1
        assert "movie_mode" in scenes.scenes

    def test_load_rules(self, scenes):
        assert len(scenes.rules) == 1
        assert "auto_cooling" in scenes.rules

    def test_list_scenes(self, scenes):
        result = scenes.list_scenes()
        assert len(result) == 1
        assert result[0]["name"] == "movie_mode"
        assert result[0]["action_count"] == 2

    def test_get_scene_actions(self, scenes):
        actions = scenes.get_scene_actions("movie_mode")
        assert len(actions) == 2
        assert actions[0]["device"] == "living_room.ceiling_light"

    def test_add_scene(self, scenes):
        scenes.add_scene("test", "Test Scene", [
            {"device": "living_room.fan", "action": "on"},
        ])
        assert "test" in scenes.scenes
        assert len(scenes.list_scenes()) == 2

    def test_remove_scene(self, scenes):
        assert scenes.remove_scene("movie_mode")
        assert not scenes.remove_scene("nonexistent")

    def test_evaluate_rules(self, scenes):
        # Temperature above threshold -> rule triggers
        triggered = scenes.evaluate_rules({"living_room.temperature": 30})
        assert len(triggered) == 1
        assert triggered[0][0].name == "auto_cooling"

        # Below threshold -> no trigger
        triggered = scenes.evaluate_rules({"living_room.temperature": 22})
        assert len(triggered) == 0

    def test_rule_cooldown(self, scenes):
        # First trigger
        triggered = scenes.evaluate_rules({"living_room.temperature": 30})
        assert len(triggered) == 1

        # Immediate re-evaluation -> cooldown blocks
        triggered = scenes.evaluate_rules({"living_room.temperature": 30})
        assert len(triggered) == 0

    def test_add_rule(self, scenes):
        scenes.add_rule(
            "test_rule", "Test Rule",
            condition={"device": "living_room.fan", "operator": "==", "value": True},
            actions=[{"device": "living_room.ceiling_light", "action": "off"}],
        )
        assert "test_rule" in scenes.rules

    def test_condition_operators(self):
        assert SceneEngine._check_condition(30, ">", 28)
        assert not SceneEngine._check_condition(25, ">", 28)
        assert SceneEngine._check_condition(20, "<", 25)
        assert SceneEngine._check_condition(28, ">=", 28)
        assert SceneEngine._check_condition(28, "<=", 28)
        assert SceneEngine._check_condition(5, "==", 5)
        assert SceneEngine._check_condition(5, "!=", 6)


# ====================================================================
# Tool Generator Tests
# ====================================================================

class TestToolGenerator:
    def test_openai_format(self, spaces):
        gen = ToolGenerator(spaces)
        tools = gen.openai_tools()
        assert len(tools) >= 8
        assert all(t["type"] == "function" for t in tools)
        assert all("function" in t for t in tools)

    def test_anthropic_format(self, spaces):
        gen = ToolGenerator(spaces)
        tools = gen.anthropic_tools()
        assert len(tools) >= 8
        assert all("input_schema" in t for t in tools)

    def test_mcp_format(self, spaces):
        gen = ToolGenerator(spaces)
        tools = gen.mcp_tools()
        assert len(tools) >= 8
        assert all("inputSchema" in t for t in tools)


# ====================================================================
# Tool Executor Tests
# ====================================================================

class TestToolExecutor:
    async def test_list_spaces(self, executor):
        result = await executor.call("list_spaces", {})
        assert "spaces" in result
        assert len(result["spaces"]) == 3

    async def test_list_devices(self, executor):
        result = await executor.call("list_devices", {"space": "living_room"})
        assert len(result["devices"]) == 3

    async def test_get_device_state(self, executor):
        result = await executor.call("get_device_state", {"device": "living_room.ceiling_light"})
        assert result["device"] == "living_room.ceiling_light"
        assert result["state"] is True

    async def test_get_state_blocked_device(self, executor):
        result = await executor.call("get_device_state", {"device": "front_door.lock"})
        assert "error" in result

    async def test_set_device(self, executor):
        result = await executor.call("set_device", {
            "device": "living_room.ceiling_light",
            "action": "on",
        })
        assert result["status"] == "succeeded"

    async def test_set_read_only_fails(self, executor):
        result = await executor.call("set_device", {
            "device": "living_room.temperature",
            "action": "set",
            "value": 25,
        })
        assert "error" in result

    async def test_set_confirm_required(self, executor):
        result = await executor.call("set_device", {
            "device": "garage.door",
            "action": "open",
        })
        assert result["status"] == "confirmation_required"
        assert "confirmation_id" in result

    async def test_activate_scene(self, executor):
        result = await executor.call("activate_scene", {"scene": "movie_mode"})
        assert result["status"] == "completed"
        assert len(result["results"]) == 2

    async def test_create_scene(self, executor):
        result = await executor.call("create_scene", {
            "name": "test_scene",
            "display_name": "Test",
            "actions": [{"device": "living_room.fan", "action": "on"}],
        })
        assert result["status"] == "created"

    async def test_unknown_tool(self, executor):
        result = await executor.call("nonexistent_tool", {})
        assert "error" in result

    async def test_get_space_summary(self, executor):
        result = await executor.call("get_space_summary", {"space": "living_room"})
        assert result["space"] == "living_room"
        assert len(result["devices"]) == 3


# ====================================================================
# MCP Server Tests
# ====================================================================

class TestMCPServer:
    @pytest.fixture
    def mcp(self, spaces, scenes, safety, executor):
        return MCPServer(spaces, safety, scenes, executor)

    async def test_initialize(self, mcp):
        resp = await mcp.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert resp["result"]["serverInfo"]["name"] == "smartspaces"

    async def test_tools_list(self, mcp):
        resp = await mcp.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        tools = resp["result"]["tools"]
        assert len(tools) >= 8
        names = {t["name"] for t in tools}
        assert "list_devices" in names
        assert "set_device" in names

    async def test_tools_call(self, mcp):
        resp = await mcp.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_spaces", "arguments": {}},
        })
        content = resp["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"

    async def test_resources_list(self, mcp):
        resp = await mcp.handle_message({
            "jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {},
        })
        resources = resp["result"]["resources"]
        assert len(resources) == 3

    async def test_resources_read_devices(self, mcp):
        resp = await mcp.handle_message({
            "jsonrpc": "2.0", "id": 5, "method": "resources/read",
            "params": {"uri": "smartspaces://devices"},
        })
        text = resp["result"]["contents"][0]["text"]
        assert "Living Room" in text

    async def test_unknown_method(self, mcp):
        resp = await mcp.handle_message({
            "jsonrpc": "2.0", "id": 6, "method": "unknown/method", "params": {},
        })
        assert "error" in resp
