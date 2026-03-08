
# Universal Physical Space Adapter Interface Spec Pack
Version: 1.1
Status: Implemented (interface complete, adapters stub-level)
Audience: adapter authors, protocol-driver authors, orchestrator developers, QA, security reviewers

## 1. Purpose

This specification defines a single **adapter interface** for connecting an AI agent/orchestrator to physical-space devices, bridges, buses, and industrial protocols across residential, commercial, and industrial environments.

The spec is designed to let a user:

1. create an account with minimal friction,
2. attach one or more physical sites,
3. connect any supported device family or protocol,
4. inventory and normalize devices into a canonical model,
5. read state and telemetry,
6. execute commands safely,
7. run scenes, schedules, and optimization policies,
8. keep working during intermittent network conditions.

This document is **platform-agnostic**. It does not prescribe your web app, mobile app, database, message bus, or deployment topology. It specifies the contract adapters must satisfy.

---

## 2. Design goals

### 2.1 Primary goals

- **Local-first**. Use local control paths whenever possible.
- **Protocol-agnostic northbound interface**. Every adapter must present the same core API to the orchestrator.
- **Capability-based normalization**. The agent should reason about `switch`, `dimmer`, `analog_input`, `thermostat`, `meter_power`, `camera_stream`, `relay_output`, `setpoint`, etc., not raw vendor payloads.
- **Low-friction onboarding**. Connecting a device or bridge should require the fewest possible steps, with discovery before manual entry.
- **Deterministic control**. Commands must have correlation IDs, idempotency keys, explicit acknowledgement states, and timeout semantics.
- **Safety-aware actuation**. The interface must support approvals, interlocks, priorities, deadbands, min-on/off times, and select-before-operate where required.
- **Offline tolerance**. Adapters must degrade gracefully when cloud services, radios, or LAN connectivity fail.
- **Multi-tenant by default**. The model must support many users, organizations, sites, and spaces.
- **Extensible**. New protocols, device classes, and capabilities must be addable without breaking old adapters.

### 2.2 Secondary goals

- Make adapter authoring easy enough that a third party can implement a new adapter with a small SDK surface.
- Support “direct device”, “bridge”, “mesh/radio controller”, and “industrial server/bus” patterns using the same contract.
- Support both event-driven and poll-driven protocols.
- Allow the agent to optimize comfort, energy, security, and operations using a consistent graph of spaces, assets, and points.

### 2.3 Non-goals

- This spec does not define UI layout.
- This spec does not define billing.
- This spec does not require one specific broker, database, or RPC framework.
- This spec does not define ML model architecture.
- This spec does not replace vendor-specific safety systems, PLC logic, or regulated operational procedures.

---

## 3. Supported integration classes

The adapter interface MUST support the following integration classes:

1. **Direct local device adapters**
   - Example: KinCony boards, Shelly, ESPHome nodes.

2. **Bridge adapters**
   - Example: Hue Bridge, Lutron Smart Bridge, ONVIF NVRs.

3. **Radio-network controller adapters**
   - Example: Zigbee coordinators, Matter controllers, Thread border-router-backed controllers, Z-Wave controllers.

4. **Message-bus adapters**
   - Example: MQTT.

5. **Industrial protocol adapters**
   - Example: Modbus, KNX, BACnet, OPC UA, DNP3.

6. **Hybrid adapters**
   - A bridge or RTU exposing multiple local protocols simultaneously.

Each adapter MUST declare its class in its manifest.

---

## 4. Normative language

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to be interpreted as normative requirements.

---

## 5. Core principles

### 5.1 Canonical-first, raw-always-available

Every adapter MUST normalize into the canonical model.  
Every adapter MUST also preserve raw protocol payloads for debugging, replay, and future feature extraction.

### 5.2 Strong identity

Every object in the system MUST have a stable canonical ID and one or more source-native identifiers.

### 5.3 Explicit quality

Every reading and state MUST carry quality metadata.

### 5.4 Separation of concerns

- Adapter: protocol/device knowledge.
- Orchestrator: reasoning, planning, policy, optimization.
- App/platform: user experience, account management, installation flow, storage, billing, reporting.

### 5.5 Safe write paths

No adapter may expose a write operation without:
- validating target existence,
- validating input type and range,
- applying required safety policy,
- returning an acknowledgement object.

---

## 6. High-level architecture

```text
User Account / Org / Site
        |
        v
 Space Graph  <---->  Asset Graph  <---->  Canonical Points / Capabilities
        |                               ^
        v                               |
   Orchestrator / Agent  <------------ Adapter SDK contract
        |                               |
        +--> command planner            +--> protocol drivers
        +--> scene engine               +--> discovery
        +--> optimizer                  +--> auth / pairing
        +--> observability              +--> subscriptions / polling
```

### 6.1 Logical layers

1. **Identity & tenancy layer**
2. **Connection & commissioning layer**
3. **Adapter layer**
4. **Canonical normalization layer**
5. **Orchestration layer**
6. **Optimization layer**
7. **Observability & diagnostics layer**

---

## 7. Canonical domain model

### 7.1 Tenancy model

#### Account
Represents a human identity.

Fields:
- `account_id`
- `display_name`
- `primary_email`
- `auth_methods[]`
- `created_at`
- `status`

#### Organization
Represents a household, business, or operator grouping.

Fields:
- `org_id`
- `name`
- `type` (`residential`, `commercial`, `industrial`, `mixed`)
- `owner_account_id`
- `members[]`
- `default_policies[]`

#### Site
Represents a physical property or campus.

Fields:
- `site_id`
- `org_id`
- `name`
- `address`
- `timezone`
- `geo`
- `utility_tariffs[]` (optional)
- `demand_limits[]` (optional)
- `safety_profile`

#### Space
Represents a building, floor, room, zone, plant area, line, cabinet, or outdoor area.

Fields:
- `space_id`
- `site_id`
- `parent_space_id`
- `space_type`
- `name`
- `tags[]`
- `semantic_attributes`
- `occupancy_model` (optional)

### 7.2 Asset model

#### Asset
A logical piece of equipment or a physical device aggregate.

Examples:
- “Living Room Lighting”
- “RTU Cabinet A”
- “AHU-3”
- “North Irrigation Valve Bank”
- “Pump Station PLC”

Fields:
- `asset_id`
- `space_id`
- `asset_type`
- `name`
- `manufacturer`
- `model`
- `serial_number`
- `semantic_tags[]`
- `control_class`
- `safety_class`
- `dependencies[]`
- `children[]`

#### Device
A directly addressable device or bridge.

Fields:
- `device_id`
- `asset_id`
- `adapter_instance_id`
- `native_device_ref`
- `device_family`
- `firmware`
- `hardware`
- `connectivity`
- `health`
- `network_refs[]`

#### Endpoint
A sub-address or component on a device.

Examples:
- relay 1
- switch input 3
- analog input 2
- Zigbee endpoint 1
- Modbus holding register group
- OPC UA node
- ONVIF camera profile
- BACnet object instance
- KNX group address mapping
- DNP3 point

Fields:
- `endpoint_id`
- `device_id`
- `native_endpoint_ref`
- `endpoint_type`
- `direction` (`read`, `write`, `read_write`, `event_only`)
- `units`
- `capabilities[]`
- `traits`
- `polling_profile` or `subscription_profile`

### 7.3 Point model

A **Point** is the lowest canonical unit the orchestrator reads or writes.

Examples:
- `switch.state`
- `switch.command`
- `power.watts`
- `energy.kwh_total`
- `temperature.celsius`
- `cover.position_pct`
- `analog_output.value`
- `camera.motion`
- `occupancy.present`
- `relay_output.state`

Fields:
- `point_id`
- `endpoint_id`
- `point_class`
- `value_type`
- `unit`
- `readable`
- `writable`
- `event_driven`
- `history_profile`
- `quality_profile`
- `metadata`

### 7.4 Capability model

A **Capability** is a set of behaviors exposed northbound.

Each endpoint MAY expose multiple capabilities.

Canonical capability families:

- `binary_switch`
- `binary_sensor`
- `relay_output`
- `momentary_output`
- `dimmer`
- `light_color`
- `cover`
- `shade`
- `blind_tilt`
- `fan`
- `thermostat`
- `climate_setpoint`
- `humidity_sensor`
- `temperature_sensor`
- `pressure_sensor`
- `flow_sensor`
- `meter_power`
- `meter_energy`
- `meter_water`
- `meter_gas`
- `occupancy`
- `motion`
- `contact`
- `lock`
- `camera_stream`
- `camera_snapshot`
- `camera_motion`
- `ptz`
- `analog_input`
- `analog_output`
- `digital_input`
- `digital_output`
- `serial_bridge`
- `ir_transmit`
- `rf_transmit`
- `scene_member`
- `scene_controller`
- `schedule_target`
- `alarm`
- `valve`
- `pump`
- `hvac_mode`
- `setpoint`
- `battery`
- `network_health`
- `radio_health`

Adapters MUST select one or more canonical capabilities for every exposed endpoint.

### 7.5 Relationship graph

The system MUST support graph relationships:

- `located_in`
- `part_of`
- `controls`
- `measures`
- `feeds`
- `adjacent_to`
- `powered_by`
- `networked_via`
- `paired_with`
- `derived_from`
- `safety_interlocked_with`

This graph is critical for optimization and planning.

---

## 8. Account and onboarding requirements

You said you are handling the application/platform, but the adapter layer still needs a contract that makes onboarding easy.

### 8.1 Friction budget

Connecting a new integration SHOULD take one of these paths:

1. **Zero-input discovery path**
   - System discovers candidate device/bridge.
   - User selects one.
   - User enters the minimum secret or performs one physical action.
   - Inventory completes automatically.

2. **Minimal manual path**
   - User enters IP/host/port or scans QR/manual code.
   - User enters secret or imports key.
   - Inventory completes automatically.

3. **Advanced expert path**
   - User imports register map, ETS keyring, certificates, or custom mapping file.

### 8.2 Account-level requirements

The platform SHOULD support:
- email magic link,
- passkeys,
- SSO for commercial/enterprise,
- household invite / site invite,
- delegated installer role,
- device-operator role,
- read-only observer role.

### 8.3 Connection wizard contract

Each adapter MUST expose a `ConnectionTemplate` describing what it needs.

`ConnectionTemplate` fields:
- `adapter_id`
- `display_name`
- `category`
- `discovery_methods[]`
- `required_fields[]`
- `optional_fields[]`
- `secret_fields[]`
- `files_to_upload[]`
- `physical_actions[]`
- `estimated_time_to_connect_sec`
- `supports_auto_inventory`
- `supports_auto_space_mapping`
- `supports_local_only_mode`
- `supports_cloud_fallback`
- `risk_level`

Example physical actions:
- “Press link button on Hue Bridge”
- “Enable permit join for Zigbee controller”
- “Put Z-Wave controller in inclusion mode”
- “Scan Matter QR code”
- “Import KNX ETS keyring”
- “Provide Modbus register map”
- “Upload OPC UA certificate”

### 8.4 Discovery before form entry

Adapters SHOULD prefer discovery before presenting manual fields.

Discovery methods MAY include:
- mDNS
- SSDP/UPnP
- ARP scan
- ICMP/TCP probe
- WebSocket probe
- HTTP fingerprinting
- BLE scan
- ONVIF WS-Discovery
- BACnet Who-Is / I-Am
- KNXnet/IP discovery
- MQTT broker device registry
- serial-port scan
- USB VID/PID detection
- QR/manual pairing code scan
- vendor bridge discovery protocol

### 8.5 Commissioning state machine

```text
new
 -> discovered
 -> candidate_selected
 -> credentials_supplied
 -> authenticated
 -> commissioned
 -> inventoried
 -> normalized
 -> healthy
```

Error states:
- `unreachable`
- `auth_failed`
- `pairing_timeout`
- `inventory_partial`
- `mapping_required`
- `unsupported_firmware`
- `security_blocked`

The platform MUST surface these states to the user.

---

## 9. Adapter taxonomy

Each adapter MUST be one of:

### 9.1 DirectDeviceAdapter
Connects to individual devices over HTTP, WebSocket, MQTT, serial, TCP, etc.

### 9.2 BridgeAdapter
Connects to a hub/bridge/NVR which then exposes downstream devices.

### 9.3 NetworkControllerAdapter
Connects to a mesh/radio controller or fabric controller and manages many downstream nodes.

### 9.4 BusAdapter
Connects to a message or field bus where many devices/points exist on one link.

### 9.5 ServerAdapter
Connects to a rich server that exposes browseable objects and subscriptions.

### 9.6 CompositeAdapter
Combines two or more of the above.

---

## 10. Adapter manifest

Every adapter package MUST include an `adapter.yaml` or equivalent JSON manifest.

### 10.1 Manifest fields

```yaml
adapter_api: "1.0"
id: "shelly.gen2"
display_name: "Shelly Gen2+/Pro"
vendor: "Shelly"
version: "1.0.0"
class: "direct_device"
runtime: "python"
supports:
  discovery: [mdns, http_probe, websocket_probe, manual_ip]
  auth: [digest, none, mqtt]
  inventory: true
  read: true
  write: true
  subscribe: true
  batch_commands: true
  scenes: limited
  schedules: false
  optimization_hints: true
transports:
  - http
  - websocket
  - mqtt
device_families:
  - relay
  - switch
  - dimmer
  - cover
  - power_meter
  - input
capability_families:
  - binary_switch
  - relay_output
  - digital_input
  - dimmer
  - cover
  - meter_power
  - meter_energy
connection_templates:
  - id: "lan_digest"
    display_name: "Local LAN (Digest Auth)"
    required_fields: [host]
    secret_fields: [username, password]
    discovery_methods: [mdns, http_probe]
```

### 10.2 Compatibility declaration

The manifest MUST declare:
- supported firmware ranges,
- known limitations,
- required user-provided artifacts,
- default polling/subscription behavior,
- safety notes,
- transport fallbacks.

---

## 11. Northbound adapter SDK

This section defines the minimal API every adapter MUST implement.

### 11.1 Lifecycle interfaces

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

AdapterClass = Literal[
    "direct_device", "bridge", "network_controller", "bus", "server", "composite"
]

@dataclass
class SecretRef:
    name: str
    handle: str  # opaque handle into your secret store

@dataclass
class ConnectionProfile:
    profile_id: str
    fields: dict[str, Any]
    secrets: list[SecretRef] = field(default_factory=list)

@dataclass
class DiscoveryRequest:
    site_id: str
    methods: list[str]
    scope: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = 15

@dataclass
class DiscoveredTarget:
    discovery_id: str
    adapter_id: str
    native_ref: str
    title: str
    address: str | None
    fingerprint: dict[str, Any]
    confidence: float
    suggested_profile_id: str | None = None

@dataclass
class CommissionResult:
    connection_id: str
    status: Literal["ok", "partial", "failed"]
    diagnostics: dict[str, Any] = field(default_factory=dict)

@dataclass
class InventorySnapshot:
    connection_id: str
    devices: list[dict[str, Any]]
    endpoints: list[dict[str, Any]]
    points: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass
class HealthStatus:
    status: Literal["healthy", "degraded", "offline", "error"]
    details: dict[str, Any] = field(default_factory=dict)

class Adapter(ABC):
    adapter_id: str
    adapter_class: AdapterClass

    @abstractmethod
    async def discover(self, request: DiscoveryRequest) -> list[DiscoveredTarget]:
        ...

    @abstractmethod
    async def commission(
        self,
        target: DiscoveredTarget | None,
        profile: ConnectionProfile,
    ) -> CommissionResult:
        ...

    @abstractmethod
    async def inventory(self, connection_id: str) -> InventorySnapshot:
        ...

    @abstractmethod
    async def subscribe(
        self,
        connection_id: str,
        point_ids: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        ...

    @abstractmethod
    async def read_point(self, connection_id: str, point_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def execute(self, connection_id: str, command: dict[str, Any]) -> dict[str, Any]:
        ...

    @abstractmethod
    async def health(self, connection_id: str) -> HealthStatus:
        ...

    @abstractmethod
    async def teardown(self, connection_id: str) -> None:
        ...
```

### 11.2 Optional interfaces

Adapters MAY also implement:
- `browse()`
- `invoke_method()`
- `export_mapping()`
- `import_mapping()`
- `firmware_update_capabilities()`
- `network_management()`
- `diagnostics()`
- `replay(raw_capture)`
- `simulate()`

### 11.3 Out-of-process adapters

If adapters run out of process, the same semantics MUST be preserved over your chosen transport (gRPC, message bus, JSON-RPC, etc.).

---

## 12. Canonical event model

### 12.1 Event envelope

All telemetry, state changes, alarms, and acknowledgements MUST use a canonical event envelope.

```json
{
  "event_id": "evt_01J...",
  "type": "point.reported",
  "occurred_at": "2026-03-06T10:12:13.123Z",
  "received_at": "2026-03-06T10:12:13.456Z",
  "site_id": "site_123",
  "space_id": "space_456",
  "asset_id": "asset_789",
  "device_id": "dev_abc",
  "endpoint_id": "end_def",
  "point_id": "pt_xyz",
  "source": {
    "adapter_id": "kincony.kcs",
    "connection_id": "conn_1",
    "native_device_ref": "192.168.1.40",
    "native_point_ref": "relay:1"
  },
  "value": {
    "kind": "bool",
    "reported": true,
    "unit": null
  },
  "quality": {
    "status": "good",
    "freshness_ms": 333,
    "source_type": "device_push",
    "confidence": 1.0
  },
  "raw": {
    "protocol": "http",
    "payload": {"POWER1": "ON"}
  }
}
```

### 12.2 Event types

Minimum required types:
- `point.reported`
- `point.derived`
- `point.quality_changed`
- `command.accepted`
- `command.executing`
- `command.acknowledged`
- `command.succeeded`
- `command.failed`
- `alarm.raised`
- `alarm.cleared`
- `device.discovered`
- `device.inventory_changed`
- `device.health_changed`
- `connection.state_changed`
- `network.node_joined`
- `network.node_left`

### 12.3 Quality model

Every reading MUST carry:
- `status`: `good | stale | uncertain | bad | simulated`
- `freshness_ms`
- `source_type`: `device_push | polled | derived | user_input | imported`
- `confidence` (0.0 to 1.0)
- optional flags:
  - `out_of_range`
  - `substituted`
  - `manual_override`
  - `requires_calibration`
  - `comm_lost`
  - `invalid`

This is especially important for industrial protocols.

---

## 13. Canonical command model

### 13.1 Command envelope

```json
{
  "command_id": "cmd_01J...",
  "idempotency_key": "spaceA-light-group-20260306T1012",
  "site_id": "site_123",
  "target": {
    "device_id": "dev_abc",
    "endpoint_id": "end_1",
    "point_id": "pt_switch_state"
  },
  "capability": "binary_switch",
  "verb": "set",
  "params": {
    "value": true
  },
  "issued_at": "2026-03-06T10:12:13.000Z",
  "expires_at": "2026-03-06T10:12:18.000Z",
  "priority": 50,
  "safety": {
    "requires_confirmation": false,
    "requires_interlock_check": true,
    "lockout_tagout_ref": null
  },
  "expectation": {
    "ack_timeout_ms": 1500,
    "final_timeout_ms": 5000,
    "verify_after_write": true
  },
  "context": {
    "initiator": "agent",
    "goal": "reduce_peak_demand",
    "trace_id": "trc_..."
  }
}
```

### 13.2 Required acknowledgement states

Adapters MUST emit:
- `accepted`
- `executing` (if applicable)
- `acknowledged` (protocol/device ack)
- `succeeded` or `failed`

### 13.3 Command classes

- `set`
- `pulse`
- `toggle`
- `open`
- `close`
- `stop`
- `dim`
- `set_color`
- `set_mode`
- `set_setpoint`
- `invoke_scene`
- `lock`
- `unlock`
- `arm`
- `disarm`
- `write_register`
- `invoke_method`
- `ptz_move`
- `ptz_preset`
- `refresh`
- `suppress_alarm`
- `enable`
- `disable`

### 13.4 Batch commands

Adapters SHOULD support atomic or near-atomic batch commands when the protocol supports them.

Batch semantics:
- `all_or_nothing`
- `best_effort`
- `ordered_best_effort`

### 13.5 Verification

If the protocol does not provide strong write acknowledgement, the adapter MUST support `verify_after_write` by reading back the effective state whenever feasible.

---

## 14. Scenes, schedules, and policies

### 14.1 Scene model

A scene is a named vector of desired point states.

Fields:
- `scene_id`
- `name`
- `scope` (space/device group)
- `members[]`
- `transition_ms`
- `rollback_scene_id` (optional)

### 14.2 Schedule model

The adapter contract does not require native schedules, but it MUST support either:
- native schedules exposed as canonical objects, or
- orchestrator-driven schedules using commands.

### 14.3 Policy hooks

Every endpoint SHOULD expose metadata useful for policies:
- `manual_override_window_sec`
- `min_cycle_time_sec`
- `deadband`
- `rate_limit`
- `safe_range`
- `writable_when_offline`
- `supports_revert`

---

## 15. Optimization metadata

To optimize physical spaces, the canonical model MUST support non-protocol metadata.

### 15.1 Required optimization tags

Points and assets SHOULD support:
- `space_impact`: `comfort | safety | security | energy | production | irrigation | access`
- `control_latency_ms`
- `settling_time_ms`
- `min_on_sec`
- `min_off_sec`
- `step_size`
- `actuation_cost`
- `wear_cost`
- `energy_cost_model`
- `occupancy_dependency`
- `weather_dependency`
- `criticality`
- `recovery_behavior`

### 15.2 Constraint model

Constraints MUST be expressible as:
- numeric ranges,
- temporal windows,
- priority rules,
- dependencies,
- interlocks.

Examples:
- “Do not short-cycle compressor under 300 sec.”
- “Pump B cannot run unless valve V3 is open.”
- “Do not raise office temperature above 24.5 C during occupancy.”
- “Industrial relay bank requires operator approval outside work order.”

### 15.3 Goal model

Goals MAY include:
- minimize energy cost,
- minimize demand peak,
- maintain comfort band,
- maximize daylight harvesting,
- maintain irrigation moisture band,
- reduce standby loads,
- pre-cool/pre-heat,
- maintain tank level,
- shift EV/thermal loads,
- detect anomalies.

The adapter is not responsible for optimization, but it MUST expose the metadata that makes optimization credible.

---

## 16. Security model

### 16.1 Secret handling

Adapters MUST NOT persist plaintext secrets inside device records.  
Secrets MUST be referenced through opaque secret handles.

### 16.2 Credential classes

Supported credential types:
- username/password
- digest auth credentials
- bearer token
- API key
- TLS client certificate
- private key
- keyring/import bundle
- pairing code / QR token
- mesh network keys
- Z-Wave S2 keys
- Thread operational dataset
- OPC UA trust material
- vendor cloud OAuth token

### 16.3 Local-first rule

When both cloud and local control exist, adapters SHOULD prefer local control for:
- lower latency,
- privacy,
- resilience,
- reduced vendor dependency.

Cloud MAY be used for bootstrap or fallback, but the adapter SHOULD make local-only mode possible if the device family supports it.

### 16.4 Least privilege

Adapters SHOULD request only the minimum permissions necessary.

### 16.5 Safety write levels

Every endpoint MUST be assigned a safety class:

- `S0 read_only`
- `S1 non_destructive` — lights, outlets, shades
- `S2 comfort_equipment` — thermostats, valves, fans
- `S3 operational_equipment` — pumps, industrial relays, RTUs
- `S4 restricted` — commands requiring special approval
- `S5 forbidden` — command path not exposed to agent

The orchestrator SHOULD enforce approval workflows for S3+.

### 16.6 Audit

All writes MUST be auditable with:
- who/what initiated,
- when,
- target,
- before state,
- after state,
- rationale or trace context.

---

## 17. Reliability and resilience

### 17.1 Connection health

Adapters MUST publish health with at least:
- `healthy`
- `degraded`
- `offline`
- `error`

Diagnostics SHOULD include:
- round-trip latency,
- auth state,
- last successful read,
- last successful write,
- reconnect attempts,
- queue depth,
- downstream device count.

### 17.2 Polling vs subscription

Each point MUST declare one of:
- `push_only`
- `poll_only`
- `push_preferred_with_poll_verify`
- `poll_preferred_with_event_assist`

### 17.3 Retry policy

Retries MUST be explicit and bounded.

Suggested defaults:
- exponential backoff with jitter,
- max 5 fast retries,
- then circuit-breaker/open-degraded state,
- background re-probe thereafter.

### 17.4 Command queueing

Adapters MAY queue commands only when:
- the target semantics allow delayed execution,
- `expires_at` has not passed,
- the command is not unsafe to replay.

Unsafe commands MUST fail fast instead of queueing.

### 17.5 Time sync

For protocols requiring time coherence, the adapter SHOULD expose time health and support device time sync where appropriate.

### 17.6 Raw capture

Adapters SHOULD support packet/payload capture mode for diagnostics, with secret redaction.

---

## 18. Normalization rules

### 18.1 Required point metadata

Every point MUST include:
- `canonical_name`
- `point_class`
- `value_type`
- `unit`
- `readable`
- `writable`
- `native_ref`
- `source_protocol`
- `semantic_tags[]`

### 18.2 Unit normalization

The canonical layer SHOULD normalize to SI or widely accepted practical units:
- temperature: `C`
- humidity: `%`
- pressure: `Pa` or `kPa`
- power: `W`
- energy: `Wh` or `kWh`
- current: `A`
- voltage: `V`
- flow: `L/min`, `m3/h`, etc. with explicit unit
- position: `%`

The raw/native unit MUST also be preserved.

### 18.3 State normalization

Examples:
- vendor `"ON"` -> canonical `true`
- vendor `"off"` -> canonical `false`
- vendor `"open"` -> canonical `position_pct=100`
- vendor `"closed"` -> canonical `position_pct=0`

### 18.4 Multi-point composition

Adapters SHOULD derive higher-level capabilities from lower-level points where safe.

Examples:
- `power` + `energy_total` + `switch` -> `smart_outlet`
- `temp` + `humidity` + `occupancy` + `relay` -> `zone_climate`
- `relay bank` + `feedback inputs` -> `safe_actuator_group`

---

## 19. Device inventory and identity

### 19.1 Stable IDs

Canonical IDs MUST survive:
- reconnect,
- IP address changes,
- bridge re-discovery,
- restarts,
- inventory refreshes.

### 19.2 Native identity precedence

Adapters SHOULD derive stable source identifiers from the strongest available source:
1. manufacturer serial or unique ID
2. protocol-level unique node ID
3. MAC address
4. bridge-specific resource ID
5. endpoint path or object path
6. fallback fingerprint

### 19.3 Inventory diffs

Inventory refresh MUST produce:
- added devices/endpoints/points
- removed devices/endpoints/points
- changed capabilities/metadata
- firmware changes
- renamed items

---

## 20. Mapping, overrides, and user customization

### 20.1 User overrides

The platform SHOULD allow users/installers to override:
- device names,
- space assignment,
- semantic tags,
- capability classification,
- scaling factors,
- writable/read-only flags,
- safety class,
- polling rate,
- scene inclusion,
- optimization participation.

### 20.2 Import/export mappings

Adapters SHOULD support mapping export/import where the native protocol is complex.

Examples:
- Modbus register maps
- KNX group address maps
- BACnet object maps
- OPC UA browse selections
- DNP3 point maps

### 20.3 Derived virtual points

The platform MAY define virtual points derived from multiple native points.  
Adapters SHOULD permit this without pretending the point is device-native.

---

## 21. Protocol family requirements

This section defines what the adapter contract MUST capture for each target ecosystem.

---

## 22. KinCony adapter profile

This family must support multiple board/firmware personalities.

### 22.1 Supported personalities

At minimum, the KinCony family adapter SHOULD support:

- Tasmota-style HTTP/MQTT devices
- KCS v3 devices
- ESPHome-flashed devices
- Arduino/custom firmware devices exposing stable local APIs
- Modbus-enabled KinCony boards where applicable

### 22.2 Board profile abstraction

Use board definition files.

`boards/kc868_a4.yaml`
`boards/kc868_a16v3.yaml`
`boards/kc868_ai.yaml`
`boards/kc868_h32b_pro.yaml`
`boards/kc868_a8m.yaml`

Each board definition SHOULD describe:
- physical channels,
- relay count,
- digital input count,
- analog input/output count,
- IR/RF support,
- CAN support,
- serial ports,
- supported firmware personalities,
- default endpoint naming.

### 22.3 KinCony endpoint patterns

Canonical endpoint examples:
- `relay/1..N`
- `digital_input/1..N`
- `analog_input/1..N`
- `analog_output/1..N`
- `ir_tx/1`
- `ir_rx/1`
- `rf_tx/1`
- `rf_rx/1`
- `rs485/1`
- `can/1`

### 22.4 KinCony read/write requirements

The adapter MUST support:
- board discovery where feasible,
- manual IP/host setup,
- inventory from known board profile,
- relay writes,
- input reads,
- analog reads,
- optional IR/RF action mapping,
- serial or Modbus child-device bridging where the board acts as a gateway.

### 22.5 KinCony firmware profile interface

The KinCony adapter MUST separate board-specific logic from firmware-specific logic:

- `firmware_profile = tasmota`
- `firmware_profile = kcs_v3`
- `firmware_profile = esphome`
- `firmware_profile = custom_http`
- `firmware_profile = modbus_gateway`

This prevents board support from being tangled with one firmware flavor.

### 22.6 KinCony safety notes

Relay outputs controlling mains, pumps, gates, valves, or industrial loads MUST default to at least `S2` or `S3` safety class until explicitly downgraded.

---

## 23. Shelly Gen2+/Pro adapter profile

### 23.1 Core behavior

The Shelly adapter SHOULD use:
- HTTP RPC for simple request/response,
- WebSocket RPC for event-driven control and notifications,
- MQTT as optional fallback or ecosystem bridge.

### 23.2 Component inventory

A Shelly device may expose:
- switch channels,
- input channels,
- covers,
- dimmers,
- meters,
- environmental sensors,
- scripts/webhooks (metadata only, unless you choose to model them).

Inventory MUST preserve component IDs (`id:0`, `id:1`, etc.).

### 23.3 Auth profiles

Connection templates:
- `open_lan`
- `digest_http_ws`
- `mqtt_only`

### 23.4 Required northbound capabilities

Minimum:
- read state,
- write switch/cover/dimmer,
- subscribe to state changes,
- meter readings if present,
- input edge events if present,
- availability/health.

### 23.5 Provisioning notes

Because some Shelly devices can be provisioned via local AP/BLE and newer secure provisioning flows, the adapter SHOULD model a commissioning phase distinct from normal operations.

### 23.6 Eventing

Prefer persistent WebSocket subscriptions for low latency.  
If WebSocket is unavailable, poll or consume MQTT.

---

## 24. ESPHome adapter profile

### 24.1 Philosophy

Treat ESPHome as a first-class endpoint ecosystem, not merely a fallback.

### 24.2 Preferred integration modes

Preferred order:
1. native ESPHome API
2. MQTT-exposed entities
3. custom bridge service if the user has one

### 24.3 Component model

ESPHome nodes are composition-heavy. Inventory MUST support many entity types per node without requiring a new adapter per board.

### 24.4 External component support

The adapter ecosystem SHOULD assume external components are the extensibility path.  
The spec SHOULD allow community device packs that extend canonical mappings without forking the core adapter SDK.

### 24.5 Required metadata

Inventory SHOULD capture:
- node name
- friendly name
- entity IDs
- API version or firmware version
- board/chip info if available
- Wi-Fi quality and uptime if available
- battery if available
- update/OTA availability if surfaced

### 24.6 Custom entity mapping

Because ESPHome is often bespoke, the platform SHOULD allow user-supplied semantic annotations at inventory time.

---

## 25. Zigbee adapter profile

### 25.1 Architecture choices

The platform MAY support Zigbee via:
- direct controller integration,
- a controller daemon/service,
- a bridge product,
- a Zigbee-to-MQTT layer.

This spec remains controller-implementation-agnostic.

### 25.2 Required concepts

The Zigbee adapter MUST model:
- network/coordinator
- node
- endpoint
- cluster
- attribute/command
- interview state
- battery/LQI/RSSI where available

### 25.3 Commissioning

The adapter MUST support:
- permit join lifecycle
- inclusion timeout
- interview progress
- unsupported/interview-failed states
- reconfigure/reinterview hooks

### 25.4 Canonical mapping

The adapter MUST map common Zigbee profiles/clusters to:
- lights
- switches
- sensors
- covers
- locks
- occupancy/motion
- power/battery
- metering

### 25.5 Radio health

Expose:
- coordinator health,
- channel/PAN metadata,
- node count,
- route quality indicators if available.

---

## 26. Matter / Thread adapter profile

### 26.1 Matter controller abstraction

Treat Matter as a **controller/fabric** integration, not as one device class.

The adapter MUST model:
- fabric/controller
- commissionable device
- commissioned node
- endpoint
- cluster/attribute/command

### 26.2 Commissioning

The adapter MUST support:
- QR/manual setup code entry,
- commissioning window management where supported,
- device attestation results if available,
- fabric membership metadata,
- node rejoin handling.

### 26.3 Thread

For Thread-backed Matter devices, the platform SHOULD track:
- border router reference,
- Thread network reference,
- operational dataset handle,
- reachability and route quality where available.

### 26.4 Canonical mapping

Map Matter device types and clusters into canonical capabilities the same way as all other families.

### 26.5 Security notes

Store fabric/controller credentials separately from normal secrets.  
These are high-impact credentials.

---

## 27. Z-Wave adapter profile

### 27.1 Controller abstraction

Model:
- controller
- node
- endpoint
- command class
- interview/ready state
- security class

### 27.2 Commissioning

The adapter MUST support:
- inclusion,
- exclusion,
- secure inclusion state,
- interview progress,
- heal/network maintenance hooks.

### 27.3 Required metadata

- node ID
- product/manufacturer
- security state
- sleeping/listening state
- route quality if available
- battery level if available

### 27.4 Canonical mapping

Map common device types and command classes to:
- switches
- dimmers
- locks
- sensors
- thermostats
- meters
- covers

### 27.5 Preferred architecture

If your platform is not itself a mature Z-Wave stack, use a proven controller service and wrap it with this adapter contract rather than implementing RF control from scratch.

---

## 28. MQTT adapter profile

### 28.1 Role

MQTT is both:
- a transport used by many device families, and
- a standalone integration surface for generic devices.

### 28.2 Generic MQTT adapter

The generic MQTT adapter MUST support:
- broker connection,
- birth/last will handling,
- retained discovery payloads,
- device discovery registry,
- wildcard subscriptions,
- command topics,
- availability topics.

### 28.3 Canonical device registry

The platform SHOULD maintain a canonical registry for MQTT-discovered devices so they look like any other adapter-produced devices.

### 28.4 Required mapping metadata

Each MQTT-mapped point SHOULD declare:
- state topic
- command topic
- availability topic
- payload transforms
- retain/QoS semantics
- unique ID
- device identity metadata
- unit/device class hints
- value template or transformation function

### 28.5 Write safety

MQTT command topics can be loosely specified. The adapter MUST validate payload generation strictly and SHOULD verify post-write state whenever possible.

---

## 29. Modbus adapter profile

### 29.1 Scope

Support:
- Modbus TCP
- Modbus RTU
- serial-over-IP variants if needed by your platform

### 29.2 Required object model

Model:
- connection/hub
- slave or unit ID
- point group
- coil
- discrete input
- holding register
- input register

### 29.3 Register map schema

Every writable or readable point MUST be defined by a register map entry:

```yaml
point_id: "pump1.run_cmd"
unit_id: 2
table: holding_register
address: 40017
data_type: uint16
endianness: big
word_order: big
scale: 1.0
offset: 0
unit: null
readable: true
writable: true
command_semantics: write_single_register
verification:
  readback: true
  delay_ms: 250
```

### 29.4 Required per-point metadata

- unit ID
- table
- address
- quantity/width
- signedness
- bitmask or bit index if applicable
- scale and offset
- endianness / word order
- read/write flags
- polling interval
- debounce/stability rule
- engineering unit
- safety class

### 29.5 Polling requirements

The adapter MUST support:
- grouped reads,
- rate limits,
- multiple hubs/connections,
- different serial settings per hub,
- point-level polling classes,
- stale quality marking when polls fail.

### 29.6 Control semantics

Support:
- write coil
- write register
- mask write / multi-register where needed
- select-before-operate emulation when required by downstream device rules

### 29.7 Import/export

The platform SHOULD let users import register maps from YAML/CSV/JSON.

---

## 30. KNX adapter profile

### 30.1 Scope

Support KNXnet/IP and secure configurations where available in your chosen implementation.

### 30.2 Required object model

Model:
- bus connection
- individual address (optional metadata)
- group address
- datapoint type
- read/write flags
- feedback/source address metadata if available

### 30.3 Mapping schema

Each mapped item MUST include:
- group address
- DPT
- direction
- canonical capability
- read/write state addresses if split
- units/scaling
- security metadata if secure group objects are used

### 30.4 Recommended endpoint patterns

- `light`
- `switch`
- `cover`
- `climate`
- `sensor`
- `scene_trigger`
- `setpoint`
- `presence`
- `meter`

### 30.5 Secure mode

The platform MUST allow keyring/import-based secure setup as an advanced commissioning path.

---

## 31. BACnet adapter profile

### 31.1 Scope

Support at least browse, read property, write property, and change-of-value subscription where available.

### 31.2 Required object model

Model:
- BACnet network
- device instance
- object type
- object instance
- property
- priority array (where relevant)
- COV support
- segmentation/transport hints if needed

### 31.3 Canonical mapping

Typical mappings:
- `analog-input` -> sensor
- `analog-output` -> analog_output/setpoint
- `analog-value` -> virtual setpoint or value
- `binary-input` -> binary_sensor
- `binary-output` -> digital_output
- `binary-value` -> state or virtual boolean
- `multi-state-*` -> mode or selector
- `schedule` -> schedule object
- `calendar` -> calendar object
- `trend-log` -> historian source

### 31.4 Required metadata

- device instance
- object identifier
- present value property mapping
- units
- writable flag
- COV capability
- priority handling requirement
- reliability/status flags if available

### 31.5 Safety and writes

BACnet writes SHOULD support priority semantics where applicable.  
The adapter MUST not clobber priority arrays blindly.

---

## 32. ONVIF adapter profile

### 32.1 Scope

Support:
- discovery,
- stream inventory,
- snapshot retrieval,
- motion/events when available,
- PTZ when available.

### 32.2 Required object model

Model:
- NVR or camera
- media profile
- stream URI descriptor
- event source/topic
- PTZ service/preset
- analytics/motion channels where available

### 32.3 Canonical mapping

Map to:
- `camera_stream`
- `camera_snapshot`
- `camera_motion`
- `ptz`
- `binary_sensor` for motion/contact analytics where exposed

### 32.4 Security

Camera credentials are high-value secrets.  
The adapter SHOULD support per-camera credentials even behind an NVR.

### 32.5 Performance

The adapter SHOULD inventory streams and profiles but MUST avoid eagerly pulling high-rate video through the orchestration plane unless explicitly requested.

---

## 33. Lutron adapter profile

### 33.1 Scope

Treat Lutron as a bridge-managed ecosystem.

### 33.2 Required object model

Model:
- bridge
- area/room
- device
- scene/button where available
- shade/dimmer/switch/sensor

### 33.3 Commissioning

Connection should be bridge-centric.  
The bridge is the primary authenticated endpoint; downstream devices are inventoried from it.

### 33.4 Canonical mapping

Map to:
- dimmers
- switches
- shades/covers
- occupancy sensors
- scene controllers

### 33.5 Naming

Inventory SHOULD preserve bridge-assigned names while allowing user overrides.

---

## 34. Hue adapter profile

### 34.1 Scope

Treat Hue as a bridge-managed lighting and sensor ecosystem.

### 34.2 Required object model

Model:
- bridge
- room
- zone
- light
- sensor
- scene
- grouped_light or group abstraction where applicable

### 34.3 Commissioning

Support bridge discovery and link-button or equivalent approval flow.

### 34.4 Canonical mapping

Map to:
- binary/dimmable/color lights
- scenes
- motion/ambient sensors where present
- grouped lighting endpoints

### 34.5 Scene handling

Native Hue scenes SHOULD be imported as canonical scene objects, while preserving their origin as native bridge scenes.

---

## 35. OPC UA adapter profile

### 35.1 Scope

Support:
- endpoint discovery,
- certificate trust management,
- namespace browse,
- monitored items / subscriptions,
- reads,
- writes,
- method calls when explicitly mapped.

### 35.2 Required object model

Model:
- server endpoint
- namespace
- node ID
- browse path
- variable node
- method node
- data type
- access level
- sampling/subscription profile

### 35.3 Canonical mapping

OPC UA is broad. The platform SHOULD support:
- direct node-to-point mapping,
- browse-path selection,
- companion-spec-aware mapping packs where available.

### 35.4 Required metadata

- endpoint URL
- security policy/mode
- node ID
- browse path
- namespace index/URI
- data type
- engineering unit if available
- writable flag
- status code quality mapping

### 35.5 Security

Trust stores, client certificates, and endpoint security mode choices MUST be first-class.

---

## 36. DNP3 adapter profile

### 36.1 Scope

Support DNP3 master/outstation integration for utility and industrial environments.

### 36.2 Required object model

Model:
- channel
- outstation
- point
- point class
- variation
- unsolicited support
- time sync status
- control operation type

### 36.3 Point classes

The adapter MUST preserve DNP3 point class information and quality flags.

### 36.4 Canonical mapping

Typical mappings:
- binary inputs -> digital_input / status
- counters -> meter or pulse count
- analog inputs -> sensor values
- analog outputs -> setpoints
- CROB / control points -> controlled outputs or commands

### 36.5 Control semantics

The adapter MUST support, when applicable:
- select-before-operate,
- direct operate,
- command result mapping,
- unsolicited event handling,
- time synchronization hooks.

### 36.6 Safety

DNP3 targets often control high-consequence infrastructure.  
Default safety class SHOULD be `S3` or higher for writable points unless explicitly downgraded.

---

## 37. Bridge vs direct-device normalization

To keep the orchestrator simple:

- A bridge MUST appear as a `device` with `bridge=true`.
- Downstream devices behind a bridge MUST still appear as first-class canonical devices.
- The bridge connection health MUST be separable from downstream node health.
- Native bridge groupings (rooms, zones, areas) SHOULD be imported into the space graph as hints, not as immutable truth.

---

## 38. Discovery and fingerprinting requirements by ecosystem

### 38.1 KinCony
- manual IP/HTTP probe
- mDNS or hostname probe if present
- firmware personality detection
- board profile identification via user selection or probe heuristics

### 38.2 Shelly
- mDNS/HTTP/WebSocket probe
- RPC device info fingerprint
- auth challenge detection

### 38.3 ESPHome
- native API discovery or imported host list
- mDNS where available
- MQTT origin detection

### 38.4 Zigbee
- coordinator inventory
- permit-join state
- interview progress events

### 38.5 Matter
- commissionable node discovery
- QR/manual code flow
- commissioned node list

### 38.6 Z-Wave
- controller node inventory
- inclusion/exclusion state
- interview progress events

### 38.7 MQTT
- broker presence
- discovery topic inspection
- retained registry bootstrap

### 38.8 Modbus
- manual IP/serial only in many cases
- optional unit ID probing
- register-map-assisted discovery

### 38.9 KNX
- IP interface discovery
- imported group-address map

### 38.10 BACnet
- Who-Is / I-Am
- device instance browse

### 38.11 ONVIF
- WS-Discovery
- profile/media inventory

### 38.12 Lutron/Hue
- bridge discovery
- bridge approval flow

### 38.13 OPC UA
- endpoint discovery
- browse test
- certificate trust onboarding

### 38.14 DNP3
- usually manual connection plus outstation config
- optional link-status probing

---

## 39. Capability mapping packs

### 39.1 Purpose

Not every device family maps cleanly out of the box. The platform SHOULD support installable mapping packs.

### 39.2 Mapping pack contents

A mapping pack MAY contain:
- device fingerprints
- board profiles
- register maps
- cluster/command-class translation rules
- semantic tags
- safe-range defaults
- scene hints
- optimization hints

### 39.3 Versioning

Mapping packs MUST be versioned independently of the core SDK.

---

## 40. Observability

### 40.1 Structured logs

Every adapter MUST emit structured logs with:
- adapter ID
- connection ID
- native target ref
- operation
- result
- duration
- severity
- trace ID

### 40.2 Metrics

Minimum metrics:
- `adapter_discover_duration_ms`
- `adapter_inventory_duration_ms`
- `adapter_read_duration_ms`
- `adapter_write_duration_ms`
- `adapter_subscribe_restarts_total`
- `adapter_reconnects_total`
- `adapter_events_total`
- `adapter_errors_total`
- `adapter_command_failures_total`
- `adapter_health_status`

### 40.3 Tracing

Command flows SHOULD be traceable end-to-end from planner -> adapter -> protocol action -> ack -> verification.

### 40.4 Diagnostics dump

Adapters SHOULD provide a redacted diagnostics bundle including:
- manifest,
- connection profile summary,
- health snapshot,
- recent errors,
- recent raw payload samples,
- inventory summary,
- firmware/protocol info.

---

## 41. Testing and certification

### 41.1 Required test layers

Every adapter MUST ship with:

1. **Unit tests**
   - payload parsing
   - normalization
   - command serialization
   - error mapping

2. **Contract tests**
   - manifest validation
   - lifecycle interface behavior
   - event envelope compliance
   - command ack sequence compliance

3. **Replay tests**
   - golden raw captures replay into normalized outputs

4. **Integration tests**
   - live device/bridge simulation or hardware-in-the-loop

5. **Failure tests**
   - auth failure
   - disconnect/reconnect
   - partial inventory
   - stale data
   - duplicate events
   - unsupported firmware

### 41.2 Certification levels

Suggested levels:
- `L1 experimental`
- `L2 community`
- `L3 supported`
- `L4 production_hardened`
- `L5 critical_infrastructure_reviewed`

### 41.3 Hardware-in-the-loop

For relay boards, industrial buses, and RTUs, HIL tests SHOULD verify actual device behavior, not just protocol parsing.

---

## 42. Error model

### 42.1 Canonical errors

Adapters MUST map native errors into canonical codes:

- `UNREACHABLE`
- `AUTH_FAILED`
- `PAIRING_REQUIRED`
- `UNSUPPORTED_FIRMWARE`
- `INVALID_TARGET`
- `INVALID_VALUE`
- `WRITE_DENIED`
- `SAFETY_BLOCKED`
- `TIMEOUT`
- `DEVICE_BUSY`
- `VERIFY_FAILED`
- `PARTIAL_INVENTORY`
- `NETWORK_DEGRADED`
- `PROTOCOL_ERROR`
- `RATE_LIMITED`
- `DEPENDENCY_FAILED`

### 42.2 Error object

```json
{
  "code": "VERIFY_FAILED",
  "message": "Write acknowledged but state did not converge within timeout",
  "retryable": true,
  "native": {
    "protocol": "modbus",
    "exception_code": 2
  }
}
```

---

## 43. Versioning and compatibility

### 43.1 API versioning

The adapter SDK MUST have a semantic version.

### 43.2 Manifest versioning

Manifest schema changes MUST be versioned.

### 43.3 Firmware compatibility

Adapters SHOULD publish tested firmware ranges.

### 43.4 Feature negotiation

The orchestrator SHOULD inspect adapter features instead of assuming they exist.

---

## 44. Performance requirements

### 44.1 General targets

These are design targets, not hard laws:

- discovery initial candidate list: < 10 sec on a normal LAN
- single local write ack: < 1.5 sec for most LAN devices
- subscription resume after disconnect: < 15 sec preferred
- inventory small device/bridge: < 30 sec
- bulk inventory large bridge/network: progressive results required

### 44.2 Backpressure

Adapters MUST protect the downstream protocol from command floods.

### 44.3 Sampling and buffering

The adapter SHOULD let high-rate telemetry points be downsampled or edge-aggregated before flooding the orchestration plane.

---

## 45. Safety and interlocks

### 45.1 Interlock model

The platform SHOULD be able to define interlocks at the canonical layer, but adapters MAY also declare native interlocks.

Examples:
- opposing motor directions
- pump/valve dependencies
- relay mutual exclusion
- heater/cooling deadband
- contactor delay sequences

### 45.2 Required command checks

Before write, the adapter or orchestrator MUST be able to check:
- endpoint exists,
- writable,
- value within safe range,
- command not expired,
- interlock status known,
- no manual lockout,
- no maintenance mode block.

### 45.3 Manual override

When a device/point is manually overridden, the adapter SHOULD expose that state so the agent does not fight a human operator.

---

## 46. Deployment model recommendations

### 46.1 Adapter runtime modes

Support at least one of:
- in-process Python package
- sidecar/container
- remote worker/edge agent

### 46.2 Edge execution

For latency-sensitive or site-local resilience, the platform SHOULD support running adapters on a site-local edge node.

### 46.3 Cloud vs edge split

Recommended:
- onboarding, inventory metadata, policy orchestration: can be cloud-backed
- real-time control, subscriptions, protocol drivers: preferably edge/local

---

## 47. Recommended repository layout

```text
physical-space-adapters/
  sdk/
    adapter_api/
      __init__.py
      models.py
      errors.py
      manifest.py
      contract_tests/
  adapters/
    kincony/
      adapter.py
      firmware_profiles/
        tasmota.py
        kcs_v3.py
        esphome_profile.py
      boards/
        kc868_a4.yaml
        kc868_a16v3.yaml
        kc868_ai.yaml
        kc868_h32b_pro.yaml
        kc868_a8m.yaml
      tests/
    shelly/
      adapter.py
      rpc_http.py
      rpc_ws.py
      mqtt.py
      tests/
    esphome/
      adapter.py
      native_api.py
      mqtt.py
      tests/
    zigbee/
      adapter.py
      mapping_packs/
    matter/
      adapter.py
    zwave/
      adapter.py
    mqtt/
      adapter.py
    modbus/
      adapter.py
      map_schema.py
    knx/
      adapter.py
    bacnet/
      adapter.py
    onvif/
      adapter.py
    lutron/
      adapter.py
    hue/
      adapter.py
    opcua/
      adapter.py
    dnp3/
      adapter.py
  mapping_packs/
  fixtures/
  docs/
```

---

## 48. Recommended implementation order

### Phase 1: fastest value / lowest complexity
- KinCony family
- Shelly
- MQTT
- Modbus
- Hue
- ONVIF

### Phase 2: local smart-home ecosystems
- ESPHome
- Zigbee
- Z-Wave
- Matter/Thread
- Lutron

### Phase 3: commercial building systems
- KNX
- BACnet

### Phase 4: industrial/utility systems
- OPC UA
- DNP3

This order maximizes useful device coverage while building the abstractions needed for harder protocols later.

---

## 49. Minimal canonical schemas

### 49.1 Device schema

```json
{
  "device_id": "dev_123",
  "adapter_instance_id": "conn_1",
  "native_device_ref": "shellyplus1pm-ec64c9...",
  "name": "Garage Pump Relay",
  "device_family": "shelly.gen2",
  "manufacturer": "Shelly",
  "model": "Plus 1PM",
  "firmware": {"version": "1.7.4"},
  "connectivity": {
    "transport": "lan",
    "address": "192.168.1.52"
  },
  "bridge_device_id": null,
  "space_id": "space_garage",
  "safety_class": "S2"
}
```

### 49.2 Endpoint schema

```json
{
  "endpoint_id": "end_123_switch0",
  "device_id": "dev_123",
  "native_endpoint_ref": "Switch:0",
  "endpoint_type": "switch_channel",
  "capabilities": ["binary_switch", "relay_output", "meter_power"],
  "traits": {
    "supports_toggle": true,
    "supports_power_meter": true
  }
}
```

### 49.3 Point schema

```json
{
  "point_id": "pt_123_switch_state",
  "endpoint_id": "end_123_switch0",
  "point_class": "switch.state",
  "value_type": "bool",
  "unit": null,
  "readable": true,
  "writable": true,
  "native_ref": "switch:0/output",
  "source_protocol": "shelly_rpc"
}
```

---

## 50. Example adapter-specific connection templates

### 50.1 KinCony KCS/Tasmota

```yaml
adapter_id: kincony.family
profiles:
  - id: tasmota_http
    required_fields: [host]
    secret_fields: []
    discovery_methods: [http_probe, manual_ip]
  - id: tasmota_mqtt
    required_fields: [broker_host, topic]
    secret_fields: [mqtt_username, mqtt_password]
  - id: kcs_v3_lan
    required_fields: [host]
    secret_fields: []
  - id: modbus_gateway
    required_fields: [host, port, unit_id]
    secret_fields: []
```

### 50.2 Shelly

```yaml
adapter_id: shelly.gen2
profiles:
  - id: digest_lan
    required_fields: [host]
    secret_fields: [username, password]
    discovery_methods: [mdns, http_probe]
  - id: open_lan
    required_fields: [host]
    secret_fields: []
  - id: mqtt
    required_fields: [broker_host, topic_prefix]
    secret_fields: [mqtt_username, mqtt_password]
```

### 50.3 Modbus

```yaml
adapter_id: modbus.generic
profiles:
  - id: tcp
    required_fields: [host, port]
    optional_fields: [unit_ids]
    files_to_upload: [register_map]
  - id: rtu
    required_fields: [serial_port, baudrate, parity, stopbits]
    optional_fields: [unit_ids]
    files_to_upload: [register_map]
```

### 50.4 KNX

```yaml
adapter_id: knx.ip
profiles:
  - id: ip_secure
    required_fields: [host]
    files_to_upload: [ets_keyring]
  - id: ip_plain
    required_fields: [host]
    optional_fields: [group_address_map]
```

### 50.5 OPC UA

```yaml
adapter_id: opcua.server
profiles:
  - id: secure_client
    required_fields: [endpoint_url]
    secret_fields: [username, password]
    files_to_upload: [client_cert, client_key, trust_bundle]
  - id: anonymous
    required_fields: [endpoint_url]
```

---

## 51. Example adapter logic patterns

### 51.1 Pattern: optimistic write + verify
Use for simple LAN devices.

1. send command
2. await protocol ack
3. verify state through event or readback
4. mark success/failure

### 51.2 Pattern: poll-only device
Use for simple Modbus devices without spontaneous events.

1. maintain polling schedule
2. surface stale quality on missed polls
3. on write, perform readback after configured delay

### 51.3 Pattern: browse + subscribe server
Use for OPC UA and BACnet.

1. browse selected namespace/objects
2. create normalized mappings
3. subscribe to monitored items/COV where possible
4. fall back to polling for unsupported points

### 51.4 Pattern: bridge inventory fan-out
Use for Hue, Lutron, Zigbee, Z-Wave, Matter.

1. authenticate with bridge/controller
2. inventory downstream nodes
3. create canonical devices/endpoints/points
4. subscribe to downstream state changes
5. separate bridge health from node health

---

## 52. What adapter authors MUST provide

For an adapter to be accepted as production-capable, the author MUST provide:

- manifest
- implementation of required SDK interfaces
- normalized capability mappings
- connection templates
- health reporting
- contract tests
- replay fixtures
- diagnostics dump
- compatibility notes
- safety notes
- example inventory output
- example command flow output

---

## 53. What the orchestrator may safely assume

The orchestrator MAY assume only that:

- every device has stable canonical IDs,
- every point has a type and quality,
- every command has ack/final states,
- health is available,
- inventory can be refreshed,
- raw/native context is available for debugging.

It MUST NOT assume:
- every adapter supports push events,
- every point is writable,
- every write can be instantly verified,
- every ecosystem supports scenes natively,
- every device name is unique,
- every protocol is low-latency.

---

## 54. Anti-patterns to avoid

- Vendor-specific logic leaking into the orchestrator.
- Requiring cloud accounts when a local path exists.
- Treating all writes as equal risk.
- Reusing IP address as stable identity.
- Hiding raw/native IDs.
- Conflating bridge health with downstream node health.
- Polling high-rate protocols unnecessarily.
- Allowing opaque custom payloads to bypass canonical validation.
- Hard-coding only one firmware flavor for a board family.
- Treating register addresses or node IDs as semantic names.

---

## 55. Implementation status

The following spec components have been implemented:

### Completed

1. **SDK and canonical models** (`sdk/adapter_api/models.py`, `base.py`, `manifest.py`, `errors.py`, `safety.py`)
2. **15 protocol adapters** (`adapters/`): KinCony (reference impl), Shelly, MQTT, Modbus, Hue, ONVIF, ESPHome, Zigbee2MQTT, Z-Wave JS, Matter, Lutron, KNX, BACnet, OPC UA, DNP3
3. **Core runtime** (`core/`): EventBus, StateStore (SQLite/WAL), AdapterRegistry (locks + timeouts), Scheduler (auto-recovery), FastAPI REST API (API key auth, audit log, idempotency)
4. **Agent Gateway** (`agent/`): SpaceRegistry (YAML-driven semantic names), AISafetyGuard (access levels, rate limits, cooldowns, confirmations), SceneEngine (presets + rules), ToolGenerator (OpenAI/Anthropic/MCP formats), ToolExecutor, MCPServer (stdio JSON-RPC), SmartSpacesClient (sync + async SDK)
5. **156 tests** across all layers

### Remaining

- 14 adapter stubs need real protocol implementations (KinCony is the only reference impl)
- Subscription/push event support across adapters
- Multi-tenant site/space graph persistence
- Scene optimization policies
- Offline operation and local fallback

---

## 56. Final normative summary

A conformant adapter system MUST:

- expose one canonical northbound API,
- separate board/device identity from protocol personality,
- normalize all state/telemetry into canonical devices/endpoints/points/capabilities,
- preserve raw payloads,
- provide discovery and low-friction commissioning,
- expose health and explicit quality,
- support safe, auditable writes,
- support multi-tenant site/space graphs,
- support local-first operation,
- allow protocol-specific advanced setup where required,
- remain extensible for future ecosystems.

---

## 57. Next priorities

The spec is fully implemented at the interface level. Next priorities:

1. **Flesh out adapter stubs** — implement real protocol logic for Shelly, MQTT, Modbus, Hue (highest value)
2. **Push event support** — wire adapter `subscribe()` through EventBus to Agent Gateway for real-time state updates
3. **Multi-tenant persistence** — persist the site/space graph in StateStore alongside device data
4. **Scene persistence** — save scenes/rules to disk so they survive restarts
5. **Offline resilience** — local command queuing when network/cloud is down
6. **Agent SDK distribution** — publish `agent` package so external AI agents can `pip install smartspaces`
