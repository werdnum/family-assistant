"""Mock tools provider for testing."""

from collections.abc import Callable
from typing import Any

from family_assistant.tools.infrastructure import ToolsProvider


class MockToolsProvider(ToolsProvider):
    """Mock tools provider for testing."""

    def __init__(self) -> None:
        self.tools = {}
        self.tool_definitions = []

    def add_tool(
        self, name: str, func: Callable, tool_def: dict[str, Any] | None = None
    ) -> None:
        """Add a tool to the mock provider."""
        self.tools[name] = func

        # Use provided tool definition or create a basic one
        if tool_def:
            self.tool_definitions.append(tool_def)
        else:
            # Try to introspect the function to create a basic definition
            import inspect

            sig = inspect.signature(func)
            properties = {}
            required = []

            for param_name, param in sig.parameters.items():
                # Skip exec_context as it's injected automatically
                if param_name in ["exec_context", "db_context"]:
                    continue

                properties[param_name] = {
                    "type": "string",
                    "description": f"Parameter {param_name}",
                }
                if param.default == inspect.Parameter.empty:
                    required.append(param_name)

            self.tool_definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Mock tool: {name}",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
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

        # Check if the function expects exec_context
        import inspect

        sig = inspect.signature(func)
        call_args = arguments.copy()

        # Add exec_context if the function expects it
        if "exec_context" in sig.parameters and context is not None:
            call_args["exec_context"] = context

        # Handle both sync and async functions
        import asyncio

        if asyncio.iscoroutinefunction(func):
            return await func(**call_args)
        else:
            return func(**call_args)

    def get_tools_definition(self) -> list[dict[str, Any]]:
        """Get the list of available tool definitions."""
        return self.tool_definitions

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions asynchronously."""
        return self.tool_definitions

    async def close(self) -> None:
        """Close resources (no-op for mock)."""
        pass
