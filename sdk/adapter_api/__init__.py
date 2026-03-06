"""Universal Physical Space Adapter SDK.

This package defines the canonical adapter interface that all protocol
adapters must implement, plus the shared domain models, error types,
safety classifications, and manifest schema.
"""

from sdk.adapter_api.base import (
    Adapter,
    AdapterClass,
    CommissionResult,
    ConnectionProfile,
    ConnectionTemplate,
    DiscoveredTarget,
    DiscoveryRequest,
    HealthStatus,
    InventorySnapshot,
    SecretRef,
)
from sdk.adapter_api.errors import (
    AdapterError,
    AuthFailedError,
    DeviceBusyError,
    InvalidTargetError,
    InvalidValueError,
    ProtocolError,
    RateLimitedError,
    SafetyBlockedError,
    TimeoutError,
    UnreachableError,
    VerifyFailedError,
    WriteDeniedError,
)
from sdk.adapter_api.models import (
    Asset,
    Capability,
    CommandEnvelope,
    Device,
    Endpoint,
    EventEnvelope,
    Organization,
    Point,
    Quality,
    RelationshipType,
    SafetyClass,
    Site,
    Space,
    ValueReport,
)
from sdk.adapter_api.safety import SafetyGuard

__all__ = [
    # Base
    "Adapter",
    "AdapterClass",
    "CommissionResult",
    "ConnectionProfile",
    "ConnectionTemplate",
    "DiscoveredTarget",
    "DiscoveryRequest",
    "HealthStatus",
    "InventorySnapshot",
    "SecretRef",
    # Models
    "Asset",
    "Capability",
    "CommandEnvelope",
    "Device",
    "Endpoint",
    "EventEnvelope",
    "Organization",
    "Point",
    "Quality",
    "RelationshipType",
    "SafetyClass",
    "Site",
    "Space",
    "ValueReport",
    # Errors
    "AdapterError",
    "AuthFailedError",
    "DeviceBusyError",
    "InvalidTargetError",
    "InvalidValueError",
    "ProtocolError",
    "RateLimitedError",
    "SafetyBlockedError",
    "TimeoutError",
    "UnreachableError",
    "VerifyFailedError",
    "WriteDeniedError",
    # Safety
    "SafetyGuard",
]
