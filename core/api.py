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

from agent.safety import AISafetyGuard, SafetyConfig
from agent.scenes import SceneEngine
from agent.spaces import SpaceRegistry
from agent.tools import ToolExecutor, ToolGenerator
from agent.events import EventStreamManager, parse_sse_filters
from agent.groups import GroupRegistry
from agent.history import ActionHistory
from agent.intent import IntentResolver
from agent.coordination import DeviceCoordinator
from agent.agent_scheduler import ActionScheduler
from agent.analytics import EnergyComfortAnalyzer
from agent.suggestions import ActionSuggester
from agent.discovery import CapabilityDescriber

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
    space_registry: SpaceRegistry | None = None,
    scene_engine: SceneEngine | None = None,
    safety_config: SafetyConfig | None = None,
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

    # ====================================================================
    # Agent Gateway API — semantic device control for AI agents
    # ====================================================================

    _space_registry = space_registry or SpaceRegistry()
    _scene_engine = scene_engine or SceneEngine()
    _safety_guard = AISafetyGuard(_space_registry, safety_config)
    _tool_executor = ToolExecutor(
        _space_registry, _safety_guard, _scene_engine,
        read_fn=registry.read_point,
        execute_fn=registry.execute,
    )
    _tool_generator = ToolGenerator(_space_registry)

    # Advanced components
    _event_manager = EventStreamManager()
    _group_registry = GroupRegistry(_space_registry)
    _action_history = ActionHistory()
    _coordinator = DeviceCoordinator()
    _analytics = EnergyComfortAnalyzer(_space_registry)
    _agent_scheduler = ActionScheduler(execute_callback=_tool_executor.call)
    _intent_resolver = IntentResolver(_space_registry, _group_registry, _scene_engine)
    _suggester = ActionSuggester(_space_registry, _scene_engine, _action_history, _analytics)
    _describer = CapabilityDescriber(_space_registry, _analytics)

    # Wire into tool executor
    _tool_executor.groups = _group_registry
    _tool_executor.history = _action_history
    _tool_executor.scheduler = _agent_scheduler
    _tool_executor.analytics = _analytics
    _tool_executor.coordinator = _coordinator
    _tool_executor.intent_resolver = _intent_resolver
    _tool_executor.suggester = _suggester
    _tool_executor.describer = _describer

    @app.get("/api/agent/spaces")
    async def agent_list_spaces(_key: str = Depends(verify_api_key)):
        """List all spaces and their devices."""
        return {"spaces": _space_registry.list_spaces()}

    @app.get("/api/agent/devices")
    async def agent_list_devices(
        space: str | None = None,
        capability: str | None = None,
        _key: str = Depends(verify_api_key),
    ):
        """List devices with optional filters."""
        return {"devices": _space_registry.list_devices(space=space, capability=capability)}

    @app.post("/api/agent/state")
    async def agent_get_state(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Read the current state of a device by semantic name."""
        result = await _tool_executor.call("get_device_state", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/api/agent/set")
    async def agent_set_device(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Control a device by semantic name."""
        result = await _tool_executor.call("set_device", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/api/agent/space_summary")
    async def agent_space_summary(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Get states of all devices in a space."""
        result = await _tool_executor.call("get_space_summary", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.get("/api/agent/scenes")
    async def agent_list_scenes(_key: str = Depends(verify_api_key)):
        """List available scenes."""
        return {"scenes": _scene_engine.list_scenes()}

    @app.post("/api/agent/scenes")
    async def agent_create_scene(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Create a new scene."""
        result = await _tool_executor.call("create_scene", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/api/agent/scenes/activate")
    async def agent_activate_scene(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Activate a named scene."""
        result = await _tool_executor.call("activate_scene", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.get("/api/agent/rules")
    async def agent_list_rules(_key: str = Depends(verify_api_key)):
        """List automation rules."""
        return {"rules": _scene_engine.list_rules()}

    @app.post("/api/agent/rules")
    async def agent_create_rule(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Create an automation rule."""
        try:
            rule = _scene_engine.add_rule(
                name=req["name"],
                display_name=req.get("display_name", req["name"]),
                condition=req["condition"],
                actions=req["actions"],
                cooldown_sec=req.get("cooldown_sec", 60.0),
            )
            return {"status": "created", "rule": rule.name}
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_error(e))

    @app.get("/api/agent/tools/{format}")
    async def agent_tool_definitions(format: str, _key: str = Depends(verify_api_key)):
        """Get LLM tool definitions in the specified format."""
        if format == "openai":
            return {"tools": _tool_generator.openai_tools()}
        elif format == "anthropic":
            return {"tools": _tool_generator.anthropic_tools()}
        elif format == "mcp":
            return {"tools": _tool_generator.mcp_tools()}
        elif format == "raw":
            return {"tools": _tool_generator.raw_definitions()}
        raise HTTPException(status_code=400, detail=f"Unknown format: {format}")

    @app.get("/api/agent/context")
    async def agent_context_prompt(_key: str = Depends(verify_api_key)):
        """Get a text summary of all devices for injection into LLM system prompts."""
        return {"context": _space_registry.to_context_prompt()}

    @app.get("/api/agent/confirmations")
    async def agent_list_confirmations(_key: str = Depends(verify_api_key)):
        """List operations pending human confirmation."""
        return {"confirmations": _safety_guard.list_pending_confirmations()}

    @app.post("/api/agent/confirmations/{confirmation_id}/approve")
    async def agent_approve_confirmation(
        confirmation_id: str, _key: str = Depends(verify_api_key),
    ):
        """Approve a pending confirmation and execute the operation."""
        req = _safety_guard.approve_confirmation(confirmation_id)
        if not req:
            raise HTTPException(status_code=404, detail="Confirmation not found")
        # Execute the approved operation
        result = await _tool_executor.call("set_device", {
            "device": req["device_name"],
            "action": req["action"],
            "value": req.get("value"),
        })
        return result

    @app.post("/api/agent/confirmations/{confirmation_id}/deny")
    async def agent_deny_confirmation(
        confirmation_id: str, _key: str = Depends(verify_api_key),
    ):
        """Deny a pending confirmation."""
        _safety_guard.deny_confirmation(confirmation_id)
        return {"status": "denied", "confirmation_id": confirmation_id}

    @app.get("/api/agent/safety/stats")
    async def agent_safety_stats(_key: str = Depends(verify_api_key)):
        """Get AI safety guard statistics."""
        return {"stats": _safety_guard.stats}

    # -- SSE Event Streaming --

    @app.get("/api/agent/events")
    async def agent_events(
        request: Any,
        spaces: str | None = None,
        devices: str | None = None,
        types: str | None = None,
        _key: str = Depends(verify_api_key),
    ):
        """Server-Sent Events stream of device state changes, actions, and alerts."""
        from starlette.responses import StreamingResponse

        filters = parse_sse_filters(spaces, devices, types)
        client_id, _ = await _event_manager.connect(filters=filters)
        return StreamingResponse(
            _event_manager.event_generator(client_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/agent/events/stats")
    async def agent_events_stats(_key: str = Depends(verify_api_key)):
        """Get event stream statistics."""
        return {"stats": _event_manager.stats}

    # -- Natural Language Intent --

    @app.post("/api/agent/intent")
    async def agent_resolve_intent(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Resolve natural language to device actions."""
        result = await _tool_executor.call("resolve_intent", req)
        return result

    # -- Device Groups --

    @app.get("/api/agent/groups")
    async def agent_list_groups(_key: str = Depends(verify_api_key)):
        """List all device groups."""
        return {"groups": _group_registry.list_groups()}

    @app.post("/api/agent/groups")
    async def agent_create_group(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Create a new device group."""
        try:
            group = _group_registry.add_group(
                name=req["name"],
                display_name=req.get("display_name", req["name"]),
                members=req.get("members"),
                match_capabilities=req.get("match_capabilities"),
                match_spaces=req.get("match_spaces"),
                tags=req.get("tags"),
            )
            return {"status": "created", "group": group.name}
        except Exception as e:
            raise HTTPException(status_code=400, detail=_safe_error(e))

    @app.post("/api/agent/groups/set")
    async def agent_set_group(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Apply an action to all devices in a group."""
        result = await _tool_executor.call("set_group", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    # -- Action History --

    @app.get("/api/agent/history")
    async def agent_get_history(
        device: str | None = None,
        space: str | None = None,
        minutes: int = 30,
        limit: int = 50,
        _key: str = Depends(verify_api_key),
    ):
        """Get recent action history."""
        return {
            "history": _action_history.query(
                device=device, space=space,
                since=__import__("time").time() - (minutes * 60),
                limit=limit,
            ),
            "stats": _action_history.stats,
        }

    # -- Scheduled Actions --

    @app.get("/api/agent/schedules")
    async def agent_list_schedules(
        active_only: bool = False,
        _key: str = Depends(verify_api_key),
    ):
        """List scheduled actions."""
        return {"schedules": _agent_scheduler.list_schedules(active_only=active_only)}

    @app.post("/api/agent/schedules")
    async def agent_schedule_action(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Schedule a device action or scene activation."""
        result = await _tool_executor.call("schedule_action", req)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/api/agent/schedules/{schedule_id}/cancel")
    async def agent_cancel_schedule(schedule_id: str, _key: str = Depends(verify_api_key)):
        """Cancel a scheduled action."""
        cancelled = await _agent_scheduler.cancel(schedule_id)
        return {"cancelled": cancelled, "schedule_id": schedule_id}

    # -- Analytics --

    @app.get("/api/agent/analytics")
    async def agent_analytics(_key: str = Depends(verify_api_key)):
        """Get energy and comfort analytics."""
        return _analytics.compute().to_dict()

    @app.get("/api/agent/analytics/context")
    async def agent_analytics_context(_key: str = Depends(verify_api_key)):
        """Get analytics as LLM context text."""
        return {"context": _analytics.to_context_prompt()}

    # -- Multi-agent Coordination --

    @app.post("/api/agent/locks/acquire")
    async def agent_acquire_lock(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Acquire exclusive write access to a device."""
        result = await _tool_executor.call("acquire_lock", req)
        return result

    @app.post("/api/agent/locks/release")
    async def agent_release_lock(req: dict[str, Any], _key: str = Depends(verify_api_key)):
        """Release exclusive device access."""
        result = await _tool_executor.call("release_lock", req)
        return result

    @app.get("/api/agent/locks")
    async def agent_list_locks(agent_id: str | None = None, _key: str = Depends(verify_api_key)):
        """List active device leases."""
        return {"locks": _coordinator.list_leases(agent_id=agent_id)}

    # -- Suggestions --

    @app.get("/api/agent/suggestions")
    async def agent_get_suggestions(
        max_suggestions: int = 5,
        _key: str = Depends(verify_api_key),
    ):
        """Get proactive action suggestions."""
        return {"suggestions": _suggester.suggest(max_suggestions=max_suggestions)}

    # -- Device Discovery --

    @app.get("/api/agent/describe/{device}")
    async def agent_describe_device(device: str, _key: str = Depends(verify_api_key)):
        """Get a natural language description of a device's capabilities."""
        result = _describer.describe(device)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    @app.get("/api/agent/describe")
    async def agent_describe_all(space: str | None = None, _key: str = Depends(verify_api_key)):
        """Get descriptions of all devices."""
        return {"devices": _describer.describe_all(space=space)}

    return app
