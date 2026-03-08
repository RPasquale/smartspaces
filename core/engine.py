"""Engine — boots the entire adapter runtime.

Initializes the event bus, state store, registry, scheduler, and API.
Provides a single entry point to start and stop the system, and
convenience methods for registering adapters and running the server.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from core.event_bus import EventBus
from core.registry import AdapterRegistry
from core.scheduler import Scheduler
from core.state_store import StateStore
from sdk.adapter_api.base import Adapter

logger = logging.getLogger(__name__)


class Engine:
    """Main runtime engine for the Physical Space Adapter system."""

    def __init__(
        self,
        db_path: str | Path = "state.db",
        default_poll_interval: float = 30.0,
        event_bus_queue_size: int = 10_000,
    ):
        self.event_bus = EventBus(max_queue_size=event_bus_queue_size)
        self.state_store = StateStore(db_path=db_path)
        self.registry = AdapterRegistry(self.event_bus, self.state_store)
        self.scheduler = Scheduler(
            self.event_bus, self.state_store, default_interval=default_poll_interval
        )
        self._api: Any = None
        self._started = False

    def register_adapter(self, adapter: Adapter) -> None:
        """Register an adapter with the runtime.

        Call this before start() to make adapters available.
        """
        self.registry.register(adapter)

    async def start(self, restore_connections: bool = True) -> None:
        """Start all runtime components."""
        if self._started:
            return

        # Open state store
        await self.state_store.open()

        # Start event bus
        await self.event_bus.start()

        # Wire up scheduler
        self.scheduler.set_read_fn(self.registry.read_point)

        # Start scheduler
        await self.scheduler.start()

        # Wire up event bus -> state store persistence for point values
        self.event_bus.subscribe("point.reported", self._on_point_reported)

        # Restore previously commissioned connections
        if restore_connections:
            try:
                await self.registry.restore_connections()
                logger.info("Connection restoration complete")
            except Exception as e:
                logger.warning("Connection restoration failed: %s", e)

        self._started = True
        logger.info("Engine started")

    async def stop(self) -> None:
        """Stop all runtime components."""
        if not self._started:
            return

        # Stop scheduler first (no more reads)
        await self.scheduler.stop()

        # Teardown all adapter connections
        await self.registry.teardown_all()

        # Stop event bus
        await self.event_bus.stop()

        # Close state store
        await self.state_store.close()

        self._started = False
        logger.info("Engine stopped")

    def create_api(
        self,
        spaces_path: str | None = None,
        scenes_path: str | None = None,
        cors_origins: list[str] | None = None,
    ) -> Any:
        """Create the FastAPI app wired to this engine.

        Args:
            spaces_path: Path to spaces.yaml config file.
            scenes_path: Path to scenes.yaml config file.
            cors_origins: List of allowed CORS origins. Reads from
                          SMARTSPACES_CORS_ORIGINS env var if not provided.

        Returns the FastAPI app instance, or None if fastapi is not installed.
        """
        import os

        from agent.spaces import SpaceRegistry
        from agent.scenes import SceneEngine
        from core.api import create_api

        space_registry = None
        scene_engine = None

        if spaces_path:
            path = Path(spaces_path)
            if path.exists():
                space_registry = SpaceRegistry.from_yaml(path)
                logger.info("Loaded spaces from %s (%d devices)", path, len(space_registry._by_semantic))
            else:
                logger.warning("Spaces file not found: %s", path)

        if scenes_path:
            path = Path(scenes_path)
            if path.exists():
                scene_engine = SceneEngine.from_yaml(path)
                logger.info("Loaded scenes from %s", path)
            else:
                logger.warning("Scenes file not found: %s", path)

        # Resolve CORS origins
        if cors_origins is None:
            env_origins = os.environ.get("SMARTSPACES_CORS_ORIGINS", "")
            if env_origins.strip():
                cors_origins = [o.strip() for o in env_origins.split(",") if o.strip()]

        self._api = create_api(
            self.registry,
            self.state_store,
            self.scheduler,
            space_registry=space_registry,
            scene_engine=scene_engine,
            cors_origins=cors_origins,
        )
        return self._api

    async def run_server(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        spaces_path: str | None = None,
        scenes_path: str | None = None,
        restore: bool = True,
        cors_origins: list[str] | None = None,
    ) -> None:
        """Start the engine and run the API server."""
        try:
            import uvicorn
        except ImportError:
            logger.error("uvicorn not installed. Run: pip install 'physical-space-adapters[server]'")
            return

        await self.start(restore_connections=restore)
        app = self.create_api(
            spaces_path=spaces_path,
            scenes_path=scenes_path,
            cors_origins=cors_origins,
        )
        if not app:
            logger.error("FastAPI not installed. Run: pip install 'physical-space-adapters[server]'")
            return

        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        # Register signal handlers for graceful shutdown
        shutdown_event = asyncio.Event()

        def _signal_handler(sig: int, _frame: Any) -> None:
            sig_name = signal.Signals(sig).name
            logger.info("Received %s, shutting down gracefully...", sig_name)
            shutdown_event.set()
            server.should_exit = True

        # SIGINT (Ctrl+C) works on all platforms
        signal.signal(signal.SIGINT, _signal_handler)
        # SIGTERM only on Unix
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, _signal_handler)

        try:
            await server.serve()
        finally:
            await self.stop()

    async def _on_point_reported(self, event: dict[str, Any]) -> None:
        """Auto-persist point values from event bus."""
        point_id = event.get("point_id")
        if point_id:
            await self.state_store.save_point_value(
                point_id,
                value=event.get("value"),
                quality=event.get("quality"),
            )

    # -- Convenience methods --

    async def quick_connect(
        self,
        adapter_id: str,
        profile_id: str,
        fields: dict[str, Any],
        secrets: dict[str, str] | None = None,
        poll_interval: float | None = None,
    ) -> str:
        """Commission, inventory, and schedule polling in one call.

        Returns the connection_id.
        """
        result = await self.registry.commission_simple(
            adapter_id, profile_id, fields, secrets
        )
        if result.status != "ok":
            raise RuntimeError(f"Commission failed: {result.diagnostics}")

        snapshot = await self.registry.inventory(result.connection_id)
        self.scheduler.add_targets_from_inventory(
            result.connection_id, snapshot.points, poll_interval
        )

        return result.connection_id


# -- CLI entry point --

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartspaces",
        description="SmartSpaces — Universal Physical Space Adapter Runtime",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="Listen port (default: 8000)",
    )
    parser.add_argument(
        "--db-path", default="state.db",
        help="SQLite database path (default: state.db)",
    )
    parser.add_argument(
        "--spaces", default=None, metavar="PATH",
        help="Path to spaces.yaml config file",
    )
    parser.add_argument(
        "--scenes", default=None, metavar="PATH",
        help="Path to scenes.yaml config file",
    )
    parser.add_argument(
        "--no-restore", action="store_true",
        help="Skip restoring previous connections on startup",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--cors-origins", default=None,
        help="Comma-separated CORS allowed origins (or set SMARTSPACES_CORS_ORIGINS)",
    )
    parser.add_argument(
        "--log-format", default="text",
        choices=["text", "json"],
        help="Log format: text (human-readable) or json (structured) (default: text)",
    )
    return parser


def main():
    """Run the engine with all available adapters."""
    # Load .env before anything else
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    args = _build_parser().parse_args()

    from core.logging_config import configure_logging
    configure_logging(level=args.log_level, log_format=args.log_format)

    # Import and register all adapters
    adapters_to_register: list[Adapter] = []

    _ADAPTER_IMPORTS = [
        ("adapters.kincony", "KinConyAdapter"),
        ("adapters.shelly", "ShellyAdapter"),
        ("adapters.mqtt_generic", "MqttGenericAdapter"),
        ("adapters.modbus", "ModbusAdapter"),
        ("adapters.hue", "HueAdapter"),
        ("adapters.onvif", "OnvifAdapter"),
        ("adapters.esphome", "ESPHomeAdapter"),
        ("adapters.zigbee", "ZigbeeAdapter"),
        ("adapters.zwave", "ZWaveAdapter"),
        ("adapters.matter", "MatterAdapter"),
        ("adapters.lutron", "LutronAdapter"),
        ("adapters.knx", "KnxAdapter"),
        ("adapters.bacnet", "BacnetAdapter"),
        ("adapters.opcua", "OpcUaAdapter"),
        ("adapters.dnp3", "Dnp3Adapter"),
    ]

    import importlib
    for module_path, class_name in _ADAPTER_IMPORTS:
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            adapters_to_register.append(cls())
        except ImportError as e:
            logger.warning("Adapter %s unavailable (missing dependency: %s)", class_name, e)
        except Exception as e:
            logger.warning("Failed to load adapter %s: %s", class_name, e)

    if not adapters_to_register:
        logger.error("No adapters available")
        sys.exit(1)

    engine = Engine(db_path=args.db_path)
    for adapter in adapters_to_register:
        engine.register_adapter(adapter)

    cors_origins = None
    if args.cors_origins:
        cors_origins = [o.strip() for o in args.cors_origins.split(",") if o.strip()]

    logger.info("Starting engine with %d adapters on %s:%d", len(adapters_to_register), args.host, args.port)
    asyncio.run(engine.run_server(
        host=args.host,
        port=args.port,
        spaces_path=args.spaces,
        scenes_path=args.scenes,
        restore=not args.no_restore,
        cors_origins=cors_origins,
    ))


if __name__ == "__main__":
    main()
