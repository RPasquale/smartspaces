"""Suggested Actions — proactive recommendations for AI agents.

Analyzes current device states, time of day, recent history, and
scenes to suggest actions the agent might want to take. Helps agents
be proactive rather than purely reactive.

Usage:
    suggester = ActionSuggester(space_registry, scene_engine, history, analytics)
    suggestions = suggester.suggest()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SuggestionPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SuggestionCategory(str, Enum):
    COMFORT = "comfort"
    ENERGY = "energy"
    SAFETY = "safety"
    ROUTINE = "routine"
    AUTOMATION = "automation"


@dataclass
class Suggestion:
    """A single suggested action."""
    suggestion_id: str
    priority: SuggestionPriority
    category: SuggestionCategory
    title: str
    description: str
    tool_calls: list[dict[str, Any]]
    reason: str
    expires_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "suggestion_id": self.suggestion_id,
            "priority": self.priority.value,
            "category": self.category.value,
            "title": self.title,
            "description": self.description,
            "tool_calls": self.tool_calls,
            "reason": self.reason,
        }
        if self.expires_at:
            d["expires_in_seconds"] = max(0, self.expires_at - time.time())
        return d


class TimePeriod(str, Enum):
    EARLY_MORNING = "early_morning"  # 5-7
    MORNING = "morning"              # 7-9
    DAYTIME = "daytime"              # 9-17
    EVENING = "evening"              # 17-21
    NIGHT = "night"                  # 21-23
    LATE_NIGHT = "late_night"        # 23-5

    @staticmethod
    def current() -> TimePeriod:
        hour = datetime.now().hour
        if 5 <= hour < 7:
            return TimePeriod.EARLY_MORNING
        elif 7 <= hour < 9:
            return TimePeriod.MORNING
        elif 9 <= hour < 17:
            return TimePeriod.DAYTIME
        elif 17 <= hour < 21:
            return TimePeriod.EVENING
        elif 21 <= hour < 23:
            return TimePeriod.NIGHT
        else:
            return TimePeriod.LATE_NIGHT


class ActionSuggester:
    """Generates proactive action suggestions based on system state.

    Considers:
    - Time of day (morning routines, bedtime, etc.)
    - Device states (lights on late, fans off when hot)
    - Recent history (avoid re-suggesting recently completed actions)
    - Available scenes (suggest matching scenes)
    - Energy usage (high consumption warnings)
    """

    def __init__(
        self,
        space_registry: Any,
        scene_engine: Any | None = None,
        history: Any | None = None,
        analytics: Any | None = None,
    ):
        self._spaces = space_registry
        self._scenes = scene_engine
        self._history = history
        self._analytics = analytics
        self._dismissed: set[str] = set()  # Dismissed suggestion IDs
        self._counter = 0

    def suggest(self, max_suggestions: int = 5) -> list[dict[str, Any]]:
        """Generate suggestions based on current state and context."""
        suggestions: list[Suggestion] = []
        period = TimePeriod.current()

        # Time-based suggestions
        suggestions.extend(self._time_suggestions(period))

        # State-based suggestions
        suggestions.extend(self._state_suggestions())

        # Scene suggestions
        suggestions.extend(self._scene_suggestions(period))

        # Energy suggestions
        suggestions.extend(self._energy_suggestions())

        # Filter dismissed and deduplicate
        suggestions = [
            s for s in suggestions
            if s.suggestion_id not in self._dismissed
        ]

        # Sort by priority
        priority_order = {
            SuggestionPriority.HIGH: 0,
            SuggestionPriority.MEDIUM: 1,
            SuggestionPriority.LOW: 2,
        }
        suggestions.sort(key=lambda s: priority_order[s.priority])

        return [s.to_dict() for s in suggestions[:max_suggestions]]

    def dismiss(self, suggestion_id: str) -> None:
        """Dismiss a suggestion so it won't be suggested again this session."""
        self._dismissed.add(suggestion_id)

    def _make_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter}"

    # ── Time-based suggestions ──

    def _time_suggestions(self, period: TimePeriod) -> list[Suggestion]:
        suggestions = []

        if period == TimePeriod.LATE_NIGHT:
            # Check if lights are still on
            lights_on = self._find_active_devices_by_capability(
                ["binary_switch", "dimmer"],
                exclude=["fan", "temperature_sensor"],
            )
            if lights_on:
                suggestions.append(Suggestion(
                    suggestion_id=self._make_id("late_lights"),
                    priority=SuggestionPriority.MEDIUM,
                    category=SuggestionCategory.ROUTINE,
                    title="Lights still on",
                    description=f"It's late and {len(lights_on)} light(s) are still on: {', '.join(lights_on[:3])}",
                    tool_calls=[
                        {"tool": "set_device", "args": {"device": d, "action": "off"}}
                        for d in lights_on
                    ],
                    reason="Late night — lights are typically off",
                    expires_at=time.time() + 3600,
                ))

        elif period == TimePeriod.MORNING:
            # Suggest morning routine if a morning scene exists
            if self._scenes and "morning" in self._scenes.scenes:
                suggestions.append(Suggestion(
                    suggestion_id=self._make_id("morning_scene"),
                    priority=SuggestionPriority.LOW,
                    category=SuggestionCategory.ROUTINE,
                    title="Morning routine available",
                    description="Start your morning routine?",
                    tool_calls=[{"tool": "activate_scene", "args": {"scene": "morning"}}],
                    reason=f"It's morning ({TimePeriod.current().value})",
                    expires_at=time.time() + 7200,
                ))

        elif period == TimePeriod.NIGHT:
            # Suggest goodnight scene
            if self._scenes and "goodnight" in self._scenes.scenes:
                suggestions.append(Suggestion(
                    suggestion_id=self._make_id("goodnight_scene"),
                    priority=SuggestionPriority.LOW,
                    category=SuggestionCategory.ROUTINE,
                    title="Goodnight routine",
                    description="Ready for bed? Activate the goodnight scene.",
                    tool_calls=[{"tool": "activate_scene", "args": {"scene": "goodnight"}}],
                    reason="It's nighttime",
                    expires_at=time.time() + 7200,
                ))

        return suggestions

    # ── State-based suggestions ──

    def _state_suggestions(self) -> list[Suggestion]:
        suggestions = []

        if not self._analytics:
            return suggestions

        snap = self._analytics.compute()

        # Temperature too high, fan not on
        if snap.avg_temperature is not None and snap.avg_temperature > 28:
            fans_off = []
            for mapping in self._spaces._by_semantic.values():
                if "fan" in mapping.capabilities and mapping.ai_access == "full":
                    state = self._analytics._states.get(mapping.semantic_name)
                    if not self._analytics._is_device_on(state):
                        fans_off.append(mapping.semantic_name)

            if fans_off:
                suggestions.append(Suggestion(
                    suggestion_id=self._make_id("hot_fan_off"),
                    priority=SuggestionPriority.HIGH,
                    category=SuggestionCategory.COMFORT,
                    title="It's hot — turn on fan?",
                    description=f"Temperature is {snap.avg_temperature:.1f}°C. Fan(s) available: {', '.join(fans_off)}",
                    tool_calls=[
                        {"tool": "set_device", "args": {"device": d, "action": "on"}}
                        for d in fans_off
                    ],
                    reason=f"Temperature {snap.avg_temperature:.1f}°C exceeds comfort range",
                ))

        # Temperature too low
        if snap.avg_temperature is not None and snap.avg_temperature < 16:
            suggestions.append(Suggestion(
                suggestion_id=self._make_id("cold"),
                priority=SuggestionPriority.HIGH,
                category=SuggestionCategory.COMFORT,
                title="It's cold",
                description=f"Temperature is {snap.avg_temperature:.1f}°C — well below comfort range.",
                tool_calls=[],
                reason="Temperature below comfort range",
            ))

        return suggestions

    # ── Scene suggestions ──

    def _scene_suggestions(self, period: TimePeriod) -> list[Suggestion]:
        suggestions = []

        if not self._scenes:
            return suggestions

        # Check if conditions match any scene's "vibe"
        for scene in self._scenes.scenes.values():
            tags = getattr(scene, "tags", [])

            if period == TimePeriod.EVENING and "evening" in tags:
                if not self._was_recently_activated(scene.name):
                    suggestions.append(Suggestion(
                        suggestion_id=self._make_id(f"scene_{scene.name}"),
                        priority=SuggestionPriority.LOW,
                        category=SuggestionCategory.ROUTINE,
                        title=f"Activate {scene.display_name}?",
                        description=f"It's evening — {scene.display_name} might be appropriate.",
                        tool_calls=[{"tool": "activate_scene", "args": {"scene": scene.name}}],
                        reason=f"Time matches scene tags: {tags}",
                    ))

        return suggestions

    # ── Energy suggestions ──

    def _energy_suggestions(self) -> list[Suggestion]:
        suggestions = []

        if not self._analytics:
            return suggestions

        snap = self._analytics.compute()

        if snap.total_power_watts > 500:
            suggestions.append(Suggestion(
                suggestion_id=self._make_id("high_power"),
                priority=SuggestionPriority.MEDIUM,
                category=SuggestionCategory.ENERGY,
                title="High power usage",
                description=(
                    f"Current draw: {snap.total_power_watts:.0f}W across "
                    f"{snap.active_device_count} devices."
                ),
                tool_calls=[],
                reason=f"Power consumption above 500W",
            ))

        # Suggest turning off devices that have been on for a long time
        long_running = []
        for device_name, state in self._analytics._states.items():
            if self._analytics._is_device_on(state):
                duration = time.time() - state.updated_at
                if duration > 14400:  # 4 hours
                    long_running.append((device_name, duration))

        if long_running:
            device_list = ", ".join(d for d, _ in long_running[:3])
            suggestions.append(Suggestion(
                suggestion_id=self._make_id("long_running"),
                priority=SuggestionPriority.LOW,
                category=SuggestionCategory.ENERGY,
                title="Devices on for a long time",
                description=f"These devices have been on for 4+ hours: {device_list}",
                tool_calls=[
                    {"tool": "set_device", "args": {"device": d, "action": "off"}}
                    for d, _ in long_running
                ],
                reason="Devices active for extended period",
            ))

        return suggestions

    # ── Helpers ──

    def _find_active_devices_by_capability(
        self,
        capabilities: list[str],
        exclude: list[str] | None = None,
    ) -> list[str]:
        """Find active devices matching capabilities."""
        results = []
        if not self._analytics:
            return results

        for mapping in self._spaces._by_semantic.values():
            if exclude and any(c in mapping.capabilities for c in exclude):
                continue
            if not any(c in mapping.capabilities for c in capabilities):
                continue
            state = self._analytics._states.get(mapping.semantic_name)
            if self._analytics._is_device_on(state):
                results.append(mapping.semantic_name)
        return results

    def _was_recently_activated(self, scene_name: str, within_hours: float = 4) -> bool:
        """Check if a scene was recently activated."""
        if not self._history:
            return False
        since = time.time() - (within_hours * 3600)
        records = self._history.query(
            action_type=self._history.__class__.__module__ and None,
            since=since,
            limit=50,
        )
        for rec in records:
            if rec.get("action_type") == "scene" and rec.get("metadata", {}).get("scene") == scene_name:
                return True
        return False
