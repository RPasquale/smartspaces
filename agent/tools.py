"""LLM Tool Definitions — auto-generates function-calling schemas from the space registry.

Produces tool definitions compatible with:
- OpenAI function calling (tools format)
- Anthropic Claude tool use
- MCP (Model Context Protocol)

Also provides an executor that routes tool calls through the safety
guard and adapter registry.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from agent.safety import AISafetyGuard
from agent.scenes import SceneEngine
from agent.spaces import SpaceRegistry

logger = logging.getLogger(__name__)


# -- Tool definitions (schema) --

TOOL_DEFINITIONS = [
    {
        "name": "list_spaces",
        "description": "List all spaces (rooms/zones) and their devices in the smart space.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_devices",
        "description": "List available devices, optionally filtered by space or capability.",
        "parameters": {
            "type": "object",
            "properties": {
                "space": {
                    "type": "string",
                    "description": "Filter by space name (e.g. 'living_room', 'kitchen'). Optional.",
                },
                "capability": {
                    "type": "string",
                    "description": "Filter by capability (e.g. 'binary_switch', 'dimmer', 'temperature_sensor'). Optional.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_device_state",
        "description": "Read the current state/value of a device. Returns the live value from the physical device.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "Device name (e.g. 'living_room.ceiling_light', 'kitchen.temperature').",
                },
            },
            "required": ["device"],
        },
    },
    {
        "name": "set_device",
        "description": "Control a device — turn on/off, set brightness, change temperature setpoint, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "Device name (e.g. 'living_room.ceiling_light').",
                },
                "action": {
                    "type": "string",
                    "enum": ["on", "off", "toggle", "set", "open", "close"],
                    "description": "Action to perform.",
                },
                "value": {
                    "description": "Optional value (brightness 0-100, temperature, etc.).",
                },
            },
            "required": ["device", "action"],
        },
    },
    {
        "name": "list_scenes",
        "description": "List available scenes (multi-device presets like 'movie_mode', 'goodnight').",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "activate_scene",
        "description": "Activate a named scene, which sets multiple devices at once.",
        "parameters": {
            "type": "object",
            "properties": {
                "scene": {
                    "type": "string",
                    "description": "Scene name (e.g. 'movie_mode', 'goodnight').",
                },
            },
            "required": ["scene"],
        },
    },
    {
        "name": "create_scene",
        "description": "Create a new scene with multiple device actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Scene name (snake_case, e.g. 'morning_routine').",
                },
                "display_name": {
                    "type": "string",
                    "description": "Human-readable name.",
                },
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "device": {"type": "string"},
                            "action": {"type": "string"},
                            "value": {},
                        },
                        "required": ["device", "action"],
                    },
                    "description": "List of device actions.",
                },
            },
            "required": ["name", "display_name", "actions"],
        },
    },
    {
        "name": "get_space_summary",
        "description": "Get a summary of all device states in a space. Useful for understanding current conditions.",
        "parameters": {
            "type": "object",
            "properties": {
                "space": {
                    "type": "string",
                    "description": "Space name (e.g. 'living_room').",
                },
            },
            "required": ["space"],
        },
    },
]


class ToolGenerator:
    """Generates and formats tool definitions for various LLM formats."""

    def __init__(self, space_registry: SpaceRegistry):
        self.spaces = space_registry

    def openai_tools(self) -> list[dict[str, Any]]:
        """Generate tool definitions in OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in TOOL_DEFINITIONS
        ]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """Generate tool definitions in Anthropic Claude tool_use format."""
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in TOOL_DEFINITIONS
        ]

    def mcp_tools(self) -> list[dict[str, Any]]:
        """Generate tool definitions in MCP format."""
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["parameters"],
            }
            for t in TOOL_DEFINITIONS
        ]

    def raw_definitions(self) -> list[dict[str, Any]]:
        """Return raw tool definitions."""
        return list(TOOL_DEFINITIONS)


class ToolExecutor:
    """Executes tool calls from AI agents.

    Routes through safety guard and adapter registry.
    """

    def __init__(
        self,
        space_registry: SpaceRegistry,
        safety_guard: AISafetyGuard,
        scene_engine: SceneEngine,
        read_fn: Any = None,    # async (connection_id, point_id) -> dict
        execute_fn: Any = None,  # async (connection_id, command) -> dict
    ):
        self.spaces = space_registry
        self.safety = safety_guard
        self.scenes = scene_engine
        self._read_fn = read_fn
        self._execute_fn = execute_fn

    def set_adapter_fns(self, read_fn: Any, execute_fn: Any) -> None:
        """Wire up the adapter registry functions."""
        self._read_fn = read_fn
        self._execute_fn = execute_fn

    async def call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool call and return the result."""
        handler = getattr(self, f"_tool_{tool_name}", None)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return await handler(args)
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return {"error": str(e)}

    async def _tool_list_spaces(self, args: dict) -> dict:
        return {"spaces": self.spaces.list_spaces()}

    async def _tool_list_devices(self, args: dict) -> dict:
        return {
            "devices": self.spaces.list_devices(
                space=args.get("space"),
                capability=args.get("capability"),
            )
        }

    async def _tool_get_device_state(self, args: dict) -> dict:
        device_name = args.get("device", "")
        ok, reason = self.safety.check_read(device_name)
        if not ok:
            return {"error": reason}

        mapping = self.spaces.resolve_name(device_name)
        if not mapping:
            return {"error": f"Unknown device: {device_name}"}

        if not self._read_fn or not mapping.connection_id:
            return {
                "device": mapping.semantic_name,
                "state": "unknown",
                "note": "No live connection available",
            }

        result = await self._read_fn(mapping.connection_id, mapping.point_id)
        return {
            "device": mapping.semantic_name,
            "display_name": mapping.display_name,
            "state": result.get("value"),
            "quality": result.get("quality"),
            "unit": mapping.unit,
        }

    async def _tool_set_device(self, args: dict) -> dict:
        device_name = args.get("device", "")
        action = args.get("action", "set")
        value = args.get("value")

        ok, reason = self.safety.check_write(device_name, action, value)
        if not ok:
            if reason.startswith("CONFIRM:"):
                conf_id = f"conf_{uuid.uuid4().hex[:8]}"
                self.safety.request_confirmation(conf_id, device_name, action, value)
                return {
                    "status": "confirmation_required",
                    "confirmation_id": conf_id,
                    "message": reason,
                    "device": device_name,
                    "action": action,
                }
            return {"error": reason}

        mapping = self.spaces.resolve_name(device_name)
        if not mapping:
            return {"error": f"Unknown device: {device_name}"}

        if not self._execute_fn or not mapping.connection_id:
            return {"error": "No live connection available"}

        # Build command
        params = {"value": value} if value is not None else {}
        if action == "on":
            params["value"] = True
        elif action == "off":
            params["value"] = False

        command = {
            "command_id": f"ai_cmd_{uuid.uuid4().hex[:8]}",
            "target": {
                "endpoint_id": mapping.endpoint_id,
                "device_id": mapping.device_id,
            },
            "capability": mapping.capabilities[0] if mapping.capabilities else "binary_switch",
            "verb": action,
            "params": params,
            "context": {"initiator": "ai_agent"},
        }

        result = await self._execute_fn(mapping.connection_id, command)
        self.safety.record_write(device_name)

        response = {
            "device": mapping.semantic_name,
            "action": action,
            "status": result.get("status", "unknown"),
        }

        # Readback if configured
        if self.safety.config.require_readback and self._read_fn:
            try:
                readback = await self._read_fn(mapping.connection_id, mapping.point_id)
                response["confirmed_state"] = readback.get("value")
            except Exception:
                response["confirmed_state"] = "readback_failed"

        return response

    async def _tool_list_scenes(self, args: dict) -> dict:
        return {"scenes": self.scenes.list_scenes()}

    async def _tool_activate_scene(self, args: dict) -> dict:
        scene_name = args.get("scene", "")
        actions = self.scenes.get_scene_actions(scene_name)
        if not actions:
            return {"error": f"Unknown scene: {scene_name}"}

        results = []
        for action in actions:
            result = await self._tool_set_device({
                "device": action["device"],
                "action": action["action"],
                "value": action.get("value"),
            })
            results.append(result)

        return {
            "scene": scene_name,
            "results": results,
            "status": "completed",
        }

    async def _tool_create_scene(self, args: dict) -> dict:
        name = args.get("name", "")
        display_name = args.get("display_name", name)
        actions = args.get("actions", [])

        # Validate all device names
        for a in actions:
            mapping = self.spaces.resolve_name(a.get("device", ""))
            if not mapping:
                return {"error": f"Unknown device in scene: {a.get('device')}"}

        scene = self.scenes.add_scene(name, display_name, actions)
        return {
            "status": "created",
            "scene": name,
            "display_name": display_name,
            "action_count": len(scene.actions),
        }

    async def _tool_get_space_summary(self, args: dict) -> dict:
        space_name = args.get("space", "")
        devices = self.spaces.list_devices(space=space_name)
        if not devices:
            return {"error": f"Unknown space or no devices: {space_name}"}

        summary = []
        for dev in devices:
            entry = {
                "name": dev["name"],
                "display_name": dev["display_name"],
                "capabilities": dev["capabilities"],
            }
            # Try to read state if connected
            if self._read_fn and dev.get("connection_id") and dev.get("point_id"):
                try:
                    result = await self._read_fn(dev["connection_id"], dev["point_id"])
                    entry["state"] = result.get("value")
                    entry["quality"] = result.get("quality", {}).get("status")
                except Exception:
                    entry["state"] = "unavailable"
            else:
                entry["state"] = "no_connection"

            summary.append(entry)

        return {"space": space_name, "devices": summary}
