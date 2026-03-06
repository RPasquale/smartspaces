"""FastAPI REST API for the adapter runtime.

Exposes endpoints for listing adapters, managing connections,
reading points, executing commands, and checking health.
External consumers (web apps, mobile apps, agents) use this API.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from typing import Any

from core.registry import AdapterRegistry
from core.scheduler import Scheduler
from core.state_store import StateStore
from sdk.adapter_api.base import ConnectionProfile, DiscoveryRequest, SecretRef

logger = logging.getLogger(__name__)

try:
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field

    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

# Fields that must never appear in API responses
_SECRET_FIELD_NAMES = frozenset({
    "password", "secret", "token", "api_key", "apikey",
    "client_secret", "client_key", "ca_cert", "client_cert",
    "private_key",
})


def _sanitize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Remove secret fields from a connection profile before returning to client."""
    cleaned = {}
    for k, v in profile.items():
        if k.lower() in _SECRET_FIELD_NAMES:
            cleaned[k] = "********"
        elif isinstance(v, dict):
            cleaned[k] = _sanitize_profile(v)
        else:
            cleaned[k] = v
    return cleaned


def _safe_error(e: Exception) -> str:
    """Return a sanitized error message suitable for API responses."""
    etype = type(e).__name__
    msg = str(e)
    # Strip file paths and stack traces
    if "/" in msg or "\\" in msg:
        return f"{etype}: operation failed"
    # Truncate overly long messages
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return f"{etype}: {msg}"


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
        idempotency_key: str | None = None
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
    api_keys: list[str] | None = None,
) -> Any:
    """Create and configure the FastAPI application.

    Args:
        registry: The adapter registry.
        state_store: The state store.
        scheduler: The scheduler.
        api_keys: List of valid API key strings. If None, reads from
                  SMARTSPACES_API_KEYS env var (comma-separated) or
                  generates a random key and logs it.

    Returns the FastAPI app instance, or None if fastapi is not installed.
    """
    if not HAS_FASTAPI:
        return None

    # Resolve API keys
    resolved_keys = api_keys
    if resolved_keys is None:
        env_keys = os.environ.get("SMARTSPACES_API_KEYS", "")
        if env_keys.strip():
            resolved_keys = [k.strip() for k in env_keys.split(",") if k.strip()]

    if not resolved_keys:
        generated = secrets.token_urlsafe(32)
        resolved_keys = [generated]
        logger.warning(
            "No API keys configured. Generated temporary key: %s  "
            "Set SMARTSPACES_API_KEYS env var for persistent keys.",
            generated,
        )

    # Hash keys for constant-time comparison
    key_hashes = {hashlib.sha256(k.encode()).hexdigest() for k in resolved_keys}

    bearer_scheme = HTTPBearer(auto_error=False)

    async def verify_api_key(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> str:
        # Check Authorization: Bearer <key>
        if credentials and credentials.scheme.lower() == "bearer":
            h = hashlib.sha256(credentials.credentials.encode()).hexdigest()
            if h in key_hashes:
                return credentials.credentials

        # Check X-API-Key header
        header_key = request.headers.get("x-api-key", "")
        if header_key:
            h = hashlib.sha256(header_key.encode()).hexdigest()
            if h in key_hashes:
                return header_key

        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    app = FastAPI(
        title="Physical Space Adapter API",
        version="0.1.0",
        description="REST API for the Universal Physical Space Adapter system",
    )

    # Track idempotency keys to prevent duplicate command execution
    _idempotency_cache: dict[str, dict[str, Any]] = {}

    # -- Adapters --

    @app.get("/api/adapters")
    async def list_adapters(_key: str = Depends(verify_api_key)):
        """List all registered adapters."""
        return {"adapters": registry.list_adapters()}

    # -- Discovery --

    @app.post("/api/discover")
    async def discover(req: DiscoverRequest, _key: str = Depends(verify_api_key)):
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
            raise HTTPException(status_code=400, detail=_safe_error(e))

    # -- Connections --

    @app.post("/api/connections")
    async def commission(req: CommissionRequest, _key: str = Depends(verify_api_key)):
        """Commission a new connection."""
        try:
            result = await registry.commission_simple(
                req.adapter_id, req.profile_id, req.fields, req.secrets or None
            )
            if result.status != "ok":
                raise HTTPException(status_code=400, detail="Commission failed")

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
            raise HTTPException(status_code=500, detail=_safe_error(e))

    @app.get("/api/connections")
    async def list_connections(_key: str = Depends(verify_api_key)):
        """List all connections (secrets redacted)."""
        connections = await state_store.list_connections()
        return {
            "connections": [
                {
                    **conn,
                    "profile": _sanitize_profile(conn.get("profile", {})),
                }
                for conn in connections
            ]
        }

    @app.delete("/api/connections/{connection_id}")
    async def disconnect(connection_id: str, _key: str = Depends(verify_api_key)):
        """Teardown a connection."""
        try:
            scheduler.remove_connection(connection_id)
            await registry.teardown(connection_id)
            return {"status": "disconnected", "connection_id": connection_id}
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_error(e))

    # -- Devices --

    @app.get("/api/devices")
    async def list_devices(
        connection_id: str | None = None, _key: str = Depends(verify_api_key),
    ):
        """List all devices, optionally filtered by connection."""
        devices = await state_store.list_devices(connection_id)
        return {"devices": devices}

    @app.get("/api/devices/{device_id}")
    async def get_device(device_id: str, _key: str = Depends(verify_api_key)):
        """Get a single device."""
        device = await state_store.get_device(device_id)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        return device

    @app.get("/api/devices/{device_id}/endpoints")
    async def list_endpoints(device_id: str, _key: str = Depends(verify_api_key)):
        """List endpoints for a device."""
        endpoints = await state_store.list_endpoints(device_id)
        return {"endpoints": endpoints}

    @app.get("/api/devices/{device_id}/points")
    async def list_device_points(device_id: str, _key: str = Depends(verify_api_key)):
        """List all points for a device (across all its endpoints)."""
        endpoints = await state_store.list_endpoints(device_id)
        all_points = []
        for ep in endpoints:
            points = await state_store.list_points(ep["endpoint_id"])
            all_points.extend(points)
        return {"points": all_points}

    # -- Points --

    @app.get("/api/points")
    async def list_points(
        endpoint_id: str | None = None, _key: str = Depends(verify_api_key),
    ):
        """List all points, optionally filtered by endpoint."""
        points = await state_store.list_points(endpoint_id)
        return {"points": points}

    @app.post("/api/points/read")
    async def read_point(req: ReadPointRequest, _key: str = Depends(verify_api_key)):
        """Read the current value of a point from the device."""
        try:
            result = await registry.read_point(req.connection_id, req.point_id)
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_error(e))

    @app.get("/api/points/{point_id}/value")
    async def get_point_value(point_id: str, _key: str = Depends(verify_api_key)):
        """Get the last-known value of a point from the state store."""
        value = await state_store.get_point_value(point_id)
        if not value:
            raise HTTPException(status_code=404, detail="No stored value for this point")
        return value

    @app.get("/api/values")
    async def get_all_values(
        connection_id: str | None = None, _key: str = Depends(verify_api_key),
    ):
        """Get all last-known point values."""
        values = await state_store.get_all_point_values(connection_id)
        return {"values": values}

    # -- Commands --

    @app.post("/api/commands")
    async def execute_command(req: ExecuteRequest, _key: str = Depends(verify_api_key)):
        """Execute a command against a device endpoint."""
        # Idempotency check
        if req.idempotency_key:
            if req.idempotency_key in _idempotency_cache:
                return _idempotency_cache[req.idempotency_key]

        try:
            command = {
                "command_id": req.command_id or f"cmd_{uuid.uuid4().hex[:8]}",
                "target": req.target,
                "capability": req.capability,
                "verb": req.verb,
                "params": req.params,
                "context": {"initiator": "api"},
            }
            result = await registry.execute(req.connection_id, command)

            # Cache idempotent result
            if req.idempotency_key:
                _idempotency_cache[req.idempotency_key] = result
                # Limit cache size
                if len(_idempotency_cache) > 10_000:
                    oldest = next(iter(_idempotency_cache))
                    del _idempotency_cache[oldest]

            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_error(e))

    # -- Health --

    @app.get("/api/health")
    async def health_all(_key: str = Depends(verify_api_key)):
        """Check health of all connections."""
        statuses = await registry.health_all()
        return {
            "connections": {
                cid: {"status": hs.status, "details": hs.details}
                for cid, hs in statuses.items()
            }
        }

    @app.get("/api/health/{connection_id}")
    async def health(connection_id: str, _key: str = Depends(verify_api_key)):
        """Check health of a specific connection."""
        try:
            status = await registry.health(connection_id)
            return {"status": status.status, "details": status.details}
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_error(e))

    # -- Scheduler --

    @app.get("/api/scheduler")
    async def scheduler_status(_key: str = Depends(verify_api_key)):
        """Get scheduler statistics and targets."""
        return {
            "stats": scheduler.stats,
            "targets": scheduler.targets,
        }

    # -- Audit --

    @app.get("/api/audit")
    async def audit_log(
        limit: int = 100, device_id: str | None = None,
        _key: str = Depends(verify_api_key),
    ):
        """Get recent audit log entries."""
        entries = await state_store.get_audit_log(limit=limit, device_id=device_id)
        return {"entries": entries}

    # -- System --

    @app.get("/api/system/stats")
    async def system_stats(_key: str = Depends(verify_api_key)):
        """Get system-wide statistics."""
        return {
            "adapters": len(registry.list_adapters()),
            "connections": len(await state_store.list_connections()),
            "devices": len(await state_store.list_devices()),
            "event_bus": registry.event_bus.stats,
            "scheduler": scheduler.stats,
        }

    return app
