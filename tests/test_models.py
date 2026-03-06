"""Tests for the canonical domain models."""

from datetime import datetime, timezone

from sdk.adapter_api.models import (
    Asset,
    Capability,
    CommandEnvelope,
    CommandTarget,
    Device,
    DeviceConnectivity,
    Endpoint,
    EndpointDirection,
    EventEnvelope,
    EventSource,
    Organization,
    OrgType,
    Point,
    Quality,
    QualityStatus,
    SafetyClass,
    Site,
    Space,
    ValueReport,
)


class TestTenancyModels:
    def test_organization(self):
        org = Organization(
            org_id="org_1",
            name="Test Household",
            type=OrgType.RESIDENTIAL,
            owner_account_id="acc_1",
        )
        assert org.org_id == "org_1"
        assert org.type == OrgType.RESIDENTIAL

    def test_site(self):
        site = Site(
            site_id="site_1",
            org_id="org_1",
            name="Main House",
            timezone="Australia/Sydney",
        )
        assert site.timezone == "Australia/Sydney"

    def test_space(self):
        space = Space(
            space_id="space_1",
            site_id="site_1",
            name="Living Room",
            space_type="room",
            tags=["living", "main_floor"],
        )
        assert "living" in space.tags


class TestAssetModels:
    def test_device(self):
        dev = Device(
            device_id="dev_1",
            native_device_ref="192.168.1.100",
            device_family="kincony.kc868_a4",
            name="Relay Board",
            manufacturer="KinCony",
            connectivity=DeviceConnectivity(transport="http", address="192.168.1.100"),
            safety_class=SafetyClass.S2_COMFORT_EQUIPMENT,
        )
        assert dev.safety_class == SafetyClass.S2_COMFORT_EQUIPMENT
        assert dev.connectivity.transport == "http"

    def test_endpoint(self):
        ep = Endpoint(
            endpoint_id="ep_relay_1",
            device_id="dev_1",
            native_endpoint_ref="Power1",
            endpoint_type="relay_channel",
            direction=EndpointDirection.READ_WRITE,
            capabilities=["relay_output", "binary_switch"],
        )
        assert "relay_output" in ep.capabilities

    def test_point(self):
        pt = Point(
            point_id="pt_relay_1_state",
            endpoint_id="ep_relay_1",
            point_class="switch.state",
            value_type="bool",
            readable=True,
            writable=True,
        )
        assert pt.writable is True


class TestQuality:
    def test_default_quality(self):
        q = Quality()
        assert q.status == QualityStatus.GOOD
        assert q.confidence == 1.0

    def test_degraded_quality(self):
        q = Quality(
            status=QualityStatus.STALE,
            freshness_ms=5000,
            confidence=0.5,
            comm_lost=True,
        )
        assert q.comm_lost is True


class TestEventEnvelope:
    def test_event_creation(self):
        evt = EventEnvelope(
            event_id="evt_001",
            type="point.reported",
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(
                adapter_id="kincony.family",
                connection_id="conn_1",
                native_device_ref="192.168.1.100",
                native_point_ref="relay:1",
            ),
            value=ValueReport(kind="bool", reported=True),
        )
        assert evt.type == "point.reported"
        assert evt.value.reported is True


class TestCommandEnvelope:
    def test_command_creation(self):
        cmd = CommandEnvelope(
            command_id="cmd_001",
            target=CommandTarget(device_id="dev_1", endpoint_id="ep_relay_1"),
            capability="binary_switch",
            verb="set",
            params={"value": True},
        )
        assert cmd.capability == "binary_switch"
        assert cmd.params["value"] is True
        assert cmd.priority == 50


class TestSafetyClass:
    def test_ordering(self):
        assert SafetyClass.S0_READ_ONLY.value == "S0"
        assert SafetyClass.S5_FORBIDDEN.value == "S5"

    def test_all_classes_exist(self):
        classes = list(SafetyClass)
        assert len(classes) == 6
