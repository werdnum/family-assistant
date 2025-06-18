"""
Starlark scripting engine for Family Assistant.

This module provides a Starlark-based scripting engine that executes user-defined
scripts with access to family assistant tools and state.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import starlark

from .apis.tools import create_tools_api
from .errors import ScriptExecutionError, ScriptSyntaxError, ScriptTimeoutError

if TYPE_CHECKING:
    from family_assistant.tools import ToolsProvider
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


@dataclass
class StarlarkConfig:
    """Configuration for the Starlark scripting engine."""

    max_execution_time: float = 30.0  # Maximum execution time in seconds
    max_memory_mb: int = 100  # Maximum memory usage in megabytes
    enable_print: bool = True  # Whether to enable the print() function
    enable_debug: bool = False  # Whether to enable debug output
    allowed_tools: set[str] | None = (
        None  # If specified, only these tools can be executed
    )
    deny_all_tools: bool = False  # If True, no tools can be executed


class StarlarkEngine:
    """
    Starlark scripting engine for executing user-defined scripts.

    This engine provides a sandboxed environment for running Starlark scripts
    with controlled access to family assistant functionality.
    """

    def __init__(
        self,
        tools_provider: "ToolsProvider | None" = None,
        config: StarlarkConfig | None = None,
    ) -> None:
        """
        Initialize the Starlark scripting engine.

        Args:
            tools_provider: Optional provider for accessing family assistant tools
            config: Optional configuration for the engine
        """
        self.tools_provider = tools_provider
        self.config = config or StarlarkConfig()

        # Note: starlark-pyo3 doesn't have direct memory limit configuration
        # Memory limits would need to be enforced at the process level

        logger.info(
            "Initialized StarlarkEngine with config: max_execution_time=%s, max_memory_mb=%s",
            self.config.max_execution_time,
            self.config.max_memory_mb,
        )

    def evaluate(
        self,
        script: str,
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:
        """
        Evaluate a Starlark expression or script synchronously.

        Args:
            script: The Starlark script to execute
            globals_dict: Optional dictionary of global variables to make available
            execution_context: Optional execution context for tools access

        Returns:
            The result of the script execution

        Raises:
            ScriptSyntaxError: If the script has invalid syntax
            ScriptExecutionError: If the script encounters a runtime error
            ScriptTimeoutError: If the script exceeds the execution time limit
        """
        try:
            # Create a module for execution
            module = starlark.Module()

            # Get standard globals
            globals_dict_to_use = starlark.Globals.standard()

            # Add JSON functions
            import json

            module.add_callable("json_decode", json.loads)
            module.add_callable("json_encode", json.dumps)

            # Add user-provided globals to module
            if globals_dict:
                for key, value in globals_dict.items():
                    if callable(value):
                        # Use add_callable for functions
                        module.add_callable(key, value)
                    else:
                        # Use regular assignment for non-callables
                        module[key] = value

            # Add built-in print if enabled
            if self.config.enable_print:
                module.add_callable("print", self._create_print_function())

            # Add tools API if we have both provider and context
            if self.tools_provider and execution_context:
                tools_api = create_tools_api(
                    self.tools_provider,
                    execution_context,
                    allowed_tools=self.config.allowed_tools,
                    deny_all_tools=self.config.deny_all_tools,
                )

                # Create a dictionary structure to simulate the tools object
                tools_dict = {
                    "list": tools_api.list,
                    "get": tools_api.get,
                    "execute": tools_api.execute,
                    "execute_json": tools_api.execute_json,
                }

                # Add each method as a callable
                for name, method in tools_dict.items():
                    module.add_callable(f"tools_{name}", method)

                # Also add a tools object that can be accessed with dot notation
                # This is a workaround since starlark-pyo3 doesn't support custom objects well
                module.add_callable("tools", lambda: tools_dict)

                # Get list of available tools and create direct callable functions
                available_tools = tools_api.list()
                for tool_info in available_tools:
                    tool_name = tool_info["name"]

                    # Create a closure to capture the tool name
                    def make_tool_wrapper(name: str) -> Any:
                        def tool_wrapper(**kwargs: Any) -> str:
                            """Execute the tool with the given arguments."""
                            return tools_api.execute(name, **kwargs)

                        return tool_wrapper

                    # Add the tool as a direct callable with its original name
                    module.add_callable(tool_name, make_tool_wrapper(tool_name))

                    # Also add it with a tool_ prefix as a fallback
                    module.add_callable(
                        f"tool_{tool_name}", make_tool_wrapper(tool_name)
                    )

                logger.debug(
                    "Added tools API to Starlark module (allowed_tools=%s, deny_all_tools=%s, direct_tools=%d)",
                    self.config.allowed_tools,
                    self.config.deny_all_tools,
                    len(available_tools),
                )

            # Parse the script first
            ast = starlark.parse("script.star", script)

            # Evaluate the parsed AST
            result = starlark.eval(module, ast, globals_dict_to_use)

            return result

        except starlark.StarlarkError as e:
            # Handle Starlark-specific errors
            error_str = str(e)

            # Try to determine if it's a syntax error
            if "parse error" in error_str.lower() or "syntax" in error_str.lower():
                # Extract line info if available from error message
                line = None
                # Parse line number from error message if present
                match = re.search(r"line (\d+)", error_str)
                if match:
                    line = int(match.group(1))
                raise ScriptSyntaxError(error_str, line=line) from e
            else:
                # Runtime error
                raise ScriptExecutionError(
                    f"Script execution failed: {error_str}"
                ) from e

        except Exception as e:
            # Handle other runtime errors
            error_msg = f"Script execution failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            raise ScriptExecutionError(error_msg) from e

    async def evaluate_async(
        self,
        script: str,
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:
        """
        Evaluate a Starlark expression or script asynchronously.

        This method runs the Starlark script in a thread pool to avoid blocking
        the event loop, and enforces execution time limits.

        Args:
            script: The Starlark script to execute
            globals_dict: Optional dictionary of global variables to make available
            execution_context: Optional execution context for tools access

        Returns:
            The result of the script execution

        Raises:
            ScriptSyntaxError: If the script has invalid syntax
            ScriptExecutionError: If the script encounters a runtime error
            ScriptTimeoutError: If the script exceeds the execution time limit
        """
        try:
            # Run the script evaluation with a timeout
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, self.evaluate, script, globals_dict, execution_context
                ),
                timeout=self.config.max_execution_time,
            )
            return result

        except asyncio.TimeoutError as e:
            error_msg = f"Script execution timed out after {self.config.max_execution_time} seconds"
            logger.error(error_msg)
            raise ScriptTimeoutError(error_msg, self.config.max_execution_time) from e

    def _create_print_function(self) -> Any:
        """Create a print function that logs output."""

        def starlark_print(*args: Any, **kwargs: Any) -> None:
            """Print function exposed to Starlark scripts."""
            # Convert all arguments to strings
            message = " ".join(str(arg) for arg in args)

            # Log at info level for script output
            logger.info("Script output: %s", message)

            # Also print to stdout if debug is enabled
            if self.config.enable_debug:
                print(f"[SCRIPT] {message}")

        return starlark_print
