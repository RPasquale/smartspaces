"""Canonical domain models for the Universal Physical Space Adapter system.

These Pydantic models define the shared vocabulary between adapters,
the orchestrator, and the platform. Every adapter normalizes its
protocol-specific data into these canonical types.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class SafetyClass(str, enum.Enum):
    S0_READ_ONLY = "S0"
    S1_NON_DESTRUCTIVE = "S1"
    S2_COMFORT_EQUIPMENT = "S2"
    S3_OPERATIONAL_EQUIPMENT = "S3"
    S4_RESTRICTED = "S4"
    S5_FORBIDDEN = "S5"


class RelationshipType(str, enum.Enum):
    LOCATED_IN = "located_in"
    PART_OF = "part_of"
    CONTROLS = "controls"
    MEASURES = "measures"
    FEEDS = "feeds"
    ADJACENT_TO = "adjacent_to"
    POWERED_BY = "powered_by"
    NETWORKED_VIA = "networked_via"
    PAIRED_WITH = "paired_with"
    DERIVED_FROM = "derived_from"
    SAFETY_INTERLOCKED_WITH = "safety_interlocked_with"


class QualityStatus(str, enum.Enum):
    GOOD = "good"
    STALE = "stale"
    UNCERTAIN = "uncertain"
    BAD = "bad"
    SIMULATED = "simulated"


class SourceType(str, enum.Enum):
    DEVICE_PUSH = "device_push"
    POLLED = "polled"
    DERIVED = "derived"
    USER_INPUT = "user_input"
    IMPORTED = "imported"


class PollingMode(str, enum.Enum):
    PUSH_ONLY = "push_only"
    POLL_ONLY = "poll_only"
    PUSH_PREFERRED_WITH_POLL_VERIFY = "push_preferred_with_poll_verify"
    POLL_PREFERRED_WITH_EVENT_ASSIST = "poll_preferred_with_event_assist"


class OrgType(str, enum.Enum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    INDUSTRIAL = "industrial"
    MIXED = "mixed"


class EndpointDirection(str, enum.Enum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"
    EVENT_ONLY = "event_only"


class CommandVerb(str, enum.Enum):
    SET = "set"
    PULSE = "pulse"
    TOGGLE = "toggle"
    OPEN = "open"
    CLOSE = "close"
    STOP = "stop"
    DIM = "dim"
    SET_COLOR = "set_color"
    SET_MODE = "set_mode"
    SET_SETPOINT = "set_setpoint"
    INVOKE_SCENE = "invoke_scene"
    LOCK = "lock"
    UNLOCK = "unlock"
    ARM = "arm"
    DISARM = "disarm"
    WRITE_REGISTER = "write_register"
    INVOKE_METHOD = "invoke_method"
    PTZ_MOVE = "ptz_move"
    PTZ_PRESET = "ptz_preset"
    REFRESH = "refresh"
    SUPPRESS_ALARM = "suppress_alarm"
    ENABLE = "enable"
    DISABLE = "disable"


class BatchSemantics(str, enum.Enum):
    ALL_OR_NOTHING = "all_or_nothing"
    BEST_EFFORT = "best_effort"
    ORDERED_BEST_EFFORT = "ordered_best_effort"


# ---------------------------------------------------------------------------
# Capability (canonical family names)
# ---------------------------------------------------------------------------

CANONICAL_CAPABILITIES = frozenset({
    "binary_switch", "binary_sensor", "relay_output", "momentary_output",
    "dimmer", "light_color", "cover", "shade", "blind_tilt", "fan",
    "thermostat", "climate_setpoint", "humidity_sensor", "temperature_sensor",
    "pressure_sensor", "flow_sensor", "meter_power", "meter_energy",
    "meter_water", "meter_gas", "occupancy", "motion", "contact", "lock",
    "camera_stream", "camera_snapshot", "camera_motion", "ptz",
    "analog_input", "analog_output", "digital_input", "digital_output",
    "serial_bridge", "ir_transmit", "rf_transmit", "scene_member",
    "scene_controller", "schedule_target", "alarm", "valve", "pump",
    "hvac_mode", "setpoint", "battery", "network_health", "radio_health",
})


class Capability(BaseModel):
    name: str
    traits: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tenancy model
# ---------------------------------------------------------------------------

class Organization(BaseModel):
    org_id: str
    name: str
    type: OrgType = OrgType.RESIDENTIAL
    owner_account_id: str | None = None
    members: list[str] = Field(default_factory=list)
    default_policies: list[str] = Field(default_factory=list)


class Site(BaseModel):
    site_id: str
    org_id: str
    name: str
    address: str | None = None
    timezone: str = "UTC"
    geo: dict[str, float] | None = None
    utility_tariffs: list[dict[str, Any]] = Field(default_factory=list)
    demand_limits: list[dict[str, Any]] = Field(default_factory=list)
    safety_profile: str | None = None


class Space(BaseModel):
    space_id: str
    site_id: str
    parent_space_id: str | None = None
    space_type: str = "room"
    name: str
    tags: list[str] = Field(default_factory=list)
    semantic_attributes: dict[str, Any] = Field(default_factory=dict)
    occupancy_model: str | None = None


# ---------------------------------------------------------------------------
# Asset model
# ---------------------------------------------------------------------------

class Asset(BaseModel):
    asset_id: str
    space_id: str | None = None
    asset_type: str = "general"
    name: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    semantic_tags: list[str] = Field(default_factory=list)
    control_class: str | None = None
    safety_class: SafetyClass = SafetyClass.S1_NON_DESTRUCTIVE
    dependencies: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)


class DeviceConnectivity(BaseModel):
    transport: str
    address: str | None = None
    port: int | None = None


class DeviceHealth(BaseModel):
    status: str = "unknown"
    last_seen: datetime | None = None
    latency_ms: float | None = None


class Device(BaseModel):
    device_id: str
    asset_id: str | None = None
    adapter_instance_id: str | None = None
    native_device_ref: str
    device_family: str
    firmware: dict[str, Any] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)
    connectivity: DeviceConnectivity | None = None
    health: DeviceHealth = Field(default_factory=DeviceHealth)
    network_refs: list[str] = Field(default_factory=list)
    bridge_device_id: str | None = None
    space_id: str | None = None
    safety_class: SafetyClass = SafetyClass.S1_NON_DESTRUCTIVE
    name: str = ""
    manufacturer: str | None = None
    model: str | None = None


class Endpoint(BaseModel):
    endpoint_id: str
    device_id: str
    native_endpoint_ref: str
    endpoint_type: str
    direction: EndpointDirection = EndpointDirection.READ_WRITE
    units: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    traits: dict[str, Any] = Field(default_factory=dict)
    polling_mode: PollingMode = PollingMode.POLL_ONLY
    safety_class: SafetyClass = SafetyClass.S1_NON_DESTRUCTIVE


class Point(BaseModel):
    point_id: str
    endpoint_id: str
    point_class: str
    value_type: str = "bool"
    unit: str | None = None
    readable: bool = True
    writable: bool = False
    event_driven: bool = False
    native_ref: str = ""
    source_protocol: str = ""
    semantic_tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Quality model
# ---------------------------------------------------------------------------

class Quality(BaseModel):
    status: QualityStatus = QualityStatus.GOOD
    freshness_ms: float | None = None
    source_type: SourceType = SourceType.POLLED
    confidence: float = 1.0
    out_of_range: bool = False
    substituted: bool = False
    manual_override: bool = False
    requires_calibration: bool = False
    comm_lost: bool = False
    invalid: bool = False


# ---------------------------------------------------------------------------
# Value report
# ---------------------------------------------------------------------------

class ValueReport(BaseModel):
    kind: str
    reported: Any
    unit: str | None = None


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------

class EventSource(BaseModel):
    adapter_id: str
    connection_id: str
    native_device_ref: str = ""
    native_point_ref: str = ""


class EventEnvelope(BaseModel):
    event_id: str
    type: str
    occurred_at: datetime
    received_at: datetime | None = None
    site_id: str | None = None
    space_id: str | None = None
    asset_id: str | None = None
    device_id: str | None = None
    endpoint_id: str | None = None
    point_id: str | None = None
    source: EventSource
    value: ValueReport | None = None
    quality: Quality = Field(default_factory=Quality)
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Command envelope
# ---------------------------------------------------------------------------

class CommandTarget(BaseModel):
    device_id: str
    endpoint_id: str | None = None
    point_id: str | None = None


class CommandSafety(BaseModel):
    requires_confirmation: bool = False
    requires_interlock_check: bool = False
    lockout_tagout_ref: str | None = None


class CommandExpectation(BaseModel):
    ack_timeout_ms: int = 1500
    final_timeout_ms: int = 5000
    verify_after_write: bool = True


class CommandContext(BaseModel):
    initiator: str = "agent"
    goal: str | None = None
    trace_id: str | None = None


class CommandEnvelope(BaseModel):
    command_id: str
    idempotency_key: str | None = None
    site_id: str | None = None
    target: CommandTarget
    capability: str
    verb: CommandVerb = CommandVerb.SET
    params: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    priority: int = 50
    safety: CommandSafety = Field(default_factory=CommandSafety)
    expectation: CommandExpectation = Field(default_factory=CommandExpectation)
    context: CommandContext = Field(default_factory=CommandContext)


# ---------------------------------------------------------------------------
# Scene model
# ---------------------------------------------------------------------------

class SceneMember(BaseModel):
    point_id: str
    value: Any
    transition_ms: int | None = None


class Scene(BaseModel):
    scene_id: str
    name: str
    scope: str | None = None
    members: list[SceneMember] = Field(default_factory=list)
    transition_ms: int = 0
    rollback_scene_id: str | None = None


# ---------------------------------------------------------------------------
# Optimization metadata
# ---------------------------------------------------------------------------

class OptimizationMeta(BaseModel):
    space_impact: str | None = None
    control_latency_ms: float | None = None
    settling_time_ms: float | None = None
    min_on_sec: float | None = None
    min_off_sec: float | None = None
    step_size: float | None = None
    actuation_cost: float | None = None
    wear_cost: float | None = None
    criticality: int | None = None
