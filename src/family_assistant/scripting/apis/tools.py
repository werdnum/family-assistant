"""
Tools API bridge for Starlark scripts.

This module provides a bridge between the async tools system and the synchronous
Starlark scripting environment, allowing scripts to discover and execute tools.
"""

import asyncio
import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Thread
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.infrastructure import ToolsProvider
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


@dataclass
class ToolInfo:
    """Information about a tool available to scripts."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolsAPI:
    """
    Provides tools access to Starlark scripts.

    This class bridges the async tools system with the synchronous Starlark
    environment by running a background event loop in a separate thread.
    """

    def __init__(
        self,
        tools_provider: "ToolsProvider",
        execution_context: "ToolExecutionContext",
        allowed_tools: set[str] | None = None,
        deny_all_tools: bool = False,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """
        Initialize the Tools API.

        Args:
            tools_provider: The tools provider to use for tool access
            execution_context: The context for tool execution
            allowed_tools: If specified, only these tools can be executed
            deny_all_tools: If True, no tools can be executed
            main_loop: The main event loop to use for async operations
        """
        self.tools_provider = tools_provider
        self.execution_context = execution_context
        self.allowed_tools = allowed_tools
        self.deny_all_tools = deny_all_tools

        # Use provided main loop or try to get the current one
        if main_loop is not None:
            self._main_loop = main_loop
        else:
            # Try to get the current event loop that created the tools
            try:
                self._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop, we'll need to handle this differently
                self._main_loop = None

        # Create a thread-local event loop for async operations
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=1)

        # Cache tool definitions
        self._tool_definitions: list[dict[str, Any]] | None = None

        logger.info(
            "Initialized ToolsAPI bridge for Starlark scripts (deny_all_tools=%s, allowed_tools=%s, main_loop=%s)",
            self.deny_all_tools,
            self.allowed_tools,
            self._main_loop is not None,
        )

    def _ensure_event_loop(self) -> asyncio.AbstractEventLoop:
        """Ensure we have a running event loop in a background thread."""
        if self._loop is None or not self._loop.is_running():
            # Create new event loop in background thread
            future: Future[asyncio.AbstractEventLoop] = Future()

            def run_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                future.set_result(loop)
                loop.run_forever()

            self._loop_thread = Thread(target=run_loop, daemon=True)
            self._loop_thread.start()
            self._loop = future.result(timeout=5.0)

        return self._loop

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine from sync context."""
        import threading

        # First, check if we're already running in an event loop
        try:
            current_loop = asyncio.get_running_loop()
            # Check if we're in the same thread as the event loop
            # If we are, we can't use run_coroutine_threadsafe - it would deadlock
            if threading.current_thread() is threading.main_thread():
                # We're being called from sync code in the main thread while an event loop is running
                # This typically happens in tests or when evaluate() is called from async context
                # We need to run in a separate thread to avoid deadlock

                # If we have access to the main loop, use it to avoid creating a new event loop
                if self._main_loop and self._main_loop.is_running():
                    # IMPORTANT: We must reuse the main event loop here to ensure database
                    # connections (especially PostgreSQL with asyncpg) are accessed from the
                    # same event loop they were created in. Creating a new event loop would
                    # cause "Future attached to a different loop" errors.
                    # See docs/postgres-test-failures-analysis.md for details.
                    import concurrent.futures

                    def run_in_main_loop() -> Any:
                        assert self._main_loop is not None  # Already checked above
                        future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
                        return future.result(timeout=30.0)

                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=1
                    ) as executor:
                        exec_future = executor.submit(run_in_main_loop)
                        return exec_future.result(timeout=30.0)
                else:
                    # Fallback to creating a new event loop if we don't have access to the main one
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=1
                    ) as executor:
                        future = executor.submit(asyncio.run, coro)
                        return future.result(timeout=30.0)
            else:
                # We're in a different thread, safe to use run_coroutine_threadsafe
                future = asyncio.run_coroutine_threadsafe(coro, current_loop)
                return future.result(timeout=30.0)
        except RuntimeError:
            # No running loop, check if we have a main loop from initialization
            if self._main_loop and self._main_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
                return future.result(timeout=30.0)
            else:
                # Last resort: create our own loop
                loop = self._ensure_event_loop()
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=30.0)

    def _is_tool_allowed(self, tool_name: str) -> bool:
        """
        Check if a tool is allowed to be executed.

        Args:
            tool_name: The name of the tool

        Returns:
            True if the tool can be executed, False otherwise
        """
        # If all tools are denied, nothing is allowed
        if self.deny_all_tools:
            return False

        # If allowed_tools is specified, only those tools are allowed
        if self.allowed_tools is not None:
            return tool_name in self.allowed_tools

        # Otherwise, all tools are allowed
        return True

    def list_tools(self) -> list[ToolInfo]:
        """
        List all available tools.

        Returns:
            List of ToolInfo objects describing available tools (filtered by security settings)
        """
        try:
            # If all tools are denied, return empty list
            if self.deny_all_tools:
                logger.debug("All tools denied - returning empty list")
                return []

            # Get tool definitions from provider
            if self._tool_definitions is None:
                # Create the coroutine directly - _run_async expects a coroutine
                self._tool_definitions = self._run_async(
                    self.tools_provider.get_tool_definitions()
                )

            # Convert to ToolInfo objects, filtering by allowed tools
            tools = []
            if self._tool_definitions:
                for tool_def in self._tool_definitions:
                    function = tool_def.get("function", {})
                    name = function.get("name", "unknown")

                    # Check if this tool is allowed
                    if not self._is_tool_allowed(name):
                        continue

                    description = function.get(
                        "description", "No description available"
                    )
                    parameters = function.get("parameters", {})

                    tools.append(
                        ToolInfo(
                            name=name, description=description, parameters=parameters
                        )
                    )

            logger.debug(f"Listed {len(tools)} allowed tools for Starlark script")
            return tools

        except Exception as e:
            logger.error(f"Error listing tools: {e}", exc_info=True)
            return []

    def get_tool(self, name: str) -> ToolInfo | None:
        """
        Get information about a specific tool.

        Args:
            name: The name of the tool

        Returns:
            ToolInfo object if tool exists and is allowed, None otherwise
        """
        # Check if the tool is allowed before returning info
        if not self._is_tool_allowed(name):
            logger.debug(f"Tool '{name}' is not allowed - returning None")
            return None

        tools = self.list_tools()
        for tool in tools:
            if tool.name == name:
                return tool
        return None

    def execute(self, tool_name: str, *args: Any, **kwargs: Any) -> str:
        """
        Execute a tool with the given arguments.

        Args:
            tool_name: Name of the tool to execute
            *args: Positional arguments to pass to the tool
            **kwargs: Keyword arguments to pass to the tool

        Returns:
            String result from tool execution

        Raises:
            Exception: If tool execution fails or is not allowed
        """
        try:
            # Security check - log and deny if tool is not allowed
            if not self._is_tool_allowed(tool_name):
                error_msg = f"Tool '{tool_name}' is not allowed for execution"
                logger.warning(
                    "Security: Attempted execution of denied tool '%s' from Starlark script",
                    tool_name,
                )
                raise PermissionError(error_msg)

            # If positional args are provided, map them to parameter names
            if args:
                # Get tool definition to find parameter names
                tool_info = self.get_tool(tool_name)
                if tool_info and tool_info.parameters:
                    required = tool_info.parameters.get("required", [])

                    # Map positional args to required parameters in order
                    for i, arg in enumerate(args):
                        if i < len(required):
                            param_name = required[i]
                            kwargs[param_name] = arg
                        else:
                            # If more positional args than required params, log warning
                            logger.warning(
                                f"Tool '{tool_name}' received more positional args than required parameters"
                            )
                            break

            logger.info(
                f"Executing tool '{tool_name}' from Starlark with args: {kwargs}"
            )

            # Create the coroutine directly - _run_async expects a coroutine
            result = self._run_async(
                self.tools_provider.execute_tool(
                    name=tool_name,
                    arguments=kwargs,
                    context=self.execution_context,
                )
            )

            logger.debug(f"Tool '{tool_name}' executed successfully")

            # Convert result to JSON string if it's a dict or list
            if isinstance(result, (dict, list)):
                return json.dumps(result)
            else:
                return str(result)

        except Exception as e:
            error_msg = f"Error executing tool '{tool_name}': {str(e)}"
            logger.error(error_msg, exc_info=True)
            raise RuntimeError(error_msg) from e

    def execute_json(self, tool_name: str, args_json: str) -> str:
        """
        Execute a tool with JSON-encoded arguments.

        This is useful when calling from Starlark with complex argument structures.

        Args:
            tool_name: Name of the tool to execute
            args_json: JSON string containing the arguments

        Returns:
            String result from tool execution

        Raises:
            Exception: If tool execution fails or JSON is invalid
        """
        try:
            args = json.loads(args_json)
            if not isinstance(args, dict):
                raise ValueError("Arguments must be a JSON object")
            return self.execute(tool_name, **args)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON arguments: {str(e)}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e

    def __del__(self) -> None:
        """Clean up resources when the API is destroyed."""
        # Stop the event loop if it's running
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        # Shutdown the executor
        self._executor.shutdown(wait=False)


class StarlarkToolsAPI:
    """Wrapper class to expose tools API to Starlark scripts."""

    def __init__(self, api: ToolsAPI) -> None:
        """Initialize the Starlark-compatible wrapper."""
        self._api = api

    def list(self) -> list[dict[str, Any]]:
        """List available tools."""
        tools = self._api.list_tools()
        # Convert ToolInfo objects to dictionaries for Starlark
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]

    def get(self, name: str) -> dict[str, Any] | None:
        """Get tool information."""
        tool = self._api.get_tool(name)
        if tool:
            return {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
        return None

    def execute(self, tool_name: str, *args: Any, **kwargs: Any) -> str:
        """Execute a tool."""
        return self._api.execute(tool_name, *args, **kwargs)

    def execute_json(self, tool_name: str, args_json: str) -> str:
        """Execute a tool with JSON arguments."""
        return self._api.execute_json(tool_name, args_json)


def create_tools_api(
    tools_provider: "ToolsProvider",
    execution_context: "ToolExecutionContext",
    allowed_tools: set[str] | None = None,
    deny_all_tools: bool = False,
    main_loop: asyncio.AbstractEventLoop | None = None,
) -> StarlarkToolsAPI:
    """
    Create a tools API object suitable for use in Starlark scripts.

    Args:
        tools_provider: The tools provider to use
        execution_context: The execution context for tools
        allowed_tools: If specified, only these tools can be executed
        deny_all_tools: If True, no tools can be executed
        main_loop: The main event loop to use for async operations

    Returns:
        StarlarkToolsAPI wrapper object
    """
    api = ToolsAPI(
        tools_provider,
        execution_context,
        allowed_tools=allowed_tools,
        deny_all_tools=deny_all_tools,
        main_loop=main_loop,
    )
    return StarlarkToolsAPI(api)
