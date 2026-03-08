"""Structured logging configuration for SmartSpaces.

Supports two output formats:
- "text" (default): human-readable log lines for development
- "json": structured JSON lines for production log aggregation

Usage:
    from core.logging_config import configure_logging
    configure_logging(level="INFO", format="json")

Request correlation IDs are attached to log records via context vars
and automatically included in JSON output.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

# Context var for per-request correlation ID
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

# Context var for extra structured fields
log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "log_context", default={}
)


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current async context."""
    correlation_id.set(cid)


def set_log_context(**kwargs: Any) -> None:
    """Set extra structured fields for the current async context."""
    current = log_context.get()
    log_context.set({**current, **kwargs})


def clear_log_context() -> None:
    """Clear all extra structured fields."""
    log_context.set({})


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Output fields: timestamp, level, logger, message, correlation_id,
    plus any extra context fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation ID if set
        cid = correlation_id.get("")
        if cid:
            entry["correlation_id"] = cid

        # Add context vars
        ctx = log_context.get({})
        if ctx:
            entry.update(ctx)

        # Add exception info
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }
            if record.exc_text:
                entry["traceback"] = record.exc_text
            else:
                import traceback
                entry["traceback"] = "".join(
                    traceback.format_exception(*record.exc_info)
                )

        return json.dumps(entry, default=str)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """ISO 8601 timestamp with milliseconds."""
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.isoformat(timespec="milliseconds")


class TextFormatter(logging.Formatter):
    """Human-readable formatter that includes correlation ID when present."""

    def format(self, record: logging.LogRecord) -> str:
        cid = correlation_id.get("")
        cid_str = f" [{cid}]" if cid else ""
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        ms = int(record.created % 1 * 1000)

        base = f"{ts}.{ms:03d} [{record.levelname}]{cid_str} {record.name}: {record.getMessage()}"

        if record.exc_info and record.exc_info[1]:
            import traceback
            tb = "".join(traceback.format_exception(*record.exc_info))
            base += "\n" + tb.rstrip()

        return base


def configure_logging(
    level: str = "INFO",
    log_format: str = "text",
) -> None:
    """Configure the root logger with the specified format.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        log_format: "text" for human-readable, "json" for structured JSON.
    """
    root = logging.getLogger()

    # Remove existing handlers to avoid duplicate output
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)

    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Suppress noisy third-party loggers
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
