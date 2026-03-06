"""
Kincony A4 Controller via Tasmota HTTP API.
Controls 4 relays and reads 4 digital inputs.
"""
import os
import requests
import sys
import time

DEVICE_IP = os.environ.get("KINCONY_IP", "192.168.0.90")
BASE_URL = f"http://{DEVICE_IP}/cm"


def send_command(cmd):
    """Send a Tasmota command and return the JSON response."""
    resp = requests.get(BASE_URL, params={"cmnd": cmd}, timeout=5)
    return resp.json()


def relay_on(num):
    """Turn on relay 1-4."""
    return send_command(f"Power{num} ON")


def relay_off(num):
    """Turn off relay 1-4."""
    return send_command(f"Power{num} OFF")


def relay_toggle(num):
    """Toggle relay 1-4."""
    return send_command(f"Power{num} TOGGLE")


def relay_status():
    """Get status of all 4 relays."""
    return send_command("Power0")


def device_info():
    """Get device info."""
    data = send_command("Status 0")
    status = data.get("Status", {})
    net = data.get("StatusNET", {})
    sts = data.get("StatusSTS", {})
    wifi = sts.get("Wifi", {})
    return {
        "device": status.get("DeviceName"),
        "ip": net.get("IPAddress"),
        "mac": net.get("Mac"),
        "wifi_ssid": wifi.get("SSId"),
        "wifi_signal": wifi.get("Signal"),
        "uptime": sts.get("Uptime"),
        "power": status.get("Power"),
    }


def demo():
    """Run a demo: show info, toggle each relay."""
    print("=== Kincony A4 Controller ===\n")

    info = device_info()
    for k, v in info.items():
        print(f"  {k}: {v}")

    print(f"\nRelay status: {relay_status()}\n")

    for i in range(1, 5):
        print(f"  Relay {i} ON  -> {relay_on(i)}")
        time.sleep(0.5)

    print(f"\n  All ON: {relay_status()}\n")
    time.sleep(1)

    for i in range(1, 5):
        print(f"  Relay {i} OFF -> {relay_off(i)}")
        time.sleep(0.5)

    print(f"\n  All OFF: {relay_status()}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        demo()
    else:
        cmd = " ".join(sys.argv[1:])
        print(send_command(cmd))
