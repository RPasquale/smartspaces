"""Engine — boots the entire adapter runtime.

Initializes the event bus, state store, registry, scheduler, and API.
Provides a single entry point to start and stop the system, and
convenience methods for registering adapters and running the server.
"""

from __future__ import annotations

import asyncio
import logging
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

    async def start(self) -> None:
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

    def create_api(self) -> Any:
        """Create the FastAPI app wired to this engine.

        Returns the FastAPI app or None if fastapi is not installed.
        """
        from core.api import create_api
        self._api = create_api(self.registry, self.state_store, self.scheduler)
        return self._api

    async def run_server(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        """Start the engine and run the API server.

        This is the main entry point for running the full system.
        """
        try:
            import uvicorn
        except ImportError:
            logger.error("uvicorn not installed. Run: pip install 'physical-space-adapters[server]'")
            return

        await self.start()
        app = self.create_api()
        if not app:
            logger.error("FastAPI not installed. Run: pip install 'physical-space-adapters[server]'")
            return

        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
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

def main():
    """Run the engine with all available adapters."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Import and register all adapters
    adapters_to_register: list[Adapter] = []

    try:
        from adapters.kincony import KinConyAdapter
        adapters_to_register.append(KinConyAdapter())
    except ImportError:
        pass

    try:
        from adapters.shelly import ShellyAdapter
        adapters_to_register.append(ShellyAdapter())
    except ImportError:
        pass

    try:
        from adapters.mqtt_generic import MqttGenericAdapter
        adapters_to_register.append(MqttGenericAdapter())
    except ImportError:
        pass

    try:
        from adapters.modbus import ModbusAdapter
        adapters_to_register.append(ModbusAdapter())
    except ImportError:
        pass

    try:
        from adapters.hue import HueAdapter
        adapters_to_register.append(HueAdapter())
    except ImportError:
        pass

    try:
        from adapters.onvif import OnvifAdapter
        adapters_to_register.append(OnvifAdapter())
    except ImportError:
        pass

    if not adapters_to_register:
        logger.error("No adapters available")
        sys.exit(1)

    engine = Engine()
    for adapter in adapters_to_register:
        engine.register_adapter(adapter)

    host = "0.0.0.0"
    port = 8000

    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--host" and i + 2 < len(sys.argv):
            host = sys.argv[i + 2]
        elif arg == "--port" and i + 2 < len(sys.argv):
            port = int(sys.argv[i + 2])

    logger.info("Starting engine with %d adapters on %s:%d", len(adapters_to_register), host, port)
    asyncio.run(engine.run_server(host=host, port=port))


if __name__ == "__main__":
    main()
