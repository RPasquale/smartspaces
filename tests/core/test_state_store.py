"""Tests for the SQLite state store."""

import pytest

from core.state_store import StateStore


@pytest.fixture
async def store(tmp_path):
    s = StateStore(db_path=tmp_path / "test.db")
    await s.open()
    yield s
    await s.close()


class TestStateStore:
    @pytest.mark.asyncio
    async def test_save_and_get_connection(self, store):
        await store.save_connection("conn_1", "kincony.family", {"host": "192.168.1.100"})
        conn = await store.get_connection("conn_1")
        assert conn is not None
        assert conn["adapter_id"] == "kincony.family"
        assert conn["profile"]["host"] == "192.168.1.100"
        assert conn["status"] == "commissioned"

    @pytest.mark.asyncio
    async def test_list_connections(self, store):
        await store.save_connection("conn_1", "kincony.family", {})
        await store.save_connection("conn_2", "shelly.gen2", {})
        all_conns = await store.list_connections()
        assert len(all_conns) == 2

        kincony_conns = await store.list_connections("kincony.family")
        assert len(kincony_conns) == 1

    @pytest.mark.asyncio
    async def test_save_and_get_device(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {"name": "Test Device", "family": "test"})
        dev = await store.get_device("dev_1")
        assert dev is not None
        assert dev["name"] == "Test Device"
        assert dev["connection_id"] == "conn_1"

    @pytest.mark.asyncio
    async def test_list_devices(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {"name": "D1"})
        await store.save_device("dev_2", "conn_1", {"name": "D2"})
        devices = await store.list_devices("conn_1")
        assert len(devices) == 2

    @pytest.mark.asyncio
    async def test_save_and_list_endpoints(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {})
        await store.save_endpoint("ep_1", "dev_1", {"type": "relay", "capabilities": ["switch"]})
        await store.save_endpoint("ep_2", "dev_1", {"type": "input", "capabilities": ["sensor"]})
        eps = await store.list_endpoints("dev_1")
        assert len(eps) == 2

    @pytest.mark.asyncio
    async def test_save_and_list_points(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {})
        await store.save_endpoint("ep_1", "dev_1", {})
        await store.save_point("pt_1", "ep_1", {"point_class": "switch.state", "value_type": "bool"})
        await store.save_point("pt_2", "ep_1", {"point_class": "analog.raw", "value_type": "int"})
        points = await store.list_points("ep_1")
        assert len(points) == 2

    @pytest.mark.asyncio
    async def test_point_values(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {})
        await store.save_endpoint("ep_1", "dev_1", {})
        await store.save_point("pt_1", "ep_1", {})

        await store.save_point_value("pt_1", value={"kind": "bool", "reported": True}, quality={"status": "good"})
        val = await store.get_point_value("pt_1")
        assert val is not None
        assert val["value"]["reported"] is True
        assert val["quality"]["status"] == "good"

    @pytest.mark.asyncio
    async def test_point_value_update(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {})
        await store.save_endpoint("ep_1", "dev_1", {})
        await store.save_point("pt_1", "ep_1", {})

        await store.save_point_value("pt_1", value={"reported": False})
        await store.save_point_value("pt_1", value={"reported": True})
        val = await store.get_point_value("pt_1")
        assert val["value"]["reported"] is True

    @pytest.mark.asyncio
    async def test_persist_inventory(self, store):
        await store.save_connection("conn_1", "test", {})
        snapshot = {
            "devices": [{"device_id": "dev_1", "name": "Test"}],
            "endpoints": [{"endpoint_id": "ep_1", "device_id": "dev_1", "type": "relay"}],
            "points": [{"point_id": "pt_1", "endpoint_id": "ep_1", "point_class": "switch.state"}],
        }
        await store.persist_inventory("conn_1", snapshot)

        devices = await store.list_devices("conn_1")
        assert len(devices) == 1
        points = await store.list_points("ep_1")
        assert len(points) == 1

    @pytest.mark.asyncio
    async def test_delete_connection_cascade(self, store):
        await store.save_connection("conn_1", "test", {})
        await store.save_device("dev_1", "conn_1", {})
        await store.save_endpoint("ep_1", "dev_1", {})
        await store.save_point("pt_1", "ep_1", {})
        await store.save_point_value("pt_1", value=True)

        await store.delete_connection("conn_1")

        assert await store.get_connection("conn_1") is None
        assert await store.get_device("dev_1") is None
        assert await store.get_point("pt_1") is None
        assert await store.get_point_value("pt_1") is None

    @pytest.mark.asyncio
    async def test_audit_log(self, store):
        await store.audit("command.succeeded", connection_id="conn_1", command_id="cmd_1", detail={"relay": 1})
        await store.audit("command.failed", connection_id="conn_1", command_id="cmd_2")

        log = await store.get_audit_log(limit=10)
        assert len(log) == 2
        assert log[0]["event_type"] == "command.failed"  # Most recent first
        assert log[1]["detail"]["relay"] == 1
