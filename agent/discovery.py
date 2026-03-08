"""Device Capability Discovery — natural language device descriptions.

Auto-generates per-device descriptions that tell AI agents exactly
what each device can do, its current state, and its constraints.
Useful for injecting into system prompts or returning as tool results.

Usage:
    describer = CapabilityDescriber(space_registry)
    desc = describer.describe("living_room.ceiling_light", current_state=True)
    # "Living Room Ceiling Light is an on/off switch. Currently ON. AI has full control."
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Capability → natural language description templates
_CAPABILITY_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    "binary_switch": {
        "type": "on/off switch",
        "actions": ["on", "off", "toggle"],
        "description": "Can be turned on or off.",
    },
    "dimmer": {
        "type": "dimmable light",
        "actions": ["on", "off", "set"],
        "value_range": "0-100%",
        "description": "Brightness can be set from 0% (off) to 100% (full brightness).",
    },
    "light_color": {
        "type": "color light",
        "actions": ["on", "off", "set"],
        "description": "Supports color and brightness control.",
    },
    "temperature_sensor": {
        "type": "temperature sensor",
        "actions": [],
        "read_only": True,
        "description": "Reads the current temperature. Cannot be controlled — read-only.",
    },
    "binary_sensor": {
        "type": "binary sensor",
        "actions": [],
        "read_only": True,
        "description": "Detects on/off or open/closed state. Read-only.",
    },
    "fan": {
        "type": "fan",
        "actions": ["on", "off", "toggle"],
        "description": "Can be turned on or off.",
    },
    "cover": {
        "type": "motorized cover/blind",
        "actions": ["open", "close", "set"],
        "value_range": "0-100% (0=closed, 100=open)",
        "description": "Can be opened, closed, or set to a specific position.",
    },
    "lock": {
        "type": "smart lock",
        "actions": ["lock", "unlock"],
        "description": "Can be locked or unlocked. Typically requires human confirmation.",
    },
    "door_lock": {
        "type": "door lock",
        "actions": ["lock", "unlock"],
        "description": "Controls a door lock. Usually blocked for AI agents.",
    },
    "thermostat": {
        "type": "thermostat",
        "actions": ["set"],
        "value_range": "temperature in °C",
        "description": "Controls heating/cooling setpoint temperature.",
    },
    "climate_setpoint": {
        "type": "climate control setpoint",
        "actions": ["set"],
        "value_range": "temperature in °C",
        "description": "Sets the target temperature for climate control.",
    },
    "meter_power": {
        "type": "power meter",
        "actions": [],
        "read_only": True,
        "description": "Measures current power consumption in watts. Read-only.",
    },
    "meter_energy": {
        "type": "energy meter",
        "actions": [],
        "read_only": True,
        "description": "Measures accumulated energy usage in kWh. Read-only.",
    },
    "camera_stream": {
        "type": "camera",
        "actions": [],
        "read_only": True,
        "description": "Provides a video stream. Read-only.",
    },
}

_ACCESS_DESCRIPTIONS = {
    "full": "AI has full control (read and write).",
    "read_only": "AI can read but cannot control this device.",
    "confirm_required": "AI can request control but needs human approval.",
    "blocked": "AI access is blocked. This device cannot be read or controlled by AI.",
}

_SAFETY_DESCRIPTIONS = {
    "S0": "Monitoring only — no actuation.",
    "S1": "Standard actuator — safe for automated control.",
    "S2": "Safety-relevant — extra caution advised.",
    "S3": "Requires human confirmation for every operation.",
    "S4": "Critical infrastructure — human confirmation required.",
    "S5": "Life-safety — AI access prohibited.",
}


class CapabilityDescriber:
    """Generates natural language descriptions of device capabilities."""

    def __init__(self, space_registry: Any, analytics: Any | None = None):
        self._spaces = space_registry
        self._analytics = analytics

    def describe(
        self,
        device_name: str,
        include_state: bool = True,
        include_constraints: bool = True,
    ) -> dict[str, Any]:
        """Generate a full description of a device.

        Returns a dict with structured info and a natural language summary.
        """
        mapping = self._spaces.resolve_name(device_name)
        if not mapping:
            return {"error": f"Unknown device: {device_name}"}

        # Build capability info
        cap_info = self._describe_capabilities(mapping.capabilities)

        # Current state
        state_info = None
        state_str = "unknown"
        if include_state and self._analytics:
            device_state = self._analytics._states.get(mapping.semantic_name)
            if device_state and device_state.value is not None:
                state_info = {
                    "value": device_state.value,
                    "updated_at": device_state.updated_at,
                }
                state_str = self._format_state(device_state.value, mapping.capabilities)

        # Access constraints
        access_desc = _ACCESS_DESCRIPTIONS.get(mapping.ai_access, "Unknown access level.")
        safety_desc = _SAFETY_DESCRIPTIONS.get(mapping.safety_class, "")

        # Build natural language summary
        summary_parts = [f"**{mapping.display_name}**"]
        summary_parts.append(f"is a {cap_info['type']}.")

        if include_state and state_str != "unknown":
            summary_parts.append(f"Currently {state_str}.")

        if cap_info.get("value_range"):
            summary_parts.append(f"Value range: {cap_info['value_range']}.")

        if cap_info["actions"]:
            summary_parts.append(f"Available actions: {', '.join(cap_info['actions'])}.")
        elif cap_info.get("read_only"):
            summary_parts.append("Read-only — no actions available.")

        if include_constraints:
            summary_parts.append(access_desc)

        if mapping.unit:
            summary_parts.append(f"Unit: {mapping.unit}.")

        return {
            "device": mapping.semantic_name,
            "display_name": mapping.display_name,
            "space": mapping.space,
            "capabilities": mapping.capabilities,
            "type": cap_info["type"],
            "available_actions": cap_info["actions"],
            "value_range": cap_info.get("value_range"),
            "is_read_only": cap_info.get("read_only", False),
            "ai_access": mapping.ai_access,
            "safety_class": mapping.safety_class,
            "unit": mapping.unit,
            "current_state": state_info,
            "access_description": access_desc,
            "safety_description": safety_desc,
            "summary": " ".join(summary_parts),
        }

    def describe_all(self, space: str | None = None) -> list[dict[str, Any]]:
        """Generate descriptions for all devices, optionally in a specific space."""
        results = []
        for mapping in self._spaces._by_semantic.values():
            if space and mapping.space != space:
                continue
            results.append(self.describe(mapping.semantic_name))
        return results

    def to_context_prompt(self, space: str | None = None) -> str:
        """Generate device capability context for LLM system prompt."""
        descriptions = self.describe_all(space=space)
        if not descriptions:
            return "No devices available."

        lines = ["# Device Capabilities"]
        current_space = None

        for desc in descriptions:
            if desc.get("error"):
                continue
            if desc["space"] != current_space:
                current_space = desc["space"]
                space_obj = self._spaces.spaces.get(current_space)
                space_display = space_obj.display_name if space_obj else current_space
                lines.append(f"\n## {space_display}")

            lines.append(f"- {desc['summary']}")

        return "\n".join(lines)

    def _describe_capabilities(self, capabilities: list[str]) -> dict[str, Any]:
        """Build a merged description from a list of capabilities."""
        if not capabilities:
            return {"type": "unknown device", "actions": [], "description": "No capabilities defined."}

        # Use the first recognized capability as primary type
        primary = None
        all_actions: list[str] = []
        value_range = None
        is_read_only = True
        descriptions = []

        for cap in capabilities:
            info = _CAPABILITY_DESCRIPTIONS.get(cap, {})
            if info:
                if not primary:
                    primary = info.get("type", cap)
                actions = info.get("actions", [])
                if actions:
                    is_read_only = False
                    for a in actions:
                        if a not in all_actions:
                            all_actions.append(a)
                if info.get("value_range") and not value_range:
                    value_range = info["value_range"]
                if info.get("description"):
                    descriptions.append(info["description"])
            else:
                if not primary:
                    primary = cap.replace("_", " ")

        result: dict[str, Any] = {
            "type": primary or "device",
            "actions": all_actions,
            "description": " ".join(descriptions),
        }
        if value_range:
            result["value_range"] = value_range
        if is_read_only:
            result["read_only"] = True

        return result

    def _format_state(self, value: Any, capabilities: list[str]) -> str:
        """Format a device state value for display."""
        if isinstance(value, bool):
            return "ON" if value else "OFF"
        if isinstance(value, (int, float)):
            # Temperature
            if "temperature_sensor" in capabilities:
                return f"{value:.1f}°C"
            # Dimmer level
            if "dimmer" in capabilities:
                return f"{value}% brightness"
            # Cover position
            if "cover" in capabilities:
                return f"{value}% open"
            return str(value)
        if isinstance(value, str):
            return value.upper() if value.lower() in ("on", "off") else value
        return str(value)
