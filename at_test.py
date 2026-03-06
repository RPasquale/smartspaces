"""
Test ESP-AT commands on the Kincony A4.
The firmware uses Espressif AT command set.
"""
import serial
import time

PORT = "COM3"

def at_test(baud):
    print(f"\n{'='*50}")
    print(f"Testing AT commands at {baud} baud")
    print(f"{'='*50}")

    ser = serial.Serial(PORT, baud, timeout=2)
    time.sleep(0.5)

    # Drain any pending data
    if ser.in_waiting:
        old = ser.read(ser.in_waiting)
        print(f"  Pending data: {old}")

    commands = [
        "AT",                # Basic test
        "AT+GMR",            # Get version info
        "AT+RST",            # Reset
    ]

    for cmd in commands:
        ser.reset_input_buffer()
        full = (cmd + "\r\n").encode()
        ser.write(full)
        print(f"\n  >>> {cmd}")

        # Wait and collect response
        time.sleep(1.5 if cmd == "AT+RST" else 0.5)

        response = b""
        while ser.in_waiting:
            response += ser.read(ser.in_waiting)
            time.sleep(0.1)

        if response:
            print(f"  <<< Raw: {response}")
            try:
                print(f"  <<< Text: {response.decode('utf-8', errors='replace')}")
            except:
                pass
            ser.close()
            return True
        else:
            print(f"  <<< (no response)")

    ser.close()
    return False

# The ESP-AT firmware defaults vary. Try common ones.
for baud in [115200, 9600, 19200, 38400, 57600, 230400, 460800, 921600, 74880, 2400000]:
    try:
        if at_test(baud):
            print(f"\n*** DEVICE RESPONDED AT {baud} BAUD! ***")
            break
    except Exception as e:
        print(f"  Error at {baud}: {e}")
    time.sleep(0.3)
else:
    print("\n\nNo response at any baud rate.")
    print("The CH340 USB may connect to a DIFFERENT UART than the AT command port.")
    print("On many Kincony boards:")
    print("  - USB/CH340 -> UART0 (used by bootloader, but firmware may not use it)")
    print("  - AT commands -> UART1 or UART2 (directly to the main MCU)")
