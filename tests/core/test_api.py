"""Tests for the REST API — auth, endpoints, error handling."""

from __future__ import annotations

import asyncio
import pytest

from core.api import create_api, _sanitize_profile, _safe_error
from core.event_bus import EventBus
from core.registry import AdapterRegistry
from core.scheduler import Scheduler
from core.state_store import StateStore
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
)

try:
    from httpx import ASGITransport, AsyncClient
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from fastapi import FastAPI
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(
    not (HAS_FASTAPI and HAS_HTTPX),
    reason="fastapi or httpx not installed",
)

TEST_API_KEY = "test-secret-key-12345"


class StubAdapter(Adapter):
    adapter_id: str = "test.stub"
    adapter_class: AdapterClass = "direct_device"

    def __init__(self):
        self._connections = {}

    def connection_templates(self):
        return [ConnectionTemplate(
            adapter_id=self.adapter_id,
            display_name="Test",
            category="test",
            discovery_methods=["manual_ip"],
            required_fields=["host"],
        )]

    async def discover(self, request):
        return [DiscoveredTarget(
            discovery_id="disc_1", adapter_id=self.adapter_id,
            native_ref="test", title="Test Device", address="127.0.0.1",
            confidence=1.0,
        )]

    async def commission(self, target, profile):
        cid = "test_conn_1"
        self._connections[cid] = True
        return CommissionResult(cid, "ok", {})

    async def inventory(self, connection_id):
        return InventorySnapshot(
            connection_id=connection_id,
            devices=[{
                "device_id": "dev_1", "native_device_ref": "test",
                "device_family": "test", "name": "Test",
                "safety_class": "S0",
            }],
            endpoints=[{
                "endpoint_id": "ep_1", "device_id": "dev_1",
                "native_endpoint_ref": "test", "endpoint_type": "test",
                "direction": "read", "capabilities": ["binary_sensor"],
                "safety_class": "S0",
            }],
            points=[{
                "point_id": "pt_1", "endpoint_id": "ep_1",
                "point_class": "test", "value_type": "bool",
                "readable": True, "writable": False,
            }],
        )

    async def subscribe(self, connection_id, point_ids=None):
        yield {"type": "heartbeat"}

    async def read_point(self, connection_id, point_id):
        return {"point_id": point_id, "value": True, "quality": {"status": "good"}}

    async def execute(self, connection_id, command):
        return {"command_id": command.get("command_id"), "status": "succeeded"}

    async def health(self, connection_id):
        return HealthStatus("healthy", {})

    async def teardown(self, connection_id):
        self._connections.pop(connection_id, None)


@pytest.fixture
async def api_client(tmp_path):
    """Create a test API client with auth configured."""
    bus = EventBus()
    store = StateStore(db_path=tmp_path / "test.db")
    await store.open()
    await bus.start()

    registry = AdapterRegistry(bus, store)
    scheduler = Scheduler(bus, store)
    scheduler.set_read_fn(registry.read_point)
    await scheduler.start()

    adapter = StubAdapter()
    registry.register(adapter)

    app = create_api(registry, store, scheduler, api_keys=[TEST_API_KEY])

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")

    yield client

    await client.aclose()
    await scheduler.stop()
    await bus.stop()
    await store.close()


def auth_headers():
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


def api_key_headers():
    return {"X-API-Key": TEST_API_KEY}


# -- Auth tests --

async def test_no_auth_returns_401(api_client):
    resp = await api_client.get("/api/adapters")
    assert resp.status_code == 401


async def test_wrong_key_returns_401(api_client):
    resp = await api_client.get("/api/adapters", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_bearer_auth_works(api_client):
    resp = await api_client.get("/api/adapters", headers=auth_headers())
    assert resp.status_code == 200


async def test_x_api_key_header_works(api_client):
    resp = await api_client.get("/api/adapters", headers=api_key_headers())
    assert resp.status_code == 200


# -- Adapter endpoints --

async def test_list_adapters(api_client):
    resp = await api_client.get("/api/adapters", headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["adapters"]) == 1
    assert data["adapters"][0]["adapter_id"] == "test.stub"


async def test_discover(api_client):
    resp = await api_client.post("/api/discover", json={
        "adapter_id": "test.stub",
        "methods": ["manual_ip"],
        "scope": {"host": "127.0.0.1"},
    }, headers=auth_headers())
    assert resp.status_code == 200
    assert len(resp.json()["targets"]) == 1


# -- Connection lifecycle --

async def test_commission_and_list(api_client):
    resp = await api_client.post("/api/connections", json={
        "adapter_id": "test.stub",
        "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["devices"] == 1

    # List connections
    resp = await api_client.get("/api/connections", headers=auth_headers())
    assert resp.status_code == 200
    assert len(resp.json()["connections"]) >= 1


async def test_commission_and_disconnect(api_client):
    resp = await api_client.post("/api/connections", json={
        "adapter_id": "test.stub",
        "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())
    cid = resp.json()["connection_id"]

    resp = await api_client.delete(f"/api/connections/{cid}", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"


# -- Device/point endpoints --

async def test_list_devices(api_client):
    # Commission first
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    resp = await api_client.get("/api/devices", headers=auth_headers())
    assert resp.status_code == 200
    assert len(resp.json()["devices"]) >= 1


async def test_get_device(api_client):
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    resp = await api_client.get("/api/devices/dev_1", headers=auth_headers())
    assert resp.status_code == 200


async def test_get_device_404(api_client):
    resp = await api_client.get("/api/devices/nonexistent", headers=auth_headers())
    assert resp.status_code == 404


async def test_read_point(api_client):
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    resp = await api_client.post("/api/points/read", json={
        "connection_id": "test_conn_1",
        "point_id": "pt_1",
    }, headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["value"] is True


# -- Command execution --

async def test_execute_command(api_client):
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    resp = await api_client.post("/api/commands", json={
        "connection_id": "test_conn_1",
        "target": {"endpoint_id": "ep_1"},
        "capability": "binary_switch",
        "verb": "set",
        "params": {"value": True},
    }, headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"


async def test_idempotency_key(api_client):
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    cmd = {
        "connection_id": "test_conn_1",
        "idempotency_key": "unique-123",
        "target": {"endpoint_id": "ep_1"},
        "capability": "binary_switch",
        "verb": "set",
        "params": {"value": True},
    }
    resp1 = await api_client.post("/api/commands", json=cmd, headers=auth_headers())
    resp2 = await api_client.post("/api/commands", json=cmd, headers=auth_headers())
    assert resp1.json() == resp2.json()


# -- Health --

async def test_health_all(api_client):
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    resp = await api_client.get("/api/health", headers=auth_headers())
    assert resp.status_code == 200


async def test_health_single(api_client):
    await api_client.post("/api/connections", json={
        "adapter_id": "test.stub", "profile_id": "test",
        "fields": {"host": "127.0.0.1"},
    }, headers=auth_headers())

    resp = await api_client.get("/api/health/test_conn_1", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# -- Scheduler / Audit / System --

async def test_scheduler_status(api_client):
    resp = await api_client.get("/api/scheduler", headers=auth_headers())
    assert resp.status_code == 200
    assert "stats" in resp.json()


async def test_audit_log(api_client):
    resp = await api_client.get("/api/audit", headers=auth_headers())
    assert resp.status_code == 200
    assert "entries" in resp.json()


async def test_system_stats(api_client):
    resp = await api_client.get("/api/system/stats", headers=auth_headers())
    assert resp.status_code == 200
    assert "adapters" in resp.json()


# -- Utility function tests --

def test_sanitize_profile():
    profile = {
        "host": "192.168.1.1",
        "password": "secret123",
        "api_key": "abc",
        "nested": {"client_secret": "xyz", "port": 8080},
    }
    cleaned = _sanitize_profile(profile)
    assert cleaned["host"] == "192.168.1.1"
    assert cleaned["password"] == "********"
    assert cleaned["api_key"] == "********"
    assert cleaned["nested"]["client_secret"] == "********"
    assert cleaned["nested"]["port"] == 8080


def test_safe_error_strips_paths():
    e = ValueError("config at /etc/secrets/key.pem not found")
    msg = _safe_error(e)
    assert "/etc" not in msg
    assert "ValueError" in msg


def test_safe_error_truncates():
    e = RuntimeError("x" * 500)
    msg = _safe_error(e)
    assert len(msg) < 300
    assert "..." in msg


def test_safe_error_normal():
    e = RuntimeError("connection refused")
    msg = _safe_error(e)
    assert msg == "RuntimeError: connection refused"
