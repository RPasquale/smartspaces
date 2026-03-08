"""Integration tests for the full API flow.

Uses starlette TestClient to exercise the Engine → API → Registry path
without a real HTTP server.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from core.engine import Engine
from adapters.kincony import KinConyAdapter
from adapters.shelly import ShellyAdapter


@pytest.fixture
def app():
    import os
    os.environ.setdefault("SMARTSPACES_API_KEYS", "test-key-123")
    eng = Engine(db_path=":memory:")
    eng.register_adapter(KinConyAdapter())
    eng.register_adapter(ShellyAdapter())
    return eng.create_api()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-key-123"}


class TestHealthAndMetrics:
    def test_healthz_no_auth(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_metrics_no_auth(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_api_requires_auth(self, client):
        resp = client.get("/api/adapters")
        assert resp.status_code in (401, 403)


class TestAdapterEndpoints:
    def test_list_adapters(self, client, auth_headers):
        resp = client.get("/api/adapters", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        adapters = data.get("adapters", data) if isinstance(data, dict) else data
        assert isinstance(adapters, list)
        ids = [a["adapter_id"] for a in adapters]
        assert "kincony.family" in ids
        assert "shelly.gen2" in ids

    def test_adapters_have_templates(self, client, auth_headers):
        resp = client.get("/api/adapters", headers=auth_headers)
        data = resp.json()
        adapters = data.get("adapters", data) if isinstance(data, dict) else data
        for a in adapters:
            assert "templates" in a
            assert len(a["templates"]) > 0


class TestCorrelationHeaders:
    def test_correlation_id_generated(self, client, auth_headers):
        resp = client.get("/api/adapters", headers=auth_headers)
        assert "x-correlation-id" in resp.headers

    def test_correlation_id_passthrough(self, client, auth_headers):
        headers = {**auth_headers, "X-Correlation-ID": "custom-trace-42"}
        resp = client.get("/api/adapters", headers=headers)
        assert resp.headers.get("x-correlation-id") == "custom-trace-42"


class TestAgentEndpoints:
    def test_agent_state_endpoint(self, client, auth_headers):
        resp = client.post(
            "/api/agent/state",
            json={"device_name": "living_room.light"},
            headers=auth_headers,
        )
        # May return 200 or 404 depending on config, but should not 500
        assert resp.status_code < 500

    def test_agent_tools_endpoint(self, client, auth_headers):
        resp = client.get("/api/agent/tools/openai", headers=auth_headers)
        assert resp.status_code == 200
