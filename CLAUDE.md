# LLM Context — SmartSpaces

This file contains everything an LLM needs to understand, modify, and extend this project with zero ambiguity.

## What This Project Is

**SmartSpaces** is a universal adapter platform for connecting AI agents to physical devices. It started with the KinCony KC868-A4 board and grew into a multi-protocol system with 15 adapters, a core runtime, and an Agent Gateway that lets any LLM control devices through semantic names with built-in safety guards.

This is NOT using Home Assistant. This is a from-scratch custom system.

### System Layers

1. **Adapter SDK** (`sdk/adapter_api/`) — Abstract base class, canonical Pydantic models, typed errors, safety classes
2. **Protocol Adapters** (`adapters/`) — 15 adapters: KinCony, Shelly, MQTT, Modbus, Hue, ONVIF, ESPHome, Zigbee2MQTT, Z-Wave JS, Matter, Lutron, KNX, BACnet, OPC UA, DNP3
3. **Core Runtime** (`core/`) — EventBus (async pub/sub), StateStore (SQLite WAL), AdapterRegistry (with locks + timeouts), Scheduler (with auto-recovery), FastAPI REST API (API key auth, audit log)
4. **Agent Gateway** (`agent/`) — SpaceRegistry (YAML-driven semantic names), AISafetyGuard (access levels, rate limits, confirmations), SceneEngine (presets + automation rules), ToolGenerator (OpenAI/Anthropic/MCP formats), ToolExecutor, MCPServer (stdio JSON-RPC), SmartSpacesClient (sync + async SDK)

### Key Design Decisions
- **Python 3.11** target runtime
- **Local-first** — control devices over LAN, no cloud dependency
- **Safety-first AI** — AI access levels (full/read_only/confirm_required/blocked), rate limiting, cooldowns, capability restrictions, S3+ requires human confirmation
- **YAML-driven config** — `spaces.yaml` maps raw device IDs to semantic names like `living_room.ceiling_light`
- **Protocol-agnostic** — every adapter presents the same interface to the orchestrator

### Running

```bash
pip install -e ".[server,dev]"
python -m core.engine                           # start server on :8000
python -m agent.mcp_server --spaces spaces.yaml # MCP server for Claude
pytest tests/ -v                                # 413 tests
docker-compose up                               # Docker deployment
```

## KC868-A4 Hardware

The original hardware this project was built for.

## Full Architecture

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
│  Agent Gateway: SpaceRegistry · SafetyGuard · SceneEngine        │
│                 ToolGenerator · ToolExecutor                      │
├─────────────────────────────────────────────────────────────────┤
│  Core Runtime: EventBus · StateStore · Registry · Scheduler      │
│                FastAPI (40+ endpoints) · API Key Auth             │
├─────────────────────────────────────────────────────────────────┤
│  Adapters: KinCony · Shelly · MQTT · Modbus · Hue · ONVIF       │
│            ESPHome · Zigbee · Z-Wave · Matter · Lutron            │
│            KNX · BACnet · OPC UA · DNP3                           │
├─────────────────────────────────────────────────────────────────┤
│  Physical Devices (each via its native protocol)                 │
└─────────────────────────────────────────────────────────────────┘
```

### KC868-A4 Hardware Diagram

```
┌─────────────────────────────────────────────────────┐
│              Tasmota 15.3.0 Firmware                  │
│              (on ESP32-D0WD-V3)                       │
├─────────────────────────────────────────────────────┤
│  GPIO4  → Relay 1    │  GPIO18 → Digital Input 1    │
│  GPIO5  → Relay 2    │  GPIO19 → Digital Input 2    │
│  GPIO11 → Relay 3    │  GPIO17 → Digital Input 3    │
│  GPIO2  → Relay 4    │  GPIO15 → Digital Input 4    │
│  GPIO9  → IR Send    │  GPIO30 → Analog Input 1     │
│  GPIO14 → IR Recv    │  GPIO31 → Analog Input 2     │
│  GPIO25 → DAC Out 1  │  GPIO28 → Analog Input 3     │
│  GPIO26 → DAC Out 2  │  GPIO29 → Analog Input 4     │
│  GPIO0  → Button S2  │                              │
├─────────────────────────────────────────────────────┤
│              KC868-A4 Hardware                        │
│  4x 10A relays │ opto-isolated inputs │ 12V power   │
└─────────────────────────────────────────────────────┘
```

## How to Talk to the Device

### HTTP API (primary method)

Every Tasmota command is a GET request to `http://<DEVICE_IP>/cm?cmnd=<COMMAND>`. Response is always JSON.

**The device IP is configured via env var `KINCONY_IP` (default: `192.168.0.90`).** Find the actual IP by checking your router's DHCP leases or running `Status 5` via serial.

#### Complete Command Reference

**Relay control** (the device has 4 relays, addressed as Power1-Power4):
```
Power1 ON          → {"POWER1":"ON"}
Power1 OFF         → {"POWER1":"OFF"}
Power1 TOGGLE      → {"POWER1":"ON"} or {"POWER1":"OFF"}
Power1             → {"POWER1":"OFF"}           # query state
Power0             → {"POWER1":"OFF","POWER2":"OFF","POWER3":"OFF","POWER4":"OFF"}  # all states
```

**Batch commands** (semicolon-separated, executed sequentially):
```
Backlog Power1 ON; Power2 ON; Delay 10; Power1 OFF; Power2 OFF
```
`Delay` unit is 0.1 seconds. `Delay 10` = 1 second.

**Pulse mode** (relay auto-turns-off after duration):
```
PulseTime1 20      # Relay 1 turns off 2.0s after being turned on (units of 0.1s)
PulseTime1 0       # Disable pulse mode for relay 1
```

**Sensor reading:**
```
Status 8  → {"StatusSNS":{"Time":"...","ANALOG":{"A3":176,"A4":176,"A1":176,"A2":176}}}
```
Analog values are raw ADC readings (0-4095, 12-bit). Voltage = value * 3.3 / 4095.

**Digital inputs** are reported as Switch1-Switch4 in status and rules. They read HIGH/LOW from opto-isolated inputs (5-24V DC triggers HIGH).

**IR send:**
```
IRsend {"Protocol":"NEC","Bits":32,"Data":"0x20DF10EF"}
```
Supported protocols: NEC, Sony, RC5, RC6, Samsung, LG, Panasonic, and many more.

**Device status:**
```
Status 0    → full JSON blob (everything)
Status 5    → network info (IP, MAC, hostname)
Status 8    → sensor data
Status 10   → switch/input states
```

**System:**
```
Restart 1           # reboot
Upgrade 1           # OTA update from Tasmota servers
WebPassword <pw>    # set web UI password
DeviceName <name>   # rename device
Timezone +10        # set timezone (AEST example)
```

### MQTT

Not enabled by default. To configure:
```
MqttHost <broker_ip>
MqttPort 1883
```

Topic structure (where `<TOPIC>` defaults to `tasmota_XXXXXX`):
- `cmnd/<TOPIC>/Power1` → send `ON`, `OFF`, `TOGGLE`
- `stat/<TOPIC>/POWER1` ← receives state changes
- `stat/<TOPIC>/RESULT` ← receives command results
- `tele/<TOPIC>/STATE` ← periodic telemetry (every 300s)
- `tele/<TOPIC>/SENSOR` ← periodic sensor data
- `tele/<TOPIC>/LWT` ← `Online` or `Offline` (last will)

### Serial (USB, 115200 baud, 8N1)

Same commands as HTTP, sent as plain text with `\r\n`. Response format:
```
HH:MM:SS.mmm CMD: <your command>
HH:MM:SS.mmm RSL: RESULT = <json response>
```

The CH340 USB-serial chip is at VID:1A86 PID:7523. On Windows it appears as COMx.

### WebSocket

Connect to `ws://<DEVICE_IP>:80/ws` for real-time push updates.

## Tasmota Template

This is the exact GPIO configuration for the KC868-A4. Apply it to any new KC868-A4 board:

```json
{"NAME":"KC868-A4","GPIO":[32,0,227,0,224,225,0,0,0,1312,1,226,0,0,480,1152,0,1120,1056,1088,0,1,1,1,0,0,0,0,4706,4707,4704,4705,1,0,0,1],"FLAG":0,"BASE":1}
```

Apply via serial or HTTP:
```
Template {"NAME":"KC868-A4","GPIO":[32,0,227,0,224,225,0,0,0,1312,1,226,0,0,480,1152,0,1120,1056,1088,0,1,1,1,0,0,0,0,4706,4707,4704,4705,1,0,0,1],"FLAG":0,"BASE":1}
Module 0
```
Device reboots after `Module 0`.

## GPIO Map (decoded from template)

| Index | GPIO   | Tasmota Code | Function     | What It Does                        |
|-------|--------|--------------|--------------|-------------------------------------|
| 0     | GPIO0  | 32           | Button1      | Physical button on board (S2)       |
| 2     | GPIO2  | 227          | Relay4       | Controls relay 4 coil               |
| 4     | GPIO4  | 224          | Relay1       | Controls relay 1 coil               |
| 5     | GPIO5  | 225          | Relay2       | Controls relay 2 coil               |
| 9     | GPIO9  | 1312         | IRsend       | IR LED transmitter                  |
| 11    | GPIO11 | 226          | Relay3       | Controls relay 3 coil               |
| 14    | GPIO14 | 480          | IRrecv       | IR receiver (38kHz demodulator)     |
| 15    | GPIO15 | 1152         | Switch4      | Opto-isolated digital input 4       |
| 17    | GPIO17 | 1120         | Switch3      | Opto-isolated digital input 3       |
| 18    | GPIO18 | 1056         | Switch1      | Opto-isolated digital input 1       |
| 19    | GPIO19 | 1088         | Switch2      | Opto-isolated digital input 2       |
| 28    | GPIO28 | 4706         | ADC_Input3   | Analog input 3 (0-3.3V, 12-bit)    |
| 29    | GPIO29 | 4707         | ADC_Input4   | Analog input 4 (0-3.3V, 12-bit)    |
| 30    | GPIO30 | 4704         | ADC_Input1   | Analog input 1 (0-3.3V, 12-bit)    |
| 31    | GPIO31 | 4705         | ADC_Input2   | Analog input 2 (0-3.3V, 12-bit)    |

GPIO25 and GPIO26 are DAC outputs (not in template, controlled via Berry: `gpio.dac_voltage(25, millivolts)`).

## Tasmota Rules Engine

Rules run locally on the device with zero latency. Up to 3 rule sets (Rule1, Rule2, Rule3).

Syntax: `Rule<n> ON <trigger> DO <action> ENDON [ON <trigger> DO <action> ENDON] ...`

**Triggers:**
- `Switch1#State=1` — digital input 1 goes HIGH
- `Switch1#State=0` — digital input 1 goes LOW
- `ANALOG#A1>2000` — analog input 1 exceeds threshold
- `ANALOG#A1<500` — analog input 1 below threshold
- `Time#Minute=360` — at minute 360 of the day (06:00)
- `System#Boot` — on device boot
- `Wifi#Connected` — on WiFi connect
- `Mqtt#Connected` — on MQTT connect
- `Power1#State=1` — when relay 1 turns on
- `Button1#State` — when physical button is pressed

**Actions:**
- `Power1 ON` / `Power1 OFF` / `Power1 TOGGLE`
- `Backlog Power1 ON; Delay 20; Power1 OFF` — sequence with delays
- `Publish stat/topic/custom payload` — send MQTT
- `WebSend [ip:port] /cm?cmnd=Power1 ON` — call another Tasmota device

Enable/disable: `Rule1 1` (enable) / `Rule1 0` (disable).

## Wiring

**Relays** — each has COM (common), NO (normally open), NC (normally closed). 10A @ 250V AC max.
- Wire your load between COM and NO for "on when relay activates"
- Wire between COM and NC for "on when relay is off"

**Digital inputs** — 5-24V DC across INx and GND triggers the opto-isolator. Safe for external circuits.

**Analog inputs** — 0-3.3V directly to ADC. Use a voltage divider for higher voltages.

**Power** — 12V DC required. USB alone powers ESP32 but not the relay coils.

## Key Modules

### Agent Gateway (`agent/`)
- `spaces.py` — `SpaceRegistry`: YAML-driven semantic device naming (`living_room.ceiling_light`)
- `safety.py` — `AISafetyGuard`: access levels (full/read_only/confirm_required/blocked), rate limiting, cooldowns, capability restrictions, confirmation workflow
- `scenes.py` — `SceneEngine`: multi-device presets and condition-action automation rules
- `tools.py` — `ToolGenerator` (OpenAI/Anthropic/MCP format) + `ToolExecutor` (routes through safety)
- `mcp_server.py` — MCP server (stdio JSON-RPC) for Claude Desktop/Code
- `client.py` — `SmartSpacesClient` (sync) + `AsyncSmartSpacesClient` wrapping `/api/agent/*`

### Core Runtime (`core/`)
- `engine.py` — boots EventBus → StateStore → Registry → Scheduler → API; full CLI with argparse
- `api.py` — FastAPI with API key auth, CORS, correlation IDs, metrics middleware, 40+ endpoints
- `event_bus.py` — in-process async pub/sub event bus
- `event_bus_redis.py` — Redis-backed distributed event bus (drop-in replacement)
- `registry.py` — adapter lifecycle with `asyncio.Lock`, configurable timeouts
- `state_store.py` — SQLite with WAL mode, write lock, schema migrations, audit log retention
- `scheduler.py` — poll scheduling with 15s timeouts and auto-recovery
- `logging_config.py` — structured logging (JSON/text formatters, correlation IDs via contextvars)
- `metrics.py` — Prometheus metrics (with no-op stubs when prometheus-client not installed)
- `network_scanner.py` — network discovery (mDNS/DNS-SD, SSDP/UPnP, async port scanning) with `--auto-discover` CLI flag and `POST /api/network/scan` endpoint

### Adapter SDK (`sdk/adapter_api/`)
- `base.py` — abstract `Adapter` ABC: discover/commission/inventory/subscribe/read_point/execute/health/teardown
- `models.py` — canonical Pydantic models (Device, Endpoint, Point, SafetyClass S0-S5, etc.)

### Original Scripts
- `kincony_control.py` — KC868-A4 HTTP control (`KINCONY_IP` env var, default `192.168.0.90`)
- `probe_device.py`, `probe_v2.py`, `deep_probe.py`, `at_test.py`, `capture_boot.py` — serial debugging

## Flashing a New KC868-A4

```bash
pip install esptool

# 1. Erase flash
python -m esptool --port COMx erase_flash

# 2. Flash Tasmota
python -m esptool --port COMx --baud 921600 write_flash 0x0 tasmota32.factory.bin

# 3. Connect to tasmota-XXXXXX-XXXX WiFi AP (open, no password)
# 4. Browse to http://192.168.4.1, enter your WiFi credentials
# 5. Device reboots and joins your network

# 6. Apply template (via serial or HTTP once on network):
#    Template {"NAME":"KC868-A4","GPIO":[32,0,227,0,224,225,0,0,0,1312,1,226,0,0,480,1152,0,1120,1056,1088,0,1,1,1,0,0,0,0,4706,4707,4704,4705,1,0,0,1],"FLAG":0,"BASE":1}
#    Module 0
```

Download firmware: `curl -L -o tasmota32.factory.bin https://ota.tasmota.com/tasmota32/release/tasmota32.factory.bin`

## Example JSON Responses

### Status 0 (full status) — abbreviated
```json
{
  "Status": {
    "Module": 0,
    "DeviceName": "Tasmota",
    "FriendlyName": ["Tasmota","Tasmota2","Tasmota3","Tasmota4"],
    "Topic": "tasmota_XXXXXX",
    "Power": "0000"
  },
  "StatusNET": {
    "Hostname": "tasmota-XXXXXX-XXXX",
    "IPAddress": "<DEVICE_IP>",
    "Mac": "<DEVICE_MAC>",
    "Webserver": 2
  },
  "StatusSNS": {
    "Time": "2026-03-06T01:23:10",
    "ANALOG": {"A3": 176, "A4": 176, "A1": 176, "A2": 176}
  },
  "StatusSTS": {
    "POWER1": "OFF", "POWER2": "OFF", "POWER3": "OFF", "POWER4": "OFF",
    "Wifi": {"SSId": "<SSID>", "RSSI": 44, "Signal": -78}
  }
}
```

`Power` field in `Status`: `"0000"` = all off, `"1000"` = relay 1 on, `"1111"` = all on. Each char is a relay (1=on, 0=off).

### Power0 (relay status)
```json
{"POWER1":"OFF","POWER2":"OFF","POWER3":"OFF","POWER4":"OFF"}
```

### Status 8 (sensors)
```json
{"StatusSNS":{"Time":"2026-03-06T01:23:10","ANALOG":{"A3":176,"A4":176,"A1":176,"A2":176}}}
```

## Dependencies

```bash
# Full install
pip install -e ".[server,dev]"

# Core: pydantic, pyyaml, httpx, aiosqlite, python-dotenv
# Server: fastapi, uvicorn
# Metrics: prometheus-client (optional)
# Redis bus: redis[hiredis] (optional)
# Protocol-specific: pymodbus, paho-mqtt, xknx, BAC0, asyncua, pydnp3, onvif-zeep (optional)
# Dev: pytest, pytest-asyncio, pytest-cov, ruff
# Original scripts: requests, pyserial
# Flashing: esptool
```

## Deployment

```bash
# Docker
docker-compose up

# CLI with all options
python -m core.engine --host 0.0.0.0 --port 8000 --db-path state.db \
  --spaces spaces.yaml --scenes scenes.yaml --log-format json --log-level INFO \
  --event-bus memory --cors-origins "http://localhost:3000"
```

Environment variables: `SMARTSPACES_API_KEYS`, `SMARTSPACES_CORS_ORIGINS`, `SMARTSPACES_EVENT_BUS`, `SMARTSPACES_REDIS_URL`, `KINCONY_IP`

## Tests

413 tests across all components. Run with:
```bash
pytest tests/ -v                                    # all tests
pytest tests/agent/ -v                              # agent gateway (124 tests)
pytest tests/core/ -v                               # core runtime
pytest tests/ --cov=agent --cov=core --cov=sdk      # with coverage
```

## Important Constraints

### Hardware (KC868-A4)
- The device MUST have 12V DC power for relays to physically switch. USB alone powers the ESP32 but not the relay coils.
- WiFi is 2.4GHz only. 5GHz is not supported by the ESP32.
- The Tasmota web UI has NO authentication by default. Set `WebPassword` immediately after setup.
- MQTT is disabled by default. Set `MqttHost` to enable.
- The analog inputs read 0-3.3V. Higher voltages WILL damage the ESP32.
- Maximum 10A per relay channel. Exceeding this will damage the relay contacts.
- The device hostname format is `tasmota-XXXXXX-XXXX` where XXXXXX is derived from the MAC address.

### Software
- Python 3.11+ required
- SQLite state store uses WAL mode — single writer, multiple readers
- API key auth required on all REST endpoints (Bearer or X-API-Key header)
- AI safety guard blocks lock/door_lock capabilities by default
- S3+ safety class devices always require human confirmation for AI writes
