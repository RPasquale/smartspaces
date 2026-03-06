"""
Deep investigation of COM3.
1. Raw byte-level sniff at multiple bauds
2. Send ESP32 bootloader sync sequence
3. Try binary protocol probing
4. Check loopback (TX->RX wired?)
"""
import serial
import time
import struct

PORT = "COM3"

def raw_sniff(baud, duration=3):
    """Listen for any bytes at all."""
    ser = serial.Serial(PORT, baud, timeout=0.1)
    ser.reset_input_buffer()
    start = time.time()
    all_data = b""
    while time.time() - start < duration:
        chunk = ser.read(256)
        if chunk:
            all_data += chunk
    ser.close()
    return all_data

def test_loopback(baud):
    """Send bytes and see if they echo back (TX-RX loopback test)."""
    ser = serial.Serial(PORT, baud, timeout=1)
    ser.reset_input_buffer()
    test_bytes = b"LOOPBACK_TEST_12345"
    ser.write(test_bytes)
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting) if ser.in_waiting else b""
    ser.close()
    return resp

def esp32_boot_sync():
    """Send ESP32 bootloader sync packet manually."""
    ser = serial.Serial(PORT, 115200, timeout=1)

    # Put ESP32 into bootloader: hold GPIO0 low during reset
    # DTR -> EN (reset), RTS -> GPIO0 (boot mode)
    ser.dtr = False  # EN high
    ser.rts = True   # GPIO0 low (boot mode)
    time.sleep(0.1)
    ser.dtr = True   # EN low (reset)
    time.sleep(0.1)
    ser.dtr = False   # EN high (release reset, ESP32 boots into bootloader)
    time.sleep(0.5)
    ser.rts = False   # Release GPIO0

    time.sleep(0.5)

    # Read any boot output
    boot_data = b""
    if ser.in_waiting:
        boot_data = ser.read(ser.in_waiting)

    # Send SLIP-encoded sync command
    # ESP32 bootloader sync: 0xC0 [header] 0xC0
    sync_cmd = b'\xc0' + b'\x00\x08\x24\x00\x00\x00\x00\x00' + b'\x07\x07\x12\x20' + (b'\x55' * 32) + b'\xc0'
    ser.write(sync_cmd)
    time.sleep(1)

    resp = b""
    if ser.in_waiting:
        resp = ser.read(ser.in_waiting)

    ser.close()
    return boot_data, resp

def try_all_bauds_sniff():
    """Quick sniff at every common baud rate."""
    bauds = [300, 1200, 2400, 4800, 9600, 14400, 19200, 28800,
             38400, 57600, 74880, 76800, 115200, 230400, 460800, 921600]
    results = {}
    for baud in bauds:
        try:
            data = raw_sniff(baud, duration=1)
            if data:
                results[baud] = data
        except Exception as e:
            results[baud] = f"ERROR: {e}"
    return results

# ==========================================
print("=" * 60)
print("DEEP INVESTIGATION OF COM3")
print("=" * 60)

# Test 1: Loopback
print("\n[1] LOOPBACK TEST (is TX wired to RX?)")
for baud in [9600, 115200]:
    resp = test_loopback(baud)
    if resp:
        print(f"  {baud}: Echo received! -> {resp}")
        if b"LOOPBACK" in resp:
            print("  ** TX and RX are looped! This means serial IS working. **")
    else:
        print(f"  {baud}: No echo (normal - TX/RX not looped)")
    time.sleep(0.3)

# Test 2: Sniff all baud rates
print("\n[2] SNIFFING ALL BAUD RATES (1 sec each)...")
results = try_all_bauds_sniff()
if results:
    for baud, data in results.items():
        print(f"  {baud}: {data}")
else:
    print("  No data received at any baud rate.")

# Test 3: ESP32 bootloader sync
print("\n[3] ESP32 BOOTLOADER SYNC (DTR/RTS reset into boot mode)...")
boot_data, sync_resp = esp32_boot_sync()
if boot_data:
    print(f"  Boot output: {boot_data}")
    try:
        print(f"  Decoded: {boot_data.decode('utf-8', errors='replace')}")
    except:
        pass
else:
    print("  No boot output.")
if sync_resp:
    print(f"  Sync response: {sync_resp}")
    print(f"  Hex: {sync_resp.hex()}")
else:
    print("  No sync response.")

# Test 4: Check pin states
print("\n[4] SERIAL PIN STATES")
ser = serial.Serial(PORT, 115200, timeout=1)
print(f"  CTS: {ser.cts}")
print(f"  DSR: {ser.dsr}")
print(f"  RI:  {ser.ri}")
print(f"  CD:  {ser.cd}")
ser.close()

# Test 5: Send break and see if anything wakes up
print("\n[5] SENDING BREAK SIGNAL...")
ser = serial.Serial(PORT, 115200, timeout=2)
ser.send_break(duration=0.5)
time.sleep(2)
if ser.in_waiting:
    data = ser.read(ser.in_waiting)
    print(f"  Response after break: {data}")
else:
    print("  No response after break.")
ser.close()

print("\n" + "=" * 60)
print("INVESTIGATION COMPLETE")
print("=" * 60)
