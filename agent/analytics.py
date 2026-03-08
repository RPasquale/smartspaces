"""Energy & Comfort Analytics — aggregate metrics for AI agent context.

Computes power consumption, temperature/comfort scores, and generates
context text for LLM system prompt injection. Helps agents make
energy-aware and comfort-aware decisions.

Usage:
    analyzer = EnergyComfortAnalyzer(space_registry)
    analyzer.update_state("living_room.temperature", 26.5)
    analyzer.update_state("living_room.fan", True)

    context = analyzer.to_context_prompt()
    stats = analyzer.compute()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Default power estimates (watts) when actual metering is unavailable
_DEFAULT_POWER_ESTIMATES: dict[str, float] = {
    "binary_switch": 60.0,      # Generic switch (assume a light)
    "dimmer": 60.0,             # Dimmable light
    "light_color": 10.0,        # LED color light
    "fan": 75.0,                # Ceiling/standing fan
    "cover": 200.0,             # Motorized cover/blind
    "thermostat": 2000.0,       # HVAC system
    "climate_setpoint": 2000.0,
}

# Comfort temperature range (°C)
_COMFORT_TEMP_MIN = 18.0
_COMFORT_TEMP_MAX = 26.0
_COMFORT_TEMP_IDEAL = 22.0


@dataclass(slots=True)
class DeviceState:
    """Tracked state of a single device."""
    device_name: str
    value: Any = None
    updated_at: float = 0.0
    power_watts: float | None = None  # Actual metered power if available


@dataclass
class AnalyticsSnapshot:
    """Point-in-time analytics snapshot."""
    timestamp: float = field(default_factory=time.time)

    # Power
    total_power_watts: float = 0.0
    active_device_count: int = 0
    inactive_device_count: int = 0
    power_by_space: dict[str, float] = field(default_factory=dict)
    power_by_device: dict[str, float] = field(default_factory=dict)
    top_consumers: list[dict[str, Any]] = field(default_factory=list)

    # Comfort
    temperatures: dict[str, float] = field(default_factory=dict)
    avg_temperature: float | None = None
    comfort_score: float | None = None  # 0.0 (uncomfortable) to 1.0 (ideal)
    comfort_assessment: str = "unknown"

    # Summary
    devices_on: list[str] = field(default_factory=list)
    devices_off: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "power": {
                "total_watts": round(self.total_power_watts, 1),
                "active_devices": self.active_device_count,
                "inactive_devices": self.inactive_device_count,
                "by_space": {k: round(v, 1) for k, v in self.power_by_space.items()},
                "top_consumers": self.top_consumers,
            },
            "comfort": {
                "temperatures": {k: round(v, 1) for k, v in self.temperatures.items()},
                "average_temperature": round(self.avg_temperature, 1) if self.avg_temperature is not None else None,
                "comfort_score": round(self.comfort_score, 2) if self.comfort_score is not None else None,
                "assessment": self.comfort_assessment,
            },
            "devices_on": self.devices_on,
            "devices_off": self.devices_off,
        }
        return d


class EnergyComfortAnalyzer:
    """Computes energy and comfort analytics from device states.

    Maintains a shadow state of all devices and computes aggregate
    metrics on demand. Power estimates are used when actual metering
    data is not available.
    """

    def __init__(self, space_registry: Any, power_estimates: dict[str, float] | None = None):
        self._spaces = space_registry
        self._power_estimates = power_estimates or dict(_DEFAULT_POWER_ESTIMATES)
        self._states: dict[str, DeviceState] = {}
        self._custom_power: dict[str, float] = {}  # device_name → watts override

    def update_state(
        self,
        device_name: str,
        value: Any,
        power_watts: float | None = None,
    ) -> None:
        """Update the tracked state of a device."""
        state = self._states.get(device_name)
        if not state:
            state = DeviceState(device_name=device_name)
            self._states[device_name] = state
        state.value = value
        state.updated_at = time.time()
        if power_watts is not None:
            state.power_watts = power_watts

    def set_power_estimate(self, device_name: str, watts: float) -> None:
        """Set a custom power estimate for a specific device."""
        self._custom_power[device_name] = watts

    def compute(self) -> AnalyticsSnapshot:
        """Compute a full analytics snapshot from current state."""
        snap = AnalyticsSnapshot()

        for mapping in self._spaces._by_semantic.values():
            device = mapping.semantic_name
            state = self._states.get(device)

            is_on = self._is_device_on(state)
            if is_on:
                snap.active_device_count += 1
                snap.devices_on.append(device)
            else:
                snap.inactive_device_count += 1
                snap.devices_off.append(device)

            # Power estimation
            if is_on:
                power = self._estimate_power(mapping, state)
                if power > 0:
                    snap.total_power_watts += power
                    snap.power_by_device[device] = power
                    space = mapping.space
                    snap.power_by_space[space] = snap.power_by_space.get(space, 0) + power

            # Temperature tracking
            if "temperature_sensor" in mapping.capabilities and state and state.value is not None:
                try:
                    temp = float(state.value)
                    snap.temperatures[device] = temp
                except (ValueError, TypeError):
                    pass

        # Top consumers (sorted by power, descending)
        snap.top_consumers = sorted(
            [{"device": d, "watts": round(w, 1)} for d, w in snap.power_by_device.items()],
            key=lambda x: x["watts"],
            reverse=True,
        )[:10]

        # Comfort computation
        if snap.temperatures:
            temps = list(snap.temperatures.values())
            snap.avg_temperature = sum(temps) / len(temps)
            snap.comfort_score = self._compute_comfort_score(snap.avg_temperature)
            snap.comfort_assessment = self._assess_comfort(snap.avg_temperature)

        return snap

    def to_context_prompt(self) -> str:
        """Generate analytics context for LLM system prompt injection."""
        snap = self.compute()
        lines = ["# Energy & Comfort Status"]

        # Power summary
        if snap.total_power_watts > 0:
            lines.append(f"\n## Power: {snap.total_power_watts:.0f}W total")
            lines.append(f"- {snap.active_device_count} devices active, {snap.inactive_device_count} inactive")
            if snap.top_consumers:
                lines.append("- Top consumers:")
                for tc in snap.top_consumers[:5]:
                    pct = (tc["watts"] / snap.total_power_watts * 100) if snap.total_power_watts else 0
                    lines.append(f"  - {tc['device']}: {tc['watts']}W ({pct:.0f}%)")
            for space, watts in sorted(snap.power_by_space.items(), key=lambda x: -x[1]):
                lines.append(f"- {space}: {watts:.0f}W")

        # Comfort summary
        if snap.avg_temperature is not None:
            lines.append(f"\n## Comfort: {snap.comfort_assessment}")
            lines.append(f"- Average temperature: {snap.avg_temperature:.1f}°C")
            if snap.comfort_score is not None:
                lines.append(f"- Comfort score: {snap.comfort_score:.0%}")
            for device, temp in snap.temperatures.items():
                lines.append(f"- {device}: {temp:.1f}°C")

        # Recommendations
        recs = self._generate_recommendations(snap)
        if recs:
            lines.append("\n## Recommendations")
            for rec in recs:
                lines.append(f"- {rec}")

        return "\n".join(lines)

    def _is_device_on(self, state: DeviceState | None) -> bool:
        """Determine if a device is currently on/active."""
        if not state or state.value is None:
            return False
        v = state.value
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v > 0
        if isinstance(v, str):
            return v.lower() in ("on", "true", "1", "open", "active")
        return False

    def _estimate_power(self, mapping: Any, state: DeviceState | None) -> float:
        """Estimate power consumption for a device."""
        device = mapping.semantic_name

        # Use actual metered value if available
        if state and state.power_watts is not None:
            return state.power_watts

        # Use custom override
        if device in self._custom_power:
            return self._custom_power[device]

        # Use capability-based estimate
        for cap in mapping.capabilities:
            if cap in self._power_estimates:
                power = self._power_estimates[cap]
                # Scale by value for dimmers
                if cap == "dimmer" and state and state.value is not None:
                    try:
                        level = float(state.value)
                        if 0 <= level <= 100:
                            return power * (level / 100)
                    except (ValueError, TypeError):
                        pass
                return power

        return 0.0

    def _compute_comfort_score(self, temp: float) -> float:
        """Compute comfort score (0.0-1.0) from temperature."""
        if _COMFORT_TEMP_MIN <= temp <= _COMFORT_TEMP_MAX:
            # Within comfortable range — score based on proximity to ideal
            deviation = abs(temp - _COMFORT_TEMP_IDEAL)
            max_deviation = max(
                _COMFORT_TEMP_IDEAL - _COMFORT_TEMP_MIN,
                _COMFORT_TEMP_MAX - _COMFORT_TEMP_IDEAL,
            )
            return max(0.0, 1.0 - (deviation / max_deviation) * 0.5)
        else:
            # Outside comfortable range
            if temp < _COMFORT_TEMP_MIN:
                deviation = _COMFORT_TEMP_MIN - temp
            else:
                deviation = temp - _COMFORT_TEMP_MAX
            return max(0.0, 0.5 - deviation * 0.1)

    def _assess_comfort(self, temp: float) -> str:
        """Generate human-readable comfort assessment."""
        if temp < 15:
            return f"Very cold ({temp:.1f}°C) — heating recommended"
        elif temp < _COMFORT_TEMP_MIN:
            return f"Cool ({temp:.1f}°C) — slightly below comfort range"
        elif temp <= _COMFORT_TEMP_MAX:
            if abs(temp - _COMFORT_TEMP_IDEAL) <= 2:
                return f"Ideal ({temp:.1f}°C)"
            elif temp < _COMFORT_TEMP_IDEAL:
                return f"Comfortable, slightly cool ({temp:.1f}°C)"
            else:
                return f"Comfortable, slightly warm ({temp:.1f}°C)"
        elif temp <= 30:
            return f"Warm ({temp:.1f}°C) — cooling recommended"
        else:
            return f"Very hot ({temp:.1f}°C) — cooling urgently recommended"

    def _generate_recommendations(self, snap: AnalyticsSnapshot) -> list[str]:
        """Generate actionable recommendations based on current state."""
        recs = []

        # Temperature recommendations
        if snap.avg_temperature is not None:
            if snap.avg_temperature > _COMFORT_TEMP_MAX:
                # Check if fans are off
                for mapping in self._spaces._by_semantic.values():
                    if "fan" in mapping.capabilities:
                        state = self._states.get(mapping.semantic_name)
                        if not self._is_device_on(state):
                            recs.append(f"Turn on {mapping.semantic_name} to cool down")

            elif snap.avg_temperature < _COMFORT_TEMP_MIN:
                recs.append("Consider increasing heating")

        # Power recommendations
        if snap.total_power_watts > 500 and snap.active_device_count > 3:
            recs.append(
                f"High power usage ({snap.total_power_watts:.0f}W) with "
                f"{snap.active_device_count} active devices"
            )

        return recs

    @property
    def stats(self) -> dict[str, Any]:
        snap = self.compute()
        return snap.to_dict()
