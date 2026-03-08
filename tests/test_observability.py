"""Tests for Phase 4 observability features.

Covers: structured logging, Prometheus metrics, correlation IDs.
"""

from __future__ import annotations

import json
import logging
import time

import pytest


# -- Structured Logging --

class TestStructuredLogging:
    """Tests for the logging configuration module."""

    def test_json_formatter_output(self):
        from core.logging_config import JSONFormatter

        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "hello world"
        assert "timestamp" in data

    def test_json_formatter_with_correlation_id(self):
        from core.logging_config import JSONFormatter, set_correlation_id

        set_correlation_id("req-abc123")
        try:
            formatter = JSONFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="", lineno=0, msg="test", args=(), exc_info=None,
            )
            output = formatter.format(record)
            data = json.loads(output)
            assert data["correlation_id"] == "req-abc123"
        finally:
            set_correlation_id("")

    def test_json_formatter_with_context(self):
        from core.logging_config import JSONFormatter, set_log_context, clear_log_context

        set_log_context(adapter_id="kincony", connection_id="conn_1")
        try:
            formatter = JSONFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="", lineno=0, msg="test", args=(), exc_info=None,
            )
            output = formatter.format(record)
            data = json.loads(output)
            assert data["adapter_id"] == "kincony"
            assert data["connection_id"] == "conn_1"
        finally:
            clear_log_context()

    def test_json_formatter_with_exception(self):
        from core.logging_config import JSONFormatter
        import sys

        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR,
            pathname="", lineno=0, msg="boom", args=(), exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["exception"]["type"] == "ValueError"
        assert data["exception"]["message"] == "test error"
        assert "traceback" in data

    def test_text_formatter_basic(self):
        from core.logging_config import TextFormatter

        formatter = TextFormatter()
        record = logging.LogRecord(
            name="myapp", level=logging.WARNING,
            pathname="", lineno=0, msg="something happened", args=(), exc_info=None,
        )
        output = formatter.format(record)
        assert "[WARNING]" in output
        assert "myapp" in output
        assert "something happened" in output

    def test_text_formatter_with_correlation(self):
        from core.logging_config import TextFormatter, set_correlation_id

        set_correlation_id("xyz-789")
        try:
            formatter = TextFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="", lineno=0, msg="msg", args=(), exc_info=None,
            )
            output = formatter.format(record)
            assert "[xyz-789]" in output
        finally:
            set_correlation_id("")

    def test_configure_logging_text(self):
        from core.logging_config import configure_logging
        configure_logging(level="DEBUG", log_format="text")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) >= 1

    def test_configure_logging_json(self):
        from core.logging_config import configure_logging, JSONFormatter
        configure_logging(level="INFO", log_format="json")
        root = logging.getLogger()
        assert any(isinstance(h.formatter, JSONFormatter) for h in root.handlers)


# -- Metrics --

class TestMetrics:
    """Tests for the metrics module."""

    def test_metrics_singleton_exists(self):
        from core.metrics import METRICS
        assert METRICS is not None

    def test_noop_stubs_dont_crash(self):
        """If prometheus_client is missing, stubs should accept calls silently."""
        from core.metrics import _NoOpMetric
        m = _NoOpMetric()
        m.inc()
        m.dec()
        m.set(42)
        m.observe(0.5)
        m.labels(foo="bar").inc()
        m.labels(foo="bar").labels(baz="qux").observe(1.0)

    def test_metrics_has_expected_attributes(self):
        from core.metrics import METRICS
        expected = [
            "requests_total",
            "request_duration_seconds",
            "requests_in_flight",
            "events_published_total",
            "events_dispatched_total",
            "events_errors_total",
            "event_queue_depth",
            "connections_active",
            "adapter_operations_total",
            "adapter_operation_duration_seconds",
            "scheduler_polls_total",
            "scheduler_targets_active",
            "scheduler_targets_suspended",
            "safety_checks_total",
            "info",
        ]
        for attr in expected:
            assert hasattr(METRICS, attr), f"Missing metric: {attr}"

    def test_metrics_enabled_property(self):
        from core.metrics import METRICS
        # Should be bool regardless of prometheus_client installation
        assert isinstance(METRICS.enabled, bool)


# -- Metrics Middleware --

class TestMetricsMiddleware:
    """Tests for the HTTP metrics middleware in api.py."""

    @pytest.fixture
    def client(self):
        from core.event_bus import EventBus
        from core.registry import AdapterRegistry
        from core.scheduler import Scheduler
        from core.state_store import StateStore
        from core.api import create_api
        from starlette.testclient import TestClient

        bus = EventBus()
        store = StateStore(db_path=":memory:")
        registry = AdapterRegistry(bus, store)
        scheduler = Scheduler(bus, store)
        app = create_api(registry, store, scheduler, api_keys=["test-key"])
        return TestClient(app)

    def test_correlation_id_returned(self, client):
        """Responses should include X-Correlation-ID header."""
        resp = client.get(
            "/api/adapters",
            headers={"Authorization": "Bearer test-key"},
        )
        assert "x-correlation-id" in resp.headers

    def test_custom_correlation_id_preserved(self, client):
        """If client sends X-Correlation-ID, server should use it."""
        resp = client.get(
            "/api/adapters",
            headers={
                "Authorization": "Bearer test-key",
                "X-Correlation-ID": "my-trace-123",
            },
        )
        assert resp.headers.get("x-correlation-id") == "my-trace-123"

    def test_metrics_endpoint_exists(self, client):
        """GET /metrics should be accessible without auth."""
        resp = client.get("/metrics")
        assert resp.status_code == 200


# -- Event Bus Metrics Integration --

class TestEventBusMetrics:
    """Tests for event bus metric instrumentation."""

    async def test_publish_increments_metrics(self):
        from core.event_bus import EventBus
        bus = EventBus()
        await bus.start()
        try:
            await bus.publish({"type": "test.event", "data": "hello"})
            assert bus.stats["published"] == 1
        finally:
            await bus.stop()

    def test_publish_nowait_increments_metrics(self):
        from core.event_bus import EventBus
        import asyncio
        bus = EventBus()
        bus._running = True  # don't start dispatch loop
        result = bus.publish_nowait({"type": "test.event"})
        assert result is True
        assert bus.stats["published"] == 1


# -- CLI Log Format Flag --

class TestCLILogFormat:
    """Tests for the --log-format CLI flag."""

    def test_parser_has_log_format(self):
        from core.engine import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--log-format", "json"])
        assert args.log_format == "json"

    def test_parser_default_log_format(self):
        from core.engine import _build_parser
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.log_format == "text"

    def test_pyproject_has_metrics_optional(self):
        from pathlib import Path
        content = Path("pyproject.toml").read_text()
        assert "prometheus-client" in content
        assert 'metrics = ["prometheus-client' in content
