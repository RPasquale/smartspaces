"""SQLite-backed state persistence.

Stores devices, endpoints, points, last-known values, connection configs,
and adapter registrations. All state survives process restarts.

Uses aiosqlite for async access. Schema is auto-migrated on open.
"""

from __future__ import annotations

import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    adapter_id TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'commissioned',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (connection_id) REFERENCES connections(connection_id)
);

CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS points (
    point_id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (endpoint_id) REFERENCES endpoints(endpoint_id)
);

CREATE TABLE IF NOT EXISTS point_values (
    point_id TEXT PRIMARY KEY,
    value_json TEXT,
    quality_json TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (point_id) REFERENCES points(point_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    connection_id TEXT,
    device_id TEXT,
    point_id TEXT,
    command_id TEXT,
    initiator TEXT,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_devices_connection ON devices(connection_id);
CREATE INDEX IF NOT EXISTS idx_endpoints_device ON endpoints(device_id);
CREATE INDEX IF NOT EXISTS idx_points_endpoint ON points(endpoint_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_device ON audit_log(device_id);
"""

# Migrations: list of (from_version, to_version, sql)
MIGRATIONS = [
    (1, 2, """
CREATE INDEX IF NOT EXISTS idx_connections_adapter ON connections(adapter_id);
CREATE INDEX IF NOT EXISTS idx_connections_status ON connections(status);
CREATE INDEX IF NOT EXISTS idx_point_values_updated ON point_values(updated_at);
"""),
]


class StateStore:
    """Async SQLite state store for the adapter runtime."""

    def __init__(self, db_path: str | Path = "state.db"):
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the database and ensure schema exists."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        # Enable WAL mode for better concurrent read performance
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")

        # Check current version
        current_version = 0
        try:
            cursor = await self._db.execute(
                "SELECT MAX(version) as v FROM schema_version"
            )
            row = await cursor.fetchone()
            if row and row["v"]:
                current_version = row["v"]
        except Exception:
            # Table doesn't exist yet — fresh DB
            pass

        if current_version == 0:
            # Fresh database: apply full schema
            await self._db.executescript(SCHEMA_V1)
            # Apply all migrations up to current version
            for _from, _to, sql in MIGRATIONS:
                await self._db.executescript(sql)
            await self._db.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            await self._db.commit()
        elif current_version < SCHEMA_VERSION:
            # Existing database: run pending migrations
            for from_ver, to_ver, sql in MIGRATIONS:
                if from_ver >= current_version:
                    logger.info("Migrating schema v%d -> v%d", from_ver, to_ver)
                    await self._db.executescript(sql)
            await self._db.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            await self._db.commit()

        logger.info("StateStore opened: %s (schema v%d)", self.db_path, SCHEMA_VERSION)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self._db:
            raise RuntimeError("StateStore not open")
        return self._db

    # -- Connection management --

    async def save_connection(
        self, connection_id: str, adapter_id: str, profile: dict[str, Any], status: str = "commissioned"
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO connections (connection_id, adapter_id, profile_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(connection_id) DO UPDATE SET
                     profile_json=excluded.profile_json, status=excluded.status, updated_at=excluded.updated_at""",
                (connection_id, adapter_id, json.dumps(profile), status, now, now),
            )
            await self.db.commit()

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM connections WHERE connection_id = ?", (connection_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "connection_id": row["connection_id"],
            "adapter_id": row["adapter_id"],
            "profile": json.loads(row["profile_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def list_connections(self, adapter_id: str | None = None) -> list[dict[str, Any]]:
        if adapter_id:
            cursor = await self.db.execute(
                "SELECT * FROM connections WHERE adapter_id = ? ORDER BY created_at", (adapter_id,)
            )
        else:
            cursor = await self.db.execute("SELECT * FROM connections ORDER BY created_at")
        rows = await cursor.fetchall()
        return [
            {
                "connection_id": r["connection_id"],
                "adapter_id": r["adapter_id"],
                "profile": json.loads(r["profile_json"]),
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def delete_connection(self, connection_id: str) -> None:
        # Cascade: remove devices, endpoints, points, values
        devices = await self.list_devices(connection_id)
        async with self._write_lock:
            for dev in devices:
                await self._delete_device_cascade(dev["device_id"])
            await self.db.execute("DELETE FROM connections WHERE connection_id = ?", (connection_id,))
            await self.db.commit()

    # -- Device management --

    async def save_device(self, device_id: str, connection_id: str, data: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO devices (device_id, connection_id, data_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     data_json=excluded.data_json, updated_at=excluded.updated_at""",
                (device_id, connection_id, json.dumps(data), now),
            )
            await self.db.commit()

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        data = json.loads(row["data_json"])
        data["device_id"] = row["device_id"]
        data["connection_id"] = row["connection_id"]
        return data

    async def list_devices(self, connection_id: str | None = None) -> list[dict[str, Any]]:
        if connection_id:
            cursor = await self.db.execute(
                "SELECT * FROM devices WHERE connection_id = ?", (connection_id,)
            )
        else:
            cursor = await self.db.execute("SELECT * FROM devices")
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            data = json.loads(r["data_json"])
            data["device_id"] = r["device_id"]
            data["connection_id"] = r["connection_id"]
            results.append(data)
        return results

    async def _delete_device_cascade(self, device_id: str) -> None:
        """Delete a device and all its endpoints, points, and values. Caller must hold _write_lock."""
        cursor = await self.db.execute(
            "SELECT endpoint_id FROM endpoints WHERE device_id = ?", (device_id,)
        )
        eps = await cursor.fetchall()
        for ep in eps:
            ep_id = ep["endpoint_id"]
            await self.db.execute("DELETE FROM point_values WHERE point_id IN (SELECT point_id FROM points WHERE endpoint_id = ?)", (ep_id,))
            await self.db.execute("DELETE FROM points WHERE endpoint_id = ?", (ep_id,))
        await self.db.execute("DELETE FROM endpoints WHERE device_id = ?", (device_id,))
        await self.db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))

    # -- Endpoint management --

    async def save_endpoint(self, endpoint_id: str, device_id: str, data: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO endpoints (endpoint_id, device_id, data_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(endpoint_id) DO UPDATE SET
                     data_json=excluded.data_json, updated_at=excluded.updated_at""",
                (endpoint_id, device_id, json.dumps(data), now),
            )
            await self.db.commit()

    async def list_endpoints(self, device_id: str) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM endpoints WHERE device_id = ?", (device_id,)
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            data = json.loads(r["data_json"])
            data["endpoint_id"] = r["endpoint_id"]
            data["device_id"] = r["device_id"]
            results.append(data)
        return results

    # -- Point management --

    async def save_point(self, point_id: str, endpoint_id: str, data: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO points (point_id, endpoint_id, data_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(point_id) DO UPDATE SET
                     data_json=excluded.data_json, updated_at=excluded.updated_at""",
                (point_id, endpoint_id, json.dumps(data), now),
            )
            await self.db.commit()

    async def list_points(self, endpoint_id: str | None = None) -> list[dict[str, Any]]:
        if endpoint_id:
            cursor = await self.db.execute(
                "SELECT * FROM points WHERE endpoint_id = ?", (endpoint_id,)
            )
        else:
            cursor = await self.db.execute("SELECT * FROM points")
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            data = json.loads(r["data_json"])
            data["point_id"] = r["point_id"]
            data["endpoint_id"] = r["endpoint_id"]
            results.append(data)
        return results

    async def get_point(self, point_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute("SELECT * FROM points WHERE point_id = ?", (point_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        data = json.loads(row["data_json"])
        data["point_id"] = row["point_id"]
        data["endpoint_id"] = row["endpoint_id"]
        return data

    # -- Point value (last-known state) --

    async def save_point_value(
        self,
        point_id: str,
        value: Any = None,
        quality: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO point_values (point_id, value_json, quality_json, raw_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(point_id) DO UPDATE SET
                     value_json=excluded.value_json, quality_json=excluded.quality_json,
                     raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
                (
                    point_id,
                    json.dumps(value) if value is not None else None,
                    json.dumps(quality) if quality else None,
                    json.dumps(raw) if raw else None,
                    now,
                ),
            )
            await self.db.commit()

    async def get_point_value(self, point_id: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM point_values WHERE point_id = ?", (point_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "point_id": row["point_id"],
            "value": json.loads(row["value_json"]) if row["value_json"] else None,
            "quality": json.loads(row["quality_json"]) if row["quality_json"] else None,
            "raw": json.loads(row["raw_json"]) if row["raw_json"] else None,
            "updated_at": row["updated_at"],
        }

    async def get_all_point_values(self, connection_id: str | None = None) -> list[dict[str, Any]]:
        if connection_id:
            cursor = await self.db.execute(
                """SELECT pv.* FROM point_values pv
                   JOIN points p ON pv.point_id = p.point_id
                   JOIN endpoints e ON p.endpoint_id = e.endpoint_id
                   JOIN devices d ON e.device_id = d.device_id
                   WHERE d.connection_id = ?""",
                (connection_id,),
            )
        else:
            cursor = await self.db.execute("SELECT * FROM point_values")
        rows = await cursor.fetchall()
        return [
            {
                "point_id": r["point_id"],
                "value": json.loads(r["value_json"]) if r["value_json"] else None,
                "quality": json.loads(r["quality_json"]) if r["quality_json"] else None,
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    # -- Inventory persistence (bulk save from adapter inventory) --

    async def persist_inventory(self, connection_id: str, snapshot_dict: dict[str, Any]) -> None:
        """Save an entire inventory snapshot to the store."""
        async with self._write_lock:
            for dev in snapshot_dict.get("devices", []):
                now = datetime.now(timezone.utc).isoformat()
                await self.db.execute(
                    """INSERT INTO devices (device_id, connection_id, data_json, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(device_id) DO UPDATE SET
                         data_json=excluded.data_json, updated_at=excluded.updated_at""",
                    (dev["device_id"], connection_id, json.dumps(dev), now),
                )
            for ep in snapshot_dict.get("endpoints", []):
                now = datetime.now(timezone.utc).isoformat()
                await self.db.execute(
                    """INSERT INTO endpoints (endpoint_id, device_id, data_json, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(endpoint_id) DO UPDATE SET
                         data_json=excluded.data_json, updated_at=excluded.updated_at""",
                    (ep["endpoint_id"], ep["device_id"], json.dumps(ep), now),
                )
            for pt in snapshot_dict.get("points", []):
                now = datetime.now(timezone.utc).isoformat()
                await self.db.execute(
                    """INSERT INTO points (point_id, endpoint_id, data_json, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(point_id) DO UPDATE SET
                         data_json=excluded.data_json, updated_at=excluded.updated_at""",
                    (pt["point_id"], pt["endpoint_id"], json.dumps(pt), now),
                )
            await self.db.commit()

    # -- Audit log --

    async def audit(
        self,
        event_type: str,
        connection_id: str | None = None,
        device_id: str | None = None,
        point_id: str | None = None,
        command_id: str | None = None,
        initiator: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self.db.execute(
                """INSERT INTO audit_log (timestamp, event_type, connection_id, device_id, point_id, command_id, initiator, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, event_type, connection_id, device_id, point_id, command_id, initiator,
                 json.dumps(detail) if detail else None),
            )
            await self.db.commit()

    async def get_audit_log(self, limit: int = 100, device_id: str | None = None) -> list[dict[str, Any]]:
        if device_id:
            cursor = await self.db.execute(
                "SELECT * FROM audit_log WHERE device_id = ? ORDER BY timestamp DESC LIMIT ?",
                (device_id, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "timestamp": r["timestamp"],
                "event_type": r["event_type"],
                "connection_id": r["connection_id"],
                "device_id": r["device_id"],
                "point_id": r["point_id"],
                "command_id": r["command_id"],
                "initiator": r["initiator"],
                "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
            }
            for r in rows
        ]
