#!/usr/bin/env python3
"""
Example demonstrating direct tool callable functionality in Starlark scripts.

This example shows how tools can be called directly as functions in Starlark,
making the scripting experience more natural and intuitive.
"""

import asyncio
import logging
from typing import Any

from family_assistant.scripting.engine import StarlarkEngine
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolExecutionContext

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SimpleToolsProvider:
    """Simple tools provider with a few example tools."""

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "greet",
                    "description": "Greet a person by name",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Person's name"},
                            "greeting": {
                                "type": "string",
                                "description": "Greeting to use",
                                "default": "Hello",
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": "Perform a calculation",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "description": "Operation: add, subtract, multiply, divide",
                            },
                            "a": {"type": "number", "description": "First number"},
                            "b": {"type": "number", "description": "Second number"},
                        },
                        "required": ["operation", "a", "b"],
                    },
                },
            },
        ]

    async def execute_tool(
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> str:
        """Execute a tool."""
        if name == "greet":
            greeting = arguments.get("greeting", "Hello")
            name_arg = arguments.get("name", "stranger")
            return f"{greeting}, {name_arg}!"
        elif name == "calculate":
            op = arguments.get("operation", "add")
            a = arguments.get("a", 0)
            b = arguments.get("b", 0)
            if op == "add":
                result = a + b
            elif op == "subtract":
                result = a - b
            elif op == "multiply":
                result = a * b
            elif op == "divide":
                result = a / b if b != 0 else "Error: Division by zero"
            else:
                result = "Error: Unknown operation"
            return f"Result: {result}"
        else:
            raise ValueError(f"Unknown tool: {name}")

    async def close(self) -> None:
        """Cleanup."""
        pass


async def main() -> None:
    """Run example demonstrating direct tool callables."""
    # Initialize components
    tools_provider = SimpleToolsProvider()

    # Create execution context
    async with DatabaseContext() as db:
        context = ToolExecutionContext(
            interface_type="demo",
            conversation_id="demo-123",
            user_name="Demo User",
            turn_id="demo-turn-1",
            db_context=db,
        )

        # Create engine
        engine = StarlarkEngine(tools_provider=tools_provider)

        # Example 1: Direct tool calls
        print("\n=== Example 1: Direct Tool Calls ===")
        script1 = """
# Call tools directly by name
greeting1 = greet(name="Alice")
greeting2 = greet(name="Bob", greeting="Howdy")

# Perform calculations
sum_result = calculate(operation="add", a=10, b=5)
product_result = calculate(operation="multiply", a=7, b=8)

# Return results
{
    "greetings": [greeting1, greeting2],
    "calculations": [sum_result, product_result]
}
"""
        result1 = await engine.evaluate_async(script1, execution_context=context)
        print(f"Result: {result1}")

        # Example 2: Using both old and new APIs
        print("\n=== Example 2: Mixed API Usage ===")
        script2 = """
# List available tools using old API
available_tools = tools_list()
tool_names = [tool["name"] for tool in available_tools]

# Call tools using new direct syntax
direct_result = greet(name="Charlie")

# Call tools using old API
old_result = tools_execute("greet", name="David", greeting="Greetings")

{
    "available": tool_names,
    "direct_call": direct_result,
    "old_api_call": old_result
}
"""
        result2 = await engine.evaluate_async(script2, execution_context=context)
        print(f"Result: {result2}")

        # Example 3: Tool with fallback prefix
        print("\n=== Example 3: Tool Prefix Fallback ===")
        script3 = """
# Tools can also be called with tool_ prefix
# This is useful if tool names conflict with built-in functions

result1 = tool_greet(name="Eve")
result2 = tool_calculate(operation="divide", a=20, b=4)

[result1, result2]
"""
        result3 = await engine.evaluate_async(script3, execution_context=context)
        print(f"Result: {result3}")


if __name__ == "__main__":
    asyncio.run(main())
