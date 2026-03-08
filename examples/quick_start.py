"""Quick-start example: connect a KinCony KC868-A4 and read relay states.

Usage:
    pip install -e ".[server,kincony]"
    export KINCONY_IP=192.168.0.90
    python examples/quick_start.py
"""

import asyncio
import os

from core.engine import Engine
from adapters.kincony import KinConyAdapter


async def main():
    host = os.environ.get("KINCONY_IP", "192.168.0.90")

    engine = Engine(db_path=":memory:")
    engine.register_adapter(KinConyAdapter())

    await engine.start(restore_connections=False)

    try:
        conn_id = await engine.quick_connect(
            adapter_id="kincony.tasmota",
            profile_id="default",
            fields={"host": host},
        )
        print(f"Connected: {conn_id}")

        # Read all points
        snapshot = await engine.registry.inventory(conn_id)
        for pt in snapshot.points:
            result = await engine.registry.read_point(conn_id, pt["point_id"])
            print(f"  {pt['point_id']}: {result.get('value')}")

    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
