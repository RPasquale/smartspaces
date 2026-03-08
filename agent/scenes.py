"""Scene & Automation Engine — multi-device presets and conditional rules.

Scenes are named presets that set multiple devices at once.
Rules are condition-action pairs that trigger automatically.
Both can be created by AI agents or loaded from YAML.

Example scenes.yaml:
    scenes:
      movie_mode:
        display_name: "Movie Mode"
        actions:
          - device: living_room.ceiling_light
            action: "off"
          - device: living_room.lamp
            action: set
            value: 20
          - device: living_room.blinds
            action: close
    rules:
      cooling:
        display_name: "Auto Cooling"
        condition:
          device: living_room.temperature
          operator: ">"
          value: 28
        actions:
          - device: living_room.fan
            action: "on"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SceneAction:
    """A single action within a scene."""
    device: str           # semantic device name
    action: str           # on, off, set, toggle, open, close
    value: Any = None     # optional value (brightness %, temperature, etc.)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scene:
    """A named multi-device preset."""
    name: str
    display_name: str
    actions: list[SceneAction] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class RuleCondition:
    """A condition that triggers a rule."""
    device: str           # semantic device name to watch
    operator: str         # >, <, ==, !=, >=, <=
    value: Any            # threshold value


@dataclass
class Rule:
    """A condition-action automation rule."""
    name: str
    display_name: str
    condition: RuleCondition
    actions: list[SceneAction] = field(default_factory=list)
    enabled: bool = True
    cooldown_sec: float = 60.0
    last_triggered: float = 0.0


class SceneEngine:
    """Manages scenes and automation rules."""

    def __init__(self):
        self.scenes: dict[str, Scene] = {}
        self.rules: dict[str, Rule] = {}

    def load_yaml(self, path: str | Path) -> None:
        """Load scenes and rules from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        self.load_dict(data)

    def load_dict(self, data: dict[str, Any]) -> None:
        """Load scenes and rules from a dict."""
        for name, sdata in data.get("scenes", {}).items():
            actions = [
                SceneAction(
                    device=a["device"],
                    action=a.get("action", "set"),
                    value=a.get("value"),
                    params=a.get("params", {}),
                )
                for a in sdata.get("actions", [])
            ]
            self.scenes[name] = Scene(
                name=name,
                display_name=sdata.get("display_name", name.replace("_", " ").title()),
                actions=actions,
                tags=sdata.get("tags", []),
            )

        for name, rdata in data.get("rules", {}).items():
            cond_data = rdata.get("condition", {})
            condition = RuleCondition(
                device=cond_data.get("device", ""),
                operator=cond_data.get("operator", "=="),
                value=cond_data.get("value"),
            )
            actions = [
                SceneAction(
                    device=a["device"],
                    action=a.get("action", "set"),
                    value=a.get("value"),
                    params=a.get("params", {}),
                )
                for a in rdata.get("actions", [])
            ]
            self.rules[name] = Rule(
                name=name,
                display_name=rdata.get("display_name", name.replace("_", " ").title()),
                condition=condition,
                actions=actions,
                enabled=rdata.get("enabled", True),
                cooldown_sec=rdata.get("cooldown_sec", 60.0),
            )

        logger.info("Loaded %d scenes, %d rules", len(self.scenes), len(self.rules))

    def add_scene(self, name: str, display_name: str, actions: list[dict[str, Any]]) -> Scene:
        """Create a new scene programmatically (e.g., from an AI agent)."""
        scene_actions = [
            SceneAction(
                device=a["device"],
                action=a.get("action", "set"),
                value=a.get("value"),
                params=a.get("params", {}),
            )
            for a in actions
        ]
        scene = Scene(name=name, display_name=display_name, actions=scene_actions)
        self.scenes[name] = scene
        return scene

    def remove_scene(self, name: str) -> bool:
        """Remove a scene. Returns True if it existed."""
        return self.scenes.pop(name, None) is not None

    def get_scene(self, name: str) -> Scene | None:
        """Get a scene by name."""
        return self.scenes.get(name)

    def list_scenes(self) -> list[dict[str, Any]]:
        """List all scenes."""
        return [
            {
                "name": s.name,
                "display_name": s.display_name,
                "action_count": len(s.actions),
                "actions": [
                    {"device": a.device, "action": a.action, "value": a.value}
                    for a in s.actions
                ],
                "tags": s.tags,
            }
            for s in self.scenes.values()
        ]

    def get_scene_actions(self, name: str) -> list[dict[str, Any]]:
        """Get the actions for a scene, ready for execution."""
        scene = self.scenes.get(name)
        if not scene:
            return []
        return [
            {
                "device": a.device,
                "action": a.action,
                "value": a.value,
                "params": a.params,
            }
            for a in scene.actions
        ]

    def evaluate_rules(self, device_states: dict[str, Any]) -> list[tuple[Rule, list[dict[str, Any]]]]:
        """Evaluate all rules against current device states.

        Args:
            device_states: dict of semantic_name -> current_value

        Returns list of (rule, actions) for rules whose conditions are met.
        """
        import time
        triggered = []
        now = time.monotonic()

        for rule in self.rules.values():
            if not rule.enabled:
                continue
            if (now - rule.last_triggered) < rule.cooldown_sec:
                continue

            current = device_states.get(rule.condition.device)
            if current is None:
                continue

            if self._check_condition(current, rule.condition.operator, rule.condition.value):
                rule.last_triggered = now
                actions = [
                    {"device": a.device, "action": a.action, "value": a.value, "params": a.params}
                    for a in rule.actions
                ]
                triggered.append((rule, actions))
                logger.info("Rule '%s' triggered: %s %s %s (current=%s)",
                          rule.name, rule.condition.device,
                          rule.condition.operator, rule.condition.value, current)

        return triggered

    def add_rule(self, name: str, display_name: str, condition: dict[str, Any],
                 actions: list[dict[str, Any]], cooldown_sec: float = 60.0) -> Rule:
        """Create a new rule programmatically."""
        cond = RuleCondition(
            device=condition["device"],
            operator=condition.get("operator", "=="),
            value=condition.get("value"),
        )
        rule_actions = [
            SceneAction(
                device=a["device"],
                action=a.get("action", "set"),
                value=a.get("value"),
                params=a.get("params", {}),
            )
            for a in actions
        ]
        rule = Rule(
            name=name, display_name=display_name,
            condition=cond, actions=rule_actions,
            cooldown_sec=cooldown_sec,
        )
        self.rules[name] = rule
        return rule

    def list_rules(self) -> list[dict[str, Any]]:
        """List all rules."""
        return [
            {
                "name": r.name,
                "display_name": r.display_name,
                "enabled": r.enabled,
                "condition": {
                    "device": r.condition.device,
                    "operator": r.condition.operator,
                    "value": r.condition.value,
                },
                "action_count": len(r.actions),
            }
            for r in self.rules.values()
        ]

    @staticmethod
    def _check_condition(current: Any, operator: str, threshold: Any) -> bool:
        """Evaluate a condition."""
        try:
            current = float(current)
            threshold = float(threshold)
        except (ValueError, TypeError):
            pass

        if operator == ">":
            return current > threshold
        elif operator == "<":
            return current < threshold
        elif operator == ">=":
            return current >= threshold
        elif operator == "<=":
            return current <= threshold
        elif operator == "==":
            return current == threshold
        elif operator == "!=":
            return current != threshold
        return False
