# Kincony KC868-A4 — Home Automation Controller Spec

## Overview

The KC868-A4 is an ESP32-based smart home controller running Tasmota firmware.
It provides 4 relays, 4 digital inputs, 4 analog inputs, IR send/receive, and RF send/receive.
It communicates over WiFi via HTTP API, MQTT, serial, and WebSocket.

---

## Hardware

| Component         | Detail                                      |
|-------------------|---------------------------------------------|
| MCU               | ESP32-D0WD-V3 (rev 3.1), Dual Core, 240MHz |
| Flash             | 4MB, DIO mode, 80MHz                        |
| RAM               | ~150KB free heap                             |
| WiFi              | 802.11 b/g/n, 2.4GHz                        |
| Bluetooth         | BLE 4.2 (available but unused by default)    |
| USB-Serial        | CH340 (for flashing and serial commands)     |
| Power Input       | 12V DC (required for relay operation)        |
| Relay Rating      | 4x NO/NC, 10A @ 250V AC each                |
| Digital Inputs    | 4x optocoupler isolated                      |
| Analog Inputs     | 4x ADC (0-3.3V range)                        |
| IR                | Send + Receive (38kHz)                        |
| RF                | 433MHz Send + Receive                         |
| DAC Outputs       | 2x (GPIO25, GPIO26) — 1-10V via Berry script |
| Temperature       | 1-wire terminal available                     |
| Buzzer            | Onboard                                       |
| Button            | S2 button (GPIO0) — mapped to Button1         |

---

## Network Identity

| Field      | Value                                          |
|------------|------------------------------------------------|
| Hostname   | `tasmota-XXXXXX-XXXX`                          |
| IP Address | `<DEVICE_IP>` (DHCP assigned)                  |
| MAC        | `<DEVICE_MAC>`                                 |
| Gateway    | `<GATEWAY_IP>`                                 |
| Subnet     | `255.255.255.0`                                |
| WiFi SSID  | `<YOUR_WIFI_SSID>` (2.4GHz)                   |
| Web UI     | http://`<DEVICE_IP>`                           |
| MQTT Topic | `tasmota_XXXXXX`                               |

> **Note**: IP is DHCP-assigned. Consider setting a static lease on your router for your device's MAC → a fixed IP.
>
> After flashing, find your device's actual values via serial: `Status 5`

---

## Firmware

| Field        | Value                                  |
|--------------|----------------------------------------|
| Firmware     | Tasmota 15.3.0 (release-tasmota32)     |
| Build Date   | 2026-02-19                             |
| ESP-IDF      | 5.3.4                                  |
| Arduino Core | 3.3.7                                  |
| OTA URL      | http://ota.tasmota.com/tasmota32/release/tasmota32.bin |
| Serial Baud  | 115200 (8N1)                           |
| USB Port     | COM3 (CH340, VID:1A86 PID:7523)        |

### Backup

Original factory firmware is backed up at:
```
kinco_serial/full_backup.bin    # Full 4MB flash dump
```

To restore original firmware:
```bash
python -m esptool --port COM3 write_flash 0x0 full_backup.bin
```

---

## GPIO Pin Map

| GPIO   | Function      | Tasmota Code | Description                     |
|--------|---------------|--------------|---------------------------------|
| GPIO0  | Button1       | 32           | Onboard S2 button               |
| GPIO2  | Relay4        | 227          | Relay channel 4 (NO/NC, 10A)    |
| GPIO4  | Relay1        | 224          | Relay channel 1 (NO/NC, 10A)    |
| GPIO5  | Relay2        | 225          | Relay channel 2 (NO/NC, 10A)    |
| GPIO9  | IRsend        | 1312         | Infrared transmitter             |
| GPIO11 | Relay3        | 226          | Relay channel 3 (NO/NC, 10A)    |
| GPIO14 | IRrecv        | 480          | Infrared receiver (38kHz)        |
| GPIO15 | Switch4       | 1152         | Digital input 4 (opto-isolated)  |
| GPIO17 | Switch3       | 1120         | Digital input 3 (opto-isolated)  |
| GPIO18 | Switch1       | 1056         | Digital input 1 (opto-isolated)  |
| GPIO19 | Switch2       | 1088         | Digital input 2 (opto-isolated)  |
| GPIO25 | DAC1          | —            | Analog output 1 (1-10V)          |
| GPIO26 | DAC2          | —            | Analog output 2 (1-10V)          |
| GPIO28 | ADC_Input3    | 4706         | Analog input 3                   |
| GPIO29 | ADC_Input4    | 4707         | Analog input 4                   |
| GPIO30 | ADC_Input1    | 4704         | Analog input 1                   |
| GPIO31 | ADC_Input2    | 4705         | Analog input 2                   |

### Tasmota Template (for reimport)

```json
{"NAME":"KC868-A4","GPIO":[32,0,227,0,224,225,0,0,0,1312,1,226,0,0,480,1152,0,1120,1056,1088,0,1,1,1,0,0,0,0,4706,4707,4704,4705,1,0,0,1],"FLAG":0,"BASE":1}
```

---

## Communication Protocols

### 1. HTTP API (primary)

Base URL: `http://<DEVICE_IP>/cm`

All commands are sent as GET requests with `cmnd` parameter.

```
GET http://<DEVICE_IP>/cm?cmnd=<COMMAND>
```

Response is always JSON.

#### Relay Control

| Action             | Command              | Response                    |
|--------------------|----------------------|-----------------------------|
| Relay 1 ON         | `Power1 ON`          | `{"POWER1":"ON"}`           |
| Relay 1 OFF        | `Power1 OFF`         | `{"POWER1":"OFF"}`          |
| Relay 1 Toggle     | `Power1 TOGGLE`      | `{"POWER1":"ON"}`           |
| All relay status   | `Power0`             | `{"POWER1":"OFF",...}`      |
| All relays ON      | `Backlog Power1 ON; Power2 ON; Power3 ON; Power4 ON` | |
| All relays OFF     | `Backlog Power1 OFF; Power2 OFF; Power3 OFF; Power4 OFF` | |
| Pulse relay 1 (2s) | `PulseTime1 20`     | Relay turns off after 2s    |

#### Sensor / Input Reading

| Action              | Command       | Response                                 |
|---------------------|---------------|------------------------------------------|
| All sensor data     | `Status 8`    | `{"StatusSNS":{"ANALOG":{"A1":x,...}}}`  |
| Digital input state | `Status 10`   | Includes switch states                   |
| Full device status  | `Status 0`    | Everything                               |

#### IR Commands

| Action        | Command                          | Notes                         |
|---------------|----------------------------------|-------------------------------|
| Send IR       | `IRsend {"Protocol":"NEC","Bits":32,"Data":"0x20DF10EF"}` | NEC, Sony, RC5, etc. |
| Last received | Check serial log or MQTT          | IRrecv outputs to log         |

#### System Commands

| Action           | Command         | Response                        |
|------------------|-----------------|---------------------------------|
| Device info      | `Status 0`      | Full JSON status                |
| Network info     | `Status 5`      | IP, MAC, hostname               |
| Restart device   | `Restart 1`     | Reboots                        |
| OTA update       | `Upgrade 1`     | Downloads and flashes latest    |
| Set device name  | `DeviceName MyRelay` |                             |
| Set timezone     | `Timezone +10`  | AEST (adjust to your zone)      |
| Backlog (multi)  | `Backlog Cmd1; Cmd2; Cmd3` | Execute multiple commands |

### 2. MQTT

Not configured by default. To enable:

```
# Configure broker
MqttHost <broker_ip>
MqttPort 1883
MqttUser <username>        # optional
MqttPassword <password>    # optional
```

#### MQTT Topics

| Topic                                  | Direction | Payload         |
|----------------------------------------|-----------|-----------------|
| `cmnd/<DEVICE_TOPIC>/POWER1`           | →         | `ON` / `OFF` / `TOGGLE` |
| `stat/<DEVICE_TOPIC>/POWER1`           | ←         | `ON` / `OFF`    |
| `stat/<DEVICE_TOPIC>/RESULT`           | ←         | JSON result     |
| `tele/<DEVICE_TOPIC>/STATE`            | ←         | Periodic state (every 300s) |
| `tele/<DEVICE_TOPIC>/SENSOR`           | ←         | Sensor data     |
| `tele/<DEVICE_TOPIC>/LWT`              | ←         | `Online` / `Offline` |
| `cmnd/<DEVICE_TOPIC>/Status`           | →         | `0` (full status) |

### 3. Serial (USB)

Connect via CH340 USB adapter at **115200 baud, 8N1**.

```python
import serial
ser = serial.Serial('COM3', 115200, timeout=2)
ser.write(b'Power1 ON\r\n')
response = ser.readline()
```

Commands are identical to HTTP API — just send them as text with `\r\n`.

### 4. WebSocket

Connect to `ws://<DEVICE_IP>:80/ws` for real-time bidirectional communication.

---

## Python Integration

### Requirements

```
pip install requests
pip install pyserial     # for serial control
pip install paho-mqtt    # for MQTT control
```

### HTTP Control (simplest)

```python
import requests

DEVICE = "http://<DEVICE_IP>/cm"  # replace with your device's IP

def cmd(command):
    return requests.get(DEVICE, params={"cmnd": command}, timeout=5).json()

# Relay control
cmd("Power1 ON")           # Turn on relay 1
cmd("Power1 OFF")          # Turn off relay 1
cmd("Power1 TOGGLE")       # Toggle relay 1
cmd("Power0")              # Get all relay states

# Read analog sensors
data = cmd("Status 8")
analogs = data["StatusSNS"]["ANALOG"]
# {"A1": 176, "A2": 176, "A3": 176, "A4": 176}

# Send IR command (e.g., NEC TV power)
cmd('IRsend {"Protocol":"NEC","Bits":32,"Data":"0x20DF10EF"}')

# Batch commands
cmd("Backlog Power1 ON; Power2 ON; Delay 20; Power1 OFF; Power2 OFF")
```

### MQTT Control (for event-driven systems)

```python
import paho.mqtt.client as mqtt
import json

BROKER = "<MQTT_BROKER_IP>"  # your MQTT broker
TOPIC_BASE = "<DEVICE_TOPIC>"  # e.g. "tasmota_XXXXXX"

def on_connect(client, userdata, flags, rc, properties=None):
    # Subscribe to all state updates
    client.subscribe(f"stat/{TOPIC_BASE}/#")
    client.subscribe(f"tele/{TOPIC_BASE}/#")

def on_message(client, userdata, msg):
    print(f"{msg.topic}: {msg.payload.decode()}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, 1883)

# Control relay
client.publish(f"cmnd/{TOPIC_BASE}/Power1", "ON")

client.loop_forever()
```

### Serial Control (direct USB, no WiFi needed)

```python
import serial
import json

ser = serial.Serial('COM3', 115200, timeout=2)

def cmd(command):
    ser.reset_input_buffer()
    ser.write(f'{command}\r\n'.encode())
    lines = []
    while True:
        line = ser.readline().decode(errors='replace').strip()
        if not line:
            break
        lines.append(line)
    # Parse the JSON from the RSL line
    for line in lines:
        if 'RSL:' in line:
            json_str = line.split('RSL: RESULT = ')[-1]
            return json.loads(json_str)
    return lines

cmd("Power1 ON")
cmd("Power0")
```

---

## Wiring Reference

### Relay Outputs (NO/NC)

Each relay has 3 terminals: **COM** (common), **NO** (normally open), **NC** (normally closed).

```
Relay 1 (GPIO4)  →  Terminals: COM1, NO1, NC1
Relay 2 (GPIO5)  →  Terminals: COM2, NO2, NC2
Relay 3 (GPIO11) →  Terminals: COM3, NO3, NC3
Relay 4 (GPIO2)  →  Terminals: COM4, NO4, NC4
```

- **NO (normally open)**: Circuit is OPEN when relay is OFF, CLOSED when ON
- **NC (normally closed)**: Circuit is CLOSED when relay is OFF, OPEN when ON
- Max load: **10A @ 250V AC** or **10A @ 30V DC** per channel

### Digital Inputs (opto-isolated)

```
Input 1 (GPIO18) →  Terminal: IN1 + GND
Input 2 (GPIO19) →  Terminal: IN2 + GND
Input 3 (GPIO17) →  Terminal: IN3 + GND
Input 4 (GPIO15) →  Terminal: IN4 + GND
```

- Voltage range: **5-24V DC**
- Opto-isolated (safe to connect to external circuits)
- Use for: door sensors, motion sensors, physical switches, dry contacts

### Analog Inputs

```
Input A1 (GPIO30) →  Terminal: A1 + GND
Input A2 (GPIO31) →  Terminal: A2 + GND
Input A3 (GPIO28) →  Terminal: A3 + GND
Input A4 (GPIO29) →  Terminal: A4 + GND
```

- Voltage range: **0-3.3V** (use voltage divider for higher voltages)
- Resolution: 12-bit (0-4095)
- Use for: light sensors, temperature sensors, potentiometers

### DAC Outputs

```
DA1 (GPIO25) →  Terminal: DA1 + GND
DA2 (GPIO26) →  Terminal: DA2 + GND
```

- Output: 1-10V (with onboard amplifier circuit)
- Control via Berry script: `gpio.dac_voltage(25, 5000)` for 5V output

### Power

```
12V DC Input →  Terminal: +12V and GND
```

- Required for relay coil operation
- ESP32 is also powered from 12V via onboard regulator
- USB provides power to ESP32 only (relays won't click without 12V)

---

## Automation Rules (Tasmota Rules Engine)

Tasmota has a built-in rules engine for local automation (no server needed).

### Example: Input triggers relay

```
# When digital input 1 goes HIGH, turn on relay 1
Rule1 ON Switch1#State=1 DO Power1 ON ENDON ON Switch1#State=0 DO Power1 OFF ENDON
Rule1 1
```

### Example: Timer-based control

```
# Turn on relay 2 at 07:00, off at 23:00
Timer1 {"Enable":1,"Mode":0,"Time":"07:00","Days":"1111111","Repeat":1,"Action":1,"Output":2}
Timer2 {"Enable":1,"Mode":0,"Time":"23:00","Days":"1111111","Repeat":1,"Action":0,"Output":2}
```

### Example: Analog threshold trigger

```
# If analog input A1 goes above 2000, turn on relay 3
Rule2 ON ANALOG#A1>2000 DO Power3 ON ENDON ON ANALOG#A1<1000 DO Power3 OFF ENDON
Rule2 1
```

### Example: Pulse mode (momentary relay)

```
# Relay 1 turns off automatically after 2 seconds
PulseTime1 20   # units of 0.1s, so 20 = 2 seconds
```

---

## Adding More Devices

To add another KC868-A4 (or any Tasmota device) to your system:

1. Flash Tasmota via USB: `python -m esptool --port COMx --baud 921600 write_flash 0x0 tasmota32.factory.bin`
2. Connect to its AP: `tasmota-XXXXXX-XXXX`
3. Configure WiFi via http://192.168.4.1
4. Apply template via serial or HTTP:
   ```
   Template {"NAME":"KC868-A4","GPIO":[32,0,227,0,224,225,0,0,0,1312,1,226,0,0,480,1152,0,1120,1056,1088,0,1,1,1,0,0,0,0,4706,4707,4704,4705,1,0,0,1],"FLAG":0,"BASE":1}
   Module 0
   ```
5. Set a unique topic: `Topic my_device_name`

Each device gets its own MQTT topic and HTTP endpoint.

---

## Troubleshooting

| Problem                     | Solution                                                    |
|-----------------------------|-------------------------------------------------------------|
| Can't reach device          | Check IP with `arp -a`, look for your device's MAC          |
| Relays don't click          | Connect 12V DC power supply                                 |
| WiFi lost                   | Device creates AP `tasmota-XXXXXX-XXXX` after 2 min         |
| Need to reflash             | Hold GPIO0 button, plug USB, run esptool                    |
| Restore original firmware   | `python -m esptool --port COM3 write_flash 0x0 full_backup.bin` |
| Factory reset Tasmota       | `Reset 1` via serial or press button 6 times quickly        |
| Check serial output         | `python -c "import serial; s=serial.Serial('COM3',115200); [print(s.readline()) for _ in range(20)]"` |

---

## File Inventory

```
kinco_serial/
├── SPEC.md                     # This file
├── kincony_control.py          # Python HTTP control script
├── probe_device.py             # Serial probe script
├── probe_v2.py                 # Extended probe script (DTR reset)
├── deep_probe.py               # Deep investigation script
├── at_test.py                  # AT command test script
├── capture_boot.py             # Boot capture script
└── .gitignore                  # Excludes binaries and sensitive files
```

> Binary dumps (firmware backups, flash dumps, .bin files) are excluded from the repo via `.gitignore`.
> Keep `full_backup.bin` locally if you ever need to restore the original firmware.

---

## Security Recommendations

1. **Set a web password** — by default Tasmota has no auth:
   ```
   WebPassword <your_password>
   ```
2. **Set a static IP** via your router's DHCP reservation
3. **Isolate IoT devices** on a separate VLAN/subnet if possible
4. **Disable unused services** — if not using MQTT: leave MqttHost blank
5. **Keep firmware updated** — `Upgrade 1` via serial or HTTP
6. **Use MQTT TLS** if exposing MQTT outside your LAN

---

## Quick Reference Card

```
USB:          COM3, 115200 baud (may vary)
POWER:        12V DC required

RELAY 1 ON:   curl "http://<DEVICE_IP>/cm?cmnd=Power1%20ON"
RELAY 1 OFF:  curl "http://<DEVICE_IP>/cm?cmnd=Power1%20OFF"
RELAY 2 ON:   curl "http://<DEVICE_IP>/cm?cmnd=Power2%20ON"
ALL STATUS:   curl "http://<DEVICE_IP>/cm?cmnd=Power0"
SENSORS:      curl "http://<DEVICE_IP>/cm?cmnd=Status%208"
FULL STATUS:  curl "http://<DEVICE_IP>/cm?cmnd=Status%200"
```
