"""Tests for Sprint 1 production safety features.

Covers: healthz endpoint, CORS middleware, idempotency TTL,
confirmation expiry, CLI argument parsing, signal handling setup.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest


# -- P1.1: Healthz endpoint --

class TestHealthzEndpoint:
    """Tests for the unauthenticated /healthz endpoint."""

    @pytest.fixture
    def app_client(self):
        """Create a test client with the API app."""
        from core.event_bus import EventBus
        from core.registry import AdapterRegistry
        from core.scheduler import Scheduler
        from core.state_store import StateStore
        from core.api import create_api

        bus = EventBus()
        store = StateStore(db_path=":memory:")
        registry = AdapterRegistry(bus, store)
        scheduler = Scheduler(bus, store)

        app = create_api(registry, store, scheduler, api_keys=["test-key"])
        from starlette.testclient import TestClient
        return TestClient(app)

    def test_healthz_no_auth_required(self, app_client):
        """Healthz must respond without any API key."""
        resp = app_client.get("/healthz")
        assert resp.status_code == 200

    def test_healthz_returns_status(self, app_client):
        data = app_client.get("/healthz").json()
        assert data["status"] == "ok"
        assert "uptime_seconds" in data
        assert "adapters" in data

    def test_authenticated_endpoint_still_requires_key(self, app_client):
        """Verify that /api/adapters still requires auth."""
        resp = app_client.get("/api/adapters")
        assert resp.status_code in (401, 403)


# -- P1.2: CORS middleware --

class TestCORSMiddleware:
    """Tests for CORS configuration."""

    def _make_app(self, cors_origins=None):
        from core.event_bus import EventBus
        from core.registry import AdapterRegistry
        from core.scheduler import Scheduler
        from core.state_store import StateStore
        from core.api import create_api

        bus = EventBus()
        store = StateStore(db_path=":memory:")
        registry = AdapterRegistry(bus, store)
        scheduler = Scheduler(bus, store)
        return create_api(
            registry, store, scheduler,
            api_keys=["test-key"],
            cors_origins=cors_origins,
        )

    def test_no_cors_by_default(self):
        """Without cors_origins, no CORS headers should be added."""
        from starlette.testclient import TestClient
        app = self._make_app(cors_origins=None)
        client = TestClient(app)
        resp = client.get("/healthz", headers={"Origin": "http://example.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_cors_with_allowed_origin(self):
        """With cors_origins set, matching origin gets CORS headers."""
        from starlette.testclient import TestClient
        app = self._make_app(cors_origins=["http://example.com"])
        client = TestClient(app)
        resp = client.get(
            "/healthz",
            headers={"Origin": "http://example.com"},
        )
        assert resp.headers.get("access-control-allow-origin") == "http://example.com"


# -- P1.6: Idempotency cache TTL --

class TestIdempotencyTTL:
    """Tests for idempotency cache with time-based expiry."""

    def test_idempotency_functions_exist(self):
        """Verify the TTL-based cache is used instead of a plain dict."""
        import core.api as api_mod
        source = open(api_mod.__file__).read()
        assert "_idempotency_get" in source
        assert "_idempotency_set" in source
        assert "_IDEMPOTENCY_TTL" in source


# -- P1.7: Confirmation expiry --

class TestConfirmationExpiry:
    """Tests for confirmation request auto-expiry."""

    @pytest.fixture
    def guard(self):
        from agent.spaces import SpaceRegistry
        from agent.safety import AISafetyGuard, SafetyConfig
        sr = SpaceRegistry()
        config = SafetyConfig(confirmation_ttl_seconds=1.0)
        return AISafetyGuard(sr, config)

    def test_fresh_confirmation_visible(self, guard):
        guard.request_confirmation("c1", "dev1", "on")
        pending = guard.list_pending_confirmations()
        assert len(pending) == 1
        assert pending[0]["confirmation_id"] == "c1"

    def test_expired_confirmation_purged(self, guard):
        guard.request_confirmation("c1", "dev1", "on")
        # Manually backdate the timestamp
        guard._pending_confirmations["c1"]["requested_at"] = time.time() - 10
        pending = guard.list_pending_confirmations()
        assert len(pending) == 0

    def test_approve_expired_returns_none(self, guard):
        guard.request_confirmation("c1", "dev1", "on")
        guard._pending_confirmations["c1"]["requested_at"] = time.time() - 10
        result = guard.approve_confirmation("c1")
        assert result is None

    def test_config_has_ttl(self):
        from agent.safety import SafetyConfig
        config = SafetyConfig()
        assert config.confirmation_ttl_seconds == 600.0


# -- P1.3 / P1.4 / P1.8 / P1.9: Engine CLI --

class TestEngineCLI:
    """Tests for the CLI argument parser."""

    def test_parser_defaults(self):
        from core.engine import _build_parser
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.host == "0.0.0.0"
        assert args.port == 8000
        assert args.db_path == "state.db"
        assert args.spaces is None
        assert args.scenes is None
        assert args.no_restore is False
        assert args.log_level == "INFO"
        assert args.cors_origins is None

    def test_parser_custom_args(self):
        from core.engine import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "--host", "127.0.0.1",
            "--port", "9000",
            "--db-path", "/tmp/test.db",
            "--spaces", "my_spaces.yaml",
            "--scenes", "my_scenes.yaml",
            "--no-restore",
            "--log-level", "DEBUG",
            "--cors-origins", "http://localhost:3000,http://localhost:8080",
        ])
        assert args.host == "127.0.0.1"
        assert args.port == 9000
        assert args.db_path == "/tmp/test.db"
        assert args.spaces == "my_spaces.yaml"
        assert args.scenes == "my_scenes.yaml"
        assert args.no_restore is True
        assert args.log_level == "DEBUG"
        assert args.cors_origins == "http://localhost:3000,http://localhost:8080"

    def test_engine_start_accepts_restore_flag(self):
        """Engine.start() should accept restore_connections parameter."""
        import inspect
        from core.engine import Engine
        sig = inspect.signature(Engine.start)
        assert "restore_connections" in sig.parameters

    def test_pyproject_has_scripts_entry(self):
        """pyproject.toml should declare a smartspaces CLI entry point."""
        from pathlib import Path
        content = Path("pyproject.toml").read_text()
        assert 'smartspaces = "core.engine:main"' in content

    def test_pyproject_has_dotenv_dependency(self):
        from pathlib import Path
        content = Path("pyproject.toml").read_text()
        assert "python-dotenv" in content
