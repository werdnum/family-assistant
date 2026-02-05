"""
Starlark scripting engine for Family Assistant.

This module provides a Starlark-based scripting engine that executes user-defined
scripts with access to family assistant tools and state.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import starlark

from .apis import time as time_api
from .apis.attachments import create_attachment_api
from .apis.tools import create_tools_api
from .errors import ScriptExecutionError, ScriptSyntaxError, ScriptTimeoutError

if TYPE_CHECKING:
    from family_assistant.tools import ToolsProvider
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


@dataclass
class StarlarkConfig:
    """Configuration for the Starlark scripting engine."""

    max_execution_time: float = (
        600.0  # Maximum execution time in seconds (10 minutes default)
    )
    enable_print: bool = True  # Whether to enable the print() function
    enable_debug: bool = False  # Whether to enable debug output
    allowed_tools: set[str] | None = (
        None  # If specified, only these tools can be executed
    )
    deny_all_tools: bool = False  # If True, no tools can be executed
    disable_apis: bool = False  # If True, no APIs (json, time, etc.) are loaded


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

        # Capture the main event loop if available
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

        logger.info(
            "Initialized StarlarkEngine with config: max_execution_time=%s, main_loop=%s",
            self.config.max_execution_time,
            self._main_loop is not None,
        )

    def evaluate(
        self,
        script: str,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:  # noqa: ANN401 # Starlark scripts can return any type
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

            # Only add APIs if not disabled
            if not self.config.disable_apis:
                # Add JSON functions
                module.add_callable("json_decode", json.loads)
                module.add_callable("json_encode", json.dumps)

                # Add time API functions
                # Time creation functions
                module.add_callable("time_now", time_api.time_now)
                module.add_callable("time_now_utc", time_api.time_now_utc)
                module.add_callable("time_create", time_api.time_create)
                module.add_callable("time_from_timestamp", time_api.time_from_timestamp)
                module.add_callable("time_parse", time_api.time_parse)

                # Time manipulation functions
                module.add_callable("time_in_location", time_api.time_in_location)
                module.add_callable("time_format", time_api.time_format)
                module.add_callable("time_add", time_api.time_add)
                module.add_callable("time_add_duration", time_api.time_add_duration)

                # Time component functions
                module.add_callable("time_year", time_api.time_year)
                module.add_callable("time_month", time_api.time_month)
                module.add_callable("time_day", time_api.time_day)
                module.add_callable("time_hour", time_api.time_hour)
                module.add_callable("time_minute", time_api.time_minute)
                module.add_callable("time_second", time_api.time_second)
                module.add_callable("time_weekday", time_api.time_weekday)

                # Time comparison functions
                module.add_callable("time_before", time_api.time_before)
                module.add_callable("time_after", time_api.time_after)
                module.add_callable("time_equal", time_api.time_equal)
                module.add_callable("time_diff", time_api.time_diff)

                # Duration functions
                module.add_callable("duration_parse", time_api.duration_parse)
                module.add_callable("duration_human", time_api.duration_human)

                # Timezone functions
                module.add_callable("timezone_is_valid", time_api.timezone_is_valid)
                module.add_callable("timezone_offset", time_api.timezone_offset)

                # Utility functions
                module.add_callable("is_between", time_api.is_between)
                module.add_callable("is_weekend", time_api.is_weekend)

                # Duration constants
                module["NANOSECOND"] = time_api.NANOSECOND
                module["MICROSECOND"] = time_api.MICROSECOND
                module["MILLISECOND"] = time_api.MILLISECOND
                module["SECOND"] = time_api.SECOND
                module["MINUTE"] = time_api.MINUTE
                module["HOUR"] = time_api.HOUR
                module["DAY"] = time_api.DAY
                module["WEEK"] = time_api.WEEK

                # Add LLM API functions (deferred import to avoid circular dependency
                # via scripting -> llm.messages -> tools -> scripting)
                from .apis.llm import create_llm_api  # noqa: PLC0415

                llm_api = create_llm_api(main_loop=self._main_loop)
                module.add_callable("llm", llm_api.call)
                module.add_callable("llm_json", llm_api.call_json)

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

            # Initialize wake_llm accumulator and store globals for later access
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            self._wake_llm_contexts: list[dict[str, Any]] = []
            self._script_globals = globals_dict or {}

            # Add wake_llm function
            module.add_callable("wake_llm", self._create_wake_llm_function())

            # Add attachment API if we have execution context with attachment service
            if execution_context and execution_context.attachment_registry:
                try:
                    attachment_api = create_attachment_api(
                        execution_context, main_loop=self._main_loop
                    )

                    # Add attachment functions (excluding list and send for security/architecture)
                    module.add_callable("attachment_get", attachment_api.get)
                    module.add_callable("attachment_read", attachment_api.read)
                    module.add_callable("attachment_create", attachment_api.create)
                    # NOTE: attachment_list intentionally excluded to prevent ID enumeration
                    # NOTE: attachment_send removed - scripts should use LLM tools:
                    #   - attach_to_response (for current user)
                    #   - send_message_to_user (for other users)
                    #   - wake_llm (to pass to LLM for processing)

                    logger.debug("Added attachment API to Starlark module")
                except Exception as e:
                    logger.warning(f"Failed to add attachment API: {e}")

            # Add tools API if we have both provider and context
            if self.tools_provider and execution_context:
                tools_api = create_tools_api(
                    self.tools_provider,
                    execution_context,
                    allowed_tools=self.config.allowed_tools,
                    deny_all_tools=self.config.deny_all_tools,
                    main_loop=self._main_loop,
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
                    def make_tool_wrapper(
                        name: str,
                    ) -> Callable[
                        ...,
                        # ast-grep-ignore: no-dict-any - Return dict for Starlark JSON compatibility
                        str | dict[str, Any] | list[Any] | int | float | bool,
                    ]:
                        def tool_wrapper(
                            *args: Any,  # noqa: ANN401 # Tool args can be any type
                            **kwargs: Any,  # noqa: ANN401
                            # ast-grep-ignore: no-dict-any - Return dict for Starlark JSON compatibility
                        ) -> str | dict[str, Any] | list[Any] | int | float | bool:
                            """Execute the tool with the given arguments."""
                            # If positional args are provided, we need to map them to kwargs
                            # This requires knowing the parameter names of the tool
                            if args:
                                # For now, we'll pass positional args as-is to the execute method
                                # The tools API will need to handle the mapping
                                return tools_api.execute(name, *args, **kwargs)
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

            # Configure dialect with useful features
            dialect = starlark.Dialect.extended()
            dialect.enable_f_strings = True
            dialect.enable_lambda = True
            dialect.enable_def = True
            dialect.enable_keyword_only_arguments = True

            # Parse the script first
            ast = starlark.parse("script.star", script, dialect=dialect)

            # Evaluate the parsed AST
            result = starlark.eval(module, ast, globals_dict_to_use)

            # Store wake_llm contexts for the caller to process
            if self._wake_llm_contexts:
                self._pending_wake_contexts = self._wake_llm_contexts.copy()
            else:
                self._pending_wake_contexts = []

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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:  # noqa: ANN401 # Starlark scripts can return any type
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

        except TimeoutError as e:
            error_msg = f"Script execution timed out after {self.config.max_execution_time} seconds"
            logger.error(error_msg)
            raise ScriptTimeoutError(error_msg, self.config.max_execution_time) from e

    def _create_print_function(self) -> Callable[..., None]:
        """Create a print function that logs output."""

        def starlark_print(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 # Print can accept any args
            """Print function exposed to Starlark scripts."""
            # Convert all arguments to strings
            message = " ".join(str(arg) for arg in args)

            # Log at info level for script output
            logger.info("Script output: %s", message)

            # Also print to stdout if debug is enabled
            if self.config.enable_debug:
                print(f"[SCRIPT] {message}")

        return starlark_print

    def _create_wake_llm_function(self) -> Callable[..., None]:
        """Create a wake_llm function for scripts."""

        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        def wake_llm(context: dict[str, Any] | str, include_event: bool = True) -> None:
            """Request to wake the LLM with context.

            Args:
                context: Either a string message or a dictionary containing:
                    - message: The text message for the LLM
                    - attachments: Optional list of attachment IDs to include
                    - Other custom context fields
                include_event: Whether to include the triggering event data
            """
            # Convert string context to dict format
            if isinstance(context, str):
                context_dict = {"message": context}
            elif isinstance(context, dict):
                context_dict = dict(context)  # Make a copy
            else:
                raise TypeError("wake_llm context must be a dictionary or string")

            # Validate attachment IDs if provided
            if "attachments" in context_dict:
                attachments = context_dict["attachments"]
                if not isinstance(attachments, list):
                    raise TypeError("attachments must be a list of attachment IDs")

                for attachment_id in attachments:
                    if not isinstance(attachment_id, str):
                        raise TypeError("attachment IDs must be strings")
                    # Basic UUID format validation
                    try:
                        uuid.UUID(attachment_id)
                    except ValueError as e:
                        raise ValueError(
                            f"Invalid attachment ID format: {attachment_id}"
                        ) from e

            # Store the wake request
            wake_request = {
                "context": context_dict,
                "include_event": include_event,
            }
            self._wake_llm_contexts.append(wake_request)

            logger.debug(f"Script requested LLM wake with context: {context_dict}")

        return wake_llm

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def get_pending_wake_contexts(self) -> list[dict[str, Any]]:
        """Get any pending wake_llm contexts from the last script execution."""
        return getattr(self, "_pending_wake_contexts", [])
