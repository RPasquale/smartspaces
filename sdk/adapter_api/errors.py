"""Canonical error types for the adapter SDK.

Every adapter maps native protocol errors into these canonical types.
The orchestrator handles these uniformly regardless of the underlying protocol.
"""

from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """Base class for all adapter errors."""

    code: str = "ADAPTER_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        native: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.native = native or {}
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "native": self.native,
        }


class UnreachableError(AdapterError):
    code = "UNREACHABLE"
    retryable = True


class AuthFailedError(AdapterError):
    code = "AUTH_FAILED"
    retryable = False


class PairingRequiredError(AdapterError):
    code = "PAIRING_REQUIRED"
    retryable = False


class UnsupportedFirmwareError(AdapterError):
    code = "UNSUPPORTED_FIRMWARE"
    retryable = False


class InvalidTargetError(AdapterError):
    code = "INVALID_TARGET"
    retryable = False


class InvalidValueError(AdapterError):
    code = "INVALID_VALUE"
    retryable = False


class WriteDeniedError(AdapterError):
    code = "WRITE_DENIED"
    retryable = False


class SafetyBlockedError(AdapterError):
    code = "SAFETY_BLOCKED"
    retryable = False


class TimeoutError(AdapterError):
    code = "TIMEOUT"
    retryable = True


class DeviceBusyError(AdapterError):
    code = "DEVICE_BUSY"
    retryable = True


class VerifyFailedError(AdapterError):
    code = "VERIFY_FAILED"
    retryable = True


class PartialInventoryError(AdapterError):
    code = "PARTIAL_INVENTORY"
    retryable = True


class NetworkDegradedError(AdapterError):
    code = "NETWORK_DEGRADED"
    retryable = True


class ProtocolError(AdapterError):
    code = "PROTOCOL_ERROR"
    retryable = False


class RateLimitedError(AdapterError):
    code = "RATE_LIMITED"
    retryable = True


class DependencyFailedError(AdapterError):
    code = "DEPENDENCY_FAILED"
    retryable = True
