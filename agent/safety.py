"""AI Safety Guard — enforces constraints on AI-initiated device operations.

Controls what an AI agent can do, how fast, and with what confirmation
requirements. Sits between the agent tools and the adapter registry.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent.spaces import DeviceMapping, SpaceRegistry

logger = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """Tracks rate limiting for a single device."""
    last_write: float = 0.0
    writes_in_window: int = 0
    window_start: float = 0.0


@dataclass
class SafetyConfig:
    """Global AI safety configuration."""
    max_writes_per_minute: int = 10
    cooldown_seconds: float = 2.0
    require_readback: bool = True
    blocked_capabilities: list[str] = field(default_factory=lambda: ["lock", "door_lock"])
    confirm_capabilities: list[str] = field(default_factory=lambda: ["thermostat", "climate_setpoint", "cover"])
    max_concurrent_writes: int = 5
    confirmation_ttl_seconds: float = 600.0  # 10 minutes


class AISafetyGuard:
    """Enforces safety constraints on AI-initiated operations.

    Checks:
    1. AI access level (full / read_only / confirm_required / blocked)
    2. Rate limiting (writes per minute per device)
    3. Cooldown (minimum time between writes to same device)
    4. Capability restrictions (certain capabilities always blocked/confirmed)
    5. Safety class restrictions (S3+ requires confirmation)
    """

    def __init__(self, space_registry: SpaceRegistry, config: SafetyConfig | None = None):
        self.spaces = space_registry
        self.config = config or SafetyConfig()
        self._rate_limits: dict[str, RateLimitState] = {}
        self._pending_confirmations: dict[str, dict[str, Any]] = {}
        self._stats = {
            "checks": 0, "allowed": 0, "blocked": 0,
            "rate_limited": 0, "confirmation_required": 0,
        }

    def check_read(self, device_name: str) -> tuple[bool, str]:
        """Check if an AI agent is allowed to read a device.

        Returns (allowed, reason).
        """
        self._stats["checks"] += 1
        mapping = self.spaces.resolve_name(device_name)
        if not mapping:
            self._stats["blocked"] += 1
            return False, f"Unknown device: {device_name}"

        if mapping.ai_access == "blocked":
            self._stats["blocked"] += 1
            return False, f"AI access blocked for {mapping.semantic_name}"

        self._stats["allowed"] += 1
        return True, "ok"

    def check_write(self, device_name: str, action: str = "", value: Any = None) -> tuple[bool, str]:
        """Check if an AI agent is allowed to write to a device.

        Returns (allowed, reason). If reason starts with "CONFIRM:",
        the operation requires human confirmation before proceeding.
        """
        self._stats["checks"] += 1
        mapping = self.spaces.resolve_name(device_name)
        if not mapping:
            self._stats["blocked"] += 1
            return False, f"Unknown device: {device_name}"

        # Access level check
        if mapping.ai_access == "blocked":
            self._stats["blocked"] += 1
            return False, f"AI access blocked for {mapping.semantic_name}"

        if mapping.ai_access == "read_only":
            self._stats["blocked"] += 1
            return False, f"AI has read-only access to {mapping.semantic_name}"

        # Capability restrictions
        for cap in mapping.capabilities:
            if cap in self.config.blocked_capabilities:
                self._stats["blocked"] += 1
                return False, f"Capability '{cap}' is blocked for AI agents"

        # Safety class check (S3+ always requires confirmation)
        safety_num = int(mapping.safety_class[1]) if len(mapping.safety_class) == 2 else 0
        if safety_num >= 3:
            self._stats["confirmation_required"] += 1
            return False, f"CONFIRM: Safety class {mapping.safety_class} requires human confirmation"

        # Confirmation-required capabilities
        if mapping.ai_access == "confirm_required":
            self._stats["confirmation_required"] += 1
            return False, f"CONFIRM: Device {mapping.semantic_name} requires human confirmation"

        for cap in mapping.capabilities:
            if cap in self.config.confirm_capabilities:
                self._stats["confirmation_required"] += 1
                return False, f"CONFIRM: Capability '{cap}' requires human confirmation"

        # Rate limiting
        rate = self._get_rate_state(mapping.semantic_name)
        now = time.monotonic()

        # Cooldown check
        if (now - rate.last_write) < self.config.cooldown_seconds:
            remaining = self.config.cooldown_seconds - (now - rate.last_write)
            self._stats["rate_limited"] += 1
            return False, f"Cooldown: wait {remaining:.1f}s before writing to {mapping.semantic_name}"

        # Window rate check
        if now - rate.window_start > 60.0:
            rate.window_start = now
            rate.writes_in_window = 0

        if rate.writes_in_window >= self.config.max_writes_per_minute:
            self._stats["rate_limited"] += 1
            return False, f"Rate limit: {mapping.semantic_name} exceeded {self.config.max_writes_per_minute} writes/min"

        self._stats["allowed"] += 1
        return True, "ok"

    def record_write(self, device_name: str) -> None:
        """Record that a write was executed (call after successful write)."""
        mapping = self.spaces.resolve_name(device_name)
        if not mapping:
            return
        rate = self._get_rate_state(mapping.semantic_name)
        now = time.monotonic()
        rate.last_write = now
        rate.writes_in_window += 1

    def request_confirmation(self, confirmation_id: str, device_name: str,
                              action: str, value: Any = None) -> dict[str, Any]:
        """Register a pending confirmation request."""
        self._pending_confirmations[confirmation_id] = {
            "device_name": device_name,
            "action": action,
            "value": value,
            "requested_at": time.time(),
            "status": "pending",
        }
        return {"confirmation_id": confirmation_id, "status": "pending"}

    def approve_confirmation(self, confirmation_id: str) -> dict[str, Any] | None:
        """Approve a pending confirmation. Returns the original request or None."""
        self._purge_expired_confirmations()
        req = self._pending_confirmations.pop(confirmation_id, None)
        if req:
            req["status"] = "approved"
        return req

    def deny_confirmation(self, confirmation_id: str) -> None:
        """Deny and remove a pending confirmation."""
        self._purge_expired_confirmations()
        self._pending_confirmations.pop(confirmation_id, None)

    def list_pending_confirmations(self) -> list[dict[str, Any]]:
        """List all pending confirmation requests, excluding expired ones."""
        self._purge_expired_confirmations()
        return [
            {"confirmation_id": cid, **req}
            for cid, req in self._pending_confirmations.items()
            if req["status"] == "pending"
        ]

    def _purge_expired_confirmations(self) -> int:
        """Remove expired confirmation requests. Returns count removed."""
        now = time.time()
        ttl = self.config.confirmation_ttl_seconds
        expired = [
            cid for cid, req in self._pending_confirmations.items()
            if req["status"] == "pending" and (now - req["requested_at"]) > ttl
        ]
        for cid in expired:
            del self._pending_confirmations[cid]
        if expired:
            logger.debug("Purged %d expired confirmations", len(expired))
        return len(expired)

    def _get_rate_state(self, name: str) -> RateLimitState:
        if name not in self._rate_limits:
            self._rate_limits[name] = RateLimitState()
        return self._rate_limits[name]

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)
