"""Tests for the safety guard."""

import pytest

from sdk.adapter_api.errors import SafetyBlockedError
from sdk.adapter_api.models import (
    CommandEnvelope,
    CommandSafety,
    CommandTarget,
    Endpoint,
    SafetyClass,
)
from sdk.adapter_api.safety import SafetyGuard


def _make_endpoint(safety_class: SafetyClass) -> Endpoint:
    return Endpoint(
        endpoint_id="ep_test",
        device_id="dev_test",
        native_endpoint_ref="test",
        endpoint_type="relay",
        safety_class=safety_class,
    )


def _make_command(requires_confirmation: bool = False) -> CommandEnvelope:
    return CommandEnvelope(
        command_id="cmd_test",
        target=CommandTarget(device_id="dev_test", endpoint_id="ep_test"),
        capability="binary_switch",
        verb="set",
        params={"value": True},
        safety=CommandSafety(requires_confirmation=requires_confirmation),
    )


class TestSafetyGuard:
    def test_s1_allows_write(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S1_NON_DESTRUCTIVE)
        guard.check(ep, _make_command())  # Should not raise

    def test_s2_allows_write(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S2_COMFORT_EQUIPMENT)
        guard.check(ep, _make_command())  # Should not raise

    def test_s0_blocks_write(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S0_READ_ONLY)
        with pytest.raises(SafetyBlockedError):
            guard.check(ep, _make_command())

    def test_s5_blocks_write(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S5_FORBIDDEN)
        with pytest.raises(SafetyBlockedError):
            guard.check(ep, _make_command())

    def test_s3_requires_confirmation(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S3_OPERATIONAL_EQUIPMENT)
        with pytest.raises(SafetyBlockedError, match="requires explicit confirmation"):
            guard.check(ep, _make_command(requires_confirmation=False))

    def test_s3_allows_with_confirmation(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S3_OPERATIONAL_EQUIPMENT)
        guard.check(ep, _make_command(requires_confirmation=True))  # Should not raise

    def test_s4_requires_confirmation(self):
        guard = SafetyGuard()
        ep = _make_endpoint(SafetyClass.S4_RESTRICTED)
        with pytest.raises(SafetyBlockedError):
            guard.check(ep, _make_command(requires_confirmation=False))

    def test_custom_max_auto_approve(self):
        guard = SafetyGuard(max_auto_approve=SafetyClass.S3_OPERATIONAL_EQUIPMENT)
        ep = _make_endpoint(SafetyClass.S3_OPERATIONAL_EQUIPMENT)
        guard.check(ep, _make_command())  # S3 is now auto-approved

    def test_level_ordering(self):
        assert SafetyGuard.level(SafetyClass.S0_READ_ONLY) < SafetyGuard.level(SafetyClass.S5_FORBIDDEN)
        assert SafetyGuard.level(SafetyClass.S2_COMFORT_EQUIPMENT) < SafetyGuard.level(SafetyClass.S3_OPERATIONAL_EQUIPMENT)
