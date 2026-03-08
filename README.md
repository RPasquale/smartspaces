# SmartSpaces

A universal adapter platform for connecting AI agents to physical devices. Control lights, sensors, relays, locks, covers, and more across 15+ protocols through a single semantic API with built-in safety guards.

**This is NOT Home Assistant.** This is a from-scratch system designed for AI-first device control.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        AI Agent / LLM                            │
│            (Claude, GPT, custom agent, etc.)                     │
└───────────┬──────────────────┬──────────────────┬────────────────┘
            │                  │                  │
      Python SDK         REST API           MCP Server
   (SmartSpacesClient)  (/api/agent/*)     (stdio JSON-RPC)
            │                  │                  │
┌───────────▼──────────────────▼──────────────────▼────────────────┐
│                       Agent Gateway                              │
│  ┌──────────────┐ ┌────────────┐ ┌──────────────┐               │
│  │ Space        │ │ AI Safety  │ │ Scene        │               │
│  │ Registry     │ │ Guard      │ │ Engine       │               │
│  │ (YAML-driven │ │ (access,   │ │ (presets,    │               │
│  │  semantic    │ │  rate-limit │ │  automation  │               │
│  │  names)      │ │  confirm)  │ │  rules)      │               │
│  └──────────────┘ └────────────┘ └──────────────┘               │
│  ┌──────────────┐ ┌────────────┐                                │
│  │ Tool         │ │ Tool       │                                │
│  │ Generator    │ │ Executor   │                                │
│  │ (OpenAI/     │ │ (routes    │                                │
│  │  Anthropic/  │ │  calls     │                                │
│  │  MCP format) │ │  safely)   │                                │
│  └──────────────┘ └────────────┘                                │
├─────────────────────────────────────────────────────────────────┤
│                       Core Runtime                               │
│  EventBus  ·  StateStore (SQLite)  ·  Registry  ·  Scheduler    │
│  FastAPI REST API  ·  API Key Auth  ·  Audit Log                │
├─────────────────────────────────────────────────────────────────┤
│                    Protocol Adapters (15)                         │
│  KinCony · Shelly · MQTT · Modbus · Hue · ONVIF · ESPHome      │
│  Zigbee2MQTT · Z-Wave JS · Matter · Lutron · KNX               │
│  BACnet · OPC UA · DNP3                                         │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Install

```bash
pip install -e ".[server,dev]"
```

### 1. Define your spaces

Create a `spaces.yaml` mapping your physical devices to semantic names:

```yaml
site: "my_home"

spaces:
  living_room:
    display_name: "Living Room"
    devices:
      ceiling_light:
        point_id: "dev_kc868_a4_192_168_1_100_relay_1_state"
        connection_id: "tasmota_abc123"
        capabilities: [binary_switch]
        ai_access: full
        safety_class: S1

      temperature:
        point_id: "dev_esphome_192_168_1_102_sensor_temp_state"
        connection_id: "esphome_ghi789"
        capabilities: [temperature_sensor]
        ai_access: read_only
        unit: "°C"

  front_door:
    display_name: "Front Door"
    devices:
      lock:
        point_id: "dev_zwave_node_5_door_lock_lock_state_value"
        connection_id: "zwave_vwx234"
        capabilities: [lock, door_lock]
        ai_access: blocked          # AI cannot touch this
        safety_class: S2
```

### 2. Define scenes (optional)

```yaml
scenes:
  movie_mode:
    display_name: "Movie Mode"
    actions:
      - device: living_room.ceiling_light
        action: "off"
      - device: living_room.lamp
        action: set
        value: 15

rules:
  auto_cooling:
    display_name: "Auto Cooling"
    condition:
      device: living_room.temperature
      operator: ">"
      value: 28
    actions:
      - device: living_room.fan
        action: "on"
    cooldown_sec: 300
```

### 3. Start the server

```bash
python -m core.engine --host 0.0.0.0 --port 8000
```

### 4. Control devices from Python

```python
from agent.client import SmartSpacesClient

ss = SmartSpacesClient(base_url="http://localhost:8000", api_key="your-key")

# Discover
spaces = ss.list_spaces()
devices = ss.list_devices(space="living_room")

# Read
state = ss.get_state("living_room.ceiling_light")

# Control
ss.set_device("living_room.ceiling_light", "on")
ss.set_device("living_room.ceiling_light", "set", value=50)

# Scenes
ss.activate_scene("movie_mode")

# Get LLM tool definitions
tools = ss.get_tool_definitions(format="openai")   # or "anthropic", "mcp"
```

### 5. Connect via MCP (Claude Desktop / Claude Code)

Add to your MCP config:

```json
{
  "mcpServers": {
    "smartspaces": {
      "command": "python",
      "args": ["-m", "agent.mcp_server", "--spaces", "spaces.yaml", "--scenes", "scenes.yaml"]
    }
  }
}
```

Then Claude can directly control your devices through natural language.

## AI Safety

The safety guard sits between the AI agent and every device operation:

| AI Access Level     | Read | Write | Notes                                    |
|---------------------|------|-------|------------------------------------------|
| `full`              | Yes  | Yes   | Normal devices (lights, fans)            |
| `read_only`         | Yes  | No    | Sensors, smoke detectors                 |
| `confirm_required`  | Yes  | Needs human approval | Garage doors, covers        |
| `blocked`           | No   | No    | Locks, security devices                  |

Additional protections:
- **Rate limiting** — max writes per minute per device (default: 10)
- **Cooldown** — minimum time between writes to same device (default: 2s)
- **Capability blocking** — `lock` and `door_lock` capabilities are always blocked for AI
- **Safety classes** — S3+ devices always require human confirmation
- **Readback** — optionally verify device state after every write
- **Confirmation workflow** — human-in-the-loop approve/deny for sensitive operations

## Protocol Adapters

| Adapter       | Integration Class    | Status          |
|---------------|----------------------|-----------------|
| KinCony       | Direct local device  | Reference impl  |
| Shelly        | Direct local device  | Stub            |
| MQTT Generic  | Message bus          | Stub            |
| Modbus        | Industrial protocol  | Stub            |
| Philips Hue   | Bridge               | Stub            |
| ONVIF         | Bridge (cameras)     | Stub            |
| ESPHome       | Direct local device  | Stub            |
| Zigbee2MQTT   | Radio-network        | Stub            |
| Z-Wave JS     | Radio-network        | Stub            |
| Matter        | Radio-network        | Stub            |
| Lutron        | Bridge               | Stub            |
| KNX           | Industrial protocol  | Stub            |
| BACnet        | Industrial protocol  | Stub            |
| OPC UA        | Industrial protocol  | Stub            |
| DNP3          | Industrial protocol  | Stub            |

Every adapter implements the same abstract interface: `discover`, `commission`, `inventory`, `subscribe`, `read_point`, `execute`, `health`, `teardown`.

## REST API

All endpoints require API key authentication (Bearer token or `X-API-Key` header).

### Core Endpoints

| Method | Path                          | Description                    |
|--------|-------------------------------|--------------------------------|
| GET    | `/api/adapters`               | List registered adapters       |
| POST   | `/api/discover`               | Discover devices on network    |
| POST   | `/api/commission`             | Connect to a device            |
| GET    | `/api/connections`            | List active connections        |
| POST   | `/api/connections/{id}/disconnect` | Disconnect                |
| GET    | `/api/devices`                | List all devices               |
| GET    | `/api/devices/{id}`           | Get device details             |
| POST   | `/api/read`                   | Read a point value             |
| POST   | `/api/execute`                | Execute a command              |
| GET    | `/api/health`                 | Health check all connections   |

### Agent Gateway Endpoints

| Method | Path                                           | Description                     |
|--------|-------------------------------------------------|---------------------------------|
| GET    | `/api/agent/spaces`                             | List spaces and devices         |
| GET    | `/api/agent/devices`                            | List devices (filterable)       |
| POST   | `/api/agent/state`                              | Read device state               |
| POST   | `/api/agent/set`                                | Control a device                |
| POST   | `/api/agent/space_summary`                      | All device states in a space    |
| GET    | `/api/agent/scenes`                             | List scenes                     |
| POST   | `/api/agent/scenes`                             | Create a scene                  |
| POST   | `/api/agent/scenes/activate`                    | Activate a scene                |
| GET    | `/api/agent/rules`                              | List automation rules           |
| POST   | `/api/agent/rules`                              | Create a rule                   |
| GET    | `/api/agent/tools/{format}`                     | Get LLM tool definitions        |
| GET    | `/api/agent/context`                            | Get LLM system prompt context   |
| GET    | `/api/agent/confirmations`                      | List pending confirmations      |
| POST   | `/api/agent/confirmations/{id}/approve`         | Approve a confirmation          |
| POST   | `/api/agent/confirmations/{id}/deny`            | Deny a confirmation             |
| GET    | `/api/agent/safety/stats`                       | Safety guard statistics         |

## Project Structure

```
smartspaces/
├── agent/                    # Agent Gateway (AI integration layer)
│   ├── spaces.py             # Semantic device registry (YAML → names)
│   ├── safety.py             # AI safety guard (access, rate limits)
│   ├── scenes.py             # Scenes & automation rules engine
│   ├── tools.py              # LLM tool definitions & executor
│   ├── mcp_server.py         # MCP server (stdio JSON-RPC)
│   └── client.py             # Python SDK (sync + async)
├── core/                     # Runtime engine
│   ├── engine.py             # Main entry point, wires everything
│   ├── api.py                # FastAPI REST API (40+ endpoints)
│   ├── event_bus.py          # Async pub/sub event bus
│   ├── registry.py           # Adapter lifecycle management
│   ├── scheduler.py          # Poll scheduling with auto-recovery
│   └── state_store.py        # SQLite persistence (WAL mode)
├── sdk/                      # Adapter SDK
│   └── adapter_api/
│       ├── base.py           # Abstract Adapter base class
│       ├── models.py         # Canonical Pydantic data models
│       ├── manifest.py       # Adapter manifest schema
│       ├── errors.py         # Typed error hierarchy
│       └── safety.py         # Safety class definitions
├── adapters/                 # Protocol adapter implementations
│   ├── kincony/              # Reference implementation (Tasmota HTTP)
│   ├── shelly/               # Shelly Gen1/Gen2
│   ├── mqtt_generic/         # Generic MQTT
│   ├── modbus/               # Modbus TCP/RTU
│   ├── hue/                  # Philips Hue (bridge)
│   ├── onvif/                # ONVIF cameras
│   ├── esphome/              # ESPHome native API
│   ├── zigbee/               # Zigbee2MQTT
│   ├── zwave/                # Z-Wave JS
│   ├── matter/               # Matter/Thread
│   ├── lutron/               # Lutron Caseta/RadioRA
│   ├── knx/                  # KNX/IP
│   ├── bacnet/               # BACnet/IP
│   ├── opcua/                # OPC UA
│   └── dnp3/                 # DNP3
├── fixtures/                 # Example YAML configs
│   ├── spaces_example.yaml
│   ├── scenes_example.yaml
│   └── modbus_example_register_map.yaml
├── tests/                    # Test suite (156 tests)
│   ├── agent/                # Agent Gateway tests (49)
│   └── core/                 # Core runtime tests (23+)
├── CLAUDE.md                 # LLM context for the KinCony hardware
├── SPEC.md                   # Hardware specification
└── universal_physical_space_adapter_spec_pack.md  # Adapter interface spec
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev,server]"

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=agent --cov=core --cov=sdk

# Lint
ruff check .
```

### Writing a New Adapter

1. Create `adapters/your_protocol/adapter.py`
2. Subclass `sdk.adapter_api.base.Adapter`
3. Implement: `discover`, `commission`, `inventory`, `read_point`, `execute`, `health`, `teardown`
4. Create `adapters/your_protocol/adapter.yaml` manifest
5. Add your adapter to the import list in `core/engine.py`

See `adapters/kincony/adapter.py` for the reference implementation.

## Requirements

- Python 3.11+
- Core: `pydantic`, `pyyaml`, `httpx`, `aiosqlite`
- Server: `fastapi`, `uvicorn`
- Protocol-specific dependencies are optional (install per-adapter)

## Origin

This project started as a control script for the **KinCony KC868-A4** — an ESP32-based relay board running Tasmota firmware. It grew into a universal adapter platform when the need arose to control many different device types through a single AI-friendly interface. See `SPEC.md` for the original hardware documentation.

## License

Private repository.
