"""Agent SDK usage example: how an AI agent interacts with SmartSpaces.

This shows both sync and async usage of the SmartSpacesClient.

Usage:
    # Start the server first:
    python -m core.engine --spaces fixtures/spaces_example.yaml

    # Then run this script:
    python examples/agent_sdk_usage.py
"""

from agent.client import SmartSpacesClient


def main():
    client = SmartSpacesClient(
        base_url="http://localhost:8000",
        api_key="your-api-key-here",
    )

    # List available tools (for tool-use LLMs)
    tools = client.get_tools(format="openai")
    print(f"Available tools: {len(tools)}")
    for tool in tools[:5]:
        print(f"  - {tool['function']['name']}: {tool['function']['description'][:60]}")

    # Get device state by semantic name
    state = client.get_state("living_room.ceiling_light")
    print(f"\nLiving room light: {state}")

    # Set a device value
    result = client.set_device("living_room.ceiling_light", True)
    print(f"Set result: {result}")

    # Activate a scene
    result = client.activate_scene("movie_night")
    print(f"Scene result: {result}")

    # Natural language intent
    result = client.resolve_intent("turn off all the lights in the kitchen")
    print(f"Intent result: {result}")


if __name__ == "__main__":
    main()
