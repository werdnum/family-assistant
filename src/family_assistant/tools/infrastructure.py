"""Infrastructure components for the tools module.

This module contains base classes, protocols, exceptions, and common utilities
used by all tool implementations.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from family_assistant.observability.metrics import (
    UNATTRIBUTED_PROFILE,
    UNKNOWN_TOOL,
    record_tool_call,
)
from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionGateOutcome,
    GateLayer,
    GateProvenance,
    PendingDefinitionReview,
)
from family_assistant.security.definition_resolution import attach_pending_verdict
from family_assistant.security.taint import (
    SensitiveReadScope,
    SinkClass,
    TaintPolicyConfig,
    TaintPolicyEvaluation,
    TaintPolicyEvaluator,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSourceType,
    TurnTaintState,
    derive_tool_result_taint_source,
    is_externally_authored,
    merge_taint_state_into_tracker,
    resolve_tool_sink_class,
)
from family_assistant.services.tool_call_review import (
    DelegatingPolicyContext,
    ToolCallReviewConstraints,
    ToolCallReviewer,
    ToolCallReviewInput,
    ToolCallReviewResult,
    ToolCallReviewStatus,
    ToolCallReviewVerdict,
    compute_trusted_destination_echo,
)
from family_assistant.storage.database import spawn_detached
from family_assistant.tools.attachment_utils import (
    is_attachment_id,
    process_attachment_arguments,
)
from family_assistant.tools.confirmation import confirmation_payload_block_reason
from family_assistant.tools.metadata import (
    ToolDescriptor,
    ToolRegistration,
    ToolTag,
    build_local_tool_descriptors,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyEvaluation,
    ToolPolicyDecision,
)
from family_assistant.tools.taint_helpers import (
    merge_artifact_taint_into_context,
)
from family_assistant.tools.types import (
    CalendarConfig,
    ConfirmationOutcome,
    DeferredConfirmationCallback,
    RequestConfirmationCallback,
    ToolArguments,
    ToolCallReviewAuthorization,
    ToolConfirmationAuthorization,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.config_models import ToolCallReviewConfig
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.storage.types import (
        TaintAuditArgumentsSummary,
        TaintAuditReviewContext,
        TaintAuditSourceSummary,
    )

logger = logging.getLogger(__name__)

_TOOL_CALL_REVIEW_AUDIT_REASON = "Automatic reviewer decision recorded; reviewer rationale omitted from durable audit."
_NAMED_SINK_CONFIRMATION_MAX_CHARS = 3800
_NO_TAINT_GATE_MODE = "none"
"""Recorded mode for a gate no runtime taint policy participated in."""


@dataclass(frozen=True, slots=True)
class _ConfirmationGateResult:
    """Terminal confirmation result plus whether execution was attempted."""

    result: str | ToolResult
    action_attempted: bool


def _review_authorization_matches(
    authorization: ToolCallReviewAuthorization | None,
    *,
    name: str,
    arguments: Mapping[str, object],
    call_id: str | None,
    sink_class: SinkClass,
    static_evaluation: PolicyEvaluation | None,
    taint_evaluation: TaintPolicyEvaluation | None,
) -> bool:
    """Consume an exact durable review authorization when all gates still match."""
    if authorization is None or authorization.consumed:
        return False
    resolved_call_id = call_id or ""
    if (
        authorization.tool_name != name
        or authorization.call_id != resolved_call_id
        or authorization.tool_args != arguments
        or authorization.sink_class != sink_class.value
    ):
        return False
    if (
        static_evaluation is not None
        and static_evaluation.decision is ToolPolicyDecision.REVIEW
        and authorization.static_policy_reason is None
    ):
        return False
    if (
        taint_evaluation is not None
        and taint_evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE
        and (
            authorization.taint_policy_reason is None
            or taint_evaluation.verdict_floor is TaintPolicyOutcome.DENY
        )
    ):
        return False
    authorization.consumed = True
    return True


def _review_authorization_for_confirmation(
    *,
    name: str,
    arguments: Mapping[str, object],
    call_id: str | None,
    sink_class: SinkClass,
    static_evaluation: PolicyEvaluation | None,
    taint_evaluation: TaintPolicyEvaluation | None,
) -> ToolCallReviewAuthorization:
    """Describe the exact reviewed call whose verdict escalated to a human."""
    return ToolCallReviewAuthorization(
        tool_name=name,
        call_id=call_id or "",
        tool_args=cast("ToolArguments", copy.deepcopy(dict(arguments))),
        sink_class=sink_class.value,
        static_policy_reason=(
            static_evaluation.reason
            if static_evaluation is not None
            and static_evaluation.decision is ToolPolicyDecision.REVIEW
            else None
        ),
        taint_policy_reason=(
            taint_evaluation.reason
            if taint_evaluation is not None
            and taint_evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE
            else None
        ),
    )


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


type AuthorizedToolExecutor = Callable[[], Awaitable[str | ToolResult]]
type PolicyExecutionCoordinator = Callable[
    [ToolDescriptor, PolicyEvaluation, AuthorizedToolExecutor],
    Awaitable[str | ToolResult],
]


@runtime_checkable
class PolicyCoordinatingToolsProvider(Protocol):
    """Provider that owns static-policy evaluation and authorized dispatch."""

    async def execute_with_policy_coordinator(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
        coordinator: PolicyExecutionCoordinator,
    ) -> str | ToolResult: ...


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

        async def _execute() -> str | ToolResult:
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
                return f"Error: {e!s}"
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
                    # Check for Database by name since we can't import it
                    if (
                        hasattr(annotation_to_check, "__name__")
                        and annotation_to_check.__name__ == "Database"
                    ):
                        needs_db_context = True
                    # Fallback for unresolved forward reference string
                    elif isinstance(param.annotation, str) and (
                        param.annotation == "Database"
                        or param.annotation.endswith(".Database")
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

        try:
            return await _execute()
        except Exception as e:
            logger.exception(f"Error executing local tool '{name}': {e}")
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
                logger.exception(
                    f"Error getting tool definitions from {type(provider).__name__}: {e}"
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
                logger.exception(
                    f"Error getting tool descriptors from {type(provider).__name__}: {e}"
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
                logger.exception(
                    f"Error getting tool descriptor '{name}' from {type(provider).__name__}: {e}"
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
                logger.exception(
                    f"Error executing tool '{name}' with provider {type(provider).__name__}: {e}"
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
                logger.exception(
                    f"Error closing provider {type(provider).__name__}: {e}"
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

    @property
    def policy_engine(self) -> PolicyEngine:
        """Return the policy engine backing this provider.

        Exposed for diagnostics (engineer profile policy resolution).
        """
        return self._policy_engine

    @property
    def descriptor_provider(self) -> ToolDescriptorProvider:
        """Return the unfiltered descriptor provider for outer coordinators."""
        return self._descriptor_provider

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
        """Execute through the provider's central static-policy chokepoint."""

        async def coordinate_default_policy(
            _descriptor: ToolDescriptor,
            evaluation: PolicyEvaluation,
            execute_authorized: AuthorizedToolExecutor,
        ) -> str | ToolResult:
            return await self._execute_default_policy_decision(
                name=name,
                arguments=arguments,
                context=context,
                call_id=call_id,
                evaluation=evaluation,
                execute_authorized=execute_authorized,
            )

        return await self.execute_with_policy_coordinator(
            name,
            arguments,
            context,
            call_id,
            coordinate_default_policy,
        )

    async def execute_with_policy_coordinator(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
        coordinator: PolicyExecutionCoordinator,
    ) -> str | ToolResult:
        """Evaluate policy once and dispatch only through an explicit coordinator."""
        descriptor = await self._descriptor_provider.get_tool_descriptor(name)
        if descriptor is None:
            raise ToolNotFoundError(name, type(self).__name__)

        evaluation = self._policy_engine.evaluate(
            descriptor,
            arguments=arguments,
        )
        if evaluation.decision is ToolPolicyDecision.DENY:
            logger.info(
                "Tool '%s' denied by policy during execution: %s",
                name,
                evaluation.reason,
            )
            raise ToolPolicyDeniedError(name, evaluation.reason or "denied by policy")

        async def execute_authorized() -> str | ToolResult:
            return await self.wrapped_provider.execute_tool(
                name, arguments, context, call_id
            )

        return await coordinator(descriptor, evaluation, execute_authorized)

    async def _execute_default_policy_decision(
        self,
        *,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
        evaluation: PolicyEvaluation,
        execute_authorized: AuthorizedToolExecutor,
    ) -> str | ToolResult:
        """Apply standalone confirmation semantics before authorized dispatch."""
        if evaluation.decision in {
            ToolPolicyDecision.CONFIRM,
            ToolPolicyDecision.REVIEW,
        }:
            # Refuse a confirm-gated call whose confirmation prompt could not show
            # the approver the full payload, instead of rendering a misleading
            # prompt. Scoped to confirm-gated calls, so unconfirmed calls are
            # never constrained by it.
            block_reason = confirmation_payload_block_reason(name, arguments)
            if block_reason is not None:
                logger.info("Refusing confirm-gated tool '%s': %s", name, block_reason)
                return ToolResult(text=block_reason, attachments=None)

            logger.info(
                "Tool '%s' requires policy confirmation%s.",
                name,
                " (review fallback)"
                if evaluation.decision is ToolPolicyDecision.REVIEW
                else "",
            )
            if not context.request_confirmation_callback:
                raise ToolPolicyDeniedError(
                    name,
                    f"{evaluation.reason}; confirmation required but unavailable",
                )

            typed_callback = context.request_confirmation_callback
            resolved_call_id = call_id or f"tool_{uuid.uuid4()}"
            if context.tools_provider is None:
                context.tools_provider = self

            try:
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
                logger.exception(
                    "Error during confirmation request for tool '%s': %s",
                    name,
                    conf_err,
                )
                return (
                    f"Error during confirmation process for tool '{name}': {conf_err}"
                )

        return await execute_authorized()

    async def close(self) -> None:
        """Close the wrapped provider."""
        await self.wrapped_provider.close()


def _taint_audit_sources(state: TurnTaintState) -> list[TaintAuditSourceSummary]:
    summaries: list[TaintAuditSourceSummary] = []
    for source in state.sources:
        trusted = not is_externally_authored(source.tier)
        summaries.append({
            # These are enum-backed, closed-vocabulary provenance fields.
            "source_type": source.source_type.value,
            "tier": source.tier.config_value,
            # Externally authored source metadata is deliberately stubbed, just
            # as it is in the reviewer prompt's provenance digest.
            "source_id": _bounded_audit_text(source.source_id, 256)
            if trusted and source.source_id is not None
            else None,
            "labels": (
                sorted(_bounded_audit_text(label, 64) for label in source.labels)[:16]
                if trusted
                else []
            ),
            "reason": (
                _bounded_audit_text(source.reason, 256)
                if trusted
                else "Externally authored source details omitted from audit."
            ),
        })
    return summaries


def _summarize_tool_arguments(
    arguments: Mapping[str, object],
    *,
    safe_keys: Collection[str] = (),
) -> TaintAuditArgumentsSummary:
    """Return argument shape, naming only keys declared by trusted tool schema."""
    allowed = set(safe_keys)
    items = sorted(arguments.items(), key=lambda item: str(item[0]))
    summarized: list[tuple[str, object]] = []
    for index, (key, value) in enumerate(items, start=1):
        raw_key = str(key)
        summarized.append((
            raw_key if raw_key in allowed else f"argument_{index}",
            value,
        ))
    return {
        "keys": [key for key, _value in summarized],
        "value_types": {key: type(value).__name__ for key, value in summarized},
    }


def _descriptor_argument_keys(descriptor: ToolDescriptor) -> frozenset[str]:
    """Return trusted top-level argument names declared by a tool descriptor."""
    # MCP schemas are supplied by the remote server. Their property names are
    # therefore untrusted content and must not be copied verbatim into durable
    # audit records; only local definitions are part of the trusted deployment.
    if descriptor.origin != "local":
        return frozenset()
    function = descriptor.definition.get("function")
    if not isinstance(function, Mapping):
        return frozenset()
    parameters = function.get("parameters")
    if not isinstance(parameters, Mapping):
        return frozenset()
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return frozenset()
    return frozenset(str(key) for key in properties)


def _normalize_audit_text(value: str) -> str:
    """Normalize one audit string, stripping controls and direction tricks."""
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in normalized
    )
    return " ".join(cleaned.split()).strip()


def _bounded_audit_text(value: str, limit: int) -> str:
    """Return normalized audit text within a fixed display bound."""
    cleaned = _normalize_audit_text(value)
    if len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned


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


_REVIEW_DISCLOSURE_SINKS = frozenset({
    SinkClass.ARBITRARY_EXTERNAL_MESSAGE,
    SinkClass.ATTACKER_ADDRESSABLE_EGRESS,
})


def _review_messages_are_current_turn_only(
    messages: Sequence[object] | None,
    *,
    active_request_role: Literal["user", "system"],
) -> bool:
    """Whether the reviewer window proves no conversation history was included."""
    # A system-role wake has no active user row. Searching backward for a user
    # would select historical conversation state and could incorrectly treat it
    # as the current confined request. Until the window carries an explicit
    # turn-boundary identity, only an active user request can prove confinement.
    if messages is None or active_request_role != "user":
        return False
    current_user_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        role = (
            message.get("role")
            if isinstance(message, Mapping)
            else getattr(message, "role", None)
        )
        is_scaffolding = (
            message.get("is_turn_scaffolding", False)
            if isinstance(message, Mapping)
            else getattr(message, "is_turn_scaffolding", False)
        )
        if role == "user" and not is_scaffolding:
            current_user_index = index
            break
    if current_user_index is None:
        return False
    return all(
        (
            message.get("role")
            if isinstance(message, Mapping)
            else getattr(message, "role", None)
        )
        == "system"
        for message in messages[:current_user_index]
    )


def _review_verdict_for_taint_outcome(
    outcome: TaintPolicyOutcome,
) -> ToolCallReviewVerdict:
    if outcome is TaintPolicyOutcome.DENY:
        return ToolCallReviewVerdict.DENY
    if outcome is TaintPolicyOutcome.CONFIRM:
        return ToolCallReviewVerdict.CONFIRM
    msg = f"Unsupported tool-call review constraint outcome: {outcome.value}"
    raise ValueError(msg)


def _strictest_review_verdict(
    verdicts: Iterable[ToolCallReviewVerdict],
) -> ToolCallReviewVerdict:
    rank = {
        ToolCallReviewVerdict.ALLOW: 0,
        ToolCallReviewVerdict.CONFIRM: 1,
        ToolCallReviewVerdict.DENY: 2,
    }
    return max(verdicts, key=rank.__getitem__)


def _review_verdict_space(
    floor: ToolCallReviewVerdict,
) -> frozenset[ToolCallReviewVerdict]:
    if floor is ToolCallReviewVerdict.DENY:
        return frozenset({ToolCallReviewVerdict.DENY})
    if floor is ToolCallReviewVerdict.CONFIRM:
        return frozenset({
            ToolCallReviewVerdict.CONFIRM,
            ToolCallReviewVerdict.DENY,
        })
    return frozenset(ToolCallReviewVerdict)


def _is_escalatable_review_denial(
    result: ToolCallReviewResult,
    constraints: ToolCallReviewConstraints,
) -> bool:
    """Return whether a model freely chose deny where confirm was available."""
    return (
        result.verdict is ToolCallReviewVerdict.DENY
        and result.status is ToolCallReviewStatus.MODEL_VERDICT
        and not result.used_fallback
        and ToolCallReviewVerdict.CONFIRM in constraints.available_verdicts
    )


def _argument_at_path(arguments: Mapping[str, object], path: str) -> object:
    value: object = arguments
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return None
        value = value[segment]
    return value


def _destination_argument(
    descriptor: ToolDescriptor,
    arguments: Mapping[str, object],
) -> str | None:
    for path in descriptor.destination_argument_paths:
        value = _argument_at_path(arguments, path)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_profile_sink(
    name: str,
    arguments: Mapping[str, object],
) -> bool:
    """Return whether this is the internal gate for the named profile."""
    profile_id = arguments.get("profile_id")
    return (
        isinstance(profile_id, str)
        and bool(profile_id)
        and name == f"profile:{profile_id}"
    )


def _is_sensitive_read_descriptor(descriptor: ToolDescriptor) -> bool:
    """Return whether successful execution acquires potentially private data."""
    return {
        ToolTag.READ_ONLY,
        ToolTag.SENSITIVE_DATA,
    }.issubset(descriptor.tags)


class TaintTrackingToolsProvider(ToolsProvider):
    """Wraps another provider with runtime taint policy and result tracking."""

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        taint_policy: TaintPolicyConfig | None = None,
        confirmation_timeout: float = 3600.0,
        delegation_sink_classes: Mapping[str, SinkClass] | None = None,
        tool_call_reviewer: ToolCallReviewer | None = None,
        review_config: ToolCallReviewConfig | None = None,
        deployment_review_guidance: str = "",
        profile_review_guidance: str = "",
        include_aggregated_context: bool | None = None,
        profile: str = UNATTRIBUTED_PROFILE,
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
        # Profile id -> the sink class a whole turn on that profile counts as,
        # so a delegation is evaluated as what the target does rather than as a
        # generic delegation. Spans profiles, so it is built once at startup.
        self._delegation_sink_classes = dict(delegation_sink_classes or {})
        self._tool_call_reviewer = tool_call_reviewer
        self._review_config = review_config
        self._deployment_review_guidance = deployment_review_guidance
        self._profile_review_guidance = profile_review_guidance
        self._include_aggregated_context = include_aggregated_context
        self._review_tasks: set[asyncio.Task[object]] = set()
        # This is the outermost provider every caller holds, so counting
        # executions here counts them whichever entry path reached them --
        # the processing loop, native voice, the tools API, Monty scripts,
        # the durable-confirmation worker. Counted here rather than in a
        # wrapper of its own because a wrapper is a new type, and the
        # providers are probed with isinstance against several capability
        # protocols; one that delegates by __getattr__ satisfies hasattr but
        # not isinstance, so it silently strips capabilities.
        self._profile = profile

    def _record_sink_approval(
        self,
        context: ToolExecutionContext,
        descriptor: ToolDescriptor,
        sink_class: SinkClass,
        arguments: Mapping[str, object],
    ) -> None:
        """Record that a human approved this turn's content for a sink.

        Called only where a confirmation was actually shown and approved --
        never for an outcome that simply did not require one. Those are
        different facts: this turn's policy not asking says nothing about a
        target profile whose own matrix tightens the same sink to `confirm`,
        and recording passage as approval would answer that profile's question
        on a user's behalf.

        Only for a delegation, and bound to its exact target profile: an ordinary
        tool call *is* the sink, and a class-wide marker would hand a later,
        unrelated named sink an approval it never asked for. A delegation is
        different -- the same content continues under the named target profile,
        whose own gate would otherwise have to infer whether this one asked.
        Recording that exact handoff on the taint means the evidence travels with
        the content it is about and is persisted with the delegation run.
        """
        tracker = context.taint_tracker
        if tracker is None:
            return
        if "delegation" not in {
            str(getattr(tag, "value", tag)) for tag in descriptor.tags
        }:
            return
        target_profile_id = arguments.get("target_service_id")
        if not isinstance(target_profile_id, str) or not target_profile_id:
            return
        tracker.replace(
            tracker.snapshot().approve_sink(
                sink_class,
                profile_id=target_profile_id,
            )
        )

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

    async def authorize_taint_sink(
        self,
        *,
        name: str,
        sink_class: SinkClass,
        # ast-grep-ignore: no-dict-any - sink arguments are dynamic audit and confirmation context
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
        taint_policy: TaintPolicyConfig | None = None,
    ) -> None:
        """Apply runtime taint policy to an egress sink outside tool dispatch."""
        if context.taint_tracker is None:
            return

        state = context.taint_tracker.snapshot()
        evaluator = (
            TaintPolicyEvaluator(taint_policy)
            if taint_policy is not None
            else self._taint_evaluator
        )
        evaluation = evaluator.evaluate(
            state=state,
            sink_class=sink_class,
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
            logger.warning(
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
        await self._record_named_policy_evaluation_audit(
            tool_name=name,
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
        profile_id = arguments.get("profile_id")
        carried_profile_approval = (
            _is_profile_sink(name, arguments)
            and isinstance(profile_id, str)
            and state.is_sink_approved(sink_class, profile_id=profile_id)
        )
        if carried_profile_approval and (
            evaluation.effective_outcome is TaintPolicyOutcome.CONFIRM
            or (
                evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE
                and evaluation.verdict_floor is not TaintPolicyOutcome.DENY
            )
        ):
            logger.info(
                "Carried human approval satisfies named sink authorization: "
                "tool=%s call_id=%s sink=%s requested=%s floor=%s",
                name,
                call_id,
                sink_class.value,
                evaluation.requested_outcome.value,
                evaluation.verdict_floor.value
                if evaluation.verdict_floor is not None
                else None,
            )
            return
        if evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE:
            descriptor = ToolDescriptor(
                name=name,
                definition={
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "Direct runtime-taint sink authorization.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                tags=frozenset(),
                origin="local",
            )
            if evaluation.mode is TaintPolicyMode.OBSERVE:
                self._start_shadow_review(
                    descriptor=descriptor,
                    arguments=arguments,
                    context=context,
                    call_id=call_id,
                    state=state,
                    sink_class=sink_class,
                    taint_evaluation=evaluation,
                    static_evaluation=None,
                )
                return
            result = await self._review_tool_call(
                descriptor=descriptor,
                arguments=arguments,
                context=context,
                call_id=call_id,
                state=state,
                sink_class=sink_class,
                taint_evaluation=evaluation,
                static_evaluation=None,
                include_observe_taint_constraints=False,
            )
            if result.verdict is ToolCallReviewVerdict.DENY:
                raise ToolPolicyDeniedError(name, result.reason)
            if result.verdict is ToolCallReviewVerdict.CONFIRM:
                await self._request_named_sink_confirmation(
                    name=name,
                    sink_class=sink_class,
                    arguments=arguments,
                    context=context,
                    call_id=call_id,
                    reason=result.reason,
                    state=state,
                )
            return
        if evaluation.effective_outcome is TaintPolicyOutcome.CONFIRM:
            await self._request_named_sink_confirmation(
                name=name,
                sink_class=sink_class,
                arguments=arguments,
                context=context,
                call_id=call_id,
                reason=evaluation.reason,
                state=state,
            )

    async def _request_named_sink_confirmation(
        self,
        *,
        name: str,
        sink_class: SinkClass,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        reason: str,
        state: TurnTaintState,
    ) -> None:
        """Request a live, decision-only confirmation for a non-tool sink.

        Named sinks such as ``profile:<id>`` authorize the model call currently
        waiting in this coroutine. They are not executable tools and therefore
        must never enter normal durable replay. A live UI manager may persist a
        decision-only request, but approval resumes only this call; unattended
        contexts without such a manager fail closed.
        """
        manager = (context.confirmation_ui_managers or {}).get(context.interface_type)
        if manager is None:
            raise ToolPolicyDeniedError(
                name,
                f"{reason}; live decision-only confirmation is unavailable in "
                "this unattended or deferred context",
            )

        resolved_call_id = call_id or f"sink_{uuid.uuid4()}"
        source_message_internal_id: int | None = None
        if context.turn_id is not None:
            source_row = (
                await context.db_context.message_history.get_user_row_by_turn_id(
                    context.turn_id
                )
            )
            if source_row is not None:
                source_message_internal_id = source_row["internal_id"]
        try:
            rendered_arguments = json.dumps(
                dict(arguments),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ToolPolicyDeniedError(
                name,
                "live confirmation refused because the complete request payload "
                "could not be rendered for the approver",
            ) from exc
        quoted_reason = "\n".join(f"> {line}" for line in reason.splitlines())
        prompt = (
            f"Allow the current request to enter '{name}'?\n\n"
            f"Complete request payload:\n{rendered_arguments}\n\n"
            f"Automatic review reason:\n{quoted_reason}"
        )
        if len(prompt) > _NAMED_SINK_CONFIRMATION_MAX_CHARS:
            raise ToolPolicyDeniedError(
                name,
                "live confirmation refused because the complete request payload "
                f"does not fit in the {_NAMED_SINK_CONFIRMATION_MAX_CHARS}-character "
                "confirmation message",
            )
        try:
            outcome = await manager.request_confirmation(
                conversation_id=context.conversation_id,
                interface_type=context.interface_type,
                turn_id=context.turn_id,
                prompt_text=prompt,
                tool_name=name,
                tool_args=dict(arguments),
                timeout=self.confirmation_timeout,
                target_user_id=context.user_id,
                tool_call_id=resolved_call_id,
                source_message_internal_id=source_message_internal_id,
                wait_for_durable_execution=False,
                taint_state_json=state.to_metadata(),
                processing_profile_id=context.processing_profile_id,
            )
        except TimeoutError as exc:
            raise ToolPolicyDeniedError(
                name,
                f"{reason}; live decision-only confirmation timed out",
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Live confirmation failed for named sink '%s'", name)
            raise ToolPolicyDeniedError(
                name,
                f"{reason}; live decision-only confirmation failed",
            ) from exc

        if outcome.kind == "approved":
            tracker = context.taint_tracker
            profile_id = arguments.get("profile_id")
            if (
                tracker is not None
                and _is_profile_sink(name, arguments)
                and isinstance(profile_id, str)
            ):
                tracker.replace(
                    tracker.snapshot().approve_sink(
                        sink_class,
                        profile_id=profile_id,
                    )
                )
            return
        detail_result = confirmation_outcome_to_tool_result(name=name, outcome=outcome)
        detail = (
            detail_result.get_text()
            if isinstance(detail_result, ToolResult)
            else detail_result
        )
        raise ToolPolicyDeniedError(
            name,
            f"{reason}; live decision-only confirmation did not approve: {detail}",
        )

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Coordinate taint review through the static-policy execution chokepoint."""
        started = time.monotonic()
        outcome = "error"
        try:
            result = await self._execute_tool_tracked(name, arguments, context, call_id)
            # `returned`, not `success`: a tool reports an expected failure by
            # returning a result, and ToolResult has no status field, so
            # nothing here can tell a refusal from an answer.
            outcome = "returned"
            return result
        except ToolPolicyDeniedError:
            outcome = "denied"
            raise
        except ToolNotFoundError:
            outcome = "not_found"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            # A name that resolved to no tool never came from the registry: it
            # came from an LLM or from /api/tools/execute/{tool_name}, so it is
            # attacker-shaped free text. Prometheus keeps every distinct label
            # tuple for the life of the process, so recording those verbatim
            # would let a typo loop grow the series set without bound. The
            # sentinel keeps "how often is a missing tool asked for" visible
            # while making the label's range finite.
            record_tool_call(
                profile=self._profile,
                tool=UNKNOWN_TOOL if outcome == "not_found" else name,
                outcome=outcome,
                duration_seconds=time.monotonic() - started,
            )

    async def _execute_tool_tracked(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """The taint-tracked execution itself, without the accounting."""
        policy_provider = (
            self.wrapped_provider
            if isinstance(self.wrapped_provider, PolicyCoordinatingToolsProvider)
            else None
        )

        if policy_provider is not None:

            async def coordinate_policy(
                descriptor: ToolDescriptor,
                static_evaluation: PolicyEvaluation,
                execute_authorized: AuthorizedToolExecutor,
            ) -> str | ToolResult:
                return await self._authorize_and_execute_tool(
                    name=name,
                    arguments=arguments,
                    context=context,
                    call_id=call_id,
                    descriptor=descriptor,
                    static_evaluation=static_evaluation,
                    execute_authorized=execute_authorized,
                )

            return await policy_provider.execute_with_policy_coordinator(
                name,
                arguments,
                context,
                call_id,
                coordinate_policy,
            )

        descriptor = await self._descriptor_provider.get_tool_descriptor(name)
        if descriptor is None:
            raise ToolNotFoundError(name, type(self).__name__)

        async def execute_authorized() -> str | ToolResult:
            return await self.wrapped_provider.execute_tool(
                name,
                arguments,
                context,
                call_id,
            )

        return await self._authorize_and_execute_tool(
            name=name,
            arguments=arguments,
            context=context,
            call_id=call_id,
            descriptor=descriptor,
            static_evaluation=None,
            execute_authorized=execute_authorized,
        )

    async def _authorize_and_execute_tool(
        self,
        *,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
        descriptor: ToolDescriptor,
        static_evaluation: PolicyEvaluation | None,
        execute_authorized: AuthorizedToolExecutor,
    ) -> str | ToolResult:
        """Gate one call, guaranteeing any shadow review learns when it finished.

        A verdict computed off the critical path attaches to the definitions the
        call wrote, so it must know when every write is registered -- including
        when the call ends at a confirmation or an exception rather than at a
        dispatch.
        """
        previous_pending = context.pending_definition_review
        context.pending_definition_review = None
        try:
            return await self._gate_and_execute_tool(
                name=name,
                arguments=arguments,
                context=context,
                call_id=call_id,
                descriptor=descriptor,
                static_evaluation=static_evaluation,
                execute_authorized=execute_authorized,
            )
        finally:
            if context.pending_definition_review is not None:
                context.pending_definition_review.settled.set()
            context.pending_definition_review = previous_pending

    async def _gate_and_execute_tool(
        self,
        *,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None,
        descriptor: ToolDescriptor,
        static_evaluation: PolicyEvaluation | None,
        execute_authorized: AuthorizedToolExecutor,
    ) -> str | ToolResult:
        """Merge static and taint decisions, then invoke authorized dispatch once."""

        state = TurnTaintState.empty()
        sink_class = resolve_tool_sink_class(
            descriptor, arguments, self._delegation_sink_classes
        )
        evaluation: TaintPolicyEvaluation | None = None
        if context.taint_tracker is not None:
            argument_taint_merged = await self._merge_argument_taint_into_context(
                arguments,
                context,
                descriptor,
            )
            state = context.taint_tracker.snapshot()
            if context.taint_policy_snapshot is not None and not argument_taint_merged:
                state = context.taint_policy_snapshot
            evaluation = self._taint_evaluator.evaluate(
                state=state, sink_class=sink_class
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
                # audit. Surface it at WARNING so dry-run diagnostics remain visible
                # without polluting error-log triage.
                logger.warning(
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

        if evaluation is not None:
            if evaluation.effective_outcome is TaintPolicyOutcome.DENY:
                raise ToolPolicyDeniedError(name, evaluation.reason)
            if evaluation.effective_outcome is TaintPolicyOutcome.REDACT:
                raise ToolPolicyDeniedError(
                    name,
                    f"{evaluation.reason}; redaction outcomes are not executable yet",
                )

        static_review = (
            static_evaluation is not None
            and static_evaluation.decision is ToolPolicyDecision.REVIEW
        )
        taint_review = (
            evaluation is not None
            and evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE
        )
        confined_exemption = (
            taint_review
            and evaluation is not None
            and not static_review
            and self._is_confined_review_exempt(
                descriptor=descriptor,
                state=state,
                sink_class=sink_class,
                evaluation=evaluation,
                review_messages=context.tool_call_review_messages,
                active_request_role=(
                    context.tool_call_review_trigger.active_request_role
                    if context.tool_call_review_trigger is not None
                    else "user"
                ),
            )
        )

        review_result: ToolCallReviewResult | None = None
        inline_confirmation_authorization: ToolConfirmationAuthorization | None = None
        # How this call left its gates, for whichever executable-persistence
        # write it goes on to perform. The gates record it; the write consumes
        # it; no tool maps verdicts to dispositions itself.
        definition_gate: DefinitionGateOutcome | None = None
        pending_definition_review: PendingDefinitionReview | None = None
        if confined_exemption and evaluation is not None:
            await self._record_confined_exemption_audit(
                descriptor=descriptor,
                arguments=arguments,
                context=context,
                call_id=call_id,
                state=state,
                evaluation=evaluation,
            )
        elif (
            taint_review
            and evaluation is not None
            and (evaluation.mode is TaintPolicyMode.OBSERVE and not static_review)
        ):
            # The verdict does not block the call, so any definition this call
            # writes lands pending and resolves uncured until the review
            # completes -- a latency-bounded window, seconds wide, in which a
            # firing enters conservative.
            pending_definition_review = PendingDefinitionReview(
                write_id=str(uuid.uuid4())
            )
            context.pending_definition_review = pending_definition_review
            definition_gate = DefinitionGateOutcome(
                disposition=None,
                gate=self._gate_provenance(
                    layer=GateLayer.TAINT_CELL, taint_evaluation=evaluation
                ),
                pending=pending_definition_review,
            )
            self._start_shadow_review(
                descriptor=descriptor,
                arguments=arguments,
                context=context,
                call_id=call_id,
                state=state,
                sink_class=sink_class,
                taint_evaluation=evaluation,
                static_evaluation=static_evaluation,
                pending=pending_definition_review,
            )
        elif (static_review or taint_review) and _review_authorization_matches(
            context.tool_call_review_authorization,
            name=name,
            arguments=arguments,
            call_id=call_id,
            sink_class=sink_class,
            static_evaluation=static_evaluation,
            taint_evaluation=evaluation,
        ):
            logger.info(
                "Reusing durable human approval for reviewed tool %s (%s)",
                name,
                call_id,
            )
            definition_gate = DefinitionGateOutcome(
                disposition=CreationDisposition.HUMAN_CONFIRMED,
                gate=self._gate_provenance(
                    layer=GateLayer.CONFIRMATION, taint_evaluation=evaluation
                ),
            )
        elif static_review or taint_review:
            review_result = await self._review_tool_call(
                descriptor=descriptor,
                arguments=arguments,
                context=context,
                call_id=call_id,
                state=state,
                sink_class=sink_class,
                taint_evaluation=evaluation,
                static_evaluation=static_evaluation,
                include_observe_taint_constraints=False,
            )

        if review_result is not None:
            definition_gate = self._judge_gate_outcome(
                review_result=review_result,
                taint_review=taint_review,
                taint_evaluation=evaluation,
                static_evaluation=static_evaluation,
            )
            if review_result.verdict is ToolCallReviewVerdict.DENY:
                state_before_confirmation = (
                    context.taint_tracker.snapshot()
                    if context.taint_tracker is not None
                    else None
                )
                escalation_result = await self._maybe_escalate_review_denial(
                    descriptor=descriptor,
                    arguments=arguments,
                    context=context,
                    call_id=call_id,
                    state=state,
                    sink_class=sink_class,
                    mode=evaluation.mode if evaluation is not None else None,
                    review_result=review_result,
                    constraints=self._review_constraints(
                        taint_evaluation=evaluation,
                        static_evaluation=static_evaluation,
                        include_observe_taint_constraints=False,
                    ),
                    authorization=_review_authorization_for_confirmation(
                        name=name,
                        arguments=arguments,
                        call_id=call_id,
                        sink_class=sink_class,
                        static_evaluation=static_evaluation,
                        taint_evaluation=evaluation,
                    ),
                )
                if escalation_result is not None:
                    action_attempted = (
                        escalation_result.action_attempted
                        if isinstance(escalation_result, _ConfirmationGateResult)
                        else False
                    )
                    result = (
                        escalation_result.result
                        if isinstance(escalation_result, _ConfirmationGateResult)
                        else escalation_result
                    )
                    if action_attempted:
                        await self._record_result_taint_and_audit(
                            descriptor=descriptor,
                            context=context,
                            call_id=call_id,
                            state_before_execution=state_before_confirmation,
                        )
                    elif context.taint_tracker is not None:
                        context.tool_result_taint_metadata[
                            call_id or descriptor.name
                        ] = context.taint_tracker.snapshot().to_metadata()
                    return result
                # The denial was escalated and a human approved the call anyway,
                # rendering it in full. That is a sighted approval; the recorded
                # denial it overrode is not what the write carries forward.
                definition_gate = DefinitionGateOutcome(
                    disposition=CreationDisposition.HUMAN_CONFIRMED,
                    gate=self._gate_provenance(
                        layer=GateLayer.CONFIRMATION,
                        taint_evaluation=evaluation,
                        verdict_id=review_result.audit_event_id,
                    ),
                )
                self._record_sink_approval(context, descriptor, sink_class, arguments)
            elif review_result.verdict is ToolCallReviewVerdict.CONFIRM:
                state_before_confirmation = (
                    context.taint_tracker.snapshot()
                    if context.taint_tracker is not None
                    else None
                )
                confirmation_result = await self._request_review_confirmation(
                    descriptor=descriptor,
                    arguments=arguments,
                    context=context,
                    call_id=call_id,
                    reason=review_result.reason,
                    authorization=_review_authorization_for_confirmation(
                        name=name,
                        arguments=arguments,
                        call_id=call_id,
                        sink_class=sink_class,
                        static_evaluation=static_evaluation,
                        taint_evaluation=evaluation,
                    ),
                )
                if confirmation_result is not None:
                    # The confirmation outcome is synthetic: the tool never ran,
                    # so its declared output provenance must not be added. Preserve
                    # the live state, including any metadata returned by the
                    # confirmation adapter itself.
                    if confirmation_result.action_attempted:
                        await self._record_result_taint_and_audit(
                            descriptor=descriptor,
                            context=context,
                            call_id=call_id,
                            state_before_execution=state_before_confirmation,
                        )
                    elif context.taint_tracker is not None:
                        context.tool_result_taint_metadata[
                            call_id or descriptor.name
                        ] = context.taint_tracker.snapshot().to_metadata()
                    return confirmation_result.result
                # The escalation or confirmation prompt rendered the complete
                # call and a human approved it, which is a sighted approval of
                # the definition it writes -- the one thing a recorded
                # ``confirm`` never was.
                definition_gate = DefinitionGateOutcome(
                    disposition=CreationDisposition.HUMAN_CONFIRMED,
                    gate=self._gate_provenance(
                        layer=GateLayer.CONFIRMATION,
                        taint_evaluation=evaluation,
                        verdict_id=review_result.audit_event_id,
                    ),
                )
                # A live human approval has already confirmed this exact reviewed
                # call. Scope a consumed generic authorization to its immediate
                # execution so an inner gate (notably delegate_to_service's own
                # confirmation) cannot prompt for the same payload a second time.
                inline_confirmation_authorization = ToolConfirmationAuthorization(
                    tool_name=name,
                    call_id=call_id or "",
                    tool_args=cast("ToolArguments", copy.deepcopy(dict(arguments))),
                    consumed=True,
                )
                self._record_sink_approval(context, descriptor, sink_class, arguments)

        hard_confirm = (
            static_evaluation is not None
            and static_evaluation.decision is ToolPolicyDecision.CONFIRM
        ) or (
            evaluation is not None
            and evaluation.effective_outcome is TaintPolicyOutcome.CONFIRM
        )
        if hard_confirm and (
            review_result is None
            or review_result.verdict is ToolCallReviewVerdict.ALLOW
        ):
            state_before_confirmation = (
                context.taint_tracker.snapshot()
                if context.taint_tracker is not None
                else None
            )
            reasons = [
                item
                for item in (
                    static_evaluation.reason
                    if static_evaluation is not None
                    and static_evaluation.decision is ToolPolicyDecision.CONFIRM
                    else None,
                    evaluation.reason
                    if evaluation is not None
                    and evaluation.effective_outcome is TaintPolicyOutcome.CONFIRM
                    else None,
                )
                if item
            ]
            confirmation_result = await self._request_taint_confirmation(
                name=name,
                arguments=arguments,
                context=context,
                call_id=call_id,
                reason="; ".join(reasons),
            )
            if confirmation_result is not None:
                if confirmation_result.action_attempted:
                    await self._record_result_taint_and_audit(
                        descriptor=descriptor,
                        context=context,
                        call_id=call_id,
                        state_before_execution=state_before_confirmation,
                    )
                elif context.taint_tracker is not None:
                    context.tool_result_taint_metadata[call_id or descriptor.name] = (
                        context.taint_tracker.snapshot().to_metadata()
                    )
                return confirmation_result.result
            definition_gate = DefinitionGateOutcome(
                disposition=CreationDisposition.HUMAN_CONFIRMED,
                gate=self._gate_provenance(
                    layer=GateLayer.CONFIRMATION, taint_evaluation=evaluation
                ),
            )
            self._record_sink_approval(context, descriptor, sink_class, arguments)

        state_before_execution = (
            context.taint_tracker.snapshot()
            if context.taint_tracker is not None
            else None
        )
        previous_confirmation_authorization = context.tool_confirmation_authorization
        previous_definition_gate = context.definition_gate_outcome
        if inline_confirmation_authorization is not None:
            context.tool_confirmation_authorization = inline_confirmation_authorization
        context.definition_gate_outcome = definition_gate
        try:
            result = await execute_authorized()
        except Exception:
            if descriptor is not None:
                await self._record_result_taint_and_audit(
                    descriptor=descriptor,
                    context=context,
                    call_id=call_id,
                    state_before_execution=state_before_execution,
                )
            raise
        finally:
            context.definition_gate_outcome = previous_definition_gate
            if inline_confirmation_authorization is not None:
                context.tool_confirmation_authorization = (
                    previous_confirmation_authorization
                )

        if descriptor is not None:
            self._record_generic_sensitive_read(
                descriptor=descriptor,
                context=context,
                state_before_execution=state_before_execution,
            )
            await self._record_result_taint_and_audit(
                descriptor=descriptor,
                context=context,
                call_id=call_id,
                state_before_execution=state_before_execution,
            )
        return result

    def _is_confined_review_exempt(
        self,
        *,
        descriptor: ToolDescriptor,
        state: TurnTaintState,
        sink_class: SinkClass,
        evaluation: TaintPolicyEvaluation,
        review_messages: Sequence[object] | None,
        active_request_role: Literal["user", "system"],
    ) -> bool:
        """Return whether a disclosure review can safely resolve to audit."""
        return (
            self._include_aggregated_context is False
            and sink_class in _REVIEW_DISCLOSURE_SINKS
            and not state.sensitive_reads
            and not state.history_high_taint_present
            and _review_messages_are_current_turn_only(
                review_messages,
                active_request_role=active_request_role,
            )
            and evaluation.verdict_floor is None
        )

    def _review_policy_contexts(
        self,
        *,
        state: TurnTaintState,
        taint_evaluation: TaintPolicyEvaluation | None,
        static_evaluation: PolicyEvaluation | None,
    ) -> list[DelegatingPolicyContext]:
        contexts: list[DelegatingPolicyContext] = []
        if (
            taint_evaluation is not None
            and taint_evaluation.requested_outcome is TaintPolicyOutcome.ADJUDICATE
        ):
            contexts.append(
                DelegatingPolicyContext(
                    kind="taint_cell",
                    identifier=(
                        f"{state.max_tier.config_value}."
                        f"{taint_evaluation.sink_class.value}"
                    ),
                    description=taint_evaluation.reason,
                )
            )
        if (
            static_evaluation is not None
            and static_evaluation.decision is ToolPolicyDecision.REVIEW
        ):
            matched_rule = static_evaluation.matched_rule
            identifier = (
                f"{matched_rule.layer}:{matched_rule.declaration_order}"
                if matched_rule is not None
                else "default"
            )
            contexts.append(
                DelegatingPolicyContext(
                    kind="static_rule",
                    identifier=identifier,
                    description=static_evaluation.reason,
                )
            )
        return contexts

    def _review_constraints(
        self,
        *,
        taint_evaluation: TaintPolicyEvaluation | None,
        static_evaluation: PolicyEvaluation | None,
        include_observe_taint_constraints: bool,
    ) -> ToolCallReviewConstraints:
        floors = [ToolCallReviewVerdict.ALLOW]
        fallbacks: list[ToolCallReviewVerdict] = []
        static_decision = (
            static_evaluation.decision if static_evaluation is not None else None
        )
        if taint_evaluation is not None and (
            taint_evaluation.mode is TaintPolicyMode.ENFORCE
            or include_observe_taint_constraints
        ):
            if taint_evaluation.verdict_floor is not None:
                floors.append(
                    _review_verdict_for_taint_outcome(taint_evaluation.verdict_floor)
                )
            if taint_evaluation.fallback_outcome is not None:
                fallbacks.append(
                    _review_verdict_for_taint_outcome(taint_evaluation.fallback_outcome)
                )
            if taint_evaluation.effective_outcome is TaintPolicyOutcome.CONFIRM:
                floors.append(ToolCallReviewVerdict.CONFIRM)
                fallbacks.append(ToolCallReviewVerdict.CONFIRM)
        if static_decision is ToolPolicyDecision.REVIEW:
            fallbacks.append(ToolCallReviewVerdict.CONFIRM)
        elif static_decision is ToolPolicyDecision.CONFIRM:
            floors.append(ToolCallReviewVerdict.CONFIRM)
            fallbacks.append(ToolCallReviewVerdict.CONFIRM)
        floor = _strictest_review_verdict(floors)
        fallback = _strictest_review_verdict([
            *fallbacks,
            ToolCallReviewVerdict.CONFIRM,
        ])
        if fallback not in _review_verdict_space(floor):
            fallback = floor
        return ToolCallReviewConstraints(
            available_verdicts=_review_verdict_space(floor),
            fallback_verdict=fallback,
        )

    def _reviewer_revision(self) -> str:
        """Identify the reviewer that produced a verdict, for a later filterable review.

        A recorded ``judge_allowed`` stands until the content changes, so
        recalibrating the reviewer -- a new model, or new guidance -- must be
        visible when an operator comes to review the judge-cured estate. Model
        and guidance are what change the verdict, so both feed the identifier.
        """
        config = self._review_config
        model = f"{config.provider or 'default'}/{config.model}" if config else "none"
        guidance = "\n".join((
            config.guidance if config is not None else "",
            self._deployment_review_guidance,
            self._profile_review_guidance,
        ))
        digest = hashlib.sha256(guidance.encode("utf-8")).hexdigest()[:12]
        return f"{model}@{digest}"

    def _allow_is_enforce_equivalent(
        self,
        *,
        taint_evaluation: TaintPolicyEvaluation | None,
        static_evaluation: PolicyEvaluation | None,
    ) -> bool:
        """Whether ``enforce`` would have offered ``allow`` at this cell.

        A merged review under ``observe`` deliberately omits the taint cell's
        constraints, so a floored cell whose space is ``{confirm, deny}`` gets
        ``allow`` back through the static layer's presence -- a verdict
        ``enforce`` could never have issued, and therefore one that cannot cure.
        Recomputing the constraints with those constraints applied is what tells
        the two apart, and it is only ever consulted for curing: the verdict the
        call actually ran under is unchanged.
        """
        enforce_equivalent = self._review_constraints(
            taint_evaluation=taint_evaluation,
            static_evaluation=static_evaluation,
            include_observe_taint_constraints=True,
        )
        return ToolCallReviewVerdict.ALLOW in enforce_equivalent.available_verdicts

    def _judge_gate_outcome(
        self,
        *,
        review_result: ToolCallReviewResult,
        taint_review: bool,
        taint_evaluation: TaintPolicyEvaluation | None,
        static_evaluation: PolicyEvaluation | None,
    ) -> DefinitionGateOutcome:
        """Record how a reviewer verdict left the gate, for any definition the call writes."""
        disposition = {
            ToolCallReviewVerdict.ALLOW: CreationDisposition.JUDGE_ALLOWED,
            ToolCallReviewVerdict.CONFIRM: CreationDisposition.JUDGE_CONFIRM_REQUIRED,
            ToolCallReviewVerdict.DENY: CreationDisposition.JUDGE_DENIED,
        }[review_result.verdict]
        return DefinitionGateOutcome(
            disposition=disposition,
            gate=self._gate_provenance(
                layer=GateLayer.TAINT_CELL if taint_review else GateLayer.STATIC_RULE,
                taint_evaluation=taint_evaluation,
                verdict_id=review_result.audit_event_id,
            ),
            cure_permitted=self._allow_is_enforce_equivalent(
                taint_evaluation=taint_evaluation,
                static_evaluation=static_evaluation,
            ),
        )

    def _gate_provenance(
        self,
        *,
        layer: GateLayer,
        taint_evaluation: TaintPolicyEvaluation | None,
        verdict_id: str | None = None,
    ) -> GateProvenance:
        return GateProvenance(
            layer=layer,
            mode=(
                taint_evaluation.mode.value
                if taint_evaluation is not None
                else _NO_TAINT_GATE_MODE
            ),
            reviewer_revision=self._reviewer_revision(),
            verdict_id=verdict_id,
        )

    async def _review_tool_call(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
        sink_class: SinkClass,
        taint_evaluation: TaintPolicyEvaluation | None,
        static_evaluation: PolicyEvaluation | None,
        include_observe_taint_constraints: bool,
        count_reserved: bool = False,
        update_denial_counters: bool = True,
    ) -> ToolCallReviewResult:
        constraints = self._review_constraints(
            taint_evaluation=taint_evaluation,
            static_evaluation=static_evaluation,
            include_observe_taint_constraints=include_observe_taint_constraints,
        )
        config = self._review_config
        budget_exhausted = (
            config is not None
            and context.tool_call_review_state.review_count
            >= config.max_reviews_per_turn
            and not count_reserved
        )
        if not budget_exhausted and not count_reserved:
            context.tool_call_review_state.review_count += 1
        messages = context.tool_call_review_messages
        if messages is None:
            messages = (
                await context.db_context.message_history.get_by_turn_id(context.turn_id)
                if context.turn_id is not None
                else []
            )
        destination_echo = compute_trusted_destination_echo(
            _destination_argument(descriptor, arguments),
            messages,
            trigger=context.tool_call_review_trigger,
        )
        policy_contexts = self._review_policy_contexts(
            state=state,
            taint_evaluation=taint_evaluation,
            static_evaluation=static_evaluation,
        )
        review_input = ToolCallReviewInput(
            messages=messages,
            descriptor=descriptor,
            arguments=arguments,
            sink_class=sink_class,
            taint_state=state,
            policy_contexts=policy_contexts,
            deployment_guidance=self._deployment_review_guidance,
            profile_guidance=self._profile_review_guidance,
            trigger=context.tool_call_review_trigger,
            destination_echo=destination_echo,
        )
        if self._tool_call_reviewer is None:
            delegating_reason = " ".join(
                item.description for item in policy_contexts if item.description
            )
            result = ToolCallReviewResult(
                verdict=constraints.fallback_verdict,
                reason=(
                    f"{delegating_reason} Tool-call reviewer is not configured. "
                    "Using caller "
                    f"fallback '{constraints.fallback_verdict.value}'."
                ).strip(),
                status=ToolCallReviewStatus.DISABLED_FALLBACK,
                latency_ms=0,
                used_fallback=True,
            )
        else:
            result = await self._tool_call_reviewer.review_tool_call(
                review_input,
                constraints,
                budget_exhausted=budget_exhausted,
            )
        audit_event_id = await self._record_tool_call_review_audit(
            descriptor=descriptor,
            arguments=arguments,
            context=context,
            call_id=call_id,
            state=state,
            sink_class=sink_class,
            policy_contexts=policy_contexts,
            constraints=constraints,
            destination_echo=(
                destination_echo.matched if destination_echo is not None else None
            ),
            result=result,
            mode=taint_evaluation.mode if taint_evaluation is not None else None,
        )
        result = result.model_copy(update={"audit_event_id": audit_event_id})
        if update_denial_counters:
            if _is_escalatable_review_denial(result, constraints):
                context.tool_call_review_state.consecutive_denials += 1
                context.tool_call_review_state.total_denials += 1
            elif result.verdict is not ToolCallReviewVerdict.DENY:
                # A non-escalatable deny (for example a timeout fallback or a
                # deny-only policy floor) is not evidence of another model
                # denial, but neither is it evidence that the denial streak
                # ended. Only an allow/confirm verdict resets that streak.
                context.tool_call_review_state.consecutive_denials = 0
        return result

    def _start_shadow_review(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
        sink_class: SinkClass,
        taint_evaluation: TaintPolicyEvaluation,
        static_evaluation: PolicyEvaluation | None,
        pending: PendingDefinitionReview | None = None,
    ) -> None:
        config = self._review_config
        budget_exhausted = (
            config is not None
            and context.tool_call_review_state.review_count
            >= config.max_reviews_per_turn
        )
        if not budget_exhausted:
            context.tool_call_review_state.review_count += 1
        review = self._review_tool_call(
            descriptor=descriptor,
            arguments=dict(arguments),
            context=context,
            call_id=call_id,
            state=state,
            sink_class=sink_class,
            taint_evaluation=taint_evaluation,
            static_evaluation=static_evaluation,
            include_observe_taint_constraints=True,
            count_reserved=not budget_exhausted,
            update_denial_counters=False,
        )
        task = spawn_detached(
            review
            if pending is None
            else self._attach_shadow_verdict(
                review,
                context=context,
                pending=pending,
                taint_evaluation=taint_evaluation,
            ),
            name=f"shadow-tool-call-review:{descriptor.name}",
        )
        self._review_tasks.add(task)
        task.add_done_callback(self._finish_shadow_review)

    async def _attach_shadow_verdict(
        self,
        review: Awaitable[ToolCallReviewResult],
        *,
        context: ToolExecutionContext,
        pending: PendingDefinitionReview,
        taint_evaluation: TaintPolicyEvaluation,
    ) -> ToolCallReviewResult:
        """Carry an off-critical-path verdict back to the definitions it judged.

        The shadow review runs under the enforce-equivalent verdict space
        already, so an ``allow`` here is one ``enforce`` would have issued. It
        attaches once the gated call has settled, because only then is every
        write it made registered; each store then checks the write id itself, so
        a mutation racing the review keeps its own pending record.
        """
        result = await review
        await pending.settled.wait()
        if not pending.writes:
            return result
        await attach_pending_verdict(
            context.db_context,
            pending,
            disposition={
                ToolCallReviewVerdict.ALLOW: CreationDisposition.JUDGE_ALLOWED,
                ToolCallReviewVerdict.CONFIRM: (
                    CreationDisposition.JUDGE_CONFIRM_REQUIRED
                ),
                ToolCallReviewVerdict.DENY: CreationDisposition.JUDGE_DENIED,
            }[result.verdict],
            gate=self._gate_provenance(
                layer=GateLayer.TAINT_CELL,
                taint_evaluation=taint_evaluation,
                verdict_id=result.audit_event_id,
            ),
        )
        return result

    def _finish_shadow_review(self, task: asyncio.Task[object]) -> None:
        self._review_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Detached tool-call shadow review failed")

    async def _record_tool_call_review_audit(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
        sink_class: SinkClass,
        policy_contexts: list[DelegatingPolicyContext],
        constraints: ToolCallReviewConstraints,
        destination_echo: bool | None,
        result: ToolCallReviewResult,
        mode: TaintPolicyMode | None,
    ) -> str:
        review_context: TaintAuditReviewContext = {
            "delegating_contexts": [
                f"{item.kind}:{item.identifier}" for item in policy_contexts
            ],
            "allowed_verdicts": sorted(
                verdict.value for verdict in constraints.available_verdicts
            ),
            "fallback_verdict": constraints.fallback_verdict.value,
            "used_fallback": result.used_fallback,
            "destination_echo": destination_echo,
        }
        event_id = str(uuid.uuid4())
        await context.db_context.taint_audit_events.add(
            event_id=event_id,
            event_type="tool_call_review",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            processing_profile_id=context.processing_profile_id,
            subconversation_id=context.subconversation_id,
            tool_name=descriptor.name,
            tool_call_id=call_id,
            sink_class=sink_class.value,
            max_tier=state.max_tier.config_value,
            sources=_taint_audit_sources(state),
            requested_outcome="review",
            effective_outcome=result.verdict.value,
            mode=mode.value if mode is not None else None,
            reason=_TOOL_CALL_REVIEW_AUDIT_REASON,
            arguments_summary=_summarize_tool_arguments(
                arguments,
                safe_keys=_descriptor_argument_keys(descriptor),
            ),
            review_verdict=result.verdict.value,
            review_status=result.status.value,
            review_latency_ms=result.latency_ms,
            review_context=review_context,
        )
        return event_id

    async def _record_confined_exemption_audit(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
        evaluation: TaintPolicyEvaluation,
    ) -> None:
        await context.db_context.taint_audit_events.add(
            event_id=str(uuid.uuid4()),
            event_type="tool_call_review",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            processing_profile_id=context.processing_profile_id,
            subconversation_id=context.subconversation_id,
            tool_name=descriptor.name,
            tool_call_id=call_id,
            sink_class=evaluation.sink_class.value,
            max_tier=state.max_tier.config_value,
            sources=_taint_audit_sources(state),
            requested_outcome=TaintPolicyOutcome.ADJUDICATE.value,
            effective_outcome=TaintPolicyOutcome.AUDIT.value,
            mode=evaluation.mode.value,
            reason=(
                "Confined-profile disclosure exemption: aggregated context is "
                "excluded, the reviewer window is current-turn-only, and the turn "
                "has no sensitive reads or high-taint history."
            ),
            arguments_summary=_summarize_tool_arguments(
                arguments,
                safe_keys=_descriptor_argument_keys(descriptor),
            ),
            review_status=ToolCallReviewStatus.CONFINED_EXEMPTION.value,
            review_context={
                "delegating_contexts": [
                    f"taint_cell:{state.max_tier.config_value}."
                    f"{evaluation.sink_class.value}"
                ],
                "allowed_verdicts": [],
                "fallback_verdict": evaluation.fallback_outcome.value
                if evaluation.fallback_outcome is not None
                else ToolCallReviewVerdict.CONFIRM.value,
                "used_fallback": False,
                "destination_echo": None,
            },
        )

    def _review_denial_result(
        self,
        name: str,
        result: ToolCallReviewResult,
    ) -> ToolResult:
        text = f"Action blocked by automatic review for tool '{name}': {result.reason}"
        if result.safer_alternative:
            text += f" Safer alternative: {result.safer_alternative}"
        return ToolResult(text=text, attachments=None)

    async def _request_review_confirmation(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        reason: str,
        authorization: ToolCallReviewAuthorization,
    ) -> _ConfirmationGateResult | None:
        name = descriptor.name
        if context.request_confirmation_callback is None:
            return _ConfirmationGateResult(
                result=ToolResult(
                    text=(
                        f"Action blocked by automatic review for tool '{name}': "
                        f"human confirmation is required but unavailable. {reason}"
                    ),
                    attachments=None,
                ),
                action_attempted=False,
            )
        if (
            isinstance(
                context.request_confirmation_callback,
                DeferredConfirmationCallback,
            )
            and context.request_confirmation_callback.is_deferred_confirmation()
            and not descriptor.deferred_confirmation_eligible
        ):
            return _ConfirmationGateResult(
                result=ToolResult(
                    text=(
                        f"Action blocked by automatic review for tool '{name}': "
                        "human confirmation is required, but deferred execution is "
                        "unsafe because this call's result is not independent and "
                        f"terminal. {reason}"
                    ),
                    attachments=None,
                ),
                action_attempted=False,
            )
        previous_reason = context.tool_call_review_confirmation_reason
        previous_authorization = context.tool_call_review_authorization
        context.tool_call_review_confirmation_reason = reason
        context.tool_call_review_authorization = authorization
        try:
            return await self._request_taint_confirmation(
                name=name,
                arguments=dict(arguments),
                context=context,
                call_id=call_id,
                reason=reason,
            )
        finally:
            context.tool_call_review_confirmation_reason = previous_reason
            context.tool_call_review_authorization = previous_authorization

    async def _maybe_escalate_review_denial(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
        sink_class: SinkClass,
        mode: TaintPolicyMode | None,
        review_result: ToolCallReviewResult,
        constraints: ToolCallReviewConstraints,
        authorization: ToolCallReviewAuthorization,
    ) -> _ConfirmationGateResult | ToolResult | None:
        if not _is_escalatable_review_denial(review_result, constraints):
            return self._review_denial_result(descriptor.name, review_result)
        config = self._review_config
        threshold_reached = config is not None and (
            context.tool_call_review_state.consecutive_denials
            >= config.escalation.consecutive_denials
            or context.tool_call_review_state.total_denials
            >= config.escalation.total_denials_per_turn
        )
        if not threshold_reached or context.tool_call_review_state.escalation_handled:
            return self._review_denial_result(descriptor.name, review_result)
        # Reserve the single turn-level escalation before yielding to the
        # audit store or confirmation channel. Concurrent denied calls sharing
        # this turn state must observe the reservation and cannot open a second
        # prompt or request another deterministic termination.
        context.tool_call_review_state.escalation_handled = True
        confirmation_unavailable = self._review_confirmation_unavailable(
            descriptor, context
        )
        escalation_status = (
            "escalation_turn_terminated"
            if confirmation_unavailable
            else "escalation_confirmation_requested"
        )
        await self._record_review_escalation_audit(
            descriptor=descriptor,
            arguments=arguments,
            context=context,
            call_id=call_id,
            state=state,
            sink_class=sink_class,
            mode=mode,
            constraints=constraints,
            status=escalation_status,
        )
        if confirmation_unavailable:
            context.tool_call_review_state.terminal_denial_escalation_message = (
                "I stopped this turn after automatic review repeatedly denied "
                "proposed actions. The blocked actions were not run, and no live "
                "or safely deferrable human confirmation was available. Retry from "
                "an interactive channel or narrow the request before continuing."
            )
            return self._review_denial_result(descriptor.name, review_result)
        confirmation_result = await self._request_review_confirmation(
            descriptor=descriptor,
            arguments=arguments,
            context=context,
            call_id=call_id,
            reason=(
                "Automatic review has repeatedly denied proposed actions this turn. "
                f"Current denial: {review_result.reason}"
            ),
            authorization=authorization,
        )
        context.tool_call_review_state.consecutive_denials = 0
        context.tool_call_review_state.total_denials = 0
        return confirmation_result

    @staticmethod
    def _review_confirmation_unavailable(
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
    ) -> bool:
        callback = context.request_confirmation_callback
        return callback is None or (
            isinstance(callback, DeferredConfirmationCallback)
            and callback.is_deferred_confirmation()
            and not descriptor.deferred_confirmation_eligible
        )

    async def _record_review_escalation_audit(
        self,
        *,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, object],
        context: ToolExecutionContext,
        call_id: str | None,
        state: TurnTaintState,
        sink_class: SinkClass,
        mode: TaintPolicyMode | None,
        constraints: ToolCallReviewConstraints,
        status: str,
    ) -> None:
        """Persist one stable, countable event when a denial threshold trips."""
        await context.db_context.taint_audit_events.add(
            event_id=str(uuid.uuid4()),
            event_type="tool_call_review_escalation",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            processing_profile_id=context.processing_profile_id,
            subconversation_id=context.subconversation_id,
            tool_name=descriptor.name,
            tool_call_id=call_id,
            sink_class=sink_class.value,
            max_tier=state.max_tier.config_value,
            sources=_taint_audit_sources(state),
            requested_outcome="review_escalation",
            effective_outcome=(
                ToolCallReviewVerdict.DENY.value
                if status == "escalation_turn_terminated"
                else ToolCallReviewVerdict.CONFIRM.value
            ),
            mode=mode.value if mode is not None else None,
            reason=(
                "Automatic-review model-denial threshold reserved exactly once; "
                + (
                    "the turn will terminate because confirmation is unavailable."
                    if status == "escalation_turn_terminated"
                    else "a human confirmation was requested."
                )
            ),
            arguments_summary=_summarize_tool_arguments(
                arguments,
                safe_keys=_descriptor_argument_keys(descriptor),
            ),
            review_verdict=ToolCallReviewVerdict.DENY.value,
            review_status=status,
            review_latency_ms=None,
            review_context={
                "delegating_contexts": ["denial_threshold"],
                "allowed_verdicts": sorted(
                    verdict.value for verdict in constraints.available_verdicts
                ),
                "fallback_verdict": constraints.fallback_verdict.value,
                "used_fallback": False,
                "destination_echo": None,
            },
        )

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
    ) -> _ConfirmationGateResult | None:
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
            return _ConfirmationGateResult(
                result=ToolResult(text=block_reason, attachments=None),
                action_attempted=False,
            )

        resolved_call_id = call_id or f"tool_{uuid.uuid4()}"
        if context.tools_provider is None:
            context.tools_provider = self
        try:
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
        except TimeoutError:
            # Confirmation adapters normally return a typed ``timed_out`` outcome,
            # but callback implementations have historically also been allowed to
            # signal the same terminal state by raising TimeoutError.  The central
            # authorization path must preserve that contract now that it bypasses
            # PolicyEnforcingToolsProvider.execute_tool().
            logger.warning("Confirmation request for tool '%s' timed out.", name)
            outcome = ConfirmationOutcome(kind="timed_out")
        if outcome.taint_metadata is not None:
            context.tool_result_taint_metadata[resolved_call_id] = (
                outcome.taint_metadata
            )
            if context.taint_tracker is not None:
                merge_taint_state_into_tracker(
                    context.taint_tracker,
                    TurnTaintState.from_metadata(outcome.taint_metadata),
                )
        if outcome.kind == "approved":
            return None
        if outcome.kind == "completed":
            return _ConfirmationGateResult(
                result=outcome.result or ToolResult(text="", attachments=None),
                action_attempted=outcome.action_attempted,
            )
        return _ConfirmationGateResult(
            result=confirmation_outcome_to_tool_result(name=name, outcome=outcome),
            action_attempted=False,
        )

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
        await self._record_named_policy_evaluation_audit(
            tool_name=descriptor.name,
            context=context,
            call_id=call_id,
            arguments=arguments,
            state=state,
            evaluation=evaluation,
            safe_argument_keys=_descriptor_argument_keys(descriptor),
        )

    async def _record_named_policy_evaluation_audit(
        self,
        *,
        tool_name: str,
        context: ToolExecutionContext,
        call_id: str | None,
        # ast-grep-ignore: no-dict-any - sink arguments are dynamic audit context
        arguments: dict[str, Any],
        state: TurnTaintState,
        evaluation: TaintPolicyEvaluation,
        safe_argument_keys: Collection[str] = (),
    ) -> None:
        await context.db_context.taint_audit_events.add(
            event_id=str(uuid.uuid4()),
            event_type="policy_evaluation",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            processing_profile_id=context.processing_profile_id,
            subconversation_id=context.subconversation_id,
            tool_name=tool_name,
            tool_call_id=call_id,
            sink_class=evaluation.sink_class.value,
            max_tier=state.max_tier.config_value,
            sources=_taint_audit_sources(state),
            requested_outcome=evaluation.requested_outcome.value,
            effective_outcome=evaluation.effective_outcome.value,
            mode=evaluation.mode.value,
            reason=evaluation.reason,
            arguments_summary=_summarize_tool_arguments(
                arguments,
                safe_keys=safe_argument_keys,
            ),
        )

    @staticmethod
    def _record_generic_sensitive_read(
        *,
        descriptor: ToolDescriptor,
        context: ToolExecutionContext,
        state_before_execution: TurnTaintState | None,
    ) -> None:
        """Record the conservative fallback for an uninstrumented private read."""
        tracker = context.taint_tracker
        if tracker is None or not _is_sensitive_read_descriptor(descriptor):
            return
        live_state = tracker.snapshot()
        if (
            state_before_execution is not None
            and live_state.sensitive_reads != state_before_execution.sensitive_reads
        ):
            return
        tracker.replace(
            live_state.add_sensitive_read(
                SensitiveReadScope(
                    kind="tool",
                    qualifier=f"tool:{descriptor.name}",
                    surfaced_ids=frozenset(),
                ),
                query_origin="model_generated",
            )
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
        """Drain shadow reviews, then close the wrapped provider."""
        if self._review_tasks:
            await asyncio.gather(*tuple(self._review_tasks), return_exceptions=True)
            self._review_tasks.clear()
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
