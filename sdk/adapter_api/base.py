"""Abstract base class for all protocol adapters.

Every adapter (KinCony, Shelly, MQTT, Modbus, etc.) must subclass
`Adapter` and implement all abstract methods. The orchestrator interacts
with adapters exclusively through this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

AdapterClass = Literal[
    "direct_device", "bridge", "network_controller", "bus", "server", "composite"
]


@dataclass
class SecretRef:
    name: str
    handle: str


@dataclass
class ConnectionProfile:
    profile_id: str
    fields: dict[str, Any]
    secrets: list[SecretRef] = field(default_factory=list)


@dataclass
class ConnectionTemplate:
    adapter_id: str
    display_name: str
    category: str = "general"
    discovery_methods: list[str] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    optional_fields: list[str] = field(default_factory=list)
    secret_fields: list[str] = field(default_factory=list)
    files_to_upload: list[str] = field(default_factory=list)
    physical_actions: list[str] = field(default_factory=list)
    supports_auto_inventory: bool = True
    supports_local_only_mode: bool = True
    risk_level: str = "low"


@dataclass
class DiscoveryRequest:
    site_id: str
    methods: list[str]
    scope: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = 15


@dataclass
class DiscoveredTarget:
    discovery_id: str
    adapter_id: str
    native_ref: str
    title: str
    address: str | None = None
    fingerprint: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    suggested_profile_id: str | None = None


@dataclass
class CommissionResult:
    connection_id: str
    status: Literal["ok", "partial", "failed"]
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class InventorySnapshot:
    connection_id: str
    devices: list[dict[str, Any]]
    endpoints: list[dict[str, Any]]
    points: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    status: Literal["healthy", "degraded", "offline", "error"]
    details: dict[str, Any] = field(default_factory=dict)


class Adapter(ABC):
    """Abstract base class every protocol adapter must implement."""

    adapter_id: str
    adapter_class: AdapterClass

    @abstractmethod
    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        """Discover candidate devices/bridges on the network."""
        ...

    @abstractmethod
    async def commission(
        self,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        """Establish a connection to a discovered or manually specified target."""
        ...

    @abstractmethod
    async def inventory(self, connection_id: str) -> InventorySnapshot:
        """Enumerate all devices, endpoints, and points for a connection."""
        ...

    @abstractmethod
    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to real-time state changes. Yields event dicts."""
        ...

    @abstractmethod
    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        """Read the current value of a single point."""
        ...

    @abstractmethod
    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        """Execute a command (write) against a device endpoint."""
        ...

    @abstractmethod
    async def health(self, connection_id: str) -> HealthStatus:
        """Return the health status of a connection."""
        ...

    @abstractmethod
    async def teardown(self, connection_id: str) -> None:
        """Cleanly disconnect and release resources for a connection."""
        ...

    def connection_templates(self) -> list[ConnectionTemplate]:
        """Return the connection templates this adapter supports.

        Override in subclasses to declare what fields/secrets are needed.
        """
        return []
