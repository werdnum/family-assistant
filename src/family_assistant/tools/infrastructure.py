"""Infrastructure components for the tools module.

This module contains base classes, protocols, exceptions, and common utilities
used by all tool implementations.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Protocol, get_type_hints

from family_assistant import calendar_integration
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator

logger = logging.getLogger(__name__)


def translate_attachment_schemas_for_llm(
    tool_definitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Translate tool schemas for LLM compatibility by converting attachment types.

    Transforms 'type': 'attachment' parameters to 'type': 'string' with descriptive text
    explaining that the LLM should provide an attachment UUID.

    Args:
        tool_definitions: List of tool definitions with potentially internal attachment types

    Returns:
        List of tool definitions compatible with LLMs (no custom attachment types)
    """

    translated_definitions = copy.deepcopy(tool_definitions)

    for tool_def in translated_definitions:
        if tool_def.get("type") == "function":
            function_def = tool_def.get("function", {})
            parameters = function_def.get("parameters", {})
            properties = parameters.get("properties", {})

            # Transform each parameter that has type: attachment
            for param_name, param_def in properties.items():
                if param_def.get("type") == "attachment":
                    # Direct attachment parameter
                    param_def["type"] = "string"

                    # Enhance description to explain UUID requirement
                    original_desc = param_def.get("description", "")
                    if (
                        "UUID" not in original_desc
                        and "attachment" in original_desc.lower()
                    ):
                        param_def["description"] = (
                            f"UUID of the {original_desc.lower()}"
                        )
                    elif "UUID" not in original_desc:
                        param_def["description"] = (
                            f"UUID of the attachment. {original_desc}"
                        )

                    logger.debug(
                        f"Translated attachment parameter '{param_name}' to string type for LLM compatibility"
                    )

                elif param_def.get("type") == "array":
                    # Handle arrays of attachments
                    items = param_def.get("items", {})
                    if isinstance(items, dict) and items.get("type") == "attachment":
                        items["type"] = "string"

                        # Enhance items description for UUID requirement
                        original_desc = items.get("description", "")
                        if not original_desc:
                            items["description"] = "UUID of an attachment"
                        elif "UUID" not in original_desc:
                            if "attachment" in original_desc.lower():
                                items["description"] = (
                                    f"UUID of the {original_desc.lower()}"
                                )
                            else:
                                items["description"] = (
                                    f"UUID of the attachment. {original_desc}"
                                )

                        logger.debug(
                            f"Translated attachment array parameter '{param_name}' items to string type for LLM compatibility"
                        )

    return translated_definitions


class ConfirmationCallbackProtocol(Protocol):
    """Protocol for confirmation callback functions.

    This protocol defines the expected signature for callbacks that request
    user confirmation for tool actions. The callback should handle the
    timeout internally.
    """

    async def __call__(
        self,
        conversation_id: str,
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Request user confirmation for a tool action.

        Args:
            conversation_id: Unique identifier for the conversation
            interface_type: Type of interface (e.g., 'telegram', 'api')
            turn_id: Optional turn identifier for tracking
            prompt_text: The confirmation prompt to show the user
            tool_name: Name of the tool requesting confirmation
            tool_args: Arguments being passed to the tool
            timeout: Timeout in seconds for the confirmation

        Returns:
            True if user confirmed, False if cancelled/timeout
        """
        ...


class ToolConfirmationRequired(Exception):
    """Raised when a tool requires user confirmation but it wasn't provided.

    This exception is used to signal that a tool action cannot proceed
    without explicit user confirmation, typically for destructive operations.
    """

    def __init__(self, tool_name: str, message: str | None = None) -> None:
        self.tool_name = tool_name
        super().__init__(message or f"Tool '{tool_name}' requires user confirmation")


class ToolConfirmationFailed(Exception):
    """Raised when user confirmation for a tool action fails or times out."""

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Confirmation failed for tool '{tool_name}': {reason}")


class ToolsProvider(Protocol):
    """Protocol for tool providers.

    Defines the interface that all tool providers must implement to provide
    tool definitions and execute tools.
    """

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Returns a list of tool definitions in LLM-compatible format.

        Returns:
            List of tool definition dictionaries following the standard
            tool definition schema.
        """
        ...

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Executes a specific tool by name with given arguments.

        Args:
            name: The name of the tool to execute
            arguments: Arguments to pass to the tool
            context: Execution context containing DB session, user info, etc.

        Returns:
            String result from the tool execution

        Raises:
            ToolNotFoundError: If the tool name is not found
            Exception: Any exception raised by the tool implementation
        """
        ...

    async def close(self) -> None:
        """Cleanup any resources held by the provider."""
        ...


class ToolNotFoundError(Exception):
    """Raised when a requested tool is not found by a provider."""

    def __init__(self, tool_name: str, provider: str | None = None) -> None:
        self.tool_name = tool_name
        self.provider = provider
        message = f"Tool '{tool_name}' not found"
        if provider:
            message += f" in {provider}"
        super().__init__(message)


class LocalToolsProvider:
    """Provides and executes locally defined Python functions as tools."""

    def __init__(
        self,
        definitions: list[dict[str, Any]],
        implementations: dict[str, Any],  # dict[str, Callable]
        embedding_generator: EmbeddingGenerator | None = None,
        calendar_config: dict[str, Any] | None = None,
    ) -> None:
        self._definitions = definitions
        self._implementations = implementations
        self._embedding_generator = embedding_generator
        self._calendar_config = calendar_config
        logger.info(
            f"LocalToolsProvider initialized with {len(self._definitions)} tools: {list(self._implementations.keys())}"
        )
        if self._embedding_generator:
            logger.info(
                f"LocalToolsProvider configured with embedding generator: {type(self._embedding_generator).__name__}"
            )

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions translated for LLM compatibility.

        Returns tool definitions with attachment types converted to string types
        with UUID descriptions for LLM compatibility.
        """
        return translate_attachment_schemas_for_llm(self._definitions)

    def get_raw_tool_definitions(self) -> list[dict[str, Any]]:
        """Get raw internal tool definitions without LLM translation.

        This is used internally by the script API to detect attachment types
        before translation.

        Returns:
            Raw tool definitions with original attachment types
        """
        return self._definitions

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        if name not in self._implementations:
            raise ToolNotFoundError(f"Local tool '{name}' not found.")

        callable_func = self._implementations[name]
        logger.info(f"Executing local tool '{name}' with args: {arguments}")
        try:
            # Prepare arguments, potentially injecting context or generator
            call_args = arguments.copy()
            logger.debug(f"Tool '{name}' - Initial arguments from LLM: {arguments}")
            logger.debug(f"Tool '{name}' - Initial call_args (copy): {call_args}")
            sig = inspect.signature(callable_func)

            resolved_hints = {}
            try:
                func_module = inspect.getmodule(callable_func)
                global_ns = func_module.__dict__ if func_module else None
                resolved_hints = get_type_hints(callable_func, globalns=global_ns)
            except Exception as e:
                logger.warning(
                    f"Could not fully resolve type hints for '{callable_func.__name__}': {e}. "
                    "Injection will rely on raw annotations where resolution failed."
                )

            needs_exec_context = False
            needs_db_context = False
            needs_embedding_generator = False
            needs_calendar_config = False

            for param_name, param in sig.parameters.items():
                # Use resolved hint if available, otherwise use the raw annotation from signature.
                annotation_to_check = resolved_hints.get(param_name, param.annotation)

                if param_name == "exec_context":
                    if annotation_to_check is ToolExecutionContext:
                        needs_exec_context = True
                    # Fallback for unresolved forward reference string
                    elif (
                        isinstance(param.annotation, str)
                        and param.annotation == "ToolExecutionContext"
                    ):
                        needs_exec_context = True
                        logger.debug(
                            f"Identified 'exec_context' for {callable_func.__name__} via string forward reference fallback."
                        )

                elif param_name == "db_context":
                    # Check for DatabaseContext by name since we can't import it
                    if (
                        hasattr(annotation_to_check, "__name__")
                        and annotation_to_check.__name__ == "DatabaseContext"
                    ):
                        needs_db_context = True

                elif param_name == "embedding_generator":
                    # Check for EmbeddingGenerator by name since we can't import it
                    if (
                        hasattr(annotation_to_check, "__name__")
                        and annotation_to_check.__name__ == "EmbeddingGenerator"
                    ):
                        needs_embedding_generator = True
                    # Also check for string annotation
                    elif (
                        isinstance(param.annotation, str)
                        and param.annotation == "EmbeddingGenerator"
                    ):
                        needs_embedding_generator = True
                        logger.debug(
                            f"Identified 'embedding_generator' for {callable_func.__name__} via string annotation."
                        )

                elif param_name == "calendar_config":
                    # Check if it's a dict type annotation
                    if annotation_to_check == dict[str, Any]:
                        needs_calendar_config = True
                    # Handle string annotation fallback
                    elif (
                        isinstance(param.annotation, str)
                        and param.annotation == "dict[str, Any]"
                    ):
                        needs_calendar_config = True
                        logger.debug(
                            f"Identified 'calendar_config' for {callable_func.__name__} via string annotation fallback."
                        )
                    # Also handle cases where the type might not match exactly
                    elif (
                        hasattr(annotation_to_check, "__origin__")
                        and annotation_to_check.__origin__ is dict
                    ):
                        needs_calendar_config = True
                        logger.debug(
                            f"Matched calendar_config via __origin__ check for {callable_func.__name__}"
                        )

            # Inject dependencies based on resolved needs
            if needs_exec_context:
                call_args["exec_context"] = context
            if needs_db_context:  # db_context is part of ToolExecutionContext
                call_args["db_context"] = context.db_context
            if needs_embedding_generator:
                if self._embedding_generator:
                    call_args["embedding_generator"] = self._embedding_generator
                else:
                    logger.error(
                        f"Tool '{name}' requires an embedding generator, but none was provided to LocalToolsProvider."
                    )
                    return f"Error: Tool '{name}' cannot be executed because the embedding generator is missing."
            if needs_calendar_config:
                if self._calendar_config:
                    call_args["calendar_config"] = self._calendar_config
                else:
                    logger.error(
                        f"Tool '{name}' requires a calendar_config, but none was provided to LocalToolsProvider."
                    )
                    return f"Error: Tool '{name}' cannot be executed because the calendar_config is missing."

            # Clean up arguments not expected by the function signature
            # (Ensures we don't pass exec_context if only db_context was needed, etc.)
            expected_args = set(sig.parameters.keys())
            args_to_remove = set(call_args.keys()) - expected_args
            for arg_name in args_to_remove:
                # Only remove if it wasn't part of the original LLM arguments
                if arg_name not in arguments:
                    del call_args[arg_name]

            # Ensure all original arguments that are expected by the function are included
            for param_name in sig.parameters:
                if param_name in arguments and param_name not in call_args:
                    call_args[param_name] = arguments[param_name]

            logger.debug(f"Tool '{name}' - Final call_args after cleanup: {call_args}")

            # Execute the function with prepared arguments
            result = await callable_func(**call_args)

            # Handle different result types
            if isinstance(result, ToolResult):
                # Return ToolResult as-is to preserve multimodal content
                logger.info(
                    f"Local tool '{name}' returned ToolResult with attachment: {result.attachment is not None}"
                )
                return result
            elif result is None:  # Handle None case explicitly
                result_str = "Tool executed successfully (returned None)."
                logger.info(f"Local tool '{name}' returned None.")
            elif isinstance(result, dict | list):
                # Convert dict or list to JSON string.
                result_str = json.dumps(result, indent=2, ensure_ascii=False)
            elif not isinstance(result, str):
                result_str = str(result)
                logger.warning(
                    f"Tool '{name}' returned non-string result ({type(result)}), converted to: '{result_str[:100]}...'"
                )
            else:
                result_str = result

            if "Error:" not in result_str:
                logger.info(f"Local tool '{name}' executed successfully.")
            else:
                logger.warning(f"Local tool '{name}' reported an error: {result_str}")
            return result_str
        except Exception as e:
            logger.error(f"Error executing local tool '{name}': {e}", exc_info=True)
            # Re-raise or return formatted error string? Returning error string for now.
            return f"Error executing tool '{name}': {e}"

    def get_calendar_config(self) -> dict[str, Any] | None:
        """Get the calendar configuration."""
        return self._calendar_config

    async def close(self) -> None:
        """Local provider has no resources to clean up."""
        logger.debug("Closing LocalToolsProvider (no-op).")


class CompositeToolsProvider:
    """Combines multiple tool providers into a single interface."""

    def __init__(self, providers: list[ToolsProvider]) -> None:
        self._providers = providers
        logger.info(
            f"CompositeToolsProvider initialized with {len(providers)} providers"
        )

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Returns combined tool definitions from all providers."""
        all_definitions = []
        for provider in self._providers:
            try:
                definitions = await provider.get_tool_definitions()
                all_definitions.extend(definitions)
            except Exception as e:
                logger.error(
                    f"Error getting tool definitions from {type(provider).__name__}: {e}",
                    exc_info=True,
                )
        return all_definitions

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Executes a tool by trying each provider until one succeeds."""
        last_error = None
        for provider in self._providers:
            try:
                logger.debug(
                    f"Attempting to execute tool '{name}' with provider {type(provider).__name__}"
                )
                result = await provider.execute_tool(name, arguments, context, call_id)
                logger.debug(
                    f"Tool '{name}' executed successfully with provider {type(provider).__name__}"
                )
                return result
            except ToolNotFoundError:
                # This provider doesn't have the tool, try the next one
                continue
            except Exception as e:
                # Log the error but keep trying other providers
                logger.error(
                    f"Error executing tool '{name}' with provider {type(provider).__name__}: {e}",
                    exc_info=True,
                )
                last_error = e
                continue

        # If we get here, no provider could execute the tool
        if last_error:
            raise last_error
        else:
            raise ToolNotFoundError(name, "any provider")

    def get_providers(self) -> list[ToolsProvider]:
        """Get the list of providers."""
        return self._providers

    async def close(self) -> None:
        """Closes all wrapped providers."""
        logger.info("Closing CompositeToolsProvider...")
        for provider in self._providers:
            try:
                await provider.close()
            except Exception as e:
                logger.error(
                    f"Error closing provider {type(provider).__name__}: {e}",
                    exc_info=True,
                )
        logger.info("CompositeToolsProvider closed.")


class FilteredToolsProvider(ToolsProvider):
    """Provides a filtered view of another ToolsProvider based on allowed tool names."""

    def __init__(
        self, wrapped_provider: ToolsProvider, allowed_tool_names: set[str] | None
    ) -> None:
        """
        Initialize the filtered provider.

        Args:
            wrapped_provider: The provider to filter
            allowed_tool_names: Set of allowed tool names. If None, all tools are allowed.
        """
        self._wrapped_provider = wrapped_provider
        self._allowed_tool_names = allowed_tool_names
        self._filtered_definitions: list[dict[str, Any]] | None = None

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get filtered tool definitions."""
        if self._filtered_definitions is None:
            all_definitions = await self._wrapped_provider.get_tool_definitions()
            if self._allowed_tool_names is None:
                # No filtering - return all tools
                self._filtered_definitions = all_definitions
            else:
                # Filter to only allowed tools
                self._filtered_definitions = [
                    d
                    for d in all_definitions
                    if d.get("function", {}).get("name") in self._allowed_tool_names
                ]
        return self._filtered_definitions

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Execute a tool if it's allowed."""
        if (
            self._allowed_tool_names is not None
            and name not in self._allowed_tool_names
        ):
            raise ToolNotFoundError(f"Tool '{name}' is not available in this profile")
        return await self._wrapped_provider.execute_tool(name, arguments, context)

    async def close(self) -> None:
        """Close the provider. Does nothing since wrapped provider is shared."""
        # Don't close the wrapped provider - it's shared across multiple filtered views
        pass


class ConfirmingToolsProvider(ToolsProvider):
    """Wraps another provider to add confirmation for specific tools."""

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        tools_requiring_confirmation: set[str],
        confirmation_timeout: float = 3600.0,
    ) -> None:
        self.wrapped_provider = wrapped_provider
        self._tools_requiring_confirmation = tools_requiring_confirmation
        self.confirmation_timeout = confirmation_timeout
        self._tool_definitions: list[dict[str, Any]] | None = None
        logger.info(
            f"ConfirmingToolsProvider initialized with tools requiring confirmation: {tools_requiring_confirmation}"
        )

    async def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Returns tool definitions from the wrapped provider."""
        if self._tool_definitions is None:
            self._tool_definitions = await self.wrapped_provider.get_tool_definitions()
            # Optionally, we could modify the descriptions to indicate confirmation required
        return self._tool_definitions

    async def _get_event_details_for_confirmation(
        self, tool_name: str, args: dict[str, Any], context: ToolExecutionContext
    ) -> dict[str, Any] | None:
        """Fetches additional details for tools that need them for confirmation.

        This is specifically for calendar tools that need to fetch event details
        before showing confirmation.
        """
        if tool_name in {"modify_calendar_event", "delete_calendar_event"}:
            uid = args.get("uid")
            calendar_url = args.get("calendar_url")
            if uid and calendar_url:
                # Try to get calendar config from the wrapped provider
                calendar_config = None

                # Check if wrapped provider is LocalToolsProvider with calendar config
                if isinstance(self.wrapped_provider, LocalToolsProvider):
                    calendar_config = self.wrapped_provider.get_calendar_config()
                    logger.debug(
                        f"Found calendar config in wrapped LocalToolsProvider: {bool(calendar_config)}"
                    )
                # Check if it's a CompositeToolsProvider wrapping a LocalToolsProvider
                elif isinstance(self.wrapped_provider, CompositeToolsProvider):
                    logger.debug(
                        "Wrapped provider is CompositeToolsProvider, checking providers..."
                    )
                    for provider in self.wrapped_provider.get_providers():
                        if isinstance(provider, LocalToolsProvider):
                            calendar_config = provider.get_calendar_config()
                            logger.debug(
                                f"Found calendar config in provider {type(provider).__name__}: {bool(calendar_config)}"
                            )
                            break
                else:
                    logger.debug(
                        f"Wrapped provider type: {type(self.wrapped_provider).__name__}, has no calendar config"
                    )

                if calendar_config:
                    try:
                        # Fetch event details using the dedicated function
                        event_details = await calendar_integration.fetch_event_details_for_confirmation(
                            uid=uid,
                            calendar_url=calendar_url,
                            calendar_config=calendar_config,
                        )
                        return event_details
                    except Exception as e:
                        logger.error(
                            f"Failed to fetch event details for {tool_name}: {e}"
                        )
        return None

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Executes tool with confirmation if required."""
        # Skip type alias here to avoid the error

        # Ensure definitions are loaded to know which tools need confirmation
        if self._tool_definitions is None:
            await self.get_tool_definitions()

        if name in self._tools_requiring_confirmation:
            logger.info(f"Tool '{name}' requires user confirmation.")
            if not context.request_confirmation_callback:
                logger.error(
                    f"Cannot request confirmation for tool '{name}': No callback provided in ToolExecutionContext."
                )
                return f"Error: Tool '{name}' requires confirmation, but the system is not configured to ask for it."

            # Request confirmation via callback (which handles Future creation/waiting)
            # Note: The callback is responsible for rendering the confirmation prompt
            try:
                logger.debug(f"Requesting confirmation for tool '{name}' via callback.")

                typed_callback = context.request_confirmation_callback

                # The callback is expected to handle the timeout internally via asyncio.wait_for
                # Pass arguments by keyword to ensure correct mapping, especially for mocks.
                # Call with positional arguments to match expected signature
                user_confirmed = await typed_callback(
                    context.interface_type,
                    context.conversation_id,
                    context.turn_id,
                    name,
                    call_id
                    or f"tool_{uuid.uuid4()}",  # Generate a call_id if none provided
                    arguments,
                    self.confirmation_timeout,
                )

                if user_confirmed:
                    logger.info(
                        f"User confirmed execution for tool '{name}'. Proceeding."
                    )
                    # Execute the tool using the wrapped provider
                    return await self.wrapped_provider.execute_tool(
                        name, arguments, context, call_id
                    )
                else:
                    logger.info(f"User cancelled execution for tool '{name}'.")
                    return f"OK. Action cancelled by user for tool '{name}'."

            except asyncio.TimeoutError:
                logger.warning(f"Confirmation request for tool '{name}' timed out.")
                return f"Action cancelled: Confirmation request for tool '{name}' timed out."
            except asyncio.CancelledError:
                logger.info(f"Confirmation request for tool '{name}' was cancelled.")
                return f"Action cancelled: Confirmation request for tool '{name}' was cancelled."
            except Exception as conf_err:
                logger.error(
                    f"Error during confirmation request for tool '{name}': {conf_err}",
                    exc_info=True,
                )
                return (
                    f"Error during confirmation process for tool '{name}': {conf_err}"
                )
        else:
            # Tool does not require confirmation, execute directly
            logger.debug(
                f"Tool '{name}' does not require confirmation. Executing directly."
            )
            return await self.wrapped_provider.execute_tool(
                name, arguments, context, call_id
            )

    async def close(self) -> None:
        """Closes the wrapped provider."""
        logger.info(
            f"Closing ConfirmingToolsProvider by closing wrapped provider {type(self.wrapped_provider).__name__}..."
        )
        await self.wrapped_provider.close()
        logger.info("ConfirmingToolsProvider finished closing wrapped provider.")
