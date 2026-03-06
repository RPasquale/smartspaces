"""
Extended probe for Kincony A4.
- Toggles DTR/RTS to reset the ESP32 and capture boot output
- Tries different line endings
- Longer listen window
"""
import serial
import time

PORT = "COM3"
BAUD = 115200

print(f"Opening {PORT} at {BAUD} baud...")
ser = serial.Serial(PORT, BAUD, timeout=3)

# Toggle DTR to reset ESP32 (like Arduino reset)
print("\n[1] Resetting ESP32 via DTR toggle...")
ser.dtr = False
time.sleep(0.1)
ser.dtr = True
time.sleep(0.1)
ser.dtr = False

# Also try RTS
ser.rts = False
time.sleep(0.1)
ser.rts = True
time.sleep(0.1)
ser.rts = False

# Wait for boot messages
print("Waiting 5 seconds for boot output...")
time.sleep(5)

if ser.in_waiting:
    data = ser.read(ser.in_waiting)
    print(f"\nReceived {len(data)} bytes:")
    print(f"  Raw: {data}")
    try:
        print(f"  Text: {data.decode('utf-8', errors='replace')}")
    except:
        pass
else:
    print("  No boot output received.")

# Try different line endings and commands
print("\n[2] Sending commands with different line endings...")
line_endings = {
    "\\r\\n": b"\r\n",
    "\\n": b"\n",
    "\\r": b"\r",
}

commands = [
    b"RELAY-SET-255,1,1",    # Turn on relay 1
    b"RELAY-STATE-255",       # Query relay state
    b"AT",
    b"+++",                   # AT escape sequence
]

for end_name, end_bytes in line_endings.items():
    print(f"\n  Line ending: {end_name}")
    for cmd in commands:
        ser.reset_input_buffer()
        full_cmd = cmd + end_bytes
        ser.write(full_cmd)
        print(f"    Sent: {cmd}", end="")
        time.sleep(1)
        if ser.in_waiting:
            resp = ser.read(ser.in_waiting)
            print(f" -> {resp}")
        else:
            print(f" -> (no response)")

# Try just listening for a while
print("\n[3] Passive listen for 5 seconds...")
ser.reset_input_buffer()
time.sleep(5)
if ser.in_waiting:
    data = ser.read(ser.in_waiting)
    print(f"  Got: {data}")
else:
    print("  Nothing received.")

# Try at 76800 baud (some ESP32 boot loaders use this)
ser.close()
print("\n[4] Trying 76800 baud (ESP32 boot ROM)...")
ser = serial.Serial(PORT, 76800, timeout=2)
ser.dtr = False
time.sleep(0.1)
ser.dtr = True
time.sleep(2)
if ser.in_waiting:
    data = ser.read(ser.in_waiting)
    print(f"  Boot ROM output: {data}")
else:
    print("  No output at 76800.")

ser.close()
print("\nDone.")
