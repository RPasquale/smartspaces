"""
Capture ESP32 boot output after esptool reset.
Opens serial immediately and listens at 115200.
Also tries 74880 (ESP32 boot ROM baud).
"""
import serial
import time

PORT = "COM3"

for baud in [115200, 74880, 9600]:
    print(f"\n{'='*50}")
    print(f"Listening at {baud} baud for 8 seconds...")
    print(f"{'='*50}")

    ser = serial.Serial(PORT, baud, timeout=0.5)

    # Reset via DTR/RTS
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.dtr = True
    time.sleep(0.1)
    ser.dtr = False
    time.sleep(0.1)
    ser.rts = False

    # Listen
    start = time.time()
    all_data = b""
    while time.time() - start < 8:
        chunk = ser.read(256)
        if chunk:
            all_data += chunk

    ser.close()

    if all_data:
        print(f"Received {len(all_data)} bytes")
        print(f"Hex (first 100): {all_data[:100].hex()}")
        try:
            text = all_data.decode('utf-8', errors='replace')
            print(f"Text:\n{text}")
        except:
            print(f"Raw: {all_data}")
    else:
        print("No data received.")

    time.sleep(1)
