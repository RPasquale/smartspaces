"""Agent SDK Client — simple Python interface for AI agents to control devices.

Usage:
    from agent.client import SmartSpacesClient

    ss = SmartSpacesClient(base_url="http://localhost:8000", api_key="...")

    # Discover what's available
    spaces = ss.list_spaces()
    devices = ss.list_devices(space="living_room")

    # Read state
    state = ss.get_state("living_room.ceiling_light")

    # Control devices
    ss.set_device("living_room.ceiling_light", "on")
    ss.set_device("living_room.ceiling_light", "set", value=50)

    # Scenes
    ss.activate_scene("movie_mode")
    ss.create_scene("bedtime", "Bedtime", [
        {"device": "living_room.ceiling_light", "action": "off"},
        {"device": "bedroom.lamp", "action": "set", "value": 10},
    ])

    # Get LLM tool definitions for your agent framework
    tools = ss.get_tool_definitions(format="openai")
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SmartSpacesClient:
    """Synchronous Python client for the SmartSpaces Agent API.

    Wraps the REST API with semantic device names and
    provides tool definitions for LLM integration.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # -- Space & device discovery --

    def list_spaces(self) -> list[dict[str, Any]]:
        """List all spaces (rooms/zones)."""
        return self._get("/api/agent/spaces")["spaces"]

    def list_devices(
        self,
        space: str | None = None,
        capability: str | None = None,
    ) -> list[dict[str, Any]]:
        """List devices, optionally filtered by space or capability."""
        params = {}
        if space:
            params["space"] = space
        if capability:
            params["capability"] = capability
        return self._get("/api/agent/devices", params=params)["devices"]

    # -- State reading --

    def get_state(self, device: str) -> dict[str, Any]:
        """Read the current state of a device by semantic name."""
        return self._post("/api/agent/state", {"device": device})

    def get_space_summary(self, space: str) -> dict[str, Any]:
        """Get states of all devices in a space."""
        return self._post("/api/agent/space_summary", {"space": space})

    # -- Device control --

    def set_device(
        self,
        device: str,
        action: str,
        value: Any = None,
    ) -> dict[str, Any]:
        """Control a device.

        Args:
            device: Semantic name (e.g. "living_room.light")
            action: on, off, toggle, set, open, close
            value: Optional value (brightness, temperature, etc.)
        """
        payload: dict[str, Any] = {"device": device, "action": action}
        if value is not None:
            payload["value"] = value
        return self._post("/api/agent/set", payload)

    # -- Scenes --

    def list_scenes(self) -> list[dict[str, Any]]:
        """List available scenes."""
        return self._get("/api/agent/scenes")["scenes"]

    def activate_scene(self, scene: str) -> dict[str, Any]:
        """Activate a named scene."""
        return self._post("/api/agent/scenes/activate", {"scene": scene})

    def create_scene(
        self,
        name: str,
        display_name: str,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a new scene."""
        return self._post("/api/agent/scenes", {
            "name": name,
            "display_name": display_name,
            "actions": actions,
        })

    # -- Rules --

    def list_rules(self) -> list[dict[str, Any]]:
        """List automation rules."""
        return self._get("/api/agent/rules")["rules"]

    def create_rule(
        self,
        name: str,
        display_name: str,
        condition: dict[str, Any],
        actions: list[dict[str, Any]],
        cooldown_sec: float = 60.0,
    ) -> dict[str, Any]:
        """Create an automation rule."""
        return self._post("/api/agent/rules", {
            "name": name,
            "display_name": display_name,
            "condition": condition,
            "actions": actions,
            "cooldown_sec": cooldown_sec,
        })

    # -- Tool definitions for LLM frameworks --

    def get_tool_definitions(self, format: str = "openai") -> list[dict[str, Any]]:
        """Get tool definitions for an LLM framework.

        Args:
            format: "openai", "anthropic", or "mcp"
        """
        return self._get(f"/api/agent/tools/{format}")["tools"]

    # -- Confirmations (for human-in-the-loop) --

    def list_pending_confirmations(self) -> list[dict[str, Any]]:
        """List operations waiting for human confirmation."""
        return self._get("/api/agent/confirmations")["confirmations"]

    def approve_confirmation(self, confirmation_id: str) -> dict[str, Any]:
        """Approve a pending confirmation."""
        return self._post(f"/api/agent/confirmations/{confirmation_id}/approve", {})

    def deny_confirmation(self, confirmation_id: str) -> dict[str, Any]:
        """Deny a pending confirmation."""
        return self._post(f"/api/agent/confirmations/{confirmation_id}/deny", {})

    # -- Internal --

    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=data)
        resp.raise_for_status()
        return resp.json()


class AsyncSmartSpacesClient:
    """Async version of SmartSpacesClient for use in async agent frameworks."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def list_spaces(self) -> list[dict[str, Any]]:
        return (await self._get("/api/agent/spaces"))["spaces"]

    async def list_devices(self, space: str | None = None, capability: str | None = None) -> list[dict[str, Any]]:
        params = {}
        if space:
            params["space"] = space
        if capability:
            params["capability"] = capability
        return (await self._get("/api/agent/devices", params=params))["devices"]

    async def get_state(self, device: str) -> dict[str, Any]:
        return await self._post("/api/agent/state", {"device": device})

    async def set_device(self, device: str, action: str, value: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"device": device, "action": action}
        if value is not None:
            payload["value"] = value
        return await self._post("/api/agent/set", payload)

    async def activate_scene(self, scene: str) -> dict[str, Any]:
        return await self._post("/api/agent/scenes/activate", {"scene": scene})

    async def get_tool_definitions(self, format: str = "openai") -> list[dict[str, Any]]:
        return (await self._get(f"/api/agent/tools/{format}"))["tools"]

    async def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(path, json=data)
        resp.raise_for_status()
        return resp.json()
