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
import time
import uuid
from typing import Any

from agent.safety import AISafetyGuard
from agent.scenes import SceneEngine
from agent.spaces import SpaceRegistry

# Type-only import guard for optional components
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.groups import GroupRegistry
    from agent.history import ActionHistory
    from agent.agent_scheduler import ActionScheduler
    from agent.analytics import EnergyComfortAnalyzer
    from agent.coordination import DeviceCoordinator
    from agent.intent import IntentResolver
    from agent.suggestions import ActionSuggester
    from agent.discovery import CapabilityDescriber

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
    {
        "name": "resolve_intent",
        "description": "Resolve a natural language request into device actions. Handles fuzzy commands like 'make it cooler' or 'dim the lights a bit'.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Natural language request (e.g. 'turn off all the lights', 'make it cooler in here').",
                },
                "execute": {
                    "type": "boolean",
                    "description": "If true, execute the resolved actions immediately. If false, just return the plan. Default false.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_groups",
        "description": "List all device groups (e.g. 'all lights', 'upstairs', 'energy consumers').",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "set_group",
        "description": "Apply an action to all devices in a group. Example: turn off all lights.",
        "parameters": {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "description": "Group name (e.g. 'all_lights', 'all_living_room').",
                },
                "action": {
                    "type": "string",
                    "enum": ["on", "off", "toggle", "set"],
                    "description": "Action to apply to all devices in the group.",
                },
                "value": {
                    "description": "Optional value (brightness, temperature, etc.).",
                },
            },
            "required": ["group", "action"],
        },
    },
    {
        "name": "get_history",
        "description": "Get recent action history. Shows what happened recently — useful for context and avoiding redundant actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "Filter by device name. Optional.",
                },
                "space": {
                    "type": "string",
                    "description": "Filter by space. Optional.",
                },
                "minutes": {
                    "type": "integer",
                    "description": "Look back this many minutes. Default 30.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results. Default 20.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "schedule_action",
        "description": "Schedule a device action or scene for a future time. Supports delays ('in 30 minutes') and absolute times.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device to control. Optional if scene is set."},
                "action": {"type": "string", "description": "Action to perform (on/off/set/toggle)."},
                "value": {"description": "Optional value."},
                "scene": {"type": "string", "description": "Scene to activate instead of device action. Optional."},
                "delay_seconds": {"type": "number", "description": "Run after this many seconds."},
                "execute_at": {"type": "number", "description": "Unix timestamp to run at."},
            },
            "required": [],
        },
    },
    {
        "name": "list_schedules",
        "description": "List all scheduled actions (pending, completed, cancelled).",
        "parameters": {
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "description": "Only show pending/running schedules. Default false."},
            },
            "required": [],
        },
    },
    {
        "name": "cancel_schedule",
        "description": "Cancel a scheduled action.",
        "parameters": {
            "type": "object",
            "properties": {
                "schedule_id": {"type": "string", "description": "The schedule ID to cancel."},
            },
            "required": ["schedule_id"],
        },
    },
    {
        "name": "get_analytics",
        "description": "Get energy consumption and comfort analytics — power usage, temperatures, comfort score.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "acquire_lock",
        "description": "Acquire exclusive write access to a device (for multi-agent coordination). Prevents other agents from controlling the device.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device to lock."},
                "agent_id": {"type": "string", "description": "Your agent identifier."},
                "duration": {"type": "number", "description": "Lease duration in seconds (5-300). Default 30."},
                "reason": {"type": "string", "description": "Why you need exclusive access."},
            },
            "required": ["device", "agent_id"],
        },
    },
    {
        "name": "release_lock",
        "description": "Release exclusive access to a device.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device to unlock."},
                "agent_id": {"type": "string", "description": "Your agent identifier."},
            },
            "required": ["device", "agent_id"],
        },
    },
    {
        "name": "get_suggestions",
        "description": "Get proactive suggestions based on current state, time of day, and context. Helps agents be proactive.",
        "parameters": {
            "type": "object",
            "properties": {
                "max_suggestions": {"type": "integer", "description": "Maximum suggestions to return. Default 5."},
            },
            "required": [],
        },
    },
    {
        "name": "describe_device",
        "description": "Get a detailed natural language description of a device — what it can do, its current state, and constraints.",
        "parameters": {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device name to describe."},
            },
            "required": ["device"],
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

        # Optional advanced components (set after construction)
        self.groups: Any = None          # GroupRegistry
        self.history: Any = None         # ActionHistory
        self.scheduler: Any = None       # ActionScheduler
        self.analytics: Any = None       # EnergyComfortAnalyzer
        self.coordinator: Any = None     # DeviceCoordinator
        self.intent_resolver: Any = None # IntentResolver
        self.suggester: Any = None       # ActionSuggester
        self.describer: Any = None       # CapabilityDescriber

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

    async def _tool_resolve_intent(self, args: dict) -> dict:
        text = args.get("text", "")
        execute = args.get("execute", False)

        if not self.intent_resolver:
            return {"error": "Intent resolver not configured"}

        resolved = self.intent_resolver.resolve(text)
        result = resolved.to_dict()

        if execute and resolved.tool_calls:
            execution_results = []
            for tc in resolved.tool_calls:
                tool_name = tc.get("tool", "")
                tool_args = tc.get("args", {})
                res = await self.call(tool_name, tool_args)
                execution_results.append({"tool": tool_name, "result": res})
            result["execution_results"] = execution_results

        return result

    async def _tool_list_groups(self, args: dict) -> dict:
        if not self.groups:
            return {"error": "Groups not configured"}
        return {"groups": self.groups.list_groups()}

    async def _tool_set_group(self, args: dict) -> dict:
        if not self.groups:
            return {"error": "Groups not configured"}

        group_name = args.get("group", "")
        action = args.get("action", "off")
        value = args.get("value")

        members = self.groups.get_writable_members(group_name)
        if not members:
            return {"error": f"Unknown group or no writable devices: {group_name}"}

        results = []
        for member in members:
            call_args: dict[str, Any] = {"device": member.semantic_name, "action": action}
            if value is not None:
                call_args["value"] = value
            result = await self._tool_set_device(call_args)
            results.append(result)

        succeeded = sum(1 for r in results if r.get("status") == "succeeded")
        return {
            "group": group_name,
            "action": action,
            "total_devices": len(members),
            "succeeded": succeeded,
            "failed": len(members) - succeeded,
            "results": results,
        }

    async def _tool_get_history(self, args: dict) -> dict:
        if not self.history:
            return {"error": "History not configured"}

        minutes = args.get("minutes", 30)
        return {
            "history": self.history.query(
                device=args.get("device"),
                space=args.get("space"),
                since=time.time() - (minutes * 60),
                limit=args.get("limit", 20),
            ),
        }

    async def _tool_schedule_action(self, args: dict) -> dict:
        if not self.scheduler:
            return {"error": "Scheduler not configured"}

        delay = args.get("delay_seconds")
        execute_at = args.get("execute_at")

        kwargs: dict[str, Any] = {
            "device": args.get("device"),
            "action": args.get("action"),
            "value": args.get("value"),
            "scene": args.get("scene"),
            "description": args.get("description", ""),
        }

        if delay:
            sched = await self.scheduler.schedule_delay(delay, **kwargs)
        elif execute_at:
            sched = await self.scheduler.schedule_at(execute_at, **kwargs)
        else:
            return {"error": "Either delay_seconds or execute_at is required"}

        return sched.to_dict()

    async def _tool_list_schedules(self, args: dict) -> dict:
        if not self.scheduler:
            return {"error": "Scheduler not configured"}
        active_only = args.get("active_only", False)
        return {"schedules": self.scheduler.list_schedules(active_only=active_only)}

    async def _tool_cancel_schedule(self, args: dict) -> dict:
        if not self.scheduler:
            return {"error": "Scheduler not configured"}
        schedule_id = args.get("schedule_id", "")
        cancelled = await self.scheduler.cancel(schedule_id)
        return {"cancelled": cancelled, "schedule_id": schedule_id}

    async def _tool_get_analytics(self, args: dict) -> dict:
        if not self.analytics:
            return {"error": "Analytics not configured"}
        return self.analytics.compute().to_dict()

    async def _tool_acquire_lock(self, args: dict) -> dict:
        if not self.coordinator:
            return {"error": "Coordinator not configured"}
        device = args.get("device", "")
        agent_id = args.get("agent_id", "")
        duration = args.get("duration", 30.0)
        reason = args.get("reason", "")

        lease = await self.coordinator.acquire(device, agent_id, duration, reason=reason)
        if not lease:
            existing = self.coordinator.get_lease(device)
            return {
                "acquired": False,
                "reason": f"Device leased by agent '{existing.agent_id}'" if existing else "Failed",
            }
        return {"acquired": True, "lease": lease.to_dict()}

    async def _tool_release_lock(self, args: dict) -> dict:
        if not self.coordinator:
            return {"error": "Coordinator not configured"}
        device = args.get("device", "")
        agent_id = args.get("agent_id", "")
        released = await self.coordinator.release_device(device, agent_id)
        return {"released": released}

    async def _tool_get_suggestions(self, args: dict) -> dict:
        if not self.suggester:
            return {"error": "Suggester not configured"}
        max_s = args.get("max_suggestions", 5)
        return {"suggestions": self.suggester.suggest(max_suggestions=max_s)}

    async def _tool_describe_device(self, args: dict) -> dict:
        if not self.describer:
            return {"error": "Describer not configured"}
        device = args.get("device", "")
        return self.describer.describe(device)
