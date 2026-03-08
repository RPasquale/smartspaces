# SmartSpaces

A universal adapter platform for connecting AI agents to physical devices. Control lights, sensors, relays, locks, covers, and more across 15 protocols through a single semantic API with built-in safety guards.

**This is NOT Home Assistant.** This is a from-scratch system designed for AI-first device control — 117 Python files, ~20,000 lines of code, 413 tests.

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
│  Agent Gateway (16 modules)                                      │
│  Spaces · Safety · Scenes · Tools · Intent · Groups · History    │
│  Coordination · Scheduler · Analytics · Suggestions · Discovery  │
│  Events (SSE) · MCP Server · Client SDK                          │
├─────────────────────────────────────────────────────────────────┤
│  Core Runtime                                                    │
│  Engine · EventBus (memory or Redis) · StateStore (SQLite WAL)   │
│  Registry · Scheduler · FastAPI (40+ endpoints) · API Key Auth   │
│  Structured Logging (JSON/text) · Prometheus Metrics · CORS      │
├─────────────────────────────────────────────────────────────────┤
│  Protocol Adapters (15)                                          │
│  KinCony · Shelly · MQTT · Modbus · Hue · ONVIF · ESPHome       │
│  Zigbee2MQTT · Z-Wave JS · Matter · Lutron · KNX                │
│  BACnet · OPC UA · DNP3                                          │
└─────────────────────────────────────────────────────────────────┘
```

## Current State

The platform is **functionally complete and production-hardened**. All components work together end-to-end.

### What Works Right Now (no hardware needed)

- Full runtime boots with `python -m core.engine` — server on port 8000
- REST API — 40+ endpoints with API key auth, CORS, correlation IDs, audit logging
- All 15 adapters load and register at startup
- Agent Gateway — AI controls devices through semantic names with safety guards
- Scenes — multi-device presets and condition-action automation rules
- Natural language — intent resolver parses "turn off the kitchen lights" into device commands
- Safety system — rate limiting, cooldowns, human confirmation for S3+ operations
- State persistence — SQLite WAL mode, audit log, connection restoration on restart
- Observability — structured JSON/text logging, Prometheus metrics at `/metrics`, correlation IDs
- Docker — `docker-compose up` runs everything
- CI/CD — GitHub Actions for lint, test, Docker build
- Distributed mode — Redis event bus for cross-process event distribution

### What Requires Hardware/Services

| Adapter | What You Need | Protocol | Library |
|---------|--------------|----------|---------|
| **KinCony** | KC868-A4 board on LAN | HTTP to Tasmota | httpx (included) |
| **Shelly** | Any Shelly Gen2 device | HTTP RPC | httpx (included) |
| **MQTT** | MQTT broker + devices | MQTT pub/sub | `paho-mqtt` |
| **Modbus** | Modbus TCP device | Modbus/TCP | `pymodbus` |
| **Hue** | Philips Hue Bridge | HTTPS REST | httpx (included) |
| **ONVIF** | IP camera | ONVIF/SOAP | `onvif-zeep` |
| **ESPHome** | ESPHome device | HTTP REST | httpx (included) |
| **Zigbee** | Zigbee2MQTT + coordinator | HTTP to Z2M | httpx (included) |
| **Z-Wave** | Z-Wave JS + USB stick | WebSocket | httpx (included) |
| **Matter** | Matter controller | HTTP to controller | httpx (included) |
| **Lutron** | Lutron Caseta bridge | HTTP/telnet | httpx (included) |
| **KNX** | KNX/IP gateway | KNX tunneling | `xknx` |
| **BACnet** | BACnet/IP device | BACnet/IP | `BAC0` |
| **OPC UA** | OPC UA server | OPC UA binary | `asyncua` |
| **DNP3** | DNP3 outstation/RTU | DNP3/TCP | `pydnp3` |

The first 11 adapters are fully implemented using HTTP/WebSocket — they work with just `httpx` (included). The last 4 (KNX, BACnet, OPC UA, DNP3) integrate with real protocol libraries as optional dependencies and fall back gracefully when those libraries aren't installed.

Every adapter implements the same abstract interface: `discover`, `commission`, `inventory`, `subscribe`, `read_point`, `execute`, `health`, `teardown`.

## Quick Start

### Install

```bash
pip install -e ".[server,dev]"
```

### 1. Start the server

```bash
# Set an API key
export SMARTSPACES_API_KEYS=my-secret-key

# Start
python -m core.engine --host 0.0.0.0 --port 8000
```

Or with Docker:

```bash
docker-compose up
```

### 2. Define your spaces

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

### 3. Define scenes (optional)

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

### 4. Connect a device (example: KinCony board)

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "Authorization: Bearer my-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"adapter_id":"kincony.family","profile_id":"tasmota_http","fields":{"host":"192.168.0.90"}}'
```

Or programmatically:

```python
import asyncio
from core.engine import Engine
from adapters.kincony import KinConyAdapter

async def main():
    engine = Engine(db_path=":memory:")
    engine.register_adapter(KinConyAdapter())
    await engine.start()
    conn_id = await engine.quick_connect(
        adapter_id="kincony.family",
        profile_id="tasmota_http",
        fields={"host": "192.168.0.90"},
    )
    print(f"Connected: {conn_id}")
    await engine.stop()

asyncio.run(main())
```

### 5. Control devices from Python (AI agent SDK)

```python
from agent.client import SmartSpacesClient

ss = SmartSpacesClient(base_url="http://localhost:8000", api_key="my-secret-key")

# Read device state
state = ss.get_state("living_room.ceiling_light")

# Control a device
ss.set_device("living_room.ceiling_light", "on")

# Activate a scene
ss.activate_scene("movie_mode")

# Natural language intent
result = ss.resolve_intent("turn off all the kitchen lights")

# Get LLM tool definitions
tools = ss.get_tool_definitions(format="openai")   # or "anthropic", "mcp"
```

### 6. Connect via MCP (Claude Desktop / Claude Code)

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
- **Confirmation workflow** — human-in-the-loop approve/deny for sensitive operations (with configurable TTL expiry)

## REST API

All endpoints require API key authentication (Bearer token or `X-API-Key` header), except `/healthz` and `/metrics`.

Every response includes an `X-Correlation-ID` header for request tracing. Pass your own `X-Correlation-ID` to have it preserved through the call chain.

### Infrastructure Endpoints (no auth required)

| Method | Path       | Description                     |
|--------|------------|---------------------------------|
| GET    | `/healthz` | Health check (uptime, status)   |
| GET    | `/metrics` | Prometheus metrics (text format)|

### Core Endpoints

| Method | Path                               | Description                    |
|--------|------------------------------------|--------------------------------|
| GET    | `/api/adapters`                    | List registered adapters       |
| POST   | `/api/discover`                    | Discover devices on network    |
| POST   | `/api/connections`                 | Commission a connection        |
| GET    | `/api/connections`                 | List active connections        |
| DELETE | `/api/connections/{id}`            | Disconnect                     |
| GET    | `/api/devices`                     | List all devices               |
| GET    | `/api/devices/{id}`                | Get device details             |
| GET    | `/api/devices/{id}/endpoints`      | Get device endpoints           |
| GET    | `/api/devices/{id}/points`         | Get device points              |
| GET    | `/api/points`                      | List all points                |
| POST   | `/api/points/read`                 | Read point values              |
| GET    | `/api/points/{id}/value`           | Read a single point            |
| GET    | `/api/values`                      | Bulk read all values           |
| POST   | `/api/commands`                    | Execute a command              |
| GET    | `/api/health`                      | Health check all connections   |
| GET    | `/api/health/{id}`                 | Health check one connection    |
| GET    | `/api/scheduler`                   | Scheduler status               |
| GET    | `/api/audit`                       | Audit log                      |
| GET    | `/api/system/stats`                | System statistics              |

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

### Advanced Agent Endpoints

| Method | Path                                           | Description                     |
|--------|-------------------------------------------------|---------------------------------|
| GET    | `/api/agent/events`                             | SSE event stream (real-time)    |
| GET    | `/api/agent/events/stats`                       | Event stream statistics         |
| POST   | `/api/agent/intent`                             | Natural language intent resolver|
| GET    | `/api/agent/groups`                             | List device groups              |
| POST   | `/api/agent/groups`                             | Create a device group           |
| POST   | `/api/agent/groups/set`                         | Control all devices in a group  |
| GET    | `/api/agent/history`                            | Query action history            |
| GET    | `/api/agent/schedules`                          | List scheduled actions          |
| POST   | `/api/agent/schedules`                          | Schedule an action              |
| POST   | `/api/agent/schedules/{id}/cancel`              | Cancel a scheduled action       |
| GET    | `/api/agent/analytics`                          | Energy & comfort analytics      |
| GET    | `/api/agent/analytics/context`                  | Analytics context for LLM       |
| POST   | `/api/agent/locks/acquire`                      | Acquire device lock (multi-agent)|
| POST   | `/api/agent/locks/release`                      | Release device lock             |
| GET    | `/api/agent/locks`                              | List active leases              |
| GET    | `/api/agent/suggestions`                        | Get proactive action suggestions|
| GET    | `/api/agent/describe/{device}`                  | Describe device capabilities    |
| GET    | `/api/agent/describe`                           | Describe all devices            |

## Advanced Agent Features

### Real-time Event Streaming (SSE)
Server-Sent Events endpoint at `/api/agent/events` with per-client filtering by space, device, and event type. Includes heartbeat keepalive and backpressure handling.

### Natural Language Intent Resolution
Rule-based NLU pipeline that converts natural language commands into tool calls without requiring an external LLM. Uses the space registry as an entity gazetteer. Supports control, query, scene, group, schedule, environment, and meta intents.

```python
# "turn on the living room light" → {"tool": "set_device", "args": {"device": "living_room.ceiling_light", "action": "on"}}
# "make it cooler in here" → resolves to fan/AC devices in context
# "dim the lamp to 50% in 10 minutes" → schedule + control
```

### Device Groups
Static groups (explicit member lists) and dynamic groups (match by capability or space). Auto-generates groups like `all_binary_switch` and `all_living_room`. Control all devices in a group with a single command.

### Action History & Audit Trail
Thread-safe ring buffer with per-device indexing. Query by device, space, action type, status, initiator, or time range. Generates context prompts for LLM injection.

### Multi-Agent Coordination
Lease-based exclusive write access with configurable expiry (5-300s). Priority-based preemption for higher-priority agents. Prevents conflicting commands from multiple AI agents.

### Scheduled Actions
One-shot delays, absolute time scheduling, and recurring intervals via asyncio. Cancel individual or all schedules programmatically.

### Energy & Comfort Analytics
Power estimation from device capabilities with customizable overrides. Temperature-based comfort scoring (0-1 scale). Generates context text and recommendations for LLM injection.

### Proactive Suggestions
Time-of-day aware suggestions across comfort, energy, safety, routine, and automation categories. Dismissable with priority ordering. Suggests scenes, energy savings, and comfort adjustments.

### Capability Discovery
Per-device natural language descriptions that tell AI agents exactly what each device can do, its current state, and its constraints. Generates system prompt context for LLM injection.

## Observability

### Structured Logging
- JSON and text formatters with ISO 8601 timestamps
- Correlation ID propagation via `contextvars` — traces requests across the stack
- Configurable log context (adapter_id, connection_id) for structured filtering
- CLI flags: `--log-format json|text`, `--log-level DEBUG|INFO|WARNING|ERROR`

### Prometheus Metrics
Available at `/metrics` (no auth required). Tracks:
- HTTP requests (total, duration, in-flight)
- Event bus (published, dispatched, errors, queue depth)
- Adapter operations (total, duration by adapter/operation)
- Scheduler (polls, active/suspended targets)
- Connections (active gauge)
- Safety checks (total by result)

Falls back to no-op stubs when `prometheus-client` is not installed.

### Health Check
`GET /healthz` returns server status and uptime without requiring authentication. Used by Docker `HEALTHCHECK` and load balancers.

## Distributed Event Bus (Redis)

The default event bus is in-process (memory). For multi-process or multi-node deployments, switch to Redis:

```bash
# Via CLI flag
python -m core.engine --event-bus redis --redis-url redis://localhost:6379

# Via environment variables
export SMARTSPACES_EVENT_BUS=redis
export SMARTSPACES_REDIS_URL=redis://localhost:6379
python -m core.engine
```

The Redis event bus is a drop-in replacement — same interface, same subscribe/publish API. It uses Redis Pub/Sub with channel naming `smartspaces:{event_type}`, supports glob pattern subscriptions, and handles reconnection with exponential backoff.

Install: `pip install 'physical-space-adapters[redis]'`

## Deployment

### Docker

```bash
# Build
docker build -t smartspaces .

# Run with docker-compose
docker-compose up

# Or run directly
docker run -p 8000:8000 \
  -e SMARTSPACES_API_KEYS=my-key \
  -v smartspaces_data:/app/data \
  smartspaces
```

The Dockerfile uses multi-stage builds (python:3.11-slim), runs as non-root user `smartspaces`, and includes a `HEALTHCHECK` on `/healthz`.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SMARTSPACES_API_KEYS` | (none) | Comma-separated API keys |
| `SMARTSPACES_CORS_ORIGINS` | (none) | Comma-separated CORS origins |
| `SMARTSPACES_EVENT_BUS` | `memory` | Event bus backend: `memory` or `redis` |
| `SMARTSPACES_REDIS_URL` | `redis://localhost:6379` | Redis URL for distributed bus |
| `KINCONY_IP` | `192.168.0.90` | Default KinCony device IP |

### CLI Flags

```bash
python -m core.engine \
  --host 0.0.0.0 \
  --port 8000 \
  --db-path state.db \
  --spaces spaces.yaml \
  --scenes scenes.yaml \
  --log-level INFO \
  --log-format json \
  --event-bus memory \
  --cors-origins "http://localhost:3000,http://localhost:5173" \
  --no-restore
```

### CI/CD (GitHub Actions)

The `.github/workflows/ci.yml` pipeline runs on every push:
- **Lint** — `ruff check` + `ruff format --check`
- **Test** — `pytest` with coverage on Python 3.11 and 3.12
- **Docker** — build + smoke test on main branch

## Project Structure

```
smartspaces/
├── agent/                    # Agent Gateway (AI integration layer, 16 modules)
│   ├── spaces.py             # Semantic device registry (YAML → names)
│   ├── safety.py             # AI safety guard (access, rate limits, confirmations)
│   ├── scenes.py             # Scenes & automation rules engine
│   ├── tools.py              # LLM tool definitions & executor (25 tools)
│   ├── mcp_server.py         # MCP server (stdio JSON-RPC)
│   ├── client.py             # Python SDK (sync + async)
│   ├── events.py             # Real-time SSE event streaming
│   ├── intent.py             # Natural language intent resolver
│   ├── groups.py             # Device groups (static + dynamic)
│   ├── history.py            # Action history & audit trail
│   ├── coordination.py       # Multi-agent lease-based locking
│   ├── agent_scheduler.py    # Scheduled & recurring actions
│   ├── analytics.py          # Energy & comfort analytics
│   ├── suggestions.py        # Proactive action suggestions
│   └── discovery.py          # Capability discovery prompts
├── core/                     # Runtime engine
│   ├── engine.py             # Main entry point, CLI, wires everything
│   ├── api.py                # FastAPI REST API (40+ endpoints)
│   ├── event_bus.py          # In-process async pub/sub event bus
│   ├── event_bus_redis.py    # Redis-backed distributed event bus
│   ├── registry.py           # Adapter lifecycle management
│   ├── scheduler.py          # Poll scheduling with auto-recovery
│   ├── state_store.py        # SQLite persistence (WAL mode)
│   ├── logging_config.py     # Structured logging (JSON/text formatters)
│   └── metrics.py            # Prometheus metrics (with no-op stubs)
├── sdk/                      # Adapter SDK
│   └── adapter_api/
│       ├── base.py           # Abstract Adapter base class
│       ├── models.py         # Canonical Pydantic data models
│       ├── manifest.py       # Adapter manifest schema
│       ├── errors.py         # Typed error hierarchy
│       ├── safety.py         # Safety class definitions
│       └── contract_tests/   # Protocol-agnostic contract test suite
├── adapters/                 # Protocol adapter implementations (15)
│   ├── kincony/              # KinCony KC868-A4 (Tasmota HTTP) — reference impl
│   ├── shelly/               # Shelly Gen2 (HTTP RPC)
│   ├── mqtt_generic/         # Generic MQTT (paho-mqtt)
│   ├── modbus/               # Modbus TCP (pymodbus)
│   ├── hue/                  # Philips Hue (REST API)
│   ├── onvif/                # ONVIF cameras (SOAP/onvif-zeep)
│   ├── esphome/              # ESPHome (HTTP REST)
│   ├── zigbee/               # Zigbee2MQTT (HTTP)
│   ├── zwave/                # Z-Wave JS (WebSocket)
│   ├── matter/               # Matter/Thread (HTTP)
│   ├── lutron/               # Lutron Caseta (HTTP/telnet)
│   ├── knx/                  # KNX/IP tunneling (xknx)
│   ├── bacnet/               # BACnet/IP (BAC0)
│   ├── opcua/                # OPC UA (asyncua)
│   └── dnp3/                 # DNP3/TCP (pydnp3)
├── examples/                 # Example scripts
│   ├── quick_start.py        # Connect a KinCony and read relay states
│   ├── multi_adapter.py      # Run server with multiple adapters
│   └── agent_sdk_usage.py    # How an AI agent uses the Python SDK
├── fixtures/                 # Example YAML configs
│   ├── spaces_example.yaml   # 6 spaces, 11 devices
│   ├── scenes_example.yaml   # 4 scenes, 2 automation rules
│   └── modbus_example_register_map.yaml
├── tests/                    # Test suite (413 tests)
│   ├── agent/                # Agent Gateway tests (124)
│   ├── integration/          # Engine + API integration tests
│   ├── test_adapter_contracts.py  # Contract tests for all 15 adapters
│   ├── test_redis_event_bus.py    # Redis event bus tests
│   ├── test_observability.py      # Logging, metrics, correlation ID tests
│   ├── test_sprint1.py            # Production safety tests
│   └── test_sprint2.py            # API hardening tests
├── .github/workflows/ci.yml  # GitHub Actions CI/CD
├── Dockerfile                # Multi-stage Docker build
├── docker-compose.yaml       # Docker Compose config
├── .env.example              # Environment variable reference
├── CLAUDE.md                 # Full LLM context (hardware + software)
├── SPEC.md                   # Hardware specification
├── pyproject.toml            # Python packaging config
└── README.md                 # This file
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev,server]"

# Run all 413 tests
pytest tests/ -v

# Run specific test suites
pytest tests/agent/ -v                              # Agent gateway (124 tests)
pytest tests/integration/ -v                         # Integration tests
pytest tests/test_adapter_contracts.py -v            # Contract tests (all 15 adapters)

# Run with coverage
pytest tests/ --cov=agent --cov=core --cov=sdk

# Lint
ruff check .
ruff format --check .
```

### Writing a New Adapter

1. Create `adapters/your_protocol/adapter.py`
2. Subclass `sdk.adapter_api.base.Adapter`
3. Implement: `discover`, `commission`, `inventory`, `read_point`, `execute`, `health`, `teardown`
4. Create `adapters/your_protocol/adapter.yaml` manifest
5. Add your adapter to the import list in `core/engine.py`
6. Add a contract test subclass in `tests/test_adapter_contracts.py`

See `adapters/kincony/adapter.py` for the reference implementation.

## Requirements

- Python 3.11+
- Core: `pydantic`, `pyyaml`, `httpx`, `aiosqlite`, `python-dotenv`
- Server: `fastapi`, `uvicorn`
- Metrics: `prometheus-client` (optional)
- Redis bus: `redis[hiredis]` (optional)
- Protocol-specific: `pymodbus`, `paho-mqtt`, `xknx`, `BAC0`, `asyncua`, `pydnp3`, `onvif-zeep` (all optional, per-adapter)
- Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`

## Origin

This project started as a control script for the **KinCony KC868-A4** — an ESP32-based relay board running Tasmota firmware. It grew into a universal adapter platform when the need arose to control many different device types through a single AI-friendly interface. See `CLAUDE.md` for the original hardware documentation and `SPEC.md` for the hardware specification.

## License

Private repository.
