"""Mock tools provider for testing."""

from collections.abc import Callable
from typing import Any

from family_assistant.tools.infrastructure import ToolsProvider


class MockToolsProvider(ToolsProvider):
    """Mock tools provider for testing."""

    def __init__(self) -> None:
        self.tools = {}
        self.tool_definitions = []

    def add_tool(self, name: str, func: Callable) -> None:
        """Add a tool to the mock provider."""
        self.tools[name] = func
        self.tool_definitions.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"Mock tool: {name}",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        })

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any | None = None,
    ) -> Any:
        """Execute a tool by name."""
        if name not in self.tools:
            raise ValueError(f"Tool not found: {name}")

        func = self.tools[name]
        # Handle both sync and async functions
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return await func(**arguments)
        else:
            return func(**arguments)

    def get_tools_definition(self) -> list[dict[str, Any]]:
        """Get the list of available tool definitions."""
        return self.tool_definitions

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions asynchronously."""
        return self.tool_definitions

    async def close(self) -> None:
        """Close resources (no-op for mock)."""
        pass
