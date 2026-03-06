"""Tasmota firmware profile for KinCony boards.

Handles all communication with KinCony boards running Tasmota firmware
via HTTP API. This is the reference firmware profile.
"""

from __future__ import annotations

from typing import Any

import httpx


class TasmotaProfile:
    """Tasmota HTTP API client for KinCony boards."""

    def __init__(self, host: str, password: str | None = None, timeout: float = 5.0):
        self.host = host
        self.password = password
        self.timeout = timeout
        self.base_url = f"http://{host}/cm"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def send_command(self, cmd: str) -> dict[str, Any]:
        """Send a Tasmota command and return the JSON response."""
        client = await self._get_client()
        params: dict[str, str] = {"cmnd": cmd}
        if self.password:
            params["user"] = "admin"
            params["password"] = self.password
        resp = await client.get(self.base_url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # -- Relay operations --

    async def relay_on(self, num: int) -> dict[str, Any]:
        return await self.send_command(f"Power{num} ON")

    async def relay_off(self, num: int) -> dict[str, Any]:
        return await self.send_command(f"Power{num} OFF")

    async def relay_toggle(self, num: int) -> dict[str, Any]:
        return await self.send_command(f"Power{num} TOGGLE")

    async def relay_status(self) -> dict[str, Any]:
        return await self.send_command("Power0")

    async def get_relay_state(self, num: int) -> bool:
        result = await self.send_command(f"Power{num}")
        key = f"POWER{num}"
        return result.get(key, "OFF") == "ON"

    # -- Sensor operations --

    async def read_sensors(self) -> dict[str, Any]:
        result = await self.send_command("Status 8")
        return result.get("StatusSNS", {})

    async def read_analog(self, channel: int) -> int:
        """Read a single analog input. Returns raw ADC value (0-4095)."""
        sensors = await self.read_sensors()
        analog = sensors.get("ANALOG", {})
        return analog.get(f"A{channel}", 0)

    async def read_analog_voltage(self, channel: int) -> float:
        """Read analog input as voltage (0-3.3V)."""
        raw = await self.read_analog(channel)
        return raw * 3.3 / 4095

    # -- Device info --

    async def device_status(self) -> dict[str, Any]:
        return await self.send_command("Status 0")

    async def network_info(self) -> dict[str, Any]:
        return await self.send_command("Status 5")

    async def device_name(self) -> str:
        status = await self.device_status()
        return status.get("Status", {}).get("DeviceName", "Unknown")

    async def firmware_version(self) -> str:
        status = await self.device_status()
        return status.get("StatusFWR", {}).get("Version", "Unknown")

    async def mac_address(self) -> str:
        net = await self.network_info()
        return net.get("StatusNET", {}).get("Mac", "")

    async def wifi_signal(self) -> int:
        status = await self.device_status()
        return status.get("StatusSTS", {}).get("Wifi", {}).get("Signal", 0)

    # -- IR operations --

    async def ir_send(self, protocol: str, bits: int, data: str) -> dict[str, Any]:
        return await self.send_command(
            f'IRsend {{"Protocol":"{protocol}","Bits":{bits},"Data":"{data}"}}'
        )

    # -- System operations --

    async def restart(self) -> dict[str, Any]:
        return await self.send_command("Restart 1")

    async def set_pulse_time(self, relay: int, deciseconds: int) -> dict[str, Any]:
        return await self.send_command(f"PulseTime{relay} {deciseconds}")

    async def backlog(self, *commands: str) -> dict[str, Any]:
        cmd = "Backlog " + "; ".join(commands)
        return await self.send_command(cmd)

    # -- Health check --

    async def ping(self) -> bool:
        """Quick connectivity check."""
        try:
            await self.send_command("Status")
            return True
        except Exception:
            return False
