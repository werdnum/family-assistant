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
import re
import uuid
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from family_assistant.security.taint import (
    TaintPolicyConfig,
    TaintPolicyEvaluation,
    TaintPolicyEvaluator,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSourceType,
    TurnTaintState,
    derive_tool_result_taint_source,
)
from family_assistant.tools.attachment_utils import (
    is_attachment_id,
    process_attachment_arguments,
)
from family_assistant.tools.confirmation import confirmation_payload_block_reason
from family_assistant.tools.metadata import (
    ToolDescriptor,
    ToolRegistration,
    build_local_tool_descriptors,
)
from family_assistant.tools.policy import PolicyEngine, ToolPolicyDecision
from family_assistant.tools.taint_helpers import merge_artifact_taint_into_context
from family_assistant.tools.types import (
    CalendarConfig,
    ConfirmationOutcome,
    RequestConfirmationCallback,
    ToolArguments,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.storage.types import (
        TaintAuditArgumentsSummary,
        TaintAuditSourceSummary,
    )

logger = logging.getLogger(__name__)


def _annotation_contains_type_name(annotation: object, type_name: str) -> bool:
    """Return whether an annotation or its union args include a named type."""
    if isinstance(annotation, str):
        identifiers = re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\b", annotation)
        return any(
            identifier == type_name or identifier.endswith(f".{type_name}")
            for identifier in identifiers
        )
    if getattr(annotation, "__name__", None) == type_name:
        return True
    if get_origin(annotation) is None:
        return False
    return any(
        _annotation_contains_type_name(arg, type_name) for arg in get_args(annotation)
    )


def translate_attachment_schemas_for_llm(
    tool_definitions: Sequence[ToolDefinition],
) -> list[ToolDefinition]:
    """
    Translate tool schemas for LLM compatibility by converting attachment types.

    Transforms 'type': 'attachment' parameters to 'type': 'string' with descriptive text
    explaining that the LLM should provide an attachment UUID.

    Args:
        tool_definitions: List of tool definitions with potentially internal attachment types

    Returns:
        List of tool definitions compatible with LLMs (no custom attachment types)
    """

    translated_definitions: list[ToolDefinition] = list(copy.deepcopy(tool_definitions))

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


# Backwards-compatible alias for public API exports.
ConfirmationCallbackProtocol = RequestConfirmationCallback


def confirmation_outcome_to_tool_result(
    *,
    name: str,
    outcome: ConfirmationOutcome,
) -> str | ToolResult:
    """Convert a terminal confirmation outcome into a tool execution result."""
    if outcome.kind == "approved":
        return f"Error: Confirmation for tool '{name}' was approved but not executed."
    if outcome.kind == "completed":
        return outcome.result if outcome.result is not None else ""
    if outcome.kind == "timed_out":
        return f"Action cancelled: Confirmation request for tool '{name}' timed out."
    if outcome.kind == "cancelled":
        return (
            f"Action cancelled: Confirmation request for tool '{name}' was cancelled."
        )
    if outcome.kind == "failed":
        if outcome.result is not None:
            return outcome.result
        return f"Error executing approved tool '{name}'."
    return f"OK. Action cancelled by user for tool '{name}'."


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


@runtime_checkable
class ToolProviderWrapper(Protocol):
    """Protocol for providers that wrap another provider."""

    @property
    def wrapped_provider(self) -> ToolsProvider: ...


@runtime_checkable
class ToolProviderComposite(Protocol):
    """Protocol for providers that contain multiple sub-providers."""

    def get_providers(self) -> list[ToolsProvider]: ...


@runtime_checkable
class ToolDescriptorProvider(Protocol):
    """Protocol for providers that can expose tool descriptors."""

    async def get_tool_descriptors(self) -> list[ToolDescriptor]: ...

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None: ...


@runtime_checkable
class SystemPromptContributingProvider(Protocol):
    """Protocol for providers that contribute text to the LLM system prompt.

    Used by providers that need to inject context (e.g. an on-demand tool catalog)
    without the LLM loop having to know about their concrete type.

    ``activated`` carries any turn-local activation state the caller wants to
    convey to contributors that need it (currently the on-demand provider).
    Contributors that do not care about activation may simply ignore it.
    """

    async def get_system_prompt_addition(
        self,
        *,
        can_confirm: bool = True,
        activated: Iterable[str] | None = None,
    ) -> str | None: ...


async def collect_system_prompt_addition(
    provider: ToolsProvider,
    *,
    can_confirm: bool = True,
    activated: Iterable[str] | None = None,
) -> str | None:
    """Walk the provider chain and join system prompt additions from contributors.

    ``can_confirm`` and ``activated`` are forwarded to contributors so the
    catalog they emit matches the policy filtering and turn-local activation
    state of the current interaction. Returns ``None`` when no provider in the
    chain contributes anything.
    """
    additions: list[str] = []
    activated_frozen: frozenset[str] | None = (
        None if activated is None else frozenset(activated)
    )

    async def _walk(current: ToolsProvider) -> None:
        if isinstance(current, SystemPromptContributingProvider):
            text = await current.get_system_prompt_addition(
                can_confirm=can_confirm, activated=activated_frozen
            )
            if text:
                additions.append(text)
        if isinstance(current, ToolProviderWrapper):
            await _walk(current.wrapped_provider)
        elif isinstance(current, ToolProviderComposite):
            for sub_provider in current.get_providers():
                await _walk(sub_provider)

    await _walk(provider)
    if not additions:
        return None
    return "\n\n".join(additions)


class ToolsProvider(Protocol):
    """Protocol for tool providers.

    Defines the interface that all tool providers must implement to provide
    tool definitions and execute tools.
    """

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Returns a list of tool definitions in LLM-compatible format.

        Returns:
            List of tool definition dictionaries following the standard
            tool definition schema.
        """
        ...

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
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


class ToolPolicyDeniedError(Exception):
    """Raised when a tool execution is denied by the policy engine.

    Deliberately NOT a subclass of ToolNotFoundError: CompositeToolsProvider
    catches ToolNotFoundError to fall through to the next provider, so a denial
    must not be silently retried on another provider.
    """

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"Tool '{tool_name}' denied by policy: {reason}")


async def get_tool_definitions_for_advertisement(
    provider: ToolsProvider,
    *,
    can_confirm: bool,
) -> list[ToolDefinition]:
    """Return advertisable tools, using confirmation-aware providers when available."""
    method = provider.get_tool_definitions
    parameters = inspect.signature(method).parameters.values()
    if any(
        parameter.name == "can_confirm"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return await cast("Any", method)(can_confirm=can_confirm)
    return await method()


def resolve_descriptors_version(provider: ToolsProvider) -> int:
    """Return a monotonic version of the descriptor set ``provider`` exposes.

    Providers whose descriptor set can change at runtime (currently
    ``MCPToolsProvider``, whose servers connect/reconnect/disconnect) expose a
    ``descriptors_version`` that increments on every change. Wrappers forward
    their inner provider's version; composites combine their children's.
    Providers without the attribute are treated as static (version 0).

    Consumers that cache policy-filtered tool listings for the process lifetime
    compare this against the version their cache was built at, so a server that
    was down at startup and later reconnects invalidates the stale cache instead
    of staying permanently unadvertisable.
    """
    version = getattr(provider, "descriptors_version", None)
    if isinstance(version, int):
        return version
    if isinstance(provider, ToolProviderWrapper):
        return resolve_descriptors_version(provider.wrapped_provider)
    if isinstance(provider, ToolProviderComposite):
        return sum(resolve_descriptors_version(sub) for sub in provider.get_providers())
    return 0


class LocalToolsProvider:
    """Provides and executes locally defined Python functions as tools."""

    def __init__(
        self,
        definitions: Sequence[ToolDefinition] | None = None,
        # ast-grep-ignore: no-dict-any - Implementation map has heterogeneous callable types
        implementations: dict[str, Any] | None = None,  # dict[str, Callable]
        embedding_generator: EmbeddingGenerator | None = None,
        calendar_config: CalendarConfig | None = None,
        registrations: Sequence[ToolRegistration] | None = None,
        descriptors: Sequence[ToolDescriptor] | None = None,
    ) -> None:
        if registrations is not None:
            self._registrations = list(registrations)
            self._definitions = [
                registration.definition for registration in self._registrations
            ]
            self._implementations = {
                registration.name: registration.implementation
                for registration in self._registrations
            }
            self._descriptors = (
                list(descriptors)
                if descriptors is not None
                else build_local_tool_descriptors(self._registrations)
            )
        else:
            if definitions is None or implementations is None:
                msg = (
                    "LocalToolsProvider requires either registrations or both "
                    "definitions and implementations."
                )
                raise ValueError(msg)
            self._registrations = None
            self._definitions = list(definitions)
            self._implementations = implementations
            self._descriptors = list(descriptors) if descriptors is not None else []
        self._embedding_generator = embedding_generator
        self._calendar_config = calendar_config
        logger.info(
            f"LocalToolsProvider initialized with {len(self._definitions)} tools: {list(self._implementations.keys())}"
        )
        if self._embedding_generator:
            logger.info(
                f"LocalToolsProvider configured with embedding generator: {type(self._embedding_generator).__name__}"
            )

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Get tool definitions translated for LLM compatibility.

        Returns tool definitions with attachment types converted to string types
        with UUID descriptions for LLM compatibility.
        """
        return translate_attachment_schemas_for_llm(self._definitions)

    def get_raw_tool_definitions(self) -> list[ToolDefinition]:
        """Get raw internal tool definitions without LLM translation.

        This is used internally by the script API to detect attachment types
        before translation.

        Returns:
            Raw tool definitions with original attachment types
        """
        return self._definitions

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return the provider's internal tool descriptors."""
        return list(self._descriptors)

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return a single descriptor by tool name."""
        for descriptor in self._descriptors:
            if descriptor.name == name:
                return descriptor
        return None

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
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

            # Process attachment arguments (convert string IDs to ScriptAttachment objects)
            try:
                # Find the tool definition to know which parameters are attachments
                tool_definition = None
                for definition in self._definitions:
                    if definition.get("function", {}).get("name") == name:
                        tool_definition = definition
                        break

                call_args = await process_attachment_arguments(
                    call_args, context, tool_definition
                )
            except ValueError as e:
                logger.error(f"Attachment processing failed for tool '{name}': {e}")
                return f"Error: {str(e)}"
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
            requires_embedding_generator = False
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
                    # Fallback for unresolved forward reference string
                    elif isinstance(param.annotation, str) and (
                        param.annotation == "DatabaseContext"
                        or param.annotation.endswith(".DatabaseContext")
                    ):
                        needs_db_context = True
                        logger.debug(
                            f"Identified 'db_context' for {callable_func.__name__} via string forward reference fallback."
                        )

                elif param_name == "embedding_generator":
                    # Check for EmbeddingGenerator by name since we can't import it
                    if _annotation_contains_type_name(
                        annotation_to_check,
                        "EmbeddingGenerator",
                    ):
                        needs_embedding_generator = True
                        requires_embedding_generator = (
                            param.default is inspect.Parameter.empty
                        )
                    # Also check for string annotation
                    elif (
                        isinstance(param.annotation, str)
                        and param.annotation == "EmbeddingGenerator"
                    ):
                        needs_embedding_generator = True
                        requires_embedding_generator = (
                            param.default is inspect.Parameter.empty
                        )
                        logger.debug(
                            f"Identified 'embedding_generator' for {callable_func.__name__} via string annotation."
                        )

                elif param_name == "calendar_config":
                    if (
                        annotation_to_check == "CalendarConfig"
                        or annotation_to_check == dict[str, Any]
                    ):
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
                    elif annotation_to_check == CalendarConfig:
                        needs_calendar_config = True

            # Inject dependencies based on resolved needs
            if needs_exec_context:
                call_args["exec_context"] = context
            if needs_db_context:  # db_context is part of ToolExecutionContext
                call_args["db_context"] = context.db_context
            if needs_embedding_generator:
                if self._embedding_generator:
                    call_args["embedding_generator"] = self._embedding_generator
                elif requires_embedding_generator:
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
                    f"Local tool '{name}' returned ToolResult with attachments: {result.attachments is not None and len(result.attachments) > 0 if result.attachments else False}"
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

    def get_calendar_config(self) -> CalendarConfig | None:
        """Get the calendar configuration."""
        return self._calendar_config

    async def close(self) -> None:
        """Local provider has no resources to clean up."""
        logger.debug("Closing LocalToolsProvider (no-op).")


class CompositeToolsProvider(ToolsProvider):
    """Combines multiple tool providers into a single interface."""

    def __init__(self, providers: list[ToolsProvider]) -> None:
        self._providers = providers
        logger.info(
            f"CompositeToolsProvider initialized with {len(providers)} providers"
        )

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Returns combined tool definitions from all providers."""
        all_definitions: list[ToolDefinition] = []
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

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return combined descriptors from providers that expose them."""
        descriptors: list[ToolDescriptor] = []
        for provider in self._providers:
            if not isinstance(provider, ToolDescriptorProvider):
                continue
            try:
                descriptors.extend(await provider.get_tool_descriptors())
            except Exception as e:
                logger.error(
                    f"Error getting tool descriptors from {type(provider).__name__}: {e}",
                    exc_info=True,
                )
        return descriptors

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return the first matching descriptor by name."""
        for provider in self._providers:
            if not isinstance(provider, ToolDescriptorProvider):
                continue
            try:
                descriptor = await provider.get_tool_descriptor(name)
                if descriptor is not None:
                    return descriptor
            except Exception as e:
                logger.error(
                    f"Error getting tool descriptor '{name}' from {type(provider).__name__}: {e}",
                    exc_info=True,
                )
        return None

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
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


def _describe_descriptor_origin(descriptor: ToolDescriptor) -> str:
    """Return a log-friendly description of where a descriptor comes from."""
    if descriptor.origin == "mcp":
        return f"MCP (server {descriptor.mcp_server_id!r})"
    return descriptor.origin


class PolicyEnforcingToolsProvider(ToolsProvider):
    """Wraps another provider and enforces policy allow/deny/confirm decisions."""

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        policy_engine: PolicyEngine,
        confirmation_timeout: float = 3600.0,
    ) -> None:
        if not isinstance(wrapped_provider, ToolDescriptorProvider):
            msg = (
                "PolicyEnforcingToolsProvider requires a wrapped provider that "
                "supports tool descriptors."
            )
            raise ValueError(msg)

        self.wrapped_provider = wrapped_provider
        self._descriptor_provider = wrapped_provider
        self._policy_engine = policy_engine
        self.confirmation_timeout = confirmation_timeout
        self._tool_definitions_by_confirmation: dict[bool, list[ToolDefinition]] = {}
        self._cached_descriptors_version: int | None = None

    async def get_tool_definitions(
        self,
        *,
        can_confirm: bool = True,
    ) -> list[ToolDefinition]:
        """Return tool definitions that are advertisable in this interaction."""
        current_version = resolve_descriptors_version(self._descriptor_provider)
        if current_version != self._cached_descriptors_version:
            # An MCP server connected, reconnected, or disconnected since this
            # cache was built. Drop it so the newly available (or removed) tools
            # are re-evaluated instead of serving a stale advertised set.
            self._tool_definitions_by_confirmation.clear()
            self._cached_descriptors_version = current_version

        if can_confirm in self._tool_definitions_by_confirmation:
            return self._tool_definitions_by_confirmation[can_confirm]

        # Execution resolves a name to the *first* descriptor with that name
        # (composite order, local-before-MCP), so advertisement must evaluate
        # policy against that same descriptor. Treating a name as allowed when
        # *any* same-named descriptor passes would advertise a tool whose
        # execution is denied (and hide the allowed one behind it).
        first_descriptor_by_name: dict[str, ToolDescriptor] = {}
        for descriptor in await self._descriptor_provider.get_tool_descriptors():
            winner = first_descriptor_by_name.setdefault(descriptor.name, descriptor)
            if winner is not descriptor:
                # A collision is almost always operator misconfiguration or a
                # misbehaving MCP server; the shadowed tool silently loses
                # otherwise, so make it loud (ERROR reaches the persistent
                # error log surfaced by the diagnostics endpoints).
                logger.error(
                    "Tool name collision on %r: %s descriptor is shadowed by "
                    "the first-registered %s descriptor and is unreachable. "
                    "Rename the tool or deny the shadowed origin by policy.",
                    descriptor.name,
                    _describe_descriptor_origin(descriptor),
                    _describe_descriptor_origin(winner),
                )
        allowed_names = {
            name
            for name, descriptor in first_descriptor_by_name.items()
            if self._policy_engine.evaluate_for_advertisement(
                descriptor,
                can_confirm=can_confirm,
            ).decision
            is not ToolPolicyDecision.DENY
        }

        # Dedupe by function name (first occurrence wins, matching the
        # first-registered precedence the root composite and MCP _tool_map use).
        # A server that was down at startup can reconnect with a name that
        # collides with a local/root tool; advertising both would send duplicate
        # function declarations, which some LLM APIs reject.
        advertised: list[ToolDefinition] = []
        seen_names: set[str] = set()
        for definition in await self.wrapped_provider.get_tool_definitions():
            name = definition.get("function", {}).get("name")
            if name not in allowed_names or name in seen_names:
                continue
            seen_names.add(name)
            advertised.append(definition)
        self._tool_definitions_by_confirmation[can_confirm] = advertised
        return self._tool_definitions_by_confirmation[can_confirm]

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return descriptors for tools that are not denied by policy."""
        return [
            descriptor
            for descriptor in await self._descriptor_provider.get_tool_descriptors()
            if self._policy_engine.evaluate(descriptor).decision
            is not ToolPolicyDecision.DENY
        ]

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return a descriptor for a tool that is not denied by policy."""
        descriptor = await self._descriptor_provider.get_tool_descriptor(name)
        if descriptor is None:
            return None
        if self._policy_engine.evaluate(descriptor).decision is ToolPolicyDecision.DENY:
            return None
        return descriptor

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Execute a tool while enforcing policy decisions."""
        descriptor = await self._descriptor_provider.get_tool_descriptor(name)
        if descriptor is None:
            raise ToolNotFoundError(name, type(self).__name__)

        evaluation = self._policy_engine.evaluate_for_execution(
            descriptor,
            arguments=arguments,
            can_confirm=context.request_confirmation_callback is not None,
        )
        if evaluation.decision is ToolPolicyDecision.DENY:
            logger.info(
                "Tool '%s' denied by policy during execution: %s",
                name,
                evaluation.reason,
            )
            raise ToolPolicyDeniedError(name, evaluation.reason or "denied by policy")

        if evaluation.decision is ToolPolicyDecision.CONFIRM:
            # Refuse a confirm-gated call whose confirmation prompt could not show
            # the approver the full payload, instead of rendering a misleading
            # prompt. Scoped to confirm-gated calls, so unconfirmed calls are
            # never constrained by it.
            block_reason = confirmation_payload_block_reason(name, arguments)
            if block_reason is not None:
                logger.info("Refusing confirm-gated tool '%s': %s", name, block_reason)
                return ToolResult(text=block_reason, attachments=None)

            logger.info("Tool '%s' requires policy confirmation.", name)
            if not context.request_confirmation_callback:
                raise ToolNotFoundError(name, type(self).__name__)

            try:
                typed_callback = context.request_confirmation_callback
                resolved_call_id = call_id or f"tool_{uuid.uuid4()}"

                if context.tools_provider is None:
                    context.tools_provider = self

                confirmation_result = await typed_callback(
                    interface_type=context.interface_type,
                    conversation_id=context.conversation_id,
                    turn_id=context.turn_id,
                    tool_name=name,
                    call_id=resolved_call_id,
                    tool_args=arguments,
                    timeout_seconds=self.confirmation_timeout,
                    context=context,
                )

                if confirmation_result.kind != "approved":
                    return confirmation_outcome_to_tool_result(
                        name=name,
                        outcome=confirmation_result,
                    )
            except TimeoutError:
                logger.warning("Confirmation request for tool '%s' timed out.", name)
                return f"Action cancelled: Confirmation request for tool '{name}' timed out."
            except asyncio.CancelledError:
                logger.info(
                    "Confirmation request for tool '%s' was cancelled.",
                    name,
                )
                return f"Action cancelled: Confirmation request for tool '{name}' was cancelled."
            except Exception as conf_err:
                logger.error(
                    "Error during confirmation request for tool '%s': %s",
                    name,
                    conf_err,
                    exc_info=True,
                )
                return (
                    f"Error during confirmation process for tool '{name}': {conf_err}"
                )

        return await self.wrapped_provider.execute_tool(
            name, arguments, context, call_id
        )

    async def close(self) -> None:
        """Close the wrapped provider."""
        await self.wrapped_provider.close()


def _taint_audit_sources(state: TurnTaintState) -> list[TaintAuditSourceSummary]:
    return [
        {
            "source_type": source.source_type.value,
            "source_id": source.source_id,
            "tier": source.tier.config_value,
            "labels": sorted(source.labels),
            "reason": source.reason,
        }
        for source in state.sources
    ]


def _summarize_tool_arguments(
    arguments: Mapping[str, object],
) -> TaintAuditArgumentsSummary:
    """Return an audit-safe shape summary for tool arguments."""
    keys = sorted(str(key) for key in arguments)
    return {
        "keys": keys,
        "value_types": {
            str(key): type(value).__name__ for key, value in sorted(arguments.items())
        },
    }


def _collect_attachment_argument_ids(
    value: object,
    *,
    schema: object = None,
    in_attachment_slot: bool = False,
) -> set[str]:
    schema_is_attachment = (
        isinstance(schema, dict) and schema.get("type") == "attachment"
    )
    in_attachment_slot = in_attachment_slot or schema_is_attachment
    if isinstance(value, dict):
        attachment_ids: set[str] = set()
        schema_properties = (
            schema.get("properties", {}) if isinstance(schema, dict) else {}
        )
        for key, child in value.items():
            child_key = str(key).lower()
            child_schema = (
                schema_properties.get(key)
                if isinstance(schema_properties, dict)
                else None
            )
            child_in_attachment_slot = in_attachment_slot or (
                "attachment" in child_key and "id" in child_key
            )
            attachment_ids.update(
                _collect_attachment_argument_ids(
                    child,
                    schema=child_schema,
                    in_attachment_slot=child_in_attachment_slot,
                )
            )
        return attachment_ids
    if isinstance(value, list | tuple):
        attachment_ids = set()
        item_schema = schema.get("items") if isinstance(schema, dict) else None
        for child in value:
            attachment_ids.update(
                _collect_attachment_argument_ids(
                    child,
                    schema=item_schema,
                    in_attachment_slot=in_attachment_slot,
                )
            )
        return attachment_ids
    if in_attachment_slot and is_attachment_id(value):
        return {cast("str", value)}
    return set()


def _tool_parameters_schema(descriptor: ToolDescriptor) -> object:
    function_definition = descriptor.definition.get("function")
    if not isinstance(function_definition, dict):
        return None
    return function_definition.get("parameters")


class TaintTrackingToolsProvider(ToolsProvider):
    """Wraps another provider with runtime taint policy and result tracking."""

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        taint_policy: TaintPolicyConfig | None = None,
        confirmation_timeout: float = 3600.0,
    ) -> None:
        if not isinstance(wrapped_provider, ToolDescriptorProvider):
            msg = (
                "TaintTrackingToolsProvider requires a wrapped provider that "
                "supports tool descriptors."
            )
            raise ValueError(msg)
        self.wrapped_provider = wrapped_provider
        self._descriptor_provider = wrapped_provider
        self._taint_policy_config = taint_policy or TaintPolicyConfig()
        self._taint_evaluator = TaintPolicyEvaluator(self._taint_policy_config)
        self.confirmation_timeout = confirmation_timeout

    async def get_tool_definitions(
        self,
        *,
        can_confirm: bool = True,
    ) -> list[ToolDefinition]:
        """Return wrapped tool definitions unchanged."""
        return await get_tool_definitions_for_advertisement(
            self.wrapped_provider,
            can_confirm=can_confirm,
        )

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return wrapped tool descriptors unchanged."""
        return await self._descriptor_provider.get_tool_descriptors()

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return the wrapped descriptor for a tool."""
        return await self._descriptor_provider.get_tool_descriptor(name)

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Execute a tool and merge result taint into the turn tracker."""
        descriptor = await self._descriptor_provider.get_tool_descriptor(name)
        if descriptor is not None and context.taint_tracker is not None:
            argument_taint_merged = await self._merge_argument_taint_into_context(
                arguments,
                context,
                descriptor,
            )
            state = context.taint_tracker.snapshot()
            if context.taint_policy_snapshot is not None and not argument_taint_merged:
                state = context.taint_policy_snapshot
            evaluation = self._taint_evaluator.evaluate_tool(
                descriptor=descriptor,
                state=state,
            )
            logger.info(
                "Runtime taint policy evaluated: tool=%s call_id=%s sink=%s "
                "requested=%s effective=%s mode=%s reason=%s",
                name,
                call_id,
                evaluation.sink_class.value,
                evaluation.requested_outcome.value,
                evaluation.effective_outcome.value,
                evaluation.mode.value,
                evaluation.reason,
            )
            if (
                evaluation.mode is TaintPolicyMode.OBSERVE
                and evaluation.requested_outcome is not evaluation.effective_outcome
            ):
                # Observe mode downgraded a gating outcome (confirm/deny/redact) to
                # audit. Surface it at ERROR so a dry run can be reviewed from the
                # error-log / diagnostics endpoints before enabling enforce mode.
                logger.error(
                    "Runtime taint WOULD ENFORCE (observe mode, not blocked): "
                    "tool=%s call_id=%s conversation=%s sink=%s would_be=%s "
                    "max_tier=%s reason=%s",
                    name,
                    call_id,
                    context.conversation_id,
                    evaluation.sink_class.value,
                    evaluation.requested_outcome.value,
                    state.max_tier.config_value,
                    evaluation.reason,
                )
            await self._record_policy_evaluation_audit(
                descriptor=descriptor,
                context=context,
                call_id=call_id,
                arguments=arguments,
                state=state,
                evaluation=evaluation,
            )
            if evaluation.effective_outcome is TaintPolicyOutcome.DENY:
                raise ToolPolicyDeniedError(name, evaluation.reason)
            if evaluation.effective_outcome is TaintPolicyOutcome.REDACT:
                raise ToolPolicyDeniedError(
                    name,
                    f"{evaluation.reason}; redaction outcomes are not executable yet",
                )
            if evaluation.effective_outcome is TaintPolicyOutcome.CONFIRM:
                state_before_confirmation = (
                    context.taint_tracker.snapshot()
                    if context.taint_tracker is not None
                    else None
                )
                confirmation_result = await self._request_taint_confirmation(
                    name=name,
                    arguments=arguments,
                    context=context,
                    call_id=call_id,
                    reason=evaluation.reason,
                )
                if confirmation_result is not None:
                    if descriptor is not None:
                        await self._record_result_taint_and_audit(
                            descriptor=descriptor,
                            context=context,
                            call_id=call_id,
                            state_before_execution=state_before_confirmation,
                        )
                    return confirmation_result

        state_before_execution = (
            context.taint_tracker.snapshot()
            if context.taint_tracker is not None
            else None
        )
        try:
            result = await self.wrapped_provider.execute_tool(
                name,
                arguments,
                context,
                call_id,
            )
        except Exception:
            if descriptor is not None:
                await self._record_result_taint_and_audit(
                    descriptor=descriptor,
                    context=context,
                    call_id=call_id,
                    state_before_execution=state_before_execution,
                )
            raise

        if descriptor is not None:
            await self._record_result_taint_and_audit(
                descriptor=descriptor,
                context=context,
                call_id=call_id,
                state_before_execution=state_before_execution,
            )
        return result

    async def _merge_argument_taint_into_context(
        self,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        descriptor: ToolDescriptor,
    ) -> bool:
        if context.taint_tracker is None or context.attachment_registry is None:
            return False

        attachment_ids = _collect_attachment_argument_ids(
            arguments,
            schema=_tool_parameters_schema(descriptor),
        )
        if not attachment_ids:
            return False

        before = context.taint_tracker.snapshot()
        metadata_by_id = await context.attachment_registry.get_attachments(
            context.db_context,
            sorted(attachment_ids),
            acting_user_id=context.user_id,
        )
        for attachment_id, metadata in metadata_by_id.items():
            merge_artifact_taint_into_context(
                context,
                provenance_metadata=metadata.metadata,
                fallback_source_type=TaintSourceType.ATTACHMENT,
                fallback_source_id=attachment_id,
                fallback_reason="Tool argument attachment provenance.",
            )
        return context.taint_tracker.snapshot() != before

    async def _request_taint_confirmation(
        self,
        *,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
        reason: str,
    ) -> str | ToolResult | None:
        if context.request_confirmation_callback is None:
            raise ToolPolicyDeniedError(
                name,
                f"{reason}; confirmation required but unavailable",
            )

        block_reason = confirmation_payload_block_reason(name, arguments)
        if block_reason is not None:
            logger.info(
                "Refusing taint confirm-gated tool '%s': %s", name, block_reason
            )
            return ToolResult(text=block_reason, attachments=None)

        resolved_call_id = call_id or f"tool_{uuid.uuid4()}"
        if context.tools_provider is None:
            context.tools_provider = self
        outcome = await context.request_confirmation_callback(
            interface_type=context.interface_type,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            tool_name=name,
            call_id=resolved_call_id,
            tool_args=cast("ToolArguments", arguments),
            timeout_seconds=self.confirmation_timeout,
            context=context,
        )
        if outcome.kind == "approved":
            return None
        if outcome.kind == "completed":
            return outcome.result or ToolResult(text="", attachments=None)
        return confirmation_outcome_to_tool_result(name=name, outcome=outcome)

    async def _record_policy_evaluation_audit(
        self,
        *,
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
        call_id: str | None,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        state: TurnTaintState,
        evaluation: TaintPolicyEvaluation,
    ) -> None:
        await context.db_context.taint_audit_events.add(
            event_id=str(uuid.uuid4()),
            event_type="policy_evaluation",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            processing_profile_id=context.processing_profile_id,
            subconversation_id=context.subconversation_id,
            tool_name=descriptor.name,
            tool_call_id=call_id,
            sink_class=evaluation.sink_class.value,
            max_tier=state.max_tier.config_value,
            sources=_taint_audit_sources(state),
            requested_outcome=evaluation.requested_outcome.value,
            effective_outcome=evaluation.effective_outcome.value,
            mode=evaluation.mode.value,
            reason=evaluation.reason,
            arguments_summary=_summarize_tool_arguments(arguments),
        )

    def _record_result_taint(
        self,
        *,
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
        call_id: str | None,
        state_before_execution: TurnTaintState | None,
    ) -> TurnTaintState | None:
        source = derive_tool_result_taint_source(
            descriptor=descriptor,
            call_id=call_id,
            default_unspecified_tool_output_tier=(
                self._taint_policy_config.default_unspecified_tool_output_tier
            ),
        )
        if context.taint_tracker is None:
            return None
        if source is None:
            state = context.taint_tracker.snapshot()
            context.tool_result_taint_metadata[call_id or descriptor.name] = (
                state.to_metadata()
            )
            if (
                state_before_execution is None
                or state.sources == state_before_execution.sources
            ):
                return None
            logger.info(
                "Tool result inherited dynamic taint: tool=%s call_id=%s max_tier=%s",
                descriptor.name,
                call_id,
                state.max_tier.config_value,
            )
            return state

        state = context.taint_tracker.add_source(source)
        metadata = state.to_metadata()
        context.tool_result_taint_metadata[call_id or descriptor.name] = metadata
        logger.info(
            "Tool result taint recorded: tool=%s call_id=%s max_tier=%s",
            descriptor.name,
            call_id,
            state.max_tier.config_value,
        )
        return state

    async def _record_result_taint_and_audit(
        self,
        *,
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
        call_id: str | None,
        state_before_execution: TurnTaintState | None,
    ) -> None:
        recorded_state = self._record_result_taint(
            descriptor=descriptor,
            context=context,
            call_id=call_id,
            state_before_execution=state_before_execution,
        )
        if recorded_state is not None:
            await self._record_result_taint_audit(
                descriptor=descriptor,
                context=context,
                call_id=call_id,
                state=recorded_state,
            )

    async def _record_result_taint_audit(
        self,
        *,
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
    ) -> None:
        await context.db_context.taint_audit_events.add(
            event_id=str(uuid.uuid4()),
            event_type="result_taint",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            processing_profile_id=context.processing_profile_id,
            subconversation_id=context.subconversation_id,
            tool_name=descriptor.name,
            tool_call_id=call_id,
            sink_class=None,
            max_tier=state.max_tier.config_value,
            sources=_taint_audit_sources(state),
            requested_outcome=None,
            effective_outcome=None,
            mode=self._taint_evaluator.mode.value,
            reason=f"Recorded taint metadata for tool '{descriptor.name}' output.",
            arguments_summary=None,
        )

    async def close(self) -> None:
        """Close the wrapped provider."""
        await self.wrapped_provider.close()


def find_provider_by_type[T](
    provider: ToolsProvider, provider_type: type[T]
) -> T | None:
    """Traverses the tool provider chain to find a provider of a specific type.

    Handles wrapper and composite providers using structural protocols instead
    of dynamic attribute access.

    Args:
        provider: The root provider to start searching from
        provider_type: The type of provider to look for

    Returns:
        The provider instance if found, or None
    """
    if isinstance(provider, provider_type):
        return provider

    # Handle wrappers via structural typing
    if isinstance(provider, ToolProviderWrapper):
        return find_provider_by_type(provider.wrapped_provider, provider_type)

    # Handle composite providers via structural typing
    if isinstance(provider, ToolProviderComposite):
        for sub_provider in provider.get_providers():
            found = find_provider_by_type(sub_provider, provider_type)
            if found:
                return found

    return None
