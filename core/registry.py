"""Adapter registry — loads, configures, and manages adapter instances.

The registry is the single point of contact for all adapter operations.
It maps adapter IDs to adapter instances, manages connections, and
coordinates inventory persistence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sdk.adapter_api.base import (
    Adapter,
    CommissionResult,
    ConnectionProfile,
    DiscoveredTarget,
    DiscoveryRequest,
    HealthStatus,
    InventorySnapshot,
    SecretRef,
)
from sdk.adapter_api.errors import InvalidTargetError

from core.event_bus import EventBus
from core.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass
class RegisteredAdapter:
    """An adapter registered with the runtime."""
    adapter: Adapter
    connections: list[str] = field(default_factory=list)


class AdapterRegistry:
    """Manages adapter lifecycle and routes operations to the correct adapter."""

    def __init__(self, event_bus: EventBus, state_store: StateStore):
        self._adapters: dict[str, RegisteredAdapter] = {}
        self._connection_to_adapter: dict[str, str] = {}
        self.event_bus = event_bus
        self.state_store = state_store

    def register(self, adapter: Adapter) -> None:
        """Register an adapter instance with the registry."""
        aid = adapter.adapter_id
        if aid in self._adapters:
            logger.warning("Adapter %s already registered, replacing", aid)
        self._adapters[aid] = RegisteredAdapter(adapter=adapter)
        logger.info("Registered adapter: %s (%s)", aid, adapter.adapter_class)

    def unregister(self, adapter_id: str) -> None:
        """Remove an adapter from the registry."""
        self._adapters.pop(adapter_id, None)
        # Clean up connection mappings
        to_remove = [cid for cid, aid in self._connection_to_adapter.items() if aid == adapter_id]
        for cid in to_remove:
            del self._connection_to_adapter[cid]

    def get_adapter(self, adapter_id: str) -> Adapter:
        """Get an adapter instance by ID."""
        reg = self._adapters.get(adapter_id)
        if not reg:
            raise InvalidTargetError(f"No adapter registered: {adapter_id}")
        return reg.adapter

    def get_adapter_for_connection(self, connection_id: str) -> Adapter:
        """Get the adapter that owns a connection."""
        aid = self._connection_to_adapter.get(connection_id)
        if not aid:
            raise InvalidTargetError(f"No adapter for connection: {connection_id}")
        return self.get_adapter(aid)

    def list_adapters(self) -> list[dict[str, Any]]:
        """List all registered adapters and their connections."""
        return [
            {
                "adapter_id": aid,
                "adapter_class": reg.adapter.adapter_class,
                "connections": list(reg.connections),
                "templates": [
                    {"adapter_id": t.adapter_id, "display_name": t.display_name, "required_fields": t.required_fields}
                    for t in reg.adapter.connection_templates()
                ],
            }
            for aid, reg in self._adapters.items()
        ]

    # -- High-level operations --

    async def discover(self, adapter_id: str, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        """Run discovery on a specific adapter."""
        adapter = self.get_adapter(adapter_id)
        targets = await adapter.discover(request)
        for t in targets:
            self.event_bus.publish_nowait({
                "type": "device.discovered",
                "adapter_id": adapter_id,
                "target": {
                    "discovery_id": t.discovery_id,
                    "title": t.title,
                    "address": t.address,
                    "confidence": t.confidence,
                },
            })
        return targets

    async def commission(
        self,
        adapter_id: str,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        """Commission a connection through an adapter and persist it."""
        adapter = self.get_adapter(adapter_id)
        result = await adapter.commission(target, profile)

        if result.status == "ok" and result.connection_id:
            cid = result.connection_id
            self._connection_to_adapter[cid] = adapter_id
            self._adapters[adapter_id].connections.append(cid)

            # Persist connection config
            await self.state_store.save_connection(
                cid, adapter_id,
                {"profile_id": profile.profile_id, "fields": profile.fields},
            )

            self.event_bus.publish_nowait({
                "type": "connection.state_changed",
                "connection_id": cid,
                "adapter_id": adapter_id,
                "status": "commissioned",
            })
            logger.info("Commissioned connection %s via %s", cid, adapter_id)

        return result

    async def commission_simple(
        self,
        adapter_id: str,
        profile_id: str,
        fields: dict[str, Any],
        secrets: dict[str, str] | None = None,
    ) -> CommissionResult:
        """Simplified commissioning helper.

        Args:
            adapter_id: Which adapter to use.
            profile_id: Connection profile ID (e.g. "tasmota_http").
            fields: Connection fields (e.g. {"host": "192.168.1.100"}).
            secrets: Optional dict of secret_name -> secret_value.
        """
        secret_refs = [SecretRef(name=k, handle=v) for k, v in (secrets or {}).items()]
        profile = ConnectionProfile(profile_id=profile_id, fields=fields, secrets=secret_refs)
        return await self.commission(adapter_id, None, profile)

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        """Run inventory and persist results."""
        adapter = self.get_adapter_for_connection(connection_id)
        snapshot = await adapter.inventory(connection_id)

        # Persist to state store
        await self.state_store.persist_inventory(connection_id, {
            "devices": snapshot.devices,
            "endpoints": snapshot.endpoints,
            "points": snapshot.points,
        })

        self.event_bus.publish_nowait({
            "type": "device.inventory_changed",
            "connection_id": connection_id,
            "device_count": len(snapshot.devices),
            "endpoint_count": len(snapshot.endpoints),
            "point_count": len(snapshot.points),
        })
        logger.info(
            "Inventory for %s: %d devices, %d endpoints, %d points",
            connection_id, len(snapshot.devices), len(snapshot.endpoints), len(snapshot.points),
        )

        return snapshot

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        """Read a point and update the state store."""
        adapter = self.get_adapter_for_connection(connection_id)
        result = await adapter.read_point(connection_id, point_id)

        # Persist last-known value
        await self.state_store.save_point_value(
            point_id,
            value=result.get("value"),
            quality=result.get("quality"),
            raw=result.get("raw"),
        )

        self.event_bus.publish_nowait({
            "type": "point.reported",
            "connection_id": connection_id,
            "point_id": point_id,
            "value": result.get("value"),
            "quality": result.get("quality"),
        })

        return result

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        """Execute a command and audit it."""
        adapter = self.get_adapter_for_connection(connection_id)

        # Audit: before
        await self.state_store.audit(
            "command.accepted",
            connection_id=connection_id,
            command_id=command.get("command_id"),
            initiator=command.get("context", {}).get("initiator", "unknown"),
            detail=command,
        )

        result = await adapter.execute(connection_id, command)

        # Audit: after
        status = result.get("status", "unknown")
        await self.state_store.audit(
            f"command.{status}",
            connection_id=connection_id,
            command_id=command.get("command_id"),
            detail=result,
        )

        self.event_bus.publish_nowait({
            "type": f"command.{status}",
            "connection_id": connection_id,
            "command_id": command.get("command_id"),
            "result": result,
        })

        return result

    async def health(self, connection_id: str) -> HealthStatus:
        """Check health of a connection."""
        adapter = self.get_adapter_for_connection(connection_id)
        status = await adapter.health(connection_id)

        self.event_bus.publish_nowait({
            "type": "device.health_changed",
            "connection_id": connection_id,
            "status": status.status,
            "details": status.details,
        })

        return status

    async def health_all(self) -> dict[str, HealthStatus]:
        """Check health of all active connections."""
        results = {}
        for cid in list(self._connection_to_adapter.keys()):
            try:
                results[cid] = await self.health(cid)
            except Exception as e:
                results[cid] = HealthStatus(status="error", details={"error": str(e)})
        return results

    async def teardown(self, connection_id: str) -> None:
        """Teardown a connection and clean up."""
        adapter = self.get_adapter_for_connection(connection_id)
        await adapter.teardown(connection_id)

        # Update state store
        await self.state_store.save_connection(
            connection_id,
            self._connection_to_adapter[connection_id],
            {},
            status="disconnected",
        )

        # Clean up registry
        aid = self._connection_to_adapter.pop(connection_id, None)
        if aid and aid in self._adapters:
            conns = self._adapters[aid].connections
            if connection_id in conns:
                conns.remove(connection_id)

        self.event_bus.publish_nowait({
            "type": "connection.state_changed",
            "connection_id": connection_id,
            "status": "disconnected",
        })
        logger.info("Torn down connection %s", connection_id)

    async def teardown_all(self) -> None:
        """Teardown all connections across all adapters."""
        for cid in list(self._connection_to_adapter.keys()):
            try:
                await self.teardown(cid)
            except Exception:
                logger.exception("Error tearing down %s", cid)

    async def restore_connections(self) -> int:
        """Restore connections from the state store on startup.

        Re-commissions all previously active connections.
        Returns the count of successfully restored connections.
        """
        connections = await self.state_store.list_connections()
        restored = 0
        for conn in connections:
            if conn["status"] != "commissioned":
                continue
            aid = conn["adapter_id"]
            if aid not in self._adapters:
                logger.warning("Cannot restore connection %s: adapter %s not registered",
                             conn["connection_id"], aid)
                continue

            profile_data = conn.get("profile", {})
            profile = ConnectionProfile(
                profile_id=profile_data.get("profile_id", ""),
                fields=profile_data.get("fields", {}),
            )
            try:
                result = await self.commission(aid, None, profile)
                if result.status == "ok":
                    restored += 1
                    # Re-run inventory
                    await self.inventory(result.connection_id)
            except Exception:
                logger.exception("Failed to restore connection %s", conn["connection_id"])

        logger.info("Restored %d/%d connections", restored, len(connections))
        return restored
