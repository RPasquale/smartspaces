"""Safety class enforcement for adapter commands.

Provides a guard that checks safety classifications before
allowing write operations to proceed.
"""

from __future__ import annotations

from sdk.adapter_api.errors import SafetyBlockedError
from sdk.adapter_api.models import CommandEnvelope, Endpoint, SafetyClass

# Safety classes that require explicit confirmation
_REQUIRES_CONFIRMATION = {SafetyClass.S3_OPERATIONAL_EQUIPMENT, SafetyClass.S4_RESTRICTED}
_FORBIDDEN = {SafetyClass.S5_FORBIDDEN}

# Numeric ordering for comparisons
_SAFETY_ORDER = {
    SafetyClass.S0_READ_ONLY: 0,
    SafetyClass.S1_NON_DESTRUCTIVE: 1,
    SafetyClass.S2_COMFORT_EQUIPMENT: 2,
    SafetyClass.S3_OPERATIONAL_EQUIPMENT: 3,
    SafetyClass.S4_RESTRICTED: 4,
    SafetyClass.S5_FORBIDDEN: 5,
}


class SafetyGuard:
    """Validates commands against endpoint safety classifications."""

    def __init__(self, max_auto_approve: SafetyClass = SafetyClass.S2_COMFORT_EQUIPMENT):
        self.max_auto_approve = max_auto_approve

    def check(self, endpoint: Endpoint, command: CommandEnvelope) -> None:
        """Raise SafetyBlockedError if the command violates safety policy.

        Args:
            endpoint: The target endpoint with its safety classification.
            command: The command being executed.

        Raises:
            SafetyBlockedError: If the safety class forbids the operation
                or requires confirmation that hasn't been provided.
        """
        sc = endpoint.safety_class

        if sc in _FORBIDDEN:
            raise SafetyBlockedError(
                f"Endpoint {endpoint.endpoint_id} is classified {sc.value} (forbidden). "
                "Command path is not exposed to the agent.",
            )

        if sc == SafetyClass.S0_READ_ONLY:
            raise SafetyBlockedError(
                f"Endpoint {endpoint.endpoint_id} is read-only ({sc.value}).",
            )

        if (
            sc in _REQUIRES_CONFIRMATION
            and _SAFETY_ORDER[sc] > _SAFETY_ORDER[self.max_auto_approve]
            and not command.safety.requires_confirmation
        ):
            raise SafetyBlockedError(
                f"Endpoint {endpoint.endpoint_id} is classified {sc.value} and "
                f"requires explicit confirmation. Set requires_confirmation=true.",
            )

    @staticmethod
    def level(safety_class: SafetyClass) -> int:
        """Return numeric safety level for comparison."""
        return _SAFETY_ORDER[safety_class]
