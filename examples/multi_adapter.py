"""Multi-adapter example: run the server with KinCony + Shelly adapters.

Usage:
    pip install -e ".[server,kincony]"
    python examples/multi_adapter.py
"""

import asyncio

from core.engine import Engine
from adapters.kincony import KinConyAdapter
from adapters.shelly import ShellyAdapter


async def main():
    engine = Engine(db_path="state.db")
    engine.register_adapter(KinConyAdapter())
    engine.register_adapter(ShellyAdapter())

    await engine.run_server(
        host="0.0.0.0",
        port=8000,
        spaces_path="fixtures/spaces_example.yaml",
        scenes_path="fixtures/scenes_example.yaml",
    )


if __name__ == "__main__":
    asyncio.run(main())
