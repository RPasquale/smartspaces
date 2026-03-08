"""Agent Gateway — connects AI agents to physical devices.

Provides semantic device naming, LLM tool definitions, MCP server,
AI safety controls, scene/automation engine, and a Python client SDK.
"""

from agent.spaces import SpaceRegistry
from agent.tools import ToolGenerator
from agent.safety import AISafetyGuard
from agent.scenes import SceneEngine
from agent.client import SmartSpacesClient

__all__ = [
    "SpaceRegistry",
    "ToolGenerator",
    "AISafetyGuard",
    "SceneEngine",
    "SmartSpacesClient",
]
