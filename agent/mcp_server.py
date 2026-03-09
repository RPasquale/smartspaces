"""MCP (Model Context Protocol) Server for SmartSpaces.

Exposes the agent tools as an MCP server that Claude Desktop,
Claude Code, or any MCP-compatible client can connect to.

Run standalone:
    python -m agent.mcp_server --spaces spaces.yaml --port 8100

Or integrate into the engine:
    engine.start_mcp_server()
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from agent.safety import AISafetyGuard, SafetyConfig
from agent.scenes import SceneEngine
from agent.spaces import SpaceRegistry
from agent.tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)


class MCPServer:
    """Minimal MCP server using stdio JSON-RPC transport.

    Implements the MCP protocol subset needed for tool discovery and execution:
    - initialize
    - tools/list
    - tools/call
    - resources/list (device context)
    """

    def __init__(
        self,
        space_registry: SpaceRegistry,
        safety_guard: AISafetyGuard,
        scene_engine: SceneEngine,
        tool_executor: ToolExecutor,
    ):
        self.spaces = space_registry
        self.safety = safety_guard
        self.scenes = scene_engine
        self.executor = tool_executor
        self._initialized = False

    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle a single JSON-RPC message and return the response."""
        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params", {})

        if method == "initialize":
            return self._respond(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {
                    "name": "smartspaces",
                    "version": "0.1.0",
                },
            })

        if method == "notifications/initialized":
            self._initialized = True
            return {}  # No response for notifications

        if method == "tools/list":
            tools = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["parameters"],
                }
                for t in TOOL_DEFINITIONS
            ]
            return self._respond(msg_id, {"tools": tools})

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self.executor.call(tool_name, arguments)
            return self._respond(msg_id, {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, indent=2, default=str),
                    }
                ],
            })

        if method == "resources/list":
            resources = [
                {
                    "uri": "smartspaces://devices",
                    "name": "Device Registry",
                    "description": "All available devices and their current configuration",
                    "mimeType": "text/plain",
                },
                {
                    "uri": "smartspaces://scenes",
                    "name": "Scenes",
                    "description": "Available scenes and automation rules",
                    "mimeType": "application/json",
                },
                {
                    "uri": "smartspaces://network",
                    "name": "Network Discovery",
                    "description": "Scan the local network for smart devices",
                    "mimeType": "application/json",
                },
            ]
            return self._respond(msg_id, {"resources": resources})

        if method == "resources/read":
            uri = params.get("uri", "")
            if uri == "smartspaces://devices":
                return self._respond(msg_id, {
                    "contents": [{
                        "uri": uri,
                        "mimeType": "text/plain",
                        "text": self.spaces.to_context_prompt(),
                    }],
                })
            elif uri == "smartspaces://scenes":
                return self._respond(msg_id, {
                    "contents": [{
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps({
                            "scenes": self.scenes.list_scenes(),
                            "rules": self.scenes.list_rules(),
                        }, indent=2),
                    }],
                })
            elif uri == "smartspaces://network":
                # Run a quick network scan
                result = await self.executor.call("discover_devices", {"timeout": 10})
                return self._respond(msg_id, {
                    "contents": [{
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(result, indent=2, default=str),
                    }],
                })
            return self._error(msg_id, -32602, f"Unknown resource: {uri}")

        return self._error(msg_id, -32601, f"Method not found: {method}")

    def _respond(self, msg_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _error(self, msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    async def run_stdio(self) -> None:
        """Run the MCP server over stdin/stdout (standard MCP transport)."""
        logger.info("MCP server starting on stdio")

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break

                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                message = json.loads(line_str)
                response = await self.handle_message(message)

                if response:
                    out = json.dumps(response) + "\n"
                    sys.stdout.buffer.write(out.encode("utf-8"))
                    sys.stdout.buffer.flush()

            except json.JSONDecodeError as e:
                logger.warning("Invalid JSON: %s", e)
            except Exception:
                logger.exception("MCP server error")


def create_mcp_server(
    space_registry: SpaceRegistry,
    scene_engine: SceneEngine | None = None,
    safety_config: SafetyConfig | None = None,
    read_fn: Any = None,
    execute_fn: Any = None,
) -> MCPServer:
    """Create an MCP server with all components wired up."""
    scenes = scene_engine or SceneEngine()
    safety = AISafetyGuard(space_registry, safety_config)
    executor = ToolExecutor(space_registry, safety, scenes, read_fn, execute_fn)

    return MCPServer(
        space_registry=space_registry,
        safety_guard=safety,
        scene_engine=scenes,
        tool_executor=executor,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SmartSpaces MCP Server")
    parser.add_argument("--spaces", required=True, help="Path to spaces.yaml")
    parser.add_argument("--scenes", help="Path to scenes.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    registry = SpaceRegistry()
    registry.load_yaml(args.spaces)

    scenes = SceneEngine()
    if args.scenes:
        scenes.load_yaml(args.scenes)

    server = create_mcp_server(registry, scenes)
    asyncio.run(server.run_stdio())
