"""Prometheus metrics for SmartSpaces.

Exposes counters, gauges, and histograms for monitoring the runtime.
Metrics are only active when prometheus_client is installed; otherwise
all metric objects are no-op stubs that silently accept calls.

Usage:
    from core.metrics import METRICS
    METRICS.requests_total.labels(method="GET", endpoint="/healthz", status=200).inc()

    # In api.py:
    app.get("/metrics")(METRICS.endpoint)
"""

from __future__ import annotations

import time
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        REGISTRY,
    )
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False


class _NoOpMetric:
    """Stub metric that accepts any call without doing anything."""

    def labels(self, *args: Any, **kwargs: Any) -> "_NoOpMetric":
        return self

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, value: float) -> None:
        pass


class Metrics:
    """Container for all SmartSpaces Prometheus metrics.

    If prometheus_client is not installed, all metrics are no-op stubs.
    """

    def __init__(self) -> None:
        if HAS_PROMETHEUS:
            self._init_real()
        else:
            self._init_stubs()

    def _init_real(self) -> None:
        # -- HTTP API metrics --
        self.requests_total = Counter(
            "smartspaces_http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status"],
        )
        self.request_duration_seconds = Histogram(
            "smartspaces_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "endpoint"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self.requests_in_flight = Gauge(
            "smartspaces_http_requests_in_flight",
            "Currently processing HTTP requests",
        )

        # -- Event bus metrics --
        self.events_published_total = Counter(
            "smartspaces_events_published_total",
            "Total events published to event bus",
            ["event_type"],
        )
        self.events_dispatched_total = Counter(
            "smartspaces_events_dispatched_total",
            "Total events dispatched to subscribers",
        )
        self.events_errors_total = Counter(
            "smartspaces_events_errors_total",
            "Total event dispatch errors",
        )
        self.event_queue_depth = Gauge(
            "smartspaces_event_queue_depth",
            "Current event bus queue depth",
        )

        # -- Adapter / connection metrics --
        self.connections_active = Gauge(
            "smartspaces_connections_active",
            "Number of active adapter connections",
        )
        self.adapter_operations_total = Counter(
            "smartspaces_adapter_operations_total",
            "Total adapter operations",
            ["operation", "adapter_id", "status"],
        )
        self.adapter_operation_duration_seconds = Histogram(
            "smartspaces_adapter_operation_duration_seconds",
            "Adapter operation latency",
            ["operation", "adapter_id"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )

        # -- Scheduler metrics --
        self.scheduler_polls_total = Counter(
            "smartspaces_scheduler_polls_total",
            "Total scheduler poll attempts",
            ["status"],  # success, error, timeout
        )
        self.scheduler_targets_active = Gauge(
            "smartspaces_scheduler_targets_active",
            "Number of active polling targets",
        )
        self.scheduler_targets_suspended = Gauge(
            "smartspaces_scheduler_targets_suspended",
            "Number of suspended polling targets",
        )

        # -- Agent safety metrics --
        self.safety_checks_total = Counter(
            "smartspaces_safety_checks_total",
            "Total AI safety guard checks",
            ["result"],  # allowed, blocked, rate_limited, confirmation_required
        )

        # -- System info --
        self.info = Gauge(
            "smartspaces_info",
            "System information",
            ["version"],
        )
        self.info.labels(version="0.1.0").set(1)

    def _init_stubs(self) -> None:
        stub = _NoOpMetric()
        self.requests_total = stub
        self.request_duration_seconds = stub
        self.requests_in_flight = stub
        self.events_published_total = stub
        self.events_dispatched_total = stub
        self.events_errors_total = stub
        self.event_queue_depth = stub
        self.connections_active = stub
        self.adapter_operations_total = stub
        self.adapter_operation_duration_seconds = stub
        self.scheduler_polls_total = stub
        self.scheduler_targets_active = stub
        self.scheduler_targets_suspended = stub
        self.safety_checks_total = stub
        self.info = stub

    @property
    def enabled(self) -> bool:
        return HAS_PROMETHEUS

    def endpoint(self) -> Any:
        """Prometheus metrics endpoint handler. Returns metrics in text format."""
        if not HAS_PROMETHEUS:
            return {"error": "prometheus_client not installed"}

        from starlette.responses import Response
        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )


# Singleton instance
METRICS = Metrics()
