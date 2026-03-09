"""Semantic Space Registry — maps raw device IDs to human/AI-readable names.

Loads a spaces.yaml config that defines the spatial hierarchy
(site > space > device) and enriches raw adapter inventory with
semantic names, roles, and AI permission levels.

Example spaces.yaml:
    site: "my_home"
    spaces:
      living_room:
        display_name: "Living Room"
        devices:
          ceiling_light:
            point_id: "dev_kc868_a4_..._relay_1_state"
            capabilities: [binary_switch, dimmer]
            ai_access: full
          temperature:
            point_id: "dev_esphome_..._sensor_temp_state"
            capabilities: [temperature_sensor]
            ai_access: read_only
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class DeviceMapping:
    """A single device in the semantic registry."""
    semantic_name: str          # e.g. "living_room.ceiling_light"
    space: str                  # e.g. "living_room"
    device_name: str            # e.g. "ceiling_light"
    display_name: str           # e.g. "Living Room Ceiling Light"
    point_id: str               # raw point_id from adapter
    connection_id: str = ""     # resolved at runtime
    endpoint_id: str = ""       # resolved at runtime
    device_id: str = ""         # resolved at runtime
    capabilities: list[str] = field(default_factory=list)
    ai_access: str = "full"     # full | read_only | confirm_required | blocked
    safety_class: str = "S1"
    unit: str | None = None
    value_type: str = "str"
    traits: dict[str, Any] = field(default_factory=dict)


@dataclass
class Space:
    """A logical space (room, zone, area)."""
    name: str
    display_name: str
    devices: dict[str, DeviceMapping] = field(default_factory=dict)


class SpaceRegistry:
    """Manages the semantic device namespace.

    Loads from YAML config and provides lookup by semantic name,
    space, capability, or raw point_id.
    """

    def __init__(self):
        self.site_name: str = "default"
        self.spaces: dict[str, Space] = {}
        self._by_semantic: dict[str, DeviceMapping] = {}
        self._by_point_id: dict[str, DeviceMapping] = {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SpaceRegistry":
        """Create a SpaceRegistry and load from a YAML file."""
        registry = cls()
        registry.load_yaml(path)
        return registry

    def load_yaml(self, path: str | Path) -> None:
        """Load space definitions from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        self.load_dict(data)

    def load_dict(self, data: dict[str, Any]) -> None:
        """Load space definitions from a dict."""
        self.site_name = data.get("site", "default")

        for space_key, space_data in data.get("spaces", {}).items():
            display_name = space_data.get("display_name", space_key.replace("_", " ").title())
            space = Space(name=space_key, display_name=display_name)

            for dev_key, dev_data in space_data.get("devices", {}).items():
                semantic = f"{space_key}.{dev_key}"
                dev_display = dev_data.get(
                    "display_name",
                    f"{display_name} {dev_key.replace('_', ' ').title()}"
                )

                mapping = DeviceMapping(
                    semantic_name=semantic,
                    space=space_key,
                    device_name=dev_key,
                    display_name=dev_display,
                    point_id=dev_data.get("point_id", ""),
                    connection_id=dev_data.get("connection_id", ""),
                    endpoint_id=dev_data.get("endpoint_id", ""),
                    device_id=dev_data.get("device_id", ""),
                    capabilities=dev_data.get("capabilities", []),
                    ai_access=dev_data.get("ai_access", "full"),
                    safety_class=dev_data.get("safety_class", "S1"),
                    unit=dev_data.get("unit"),
                    value_type=dev_data.get("value_type", "str"),
                    traits=dev_data.get("traits", {}),
                )
                space.devices[dev_key] = mapping
                self._by_semantic[semantic] = mapping
                self._by_point_id[mapping.point_id] = mapping

            self.spaces[space_key] = space

        logger.info(
            "Loaded space registry: site=%s, %d spaces, %d devices",
            self.site_name, len(self.spaces), len(self._by_semantic),
        )

    def enrich_from_state_store(self, devices: list[dict], endpoints: list[dict], points: list[dict]) -> None:
        """Back-fill connection_id, device_id, endpoint_id from live state store data."""
        point_index = {}
        for pt in points:
            point_index[pt.get("point_id", "")] = pt

        ep_index = {}
        for ep in endpoints:
            ep_index[ep.get("endpoint_id", "")] = ep

        dev_index = {}
        for dev in devices:
            dev_index[dev.get("device_id", "")] = dev

        for mapping in self._by_semantic.values():
            pt = point_index.get(mapping.point_id)
            if pt:
                mapping.endpoint_id = pt.get("endpoint_id", "")
                ep = ep_index.get(mapping.endpoint_id)
                if ep:
                    mapping.device_id = ep.get("device_id", "")
                    dev = dev_index.get(mapping.device_id)
                    if dev:
                        mapping.connection_id = dev.get("connection_id", "")

    def get(self, semantic_name: str) -> DeviceMapping | None:
        """Look up a device by semantic name like 'living_room.ceiling_light'."""
        return self._by_semantic.get(semantic_name)

    def get_by_point_id(self, point_id: str) -> DeviceMapping | None:
        """Reverse-lookup: raw point_id -> semantic mapping."""
        return self._by_point_id.get(point_id)

    def list_spaces(self) -> list[dict[str, Any]]:
        """List all spaces with their device counts."""
        return [
            {
                "name": s.name,
                "display_name": s.display_name,
                "device_count": len(s.devices),
                "devices": list(s.devices.keys()),
            }
            for s in self.spaces.values()
        ]

    def list_devices(
        self,
        space: str | None = None,
        capability: str | None = None,
        ai_access: str | None = None,
    ) -> list[dict[str, Any]]:
        """List devices with optional filters."""
        results = []
        for mapping in self._by_semantic.values():
            if space and mapping.space != space:
                continue
            if capability and capability not in mapping.capabilities:
                continue
            if ai_access and mapping.ai_access != ai_access:
                continue
            results.append(self._mapping_to_dict(mapping))
        return results

    def resolve_name(self, name: str) -> DeviceMapping | None:
        """Fuzzy resolve: accepts 'living_room.light', 'ceiling_light', 'living room light', etc."""
        # Exact match
        if name in self._by_semantic:
            return self._by_semantic[name]

        # Normalize
        normalized = name.lower().replace(" ", "_").replace("-", "_")
        if normalized in self._by_semantic:
            return self._by_semantic[normalized]

        # Try just device name (search all spaces)
        for mapping in self._by_semantic.values():
            if mapping.device_name == normalized:
                return mapping

        # Fuzzy: check if normalized is a substring of semantic name
        for key, mapping in self._by_semantic.items():
            if normalized in key:
                return mapping

        return None

    def _mapping_to_dict(self, m: DeviceMapping) -> dict[str, Any]:
        return {
            "name": m.semantic_name,
            "display_name": m.display_name,
            "space": m.space,
            "capabilities": m.capabilities,
            "ai_access": m.ai_access,
            "safety_class": m.safety_class,
            "unit": m.unit,
            "value_type": m.value_type,
            "point_id": m.point_id,
            "connection_id": m.connection_id,
        }

    def to_context_prompt(self) -> str:
        """Generate a text summary suitable for injection into an LLM system prompt."""
        lines = [f"# Available Devices (site: {self.site_name})\n"]
        for space in self.spaces.values():
            lines.append(f"## {space.display_name}")
            for dev in space.devices.values():
                caps = ", ".join(dev.capabilities) if dev.capabilities else "unknown"
                access = dev.ai_access
                unit_str = f" ({dev.unit})" if dev.unit else ""
                lines.append(f"- **{dev.semantic_name}**: {dev.display_name} [{caps}]{unit_str} (access: {access})")
            lines.append("")
        return "\n".join(lines)
