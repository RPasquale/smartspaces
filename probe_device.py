"""
Probe Kincony A4 ESP32 board on COM3.
Tries serial at common baud rates, sends basic commands to see what responds.
"""
import serial
import time

PORT = "COM3"

def probe(baud):
    print(f"\n--- Trying {baud} baud ---")
    try:
        ser = serial.Serial(PORT, baud, timeout=2)
        time.sleep(1)  # Give ESP32 time after connection

        # Check if anything is already in the buffer
        if ser.in_waiting:
            data = ser.read(ser.in_waiting)
            print(f"  Boot data: {data}")

        # Try common ESP32 / Kincony commands
        commands = [
            b"\r\n",           # Empty line - might trigger a prompt
            b"AT\r\n",         # AT command
            b"RELAY-STATE-255\r\n",  # Kincony relay state query
            b"RELAY-READ-255\r\n",   # Kincony read all relays
            b"help\r\n",       # Some firmwares respond to help
        ]

        for cmd in commands:
            ser.reset_input_buffer()
            ser.write(cmd)
            print(f"  Sent: {cmd.strip()}")
            time.sleep(0.5)

            if ser.in_waiting:
                resp = ser.read(ser.in_waiting)
                print(f"  Response: {resp}")
                try:
                    print(f"  Decoded:  {resp.decode('utf-8', errors='replace')}")
                except:
                    pass
            else:
                print(f"  No response")

        ser.close()
    except Exception as e:
        print(f"  Error: {e}")


print("=" * 50)
print(f"Probing Kincony A4 on {PORT}")
print("=" * 50)

for baud in [115200, 9600, 19200]:
    probe(baud)
    time.sleep(0.5)

print("\n" + "=" * 50)
print("Probe complete.")
