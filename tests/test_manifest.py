"""Tests for manifest loading and validation."""

from pathlib import Path

from sdk.adapter_api.manifest import AdapterManifest, load_manifest, load_manifest_dict


FIXTURES_DIR = Path(__file__).parent.parent / "adapters"


class TestManifestLoading:
    def test_load_kincony_manifest(self):
        manifest = load_manifest(FIXTURES_DIR / "kincony" / "adapter.yaml")
        assert manifest.id == "kincony.family"
        assert manifest.adapter_class == "direct_device"
        assert "http" in manifest.transports
        assert "relay_output" in manifest.capability_families
        assert len(manifest.connection_templates) == 2

    def test_load_shelly_manifest(self):
        manifest = load_manifest(FIXTURES_DIR / "shelly" / "adapter.yaml")
        assert manifest.id == "shelly.gen2"
        assert "websocket" in manifest.transports

    def test_load_mqtt_manifest(self):
        manifest = load_manifest(FIXTURES_DIR / "mqtt_generic" / "adapter.yaml")
        assert manifest.id == "mqtt.generic"
        assert manifest.adapter_class == "bus"

    def test_load_modbus_manifest(self):
        manifest = load_manifest(FIXTURES_DIR / "modbus" / "adapter.yaml")
        assert manifest.id == "modbus.generic"
        assert "modbus_tcp" in manifest.transports

    def test_load_hue_manifest(self):
        manifest = load_manifest(FIXTURES_DIR / "hue" / "adapter.yaml")
        assert manifest.id == "hue.bridge"
        assert manifest.adapter_class == "bridge"

    def test_load_onvif_manifest(self):
        manifest = load_manifest(FIXTURES_DIR / "onvif" / "adapter.yaml")
        assert manifest.id == "onvif.camera"
        assert "camera_stream" in manifest.capability_families

    def test_load_from_dict(self):
        manifest = load_manifest_dict({
            "id": "test.adapter",
            "display_name": "Test",
            "version": "0.1.0",
        })
        assert manifest.id == "test.adapter"
        assert manifest.adapter_api == "1.0"

    def test_manifest_supports_defaults(self):
        manifest = load_manifest_dict({
            "id": "test.adapter",
            "display_name": "Test",
        })
        assert manifest.supports.inventory is True
        assert manifest.supports.subscribe is False

    def test_compatibility_section(self):
        manifest = load_manifest(FIXTURES_DIR / "kincony" / "adapter.yaml")
        assert len(manifest.compatibility.firmware_ranges) > 0
        assert len(manifest.compatibility.safety_notes) > 0
