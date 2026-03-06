"""FastAPI REST API for the adapter runtime.

Exposes endpoints for listing adapters, managing connections,
reading points, executing commands, and checking health.
External consumers (web apps, mobile apps, agents) use this API.
"""

from __future__ import annotations

from typing import Any

from core.registry import AdapterRegistry
from core.scheduler import Scheduler
from core.state_store import StateStore
from sdk.adapter_api.base import ConnectionProfile, DiscoveryRequest, SecretRef

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


# -- Request/Response models --

if HAS_FASTAPI:

    class DiscoverRequest(BaseModel):
        adapter_id: str
        site_id: str = "default"
        methods: list[str] = Field(default_factory=lambda: ["http_probe", "manual_ip"])
        scope: dict[str, Any] = Field(default_factory=dict)
        timeout_sec: int = 15

    class CommissionRequest(BaseModel):
        adapter_id: str
        profile_id: str
        fields: dict[str, Any]
        secrets: dict[str, str] = Field(default_factory=dict)

    class ExecuteRequest(BaseModel):
        connection_id: str
        command_id: str | None = None
        target: dict[str, str]
        capability: str
        verb: str = "set"
        params: dict[str, Any] = Field(default_factory=dict)

    class ReadPointRequest(BaseModel):
        connection_id: str
        point_id: str


def create_api(
    registry: AdapterRegistry,
    state_store: StateStore,
    scheduler: Scheduler,
) -> Any:
    """Create and configure the FastAPI application.

    Returns the FastAPI app instance. Returns None if fastapi is not installed.
    """
    if not HAS_FASTAPI:
        return None

    app = FastAPI(
        title="Physical Space Adapter API",
        version="0.1.0",
        description="REST API for the Universal Physical Space Adapter system",
    )

    # -- Adapters --

    @app.get("/api/adapters")
    async def list_adapters():
        """List all registered adapters."""
        return {"adapters": registry.list_adapters()}

    # -- Discovery --

    @app.post("/api/discover")
    async def discover(req: DiscoverRequest):
        """Run device discovery for an adapter."""
        try:
            request = DiscoveryRequest(
                site_id=req.site_id,
                methods=req.methods,
                scope=req.scope,
                timeout_sec=req.timeout_sec,
            )
            targets = await registry.discover(req.adapter_id, request)
            return {
                "targets": [
                    {
                        "discovery_id": t.discovery_id,
                        "adapter_id": t.adapter_id,
                        "title": t.title,
                        "address": t.address,
                        "confidence": t.confidence,
                        "fingerprint": t.fingerprint,
                    }
                    for t in targets
                ]
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- Connections --

    @app.post("/api/connections")
    async def commission(req: CommissionRequest):
        """Commission a new connection."""
        try:
            result = await registry.commission_simple(
                req.adapter_id, req.profile_id, req.fields, req.secrets or None
            )
            if result.status != "ok":
                raise HTTPException(status_code=400, detail=result.diagnostics)

            # Auto-inventory after commission
            snapshot = await registry.inventory(result.connection_id)

            # Auto-schedule polling for readable points
            scheduler.add_targets_from_inventory(result.connection_id, snapshot.points)

            return {
                "connection_id": result.connection_id,
                "status": result.status,
                "devices": len(snapshot.devices),
                "endpoints": len(snapshot.endpoints),
                "points": len(snapshot.points),
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/connections")
    async def list_connections():
        """List all connections."""
        connections = await state_store.list_connections()
        return {"connections": connections}

    @app.delete("/api/connections/{connection_id}")
    async def disconnect(connection_id: str):
        """Teardown a connection."""
        try:
            scheduler.remove_connection(connection_id)
            await registry.teardown(connection_id)
            return {"status": "disconnected", "connection_id": connection_id}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- Devices --

    @app.get("/api/devices")
    async def list_devices(connection_id: str | None = None):
        """List all devices, optionally filtered by connection."""
        devices = await state_store.list_devices(connection_id)
        return {"devices": devices}

    @app.get("/api/devices/{device_id}")
    async def get_device(device_id: str):
        """Get a single device."""
        device = await state_store.get_device(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return device

    @app.get("/api/devices/{device_id}/endpoints")
    async def list_endpoints(device_id: str):
        """List endpoints for a device."""
        endpoints = await state_store.list_endpoints(device_id)
        return {"endpoints": endpoints}

    @app.get("/api/devices/{device_id}/points")
    async def list_device_points(device_id: str):
        """List all points for a device (across all its endpoints)."""
        endpoints = await state_store.list_endpoints(device_id)
        all_points = []
        for ep in endpoints:
            points = await state_store.list_points(ep["endpoint_id"])
            all_points.extend(points)
        return {"points": all_points}

    # -- Points --

    @app.get("/api/points")
    async def list_points(endpoint_id: str | None = None):
        """List all points, optionally filtered by endpoint."""
        points = await state_store.list_points(endpoint_id)
        return {"points": points}

    @app.post("/api/points/read")
    async def read_point(req: ReadPointRequest):
        """Read the current value of a point from the device."""
        try:
            result = await registry.read_point(req.connection_id, req.point_id)
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/points/{point_id}/value")
    async def get_point_value(point_id: str):
        """Get the last-known value of a point from the state store."""
        value = await state_store.get_point_value(point_id)
        if not value:
            raise HTTPException(status_code=404, detail="No stored value for this point")
        return value

    @app.get("/api/values")
    async def get_all_values(connection_id: str | None = None):
        """Get all last-known point values."""
        values = await state_store.get_all_point_values(connection_id)
        return {"values": values}

    # -- Commands --

    @app.post("/api/commands")
    async def execute_command(req: ExecuteRequest):
        """Execute a command against a device endpoint."""
        try:
            command = {
                "command_id": req.command_id or f"api_cmd",
                "target": req.target,
                "capability": req.capability,
                "verb": req.verb,
                "params": req.params,
                "context": {"initiator": "api"},
            }
            result = await registry.execute(req.connection_id, command)
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- Health --

    @app.get("/api/health")
    async def health_all():
        """Check health of all connections."""
        statuses = await registry.health_all()
        return {
            "connections": {
                cid: {"status": hs.status, "details": hs.details}
                for cid, hs in statuses.items()
            }
        }

    @app.get("/api/health/{connection_id}")
    async def health(connection_id: str):
        """Check health of a specific connection."""
        try:
            status = await registry.health(connection_id)
            return {"status": status.status, "details": status.details}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- Scheduler --

    @app.get("/api/scheduler")
    async def scheduler_status():
        """Get scheduler statistics and targets."""
        return {
            "stats": scheduler.stats,
            "targets": scheduler.targets,
        }

    # -- Audit --

    @app.get("/api/audit")
    async def audit_log(limit: int = 100, device_id: str | None = None):
        """Get recent audit log entries."""
        entries = await state_store.get_audit_log(limit=limit, device_id=device_id)
        return {"entries": entries}

    # -- System --

    @app.get("/api/system/stats")
    async def system_stats():
        """Get system-wide statistics."""
        return {
            "adapters": len(registry.list_adapters()),
            "connections": len(await state_store.list_connections()),
            "devices": len(await state_store.list_devices()),
            "event_bus": registry.event_bus.stats,
            "scheduler": scheduler.stats,
        }

    return app
