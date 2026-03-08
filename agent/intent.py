"""Natural Language Intent Resolver — maps fuzzy requests to tool calls.

Converts natural language like "make it cooler" or "dim the lights a bit"
into structured tool calls without requiring an external LLM. Uses
domain-specific pattern matching with the space registry as a gazetteer.

Pipeline:
  1. Normalize text (lowercase, expand contractions)
  2. Extract entities (devices, spaces, groups, values, times)
  3. Classify intent (control, query, scene, group, schedule, meta)
  4. Resolve to one or more tool calls

Not a general NLU — optimized specifically for home/building automation commands.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class IntentCategory(str, Enum):
    CONTROL = "control"      # Turn on/off, set value
    QUERY = "query"          # What's the temperature?
    SCENE = "scene"          # Activate movie mode
    GROUP = "group"          # All lights off
    SCHEDULE = "schedule"    # Turn off at 11pm
    META = "meta"            # List devices, list rooms
    ENVIRONMENT = "environment"  # Make it cooler, brighter
    UNKNOWN = "unknown"


@dataclass
class ResolvedIntent:
    """Result of intent resolution — one or more tool calls."""
    category: IntentCategory
    confidence: float
    tool_calls: list[dict[str, Any]]
    explanation: str
    original_text: str
    extracted_entities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "confidence": round(self.confidence, 2),
            "tool_calls": self.tool_calls,
            "explanation": self.explanation,
            "original_text": self.original_text,
            "extracted_entities": self.extracted_entities,
        }


# ── Verb patterns ──

_VERB_ON = [
    "turn on", "switch on", "power on", "enable", "activate",
    "start", "light up", "power up", "fire up",
]
_VERB_OFF = [
    "turn off", "switch off", "power off", "disable", "deactivate",
    "stop", "shut off", "shut down", "kill", "cut", "power down",
]
_VERB_TOGGLE = ["toggle", "flip", "switch"]
_VERB_DIM = [
    "dim", "lower the", "reduce", "decrease", "turn down", "darken",
    "make darker", "bring down",
]
_VERB_BRIGHTEN = [
    "brighten", "raise", "increase", "turn up", "boost",
    "make brighter", "bring up", "more light",
]
_VERB_SET = ["set", "adjust", "change", "put", "make it"]
_VERB_OPEN = ["open", "raise", "lift"]
_VERB_CLOSE = ["close", "shut", "lower", "drop"]
_VERB_QUERY = [
    "what is", "what's", "how is", "how's", "is the", "are the",
    "check", "status of", "tell me", "show me", "get the",
    "what are", "read", "how much", "how many",
]
_VERB_LIST = [
    "list", "show", "what devices", "what rooms", "what spaces",
    "what scenes", "what groups", "what do i have", "enumerate",
]

# Environment intent patterns
_ENV_COOLER = [
    "make it cooler", "cool down", "too hot", "too warm",
    "it's hot", "it's warm", "feels hot", "feels warm",
    "need cooling", "cooler in", "cool off",
]
_ENV_WARMER = [
    "make it warmer", "heat up", "warm up", "too cold",
    "too cool", "it's cold", "it's cool", "feels cold",
    "need heating", "warmer in",
]
_ENV_BRIGHTER = [
    "make it brighter", "too dark", "more light", "need more light",
    "it's dark", "can't see", "lighten up", "brighter in",
]
_ENV_DARKER = [
    "make it darker", "too bright", "less light", "need less light",
    "it's too bright", "dim everything", "darker in",
]

# Value qualifiers
_QUALIFIER_SMALL = ["a little", "a bit", "slightly", "a touch", "somewhat"]
_QUALIFIER_LARGE = ["a lot", "much", "significantly", "way", "drastically"]
_QUALIFIER_MAX = ["completely", "fully", "all the way", "maximum", "max", "100"]
_QUALIFIER_MIN = ["minimum", "min", "lowest", "off"]
_QUALIFIER_HALF = ["halfway", "half", "50", "medium", "mid"]

# Schedule time patterns
_TIME_PATTERN = re.compile(
    r'(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM)?'
    r'|in\s+(\d+)\s*(second|minute|hour|min|sec|hr)s?'
    r'|(?:after|wait)\s+(\d+)\s*(second|minute|hour|min|sec|hr)s?',
    re.IGNORECASE,
)

# Value extraction
_VALUE_PATTERN = re.compile(
    r'(?:to\s+)?(\d+)\s*(?:%|percent|degrees?|°[CF]?)?'
    r'|(\d+)\s*(?:%|percent)',
    re.IGNORECASE,
)

# Contractions
_CONTRACTIONS = {
    "can't": "cannot", "won't": "will not", "don't": "do not",
    "it's": "it is", "what's": "what is", "that's": "that is",
    "i'm": "i am", "let's": "let us", "there's": "there is",
    "how's": "how is", "here's": "here is",
}

# Scene activation keywords
_SCENE_TRIGGERS = [
    "activate", "run", "start", "trigger", "do",
    "time for", "mode", "routine",
]


class IntentResolver:
    """Resolves natural language into structured tool calls.

    Uses the SpaceRegistry and optionally GroupRegistry/SceneEngine
    as gazetteers for entity extraction.
    """

    def __init__(
        self,
        space_registry: Any,
        group_registry: Any | None = None,
        scene_engine: Any | None = None,
    ):
        self._spaces = space_registry
        self._groups = group_registry
        self._scenes = scene_engine
        self._build_gazetteers()

    def _build_gazetteers(self) -> None:
        """Pre-build lookup tables from the registries."""
        # Device name gazetteer: all possible ways to refer to a device
        self._device_names: dict[str, str] = {}  # normalized → semantic_name
        for mapping in self._spaces._by_semantic.values():
            sem = mapping.semantic_name
            self._device_names[sem] = sem
            self._device_names[mapping.device_name] = sem
            # "living room light" → "living_room.ceiling_light"
            display_lower = mapping.display_name.lower()
            self._device_names[display_lower] = sem
            # "ceiling light" → "living_room.ceiling_light"
            self._device_names[mapping.device_name.replace("_", " ")] = sem

        # Space name gazetteer
        self._space_names: dict[str, str] = {}
        for space in self._spaces.spaces.values():
            self._space_names[space.name] = space.name
            self._space_names[space.display_name.lower()] = space.name
            self._space_names[space.name.replace("_", " ")] = space.name

        # Scene name gazetteer
        self._scene_names: dict[str, str] = {}
        if self._scenes:
            for scene_name in self._scenes.scenes:
                scene = self._scenes.scenes[scene_name]
                self._scene_names[scene_name] = scene_name
                self._scene_names[scene_name.replace("_", " ")] = scene_name
                self._scene_names[scene.display_name.lower()] = scene_name

        # Group name gazetteer
        self._group_names: dict[str, str] = {}
        if self._groups:
            for group in self._groups.list_groups():
                name = group["name"]
                self._group_names[name] = name
                self._group_names[name.replace("_", " ")] = name
                self._group_names[group["display_name"].lower()] = name

    def resolve(self, text: str) -> ResolvedIntent:
        """Resolve natural language text to structured tool calls."""
        original = text
        text = self._normalize(text)

        entities: dict[str, Any] = {}

        # Extract entities
        devices = self._extract_devices(text)
        spaces = self._extract_spaces(text)
        scene = self._extract_scene(text)
        group = self._extract_group(text)
        value = self._extract_value(text)
        time_info = self._extract_time(text)

        if devices:
            entities["devices"] = devices
        if spaces:
            entities["spaces"] = spaces
        if scene:
            entities["scene"] = scene
        if group:
            entities["group"] = group
        if value is not None:
            entities["value"] = value
        if time_info:
            entities["time"] = time_info

        # Classify intent and build tool calls
        # Priority order: schedule > scene > environment > group > control > query > meta

        if time_info:
            return self._resolve_schedule(text, entities, original, devices, value, scene, time_info)

        if scene:
            return ResolvedIntent(
                category=IntentCategory.SCENE,
                confidence=0.9,
                tool_calls=[{"tool": "activate_scene", "args": {"scene": scene}}],
                explanation=f"Activate scene '{scene}'",
                original_text=original,
                extracted_entities=entities,
            )

        env = self._check_environment(text)
        if env:
            return self._resolve_environment(text, env, entities, original, spaces)

        if self._is_meta(text):
            return self._resolve_meta(text, entities, original)

        if group or self._has_group_words(text):
            return self._resolve_group(text, entities, original, group, value)

        action = self._extract_action(text)

        if self._is_query(text):
            return self._resolve_query(text, entities, original, devices, spaces)

        if action and devices:
            return self._resolve_control(text, entities, original, devices, action, value)

        if action and spaces:
            # Action on a space → treat as group
            return self._resolve_space_action(text, entities, original, spaces[0], action, value)

        # Fallback: try to guess
        if devices:
            return self._resolve_query(text, entities, original, devices, spaces)

        return ResolvedIntent(
            category=IntentCategory.UNKNOWN,
            confidence=0.1,
            tool_calls=[],
            explanation="Could not determine intent. Try: 'turn on living room light' or 'what's the temperature?'",
            original_text=original,
            extracted_entities=entities,
        )

    # ── Normalization ──

    def _normalize(self, text: str) -> str:
        text = text.lower().strip()
        for contraction, expansion in _CONTRACTIONS.items():
            text = text.replace(contraction, expansion)
        text = re.sub(r'[!?.]+$', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text

    # ── Entity extraction ──

    def _extract_devices(self, text: str) -> list[str]:
        """Extract device references from text, longest match first."""
        found = []
        # Sort by length (longest first) to prefer "living room ceiling light" over "light"
        candidates = sorted(self._device_names.keys(), key=len, reverse=True)
        remaining = text
        for name in candidates:
            if name in remaining and len(name) > 2:
                semantic = self._device_names[name]
                if semantic not in found:
                    found.append(semantic)
                    remaining = remaining.replace(name, " ", 1)
        return found

    def _extract_spaces(self, text: str) -> list[str]:
        found = []
        for name, space_key in self._space_names.items():
            if name in text and space_key not in found and len(name) > 2:
                found.append(space_key)
        return found

    def _extract_scene(self, text: str) -> str | None:
        for name, scene_key in self._scene_names.items():
            if name in text:
                return scene_key
        # Check for scene trigger patterns
        for trigger in _SCENE_TRIGGERS:
            if trigger in text:
                # Look for what follows the trigger
                idx = text.index(trigger) + len(trigger)
                remainder = text[idx:].strip()
                for name, scene_key in self._scene_names.items():
                    if name in remainder:
                        return scene_key
        return None

    def _extract_group(self, text: str) -> str | None:
        for name, group_key in self._group_names.items():
            if name in text:
                return group_key
        return None

    def _extract_value(self, text: str) -> int | float | None:
        # Check qualifiers first
        for q in _QUALIFIER_MAX:
            if q in text:
                return 100
        for q in _QUALIFIER_MIN:
            if q in text:
                return 0
        for q in _QUALIFIER_HALF:
            if q in text:
                return 50

        # Check for relative qualifiers
        for q in _QUALIFIER_SMALL:
            if q in text:
                return 10  # Will be used as relative delta
        for q in _QUALIFIER_LARGE:
            if q in text:
                return 30

        # Extract numeric values
        match = _VALUE_PATTERN.search(text)
        if match:
            val_str = match.group(1) or match.group(2)
            if val_str:
                return int(val_str)

        return None

    def _extract_time(self, text: str) -> dict[str, Any] | None:
        # Check for relative time: "in X minutes/hours"
        rel_match = re.search(
            r'in\s+(\d+)\s*(second|minute|hour|min|sec|hr)s?',
            text, re.IGNORECASE,
        )
        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2).lower()
            multiplier = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}
            seconds = amount * multiplier.get(unit, 60)
            return {"type": "delay", "seconds": seconds}

        # Check for absolute time: "at 11pm", "at 7:30"
        abs_match = re.search(
            r'at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?',
            text, re.IGNORECASE,
        )
        if abs_match:
            hour = int(abs_match.group(1))
            minute = int(abs_match.group(2) or 0)
            ampm = abs_match.group(3)
            if ampm:
                ampm = ampm.lower()
                if ampm == "pm" and hour < 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0

            now = time.time()
            from datetime import datetime
            today = datetime.fromtimestamp(now)
            target = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
            target_ts = target.timestamp()

            if target_ts <= now:
                target_ts += 86400  # Tomorrow

            return {"type": "absolute", "timestamp": target_ts, "time_str": f"{hour:02d}:{minute:02d}"}

        return None

    def _extract_action(self, text: str) -> str | None:
        # Check each verb group, longest phrase first
        for verb in sorted(_VERB_ON, key=len, reverse=True):
            if verb in text:
                return "on"
        for verb in sorted(_VERB_OFF, key=len, reverse=True):
            if verb in text:
                return "off"
        for verb in sorted(_VERB_TOGGLE, key=len, reverse=True):
            if verb in text:
                return "toggle"
        for verb in sorted(_VERB_DIM, key=len, reverse=True):
            if verb in text:
                return "dim"
        for verb in sorted(_VERB_BRIGHTEN, key=len, reverse=True):
            if verb in text:
                return "brighten"
        for verb in sorted(_VERB_OPEN, key=len, reverse=True):
            if verb in text:
                return "open"
        for verb in sorted(_VERB_CLOSE, key=len, reverse=True):
            if verb in text:
                return "close"
        for verb in sorted(_VERB_SET, key=len, reverse=True):
            if verb in text:
                return "set"
        return None

    def _check_environment(self, text: str) -> str | None:
        for phrase in _ENV_COOLER:
            if phrase in text:
                return "cooler"
        for phrase in _ENV_WARMER:
            if phrase in text:
                return "warmer"
        for phrase in _ENV_BRIGHTER:
            if phrase in text:
                return "brighter"
        for phrase in _ENV_DARKER:
            if phrase in text:
                return "darker"
        return None

    def _is_query(self, text: str) -> bool:
        return any(v in text for v in _VERB_QUERY)

    def _is_meta(self, text: str) -> bool:
        return any(v in text for v in _VERB_LIST)

    def _has_group_words(self, text: str) -> bool:
        return any(w in text for w in ["all ", "every ", "each ", "everything"])

    # ── Resolution builders ──

    def _resolve_control(
        self, text: str, entities: dict, original: str,
        devices: list[str], action: str, value: int | float | None,
    ) -> ResolvedIntent:
        tool_calls = []
        for device in devices:
            # Map action verbs to set_device actions
            mapped_action = action
            mapped_value = value
            if action == "dim":
                mapped_action = "set"
                if mapped_value is None:
                    mapped_value = 30  # Default dim level
            elif action == "brighten":
                mapped_action = "set"
                if mapped_value is None:
                    mapped_value = 80  # Default bright level

            args: dict[str, Any] = {"device": device, "action": mapped_action}
            if mapped_value is not None:
                args["value"] = mapped_value
            tool_calls.append({"tool": "set_device", "args": args})

        device_names = ", ".join(devices)
        return ResolvedIntent(
            category=IntentCategory.CONTROL,
            confidence=0.85,
            tool_calls=tool_calls,
            explanation=f"Set {device_names} to {action}" + (f" ({value})" if value else ""),
            original_text=original,
            extracted_entities=entities,
        )

    def _resolve_query(
        self, text: str, entities: dict, original: str,
        devices: list[str], spaces: list[str],
    ) -> ResolvedIntent:
        tool_calls = []
        if devices:
            for device in devices:
                tool_calls.append({"tool": "get_device_state", "args": {"device": device}})
        elif spaces:
            for space in spaces:
                tool_calls.append({"tool": "get_space_summary", "args": {"space": space}})
        else:
            # General query — list everything
            tool_calls.append({"tool": "list_spaces", "args": {}})

        return ResolvedIntent(
            category=IntentCategory.QUERY,
            confidence=0.80,
            tool_calls=tool_calls,
            explanation="Query device/space state",
            original_text=original,
            extracted_entities=entities,
        )

    def _resolve_meta(self, text: str, entities: dict, original: str) -> ResolvedIntent:
        if any(w in text for w in ["scene", "scenes"]):
            return ResolvedIntent(
                category=IntentCategory.META,
                confidence=0.9,
                tool_calls=[{"tool": "list_scenes", "args": {}}],
                explanation="List available scenes",
                original_text=original,
                extracted_entities=entities,
            )
        if any(w in text for w in ["group", "groups"]):
            return ResolvedIntent(
                category=IntentCategory.META,
                confidence=0.9,
                tool_calls=[{"tool": "list_groups", "args": {}}],
                explanation="List device groups",
                original_text=original,
                extracted_entities=entities,
            )
        if any(w in text for w in ["room", "space", "rooms", "spaces"]):
            return ResolvedIntent(
                category=IntentCategory.META,
                confidence=0.9,
                tool_calls=[{"tool": "list_spaces", "args": {}}],
                explanation="List available spaces",
                original_text=original,
                extracted_entities=entities,
            )

        return ResolvedIntent(
            category=IntentCategory.META,
            confidence=0.7,
            tool_calls=[{"tool": "list_devices", "args": {}}],
            explanation="List available devices",
            original_text=original,
            extracted_entities=entities,
        )

    def _resolve_group(
        self, text: str, entities: dict, original: str,
        group: str | None, value: int | float | None,
    ) -> ResolvedIntent:
        action = self._extract_action(text) or "off"

        if not group:
            # "all lights" → try to find a matching group
            if "light" in text:
                group = "all_binary_switch"
            elif "fan" in text:
                group = "all_fan"
            else:
                group = "all_devices"

        args: dict[str, Any] = {"group": group, "action": action}
        if value is not None:
            args["value"] = value

        return ResolvedIntent(
            category=IntentCategory.GROUP,
            confidence=0.80,
            tool_calls=[{"tool": "set_group", "args": args}],
            explanation=f"Set group '{group}' to {action}",
            original_text=original,
            extracted_entities=entities,
        )

    def _resolve_environment(
        self, text: str, env_type: str, entities: dict,
        original: str, spaces: list[str],
    ) -> ResolvedIntent:
        """Resolve environment-level intents to device actions."""
        tool_calls = []
        space = spaces[0] if spaces else None

        if env_type == "cooler":
            # Find fans and AC in the target space
            fans = self._find_devices_by_capability(["fan", "binary_switch"], space, exclude_caps=["temperature_sensor"])
            for dev in fans[:2]:  # Limit to 2 devices
                tool_calls.append({"tool": "set_device", "args": {"device": dev, "action": "on"}})
            explanation = "Turn on cooling devices"

        elif env_type == "warmer":
            # Find heaters
            heaters = self._find_devices_by_capability(["thermostat", "climate_setpoint"], space)
            if heaters:
                for dev in heaters[:2]:
                    tool_calls.append({"tool": "set_device", "args": {"device": dev, "action": "set", "value": 24}})
            explanation = "Adjust heating"

        elif env_type == "brighter":
            lights = self._find_devices_by_capability(["binary_switch", "dimmer"], space, exclude_caps=["fan", "temperature_sensor"])
            for dev in lights:
                tool_calls.append({"tool": "set_device", "args": {"device": dev, "action": "on"}})
            explanation = "Increase lighting"

        elif env_type == "darker":
            lights = self._find_devices_by_capability(["binary_switch", "dimmer"], space, exclude_caps=["fan", "temperature_sensor"])
            for dev in lights:
                tool_calls.append({"tool": "set_device", "args": {"device": dev, "action": "off"}})
            explanation = "Decrease lighting"
        else:
            explanation = f"Unknown environment intent: {env_type}"

        if not tool_calls:
            return ResolvedIntent(
                category=IntentCategory.ENVIRONMENT,
                confidence=0.5,
                tool_calls=[],
                explanation=f"No suitable devices found for '{env_type}'",
                original_text=original,
                extracted_entities=entities,
            )

        return ResolvedIntent(
            category=IntentCategory.ENVIRONMENT,
            confidence=0.75,
            tool_calls=tool_calls,
            explanation=explanation,
            original_text=original,
            extracted_entities=entities,
        )

    def _resolve_schedule(
        self, text: str, entities: dict, original: str,
        devices: list[str], value: int | float | None,
        scene: str | None, time_info: dict,
    ) -> ResolvedIntent:
        action = self._extract_action(text)
        args: dict[str, Any] = {}

        if time_info["type"] == "delay":
            args["delay_seconds"] = time_info["seconds"]
        else:
            args["execute_at"] = time_info["timestamp"]
            args["time_str"] = time_info.get("time_str", "")

        if scene:
            args["scene"] = scene
        elif devices:
            args["device"] = devices[0]
            args["action"] = action or "off"
            if value is not None:
                args["value"] = value

        return ResolvedIntent(
            category=IntentCategory.SCHEDULE,
            confidence=0.80,
            tool_calls=[{"tool": "schedule_action", "args": args}],
            explanation=f"Schedule action for {time_info.get('time_str', str(time_info.get('seconds', 0)) + 's')}",
            original_text=original,
            extracted_entities=entities,
        )

    def _resolve_space_action(
        self, text: str, entities: dict, original: str,
        space: str, action: str, value: int | float | None,
    ) -> ResolvedIntent:
        group_name = f"all_{space}"
        args: dict[str, Any] = {"group": group_name, "action": action}
        if value is not None:
            args["value"] = value

        return ResolvedIntent(
            category=IntentCategory.GROUP,
            confidence=0.75,
            tool_calls=[{"tool": "set_group", "args": args}],
            explanation=f"Set all devices in {space} to {action}",
            original_text=original,
            extracted_entities=entities,
        )

    # ── Helpers ──

    def _find_devices_by_capability(
        self,
        capabilities: list[str],
        space: str | None = None,
        exclude_caps: list[str] | None = None,
    ) -> list[str]:
        """Find devices matching any of the given capabilities."""
        results = []
        for mapping in self._spaces._by_semantic.values():
            if space and mapping.space != space:
                continue
            if mapping.ai_access in ("blocked", "read_only"):
                continue
            if exclude_caps and any(c in mapping.capabilities for c in exclude_caps):
                continue
            if any(c in mapping.capabilities for c in capabilities):
                results.append(mapping.semantic_name)
        return results
