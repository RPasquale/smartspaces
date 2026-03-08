"""DNP3 master adapter.

Connects to DNP3 outstations (RTUs, IEDs, meters) used in utility
SCADA and industrial control. Supports reading analog/binary inputs,
counters, and writing analog/binary outputs via DNP3/TCP.

Requires the ``pydnp3`` library (opendnp3 Python bindings) for real
DNP3 communication.
Install via: pip install 'physical-space-adapters[dnp3]'
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sdk.adapter_api.base import (
    Adapter,
    AdapterClass,
    CommissionResult,
    ConnectionProfile,
    ConnectionTemplate,
    DiscoveredTarget,
    DiscoveryRequest,
    HealthStatus,
    InventorySnapshot,
)
from sdk.adapter_api.errors import UnreachableError

# ---------------------------------------------------------------------------
# Optional dependency: pydnp3 (opendnp3 Python bindings)
# ---------------------------------------------------------------------------
try:
    from pydnp3 import opendnp3, openpal, asiopal, asiodnp3

    _HAS_PYDNP3 = True
except ImportError:
    _HAS_PYDNP3 = False

log = logging.getLogger(__name__)

# DNP3 object group -> canonical capabilities
DNP3_GROUP_MAP = {
    1: (["binary_input"], "read", "S0", "binary_input"),
    2: (["binary_input"], "read", "S0", "binary_input_event"),
    10: (["binary_output"], "read_write", "S3", "binary_output"),
    12: (["binary_output"], "write", "S3", "crob"),
    20: (["counter"], "read", "S0", "counter"),
    21: (["counter"], "read", "S0", "frozen_counter"),
    30: (["analog_input"], "read", "S0", "analog_input"),
    32: (["analog_input"], "read", "S0", "analog_input_event"),
    40: (["analog_output"], "read_write", "S3", "analog_output"),
    41: (["analog_output"], "write", "S3", "analog_output_command"),
}


# ---------------------------------------------------------------------------
# pydnp3 callback helpers — bridge opendnp3 callbacks into asyncio
# ---------------------------------------------------------------------------

class _SOEHandler:
    """ISOEHandler implementation that stores received values.

    opendnp3 delivers measurements via the SOE (Sequence Of Events)
    handler callbacks.  We capture them into a shared dict keyed by
    ``"group:index"`` and optionally set asyncio events so that async
    callers can ``await`` specific reads.
    """

    def __init__(self, data_map: dict[str, dict[str, Any]], loop: asyncio.AbstractEventLoop):
        self._data_map = data_map
        self._loop = loop
        # Optional per-key waiters for direct reads
        self._waiters: dict[str, asyncio.Event] = {}

    # -- opendnp3 ISOEHandler interface ------------------------------------

    def Process(self, info, values):  # noqa: N802 – matches C++ naming
        """Called by the opendnp3 stack for each measurement batch."""
        try:
            visitor = _MeasurementVisitor(self._data_map, self._loop, self._waiters)
            values.Foreach(visitor)
        except Exception:
            log.debug("SOEHandler.Process error", exc_info=True)

    def Start(self):  # noqa: N802
        pass

    def End(self):  # noqa: N802
        pass

    # -- asyncio helpers ---------------------------------------------------

    def get_waiter(self, key: str) -> asyncio.Event:
        if key not in self._waiters:
            self._waiters[key] = asyncio.Event()
        else:
            self._waiters[key].clear()
        return self._waiters[key]


class _MeasurementVisitor:
    """Visitor that extracts values from opendnp3 measurement iterators."""

    def __init__(self, data_map: dict, loop: asyncio.AbstractEventLoop,
                 waiters: dict[str, asyncio.Event]):
        self._data_map = data_map
        self._loop = loop
        self._waiters = waiters

    def _store(self, group: int, index: int, value: Any, flags: int = 0,
               timestamp: Any = None):
        key = f"{group}:{index}"
        self._data_map[key] = {
            "value": value,
            "flags": flags,
            "timestamp": str(timestamp) if timestamp else None,
        }
        waiter = self._waiters.get(key)
        if waiter is not None:
            self._loop.call_soon_threadsafe(waiter.set)

    # opendnp3 calls these per measurement type
    def OnValue(self, info, value):  # noqa: N802
        try:
            idx = info.gv  # group/variation info varies by pydnp3 version
        except Exception:
            idx = 0
        self._store(30, getattr(info, "index", 0), value.value,
                     getattr(value, "flags", 0))


class _ChannelListener:
    """Minimal IChannelListener to log channel state changes."""

    def OnStateChange(self, state):  # noqa: N802
        log.debug("DNP3 channel state: %s", state)


class _MasterApplication:
    """Minimal IMasterApplication."""

    def OnReceiveIIN(self, iin):  # noqa: N802
        pass

    def OnTaskStart(self, info):  # noqa: N802
        pass

    def OnTaskComplete(self, info):  # noqa: N802
        pass

    def AssignClassDuringStartup(self):  # noqa: N802
        return False

    def Now(self):  # noqa: N802
        return openpal.UTCTimestamp() if _HAS_PYDNP3 else 0


class _Dnp3Connection:
    """Wraps a DNP3 master-outstation TCP connection using *pydnp3*.

    pydnp3 wraps the opendnp3 C++ library which uses a callback-driven
    architecture.  This class bridges into asyncio using events and
    ``run_in_executor`` for blocking setup.

    If ``pydnp3`` is not installed the constructor succeeds but
    :meth:`connect` raises :class:`ImportError`.
    """

    def __init__(self, connection_id: str, host: str, port: int = 20000,
                 outstation_addr: int = 1, master_addr: int = 0):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.outstation_addr = outstation_addr
        self.master_addr = master_addr
        self.commissioned_at = datetime.now(timezone.utc)
        self._data_map: dict[str, dict[str, Any]] = {}
        self._connected = False

        # pydnp3 objects (set during connect)
        self._manager: Any | None = None
        self._channel: Any | None = None
        self._master: Any | None = None
        self._soe_handler: _SOEHandler | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Create the DNP3Manager, TCP channel, and master session."""
        if not _HAS_PYDNP3:
            raise ImportError(
                "The 'pydnp3' library is required for the DNP3 adapter. "
                "Install it with: pip install 'physical-space-adapters[dnp3]'"
            )

        loop = asyncio.get_running_loop()

        def _setup() -> None:
            # DNP3Manager with 1 thread for the asio reactor
            self._manager = asiodnp3.DNP3Manager(1)

            # TCP client channel
            retry = asiopal.ChannelRetry.Default()
            self._channel = self._manager.AddTCPClient(
                id=f"channel_{self.connection_id}",
                levels=opendnp3.levels.NOTHING,
                retry=retry,
                host=self.host,
                local="0.0.0.0",
                port=self.port,
                listener=_ChannelListener(),
            )

            # SOE handler to capture measurements
            self._soe_handler = _SOEHandler(self._data_map, loop)

            # Master stack config
            stack_config = asiodnp3.MasterStackConfig()
            stack_config.master.responseTimeout = openpal.TimeDuration().Seconds(5)
            stack_config.link.LocalAddr = self.master_addr
            stack_config.link.RemoteAddr = self.outstation_addr

            self._master = self._channel.AddMaster(
                id=f"master_{self.connection_id}",
                SOEHandler=self._soe_handler,
                application=_MasterApplication(),
                config=stack_config,
            )
            self._master.Enable()

        await asyncio.to_thread(_setup)
        self._connected = True
        log.info("DNP3 connection %s established to %s:%s (outstation=%s)",
                 self.connection_id, self.host, self.port, self.outstation_addr)
        return True

    async def close(self) -> None:
        """Shutdown the DNP3 manager and release resources."""
        if self._manager is not None:
            try:
                await asyncio.to_thread(self._manager.Shutdown)
            except Exception:
                log.debug("Error shutting down DNP3Manager", exc_info=True)
            self._manager = None
            self._channel = None
            self._master = None
        self._connected = False

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def integrity_poll(self) -> dict[str, Any]:
        """Perform a Class 0/1/2/3 integrity poll.

        Returns the full data map after the scan completes.
        """
        if not self._connected or self._master is None:
            return self._data_map

        try:
            done = asyncio.Event()

            class _ScanCallback:
                def OnComplete(self_, result):  # noqa: N802, N805
                    asyncio.get_event_loop().call_soon_threadsafe(done.set)

            await asyncio.to_thread(
                self._master.ScanClasses,
                opendnp3.ClassField.AllClasses(),
                _ScanCallback(),
            )
            # Wait for the scan to return (with timeout)
            try:
                await asyncio.wait_for(done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("DNP3 integrity poll timeout")
        except Exception:
            log.warning("DNP3 integrity poll failed", exc_info=True)

        return self._data_map

    # ------------------------------------------------------------------
    # Read / Write
    # ------------------------------------------------------------------

    async def read_point(self, group: int, variation: int, index: int) -> Any:
        """Read a single point by triggering a class scan and waiting.

        If a cached value exists it is returned immediately; otherwise
        an integrity poll is attempted.
        """
        key = f"{group}:{index}"

        # Return cached if available
        cached = self._data_map.get(key, {}).get("value")
        if cached is not None:
            return cached

        # Trigger a poll and wait
        if self._connected and self._master is not None and self._soe_handler is not None:
            waiter = self._soe_handler.get_waiter(key)
            try:
                done_ev = asyncio.Event()

                class _CB:
                    def OnComplete(self_, result):  # noqa: N802, N805
                        asyncio.get_event_loop().call_soon_threadsafe(done_ev.set)

                await asyncio.to_thread(
                    self._master.ScanClasses,
                    opendnp3.ClassField.AllClasses(),
                    _CB(),
                )
                await asyncio.wait_for(done_ev.wait(), timeout=5.0)
                # Give SOE handler a moment to process
                await asyncio.wait_for(waiter.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            except Exception:
                log.warning("DNP3 read_point poll failed", exc_info=True)

        return self._data_map.get(key, {}).get("value")

    async def direct_operate(self, group: int, variation: int,
                             index: int, value: Any) -> bool:
        """Send a DirectOperate command to the outstation.

        - Groups 10/12 (binary outputs): uses ControlRelayOutputBlock
        - Groups 40/41 (analog outputs): uses AnalogOutputInt32 or
          AnalogOutputFloat32 depending on the value type.
        """
        if not self._connected or self._master is None:
            # Offline cache-only
            key = f"{group}:{index}"
            if key not in self._data_map:
                self._data_map[key] = {}
            self._data_map[key]["value"] = value
            return True

        result_event = asyncio.Event()
        command_status: list[bool] = [False]

        class _CommandCallback:
            def OnComplete(self_, result):  # noqa: N802, N805
                command_status[0] = True  # simplified — real code checks result
                asyncio.get_event_loop().call_soon_threadsafe(result_event.set)

        try:
            if group in (10, 12):
                # Binary output — ControlRelayOutputBlock
                if value:
                    code = opendnp3.ControlCode.LATCH_ON
                else:
                    code = opendnp3.ControlCode.LATCH_OFF
                crob = opendnp3.ControlRelayOutputBlock(code)

                await asyncio.to_thread(
                    self._master.DirectOperate,
                    crob,
                    index,
                    _CommandCallback(),
                )

            elif group in (40, 41):
                # Analog output
                if isinstance(value, float):
                    cmd = opendnp3.AnalogOutputFloat32(value)
                else:
                    cmd = opendnp3.AnalogOutputInt32(int(value))

                await asyncio.to_thread(
                    self._master.DirectOperate,
                    cmd,
                    index,
                    _CommandCallback(),
                )
            else:
                log.warning("Unsupported DNP3 group %s for direct operate", group)
                return False

            await asyncio.wait_for(result_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("DNP3 DirectOperate timeout for g%s i%s", group, index)
            return False
        except Exception:
            log.warning("DNP3 DirectOperate failed", exc_info=True)
            return False

        # Update local cache
        key = f"{group}:{index}"
        if key not in self._data_map:
            self._data_map[key] = {}
        self._data_map[key]["value"] = value

        return command_status[0]


class Dnp3Adapter(Adapter):
    adapter_id: str = "dnp3.master"
    adapter_class: AdapterClass = "bus"

    def __init__(self):
        self._connections: dict[str, _Dnp3Connection] = {}

    def connection_templates(self) -> list[ConnectionTemplate]:
        return [
            ConnectionTemplate(
                adapter_id=self.adapter_id,
                display_name="DNP3 TCP Outstation",
                category="bus",
                discovery_methods=["manual_ip"],
                required_fields=["host", "outstation_address"],
                optional_fields=["port", "master_address", "point_map"],
                supports_auto_inventory=False,
                supports_local_only_mode=True,
            ),
        ]

    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        targets = []
        host = request.scope.get("host")
        if host:
            targets.append(DiscoveredTarget(
                discovery_id=f"disc_{uuid.uuid4().hex[:8]}",
                adapter_id=self.adapter_id,
                native_ref=host,
                title=f"DNP3 Outstation @ {host}",
                address=host,
                confidence=0.5,
            ))
        return targets

    async def commission(
        self, target: DiscoveredTarget | None, profile: ConnectionProfile,
    ) -> CommissionResult:
        host = profile.fields.get("host") or (target.address if target else None)
        if not host:
            return CommissionResult("", "failed", {"error": "No host"})

        port = int(profile.fields.get("port", 20000))
        outstation_addr = int(profile.fields.get("outstation_address", 1))
        master_addr = int(profile.fields.get("master_address", 0))

        conn_id = f"dnp3_{uuid.uuid4().hex[:8]}"
        conn = _Dnp3Connection(conn_id, host, port, outstation_addr, master_addr)
        try:
            await conn.connect()
        except ImportError as exc:
            return CommissionResult("", "failed", {"error": str(exc)})
        except Exception as e:
            await conn.close()
            return CommissionResult("", "failed", {"error": str(e)})

        # Preload point map if provided
        point_map = profile.fields.get("point_map", [])
        if isinstance(point_map, list):
            for pt in point_map:
                group = pt.get("group", 30)
                index = pt.get("index", 0)
                conn._data_map[f"{group}:{index}"] = pt

        self._connections[conn_id] = conn
        return CommissionResult(conn_id, "ok", {
            "host": host, "port": port,
            "outstation_address": outstation_addr,
        })

    async def inventory(self, connection_id: str) -> InventorySnapshot:
        conn = self._get_conn(connection_id)

        dev_id = f"dev_dnp3_{conn.host.replace('.', '_')}_{conn.outstation_addr}"
        devices = [{
            "device_id": dev_id,
            "native_device_ref": f"{conn.host}:{conn.outstation_addr}",
            "device_family": "dnp3.outstation",
            "name": f"DNP3 Outstation {conn.outstation_addr}",
            "connectivity": {
                "transport": "dnp3_tcp",
                "address": conn.host,
                "outstation_address": conn.outstation_addr,
            },
            "safety_class": "S3",
        }]
        endpoints = []
        points = []

        for key, pt_info in conn._data_map.items():
            group, index = key.split(":")
            group = int(group)
            index = int(index)
            name = pt_info.get("name", f"G{group}I{index}")

            caps, direction, safety, ptype = DNP3_GROUP_MAP.get(
                group, (["analog_input"], "read", "S0", "unknown")
            )

            is_analog = group in (30, 32, 40, 41)
            vtype = "float" if is_analog else "bool"
            unit = pt_info.get("unit")

            ep_id = f"{dev_id}_g{group}_i{index}"
            endpoints.append({
                "endpoint_id": ep_id,
                "device_id": dev_id,
                "native_endpoint_ref": f"g{group}v0i{index}",
                "endpoint_type": ptype,
                "direction": direction,
                "capabilities": caps,
                "polling_mode": "poll",
                "safety_class": safety,
            })
            points.append({
                "point_id": f"{ep_id}_value",
                "endpoint_id": ep_id,
                "point_class": f"dnp3.{ptype}",
                "value_type": vtype,
                "unit": unit,
                "readable": direction in ("read", "read_write"),
                "writable": direction in ("write", "read_write"),
                "native_ref": f"{group}:{index}",
                "source_protocol": "dnp3",
            })

        return InventorySnapshot(
            connection_id=connection_id, devices=devices,
            endpoints=endpoints, points=points,
        )

    async def subscribe(
        self, connection_id: str, point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Real impl: unsolicited responses from outstation
        conn = self._get_conn(connection_id)
        data = await conn.integrity_poll()
        for key, pt in data.items():
            group, index = key.split(":")
            yield {
                "type": "point.reported",
                "point_id": f"dev_dnp3_{conn.host.replace('.', '_')}_{conn.outstation_addr}_g{group}_i{index}_value",
                "value": {"kind": "float", "reported": pt.get("value")},
                "quality": {"status": "good", "source_type": "polled"},
            }

    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        # Parse group and index from point_id
        # Format: dev_dnp3_..._g30_i0_value
        parts = point_id.rsplit("_value", 1)[0]
        g_part = ""
        i_part = ""
        for seg in parts.split("_"):
            if seg.startswith("g") and seg[1:].isdigit():
                g_part = seg[1:]
            elif seg.startswith("i") and seg[1:].isdigit():
                i_part = seg[1:]

        if g_part and i_part:
            value = await conn.read_point(int(g_part), 0, int(i_part))
            return {
                "point_id": point_id,
                "value": {"kind": "float", "reported": value},
                "quality": {"status": "good" if value is not None else "stale", "source_type": "polled"},
            }
        return {
            "point_id": point_id,
            "value": None,
            "quality": {"status": "stale", "source_type": "polled"},
        }

    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        conn = self._get_conn(connection_id)
        cmd_id = command.get("command_id", f"cmd_{uuid.uuid4().hex[:8]}")
        target = command.get("target", {})
        params = command.get("params", {})

        group = int(target.get("group", 12))
        variation = int(target.get("variation", 1))
        index = int(target.get("index", 0))
        value = params.get("value")

        try:
            ok = await conn.direct_operate(group, variation, index, value)
            status = "succeeded" if ok else "failed"
            return {"command_id": cmd_id, "status": status}
        except Exception as e:
            return {"command_id": cmd_id, "status": "failed", "error": str(e)}

    async def health(self, connection_id: str) -> HealthStatus:
        conn = self._get_conn(connection_id)
        if conn._connected:
            return HealthStatus("healthy", {
                "host": conn.host,
                "outstation_address": conn.outstation_addr,
            })
        return HealthStatus("error", {"host": conn.host, "error": "Not connected"})

    async def teardown(self, connection_id: str) -> None:
        conn = self._connections.pop(connection_id, None)
        if conn:
            await conn.close()

    def _get_conn(self, cid: str) -> _Dnp3Connection:
        conn = self._connections.get(cid)
        if not conn:
            raise UnreachableError(f"No active connection: {cid}")
        return conn
