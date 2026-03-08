"""Device Groups — logical groupings for bulk operations.

Groups can be defined explicitly (member list), by capability match,
by space match, or by tag. Agents use groups to control multiple
devices in a single tool call: "turn off all lights".

Groups are defined in spaces.yaml:

    groups:
      all_lights:
        display_name: "All Lights"
        match:
          capability: [binary_switch, dimmer]
      upstairs:
        display_name: "Upstairs"
        match:
          space: [bedroom, bathroom]
      energy_consumers:
        display_name: "Energy Consumers"
        members:
          - living_room.fan
          - kitchen.oven
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.spaces import DeviceMapping, SpaceRegistry

logger = logging.getLogger(__name__)


@dataclass
class DeviceGroup:
    """A named group of devices."""
    name: str
    display_name: str
    members: list[str] = field(default_factory=list)          # explicit semantic names
    match_capabilities: list[str] = field(default_factory=list)  # auto-match by capability
    match_spaces: list[str] = field(default_factory=list)        # auto-match by space
    match_ai_access: list[str] = field(default_factory=list)     # auto-match by access level
    tags: list[str] = field(default_factory=list)


class GroupRegistry:
    """Manages device groups with static and dynamic membership.

    Static groups have explicit member lists.
    Dynamic groups auto-resolve from capability, space, or access level filters.
    Both types are resolved at query time against the current SpaceRegistry state.
    """

    def __init__(self, space_registry: SpaceRegistry):
        self._spaces = space_registry
        self._groups: dict[str, DeviceGroup] = {}
        self._generate_auto_groups()

    def load_dict(self, data: dict[str, Any]) -> None:
        """Load group definitions from a dict (typically from spaces.yaml)."""
        for name, gdata in data.get("groups", {}).items():
            match = gdata.get("match", {})
            group = DeviceGroup(
                name=name,
                display_name=gdata.get("display_name", name.replace("_", " ").title()),
                members=gdata.get("members", []),
                match_capabilities=_ensure_list(match.get("capability")),
                match_spaces=_ensure_list(match.get("space")),
                match_ai_access=_ensure_list(match.get("ai_access")),
                tags=gdata.get("tags", []),
            )
            self._groups[name] = group

        logger.info("Loaded %d custom groups", len(data.get("groups", {})))
        self._generate_auto_groups()

    def _generate_auto_groups(self) -> None:
        """Generate automatic groups from capabilities and spaces."""
        # Collect all capabilities across all devices
        cap_devices: dict[str, list[str]] = {}
        for mapping in self._spaces._by_semantic.values():
            for cap in mapping.capabilities:
                cap_devices.setdefault(cap, []).append(mapping.semantic_name)

        # Auto-group by capability (only if not already defined)
        for cap, devices in cap_devices.items():
            group_name = f"all_{cap}"
            if group_name not in self._groups and len(devices) > 1:
                self._groups[group_name] = DeviceGroup(
                    name=group_name,
                    display_name=f"All {cap.replace('_', ' ').title()} Devices",
                    match_capabilities=[cap],
                )

        # Auto-group by space (only if not already defined)
        for space_name, space in self._spaces.spaces.items():
            group_name = f"all_{space_name}"
            if group_name not in self._groups and len(space.devices) > 1:
                self._groups[group_name] = DeviceGroup(
                    name=group_name,
                    display_name=f"All {space.display_name} Devices",
                    match_spaces=[space_name],
                )

        # Global "all_devices" group
        if "all_devices" not in self._groups:
            self._groups["all_devices"] = DeviceGroup(
                name="all_devices",
                display_name="All Devices",
                match_spaces=list(self._spaces.spaces.keys()),
            )

    def resolve(self, group_name: str) -> list[DeviceMapping]:
        """Resolve a group name to its current list of device mappings.

        Handles both static (explicit members) and dynamic (match-based) groups.
        """
        group = self._groups.get(group_name)
        if not group:
            # Try fuzzy match
            normalized = group_name.lower().replace(" ", "_").replace("-", "_")
            group = self._groups.get(normalized)
            if not group:
                # Try prefix match
                for key, g in self._groups.items():
                    if normalized in key or key in normalized:
                        group = g
                        break

        if not group:
            return []

        result_set: dict[str, DeviceMapping] = {}

        # Add explicit members
        for member_name in group.members:
            mapping = self._spaces.resolve_name(member_name)
            if mapping:
                result_set[mapping.semantic_name] = mapping

        # Add by capability match
        for cap in group.match_capabilities:
            for mapping in self._spaces._by_semantic.values():
                if cap in mapping.capabilities:
                    result_set[mapping.semantic_name] = mapping

        # Add by space match
        for space_name in group.match_spaces:
            space = self._spaces.spaces.get(space_name)
            if space:
                for mapping in space.devices.values():
                    result_set[mapping.semantic_name] = mapping

        # Add by ai_access match
        for access in group.match_ai_access:
            for mapping in self._spaces._by_semantic.values():
                if mapping.ai_access == access:
                    result_set[mapping.semantic_name] = mapping

        return list(result_set.values())

    def resolve_name(self, name: str) -> DeviceGroup | None:
        """Fuzzy-resolve a group name."""
        if name in self._groups:
            return self._groups[name]

        normalized = name.lower().replace(" ", "_").replace("-", "_")
        if normalized in self._groups:
            return self._groups[normalized]

        # Partial match
        for key, group in self._groups.items():
            if normalized in key or key in normalized:
                return group
            if normalized in group.display_name.lower():
                return group

        return None

    def add_group(
        self,
        name: str,
        display_name: str,
        members: list[str] | None = None,
        match_capabilities: list[str] | None = None,
        match_spaces: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> DeviceGroup:
        """Create a new group programmatically."""
        group = DeviceGroup(
            name=name,
            display_name=display_name,
            members=members or [],
            match_capabilities=match_capabilities or [],
            match_spaces=match_spaces or [],
            tags=tags or [],
        )
        self._groups[name] = group
        return group

    def remove_group(self, name: str) -> bool:
        """Remove a group. Returns True if it existed."""
        return self._groups.pop(name, None) is not None

    def list_groups(self) -> list[dict[str, Any]]:
        """List all groups with resolved member counts."""
        result = []
        for group in self._groups.values():
            members = self.resolve(group.name)
            result.append({
                "name": group.name,
                "display_name": group.display_name,
                "member_count": len(members),
                "members": [m.semantic_name for m in members],
                "tags": group.tags,
                "is_dynamic": bool(
                    group.match_capabilities or group.match_spaces or group.match_ai_access
                ),
            })
        return result

    def get_writable_members(self, group_name: str) -> list[DeviceMapping]:
        """Resolve a group but only return devices with AI write access."""
        return [
            m for m in self.resolve(group_name)
            if m.ai_access == "full"
        ]

    def find_groups_for_device(self, device_name: str) -> list[str]:
        """Find all groups that contain a specific device."""
        result = []
        for group_name, group in self._groups.items():
            members = self.resolve(group_name)
            if any(m.semantic_name == device_name for m in members):
                result.append(group_name)
        return result


def _ensure_list(val: Any) -> list[str]:
    """Normalize a value to a list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)
