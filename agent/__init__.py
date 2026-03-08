"""Agent Gateway — connects AI agents to physical devices.

Provides semantic device naming, LLM tool definitions, MCP server,
AI safety controls, scene/automation engine, event streaming,
natural language intent resolution, device groups, action history,
scheduling, analytics, multi-agent coordination, suggestions,
capability discovery, and a Python client SDK.
"""

from agent.spaces import SpaceRegistry
from agent.tools import ToolGenerator
from agent.safety import AISafetyGuard
from agent.scenes import SceneEngine
from agent.client import SmartSpacesClient
from agent.events import EventStreamManager
from agent.groups import GroupRegistry
from agent.history import ActionHistory
from agent.intent import IntentResolver
from agent.coordination import DeviceCoordinator
from agent.agent_scheduler import ActionScheduler
from agent.analytics import EnergyComfortAnalyzer
from agent.suggestions import ActionSuggester
from agent.discovery import CapabilityDescriber

__all__ = [
    "SpaceRegistry",
    "ToolGenerator",
    "AISafetyGuard",
    "SceneEngine",
    "SmartSpacesClient",
    "EventStreamManager",
    "GroupRegistry",
    "ActionHistory",
    "IntentResolver",
    "DeviceCoordinator",
    "ActionScheduler",
    "EnergyComfortAnalyzer",
    "ActionSuggester",
    "CapabilityDescriber",
]
