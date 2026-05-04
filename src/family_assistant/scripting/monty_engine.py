"""
Monty scripting engine for Family Assistant.

This module provides a Monty-based scripting engine (pydantic-monty) that executes
user-defined scripts with access to family assistant tools and state. It uses
Monty's pause/resume model for clean async external function handling.
"""

import asyncio
import base64
import json
import logging
import mimetypes
import re
import uuid
from collections.abc import Callable
from datetime import tzinfo
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict

import pydantic_monty

from family_assistant.tools.attachment_utils import (
    fetch_attachment_object,
    process_attachment_arguments,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolParametersSchema, ToolResult

from .apis import time as time_api
from .apis.attachments import AttachmentInfoDict, create_attachment_api
from .config import ScriptConfig
from .errors import ScriptExecutionError, ScriptSyntaxError, ScriptTimeoutError

if TYPE_CHECKING:
    from family_assistant.tools import ToolsProvider
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class ToolInfo(TypedDict):
    """Metadata about a tool exposed to scripts via tools_list/tools_get."""

    name: str
    description: str
    parameters: ToolParametersSchema


class WakeRequest(TypedDict):
    """A request from a script to wake the LLM with context."""

    context: dict[str, str | list[str]]
    include_event: bool


class AttachmentResultInfo(TypedDict):
    """Attachment metadata returned to scripts from tool results."""

    id: str
    mime_type: str
    description: str
    size: int
    filename: str | None


class AttachmentResultWithText(TypedDict):
    """Tool result containing text and multiple attachment references."""

    text: str | None
    attachments: list[AttachmentResultInfo]


logger = logging.getLogger(__name__)


def _safe_json_decode(value: Any) -> Any:  # noqa: ANN401 - JSON decode returns arbitrary types
    """JSON decode that passes through already-decoded Python objects.

    Tool results may return structured data (dicts, lists) directly
    rather than JSON strings. This wrapper makes json_decode safe to
    call on any tool result.
    """
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _base64_encode(data: str | bytes) -> str:
    """Encode a string or bytes to a base64 string."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def _base64_decode(data: str) -> str:
    """Decode a base64 string to a UTF-8 string."""
    return base64.b64decode(data, validate=True).decode("utf-8")


def _base64_decode_bytes(data: str) -> bytes:
    """Decode a base64 string to raw bytes."""
    return base64.b64decode(data, validate=True)


class MontyEngine:
    """
    Monty scripting engine for executing user-defined scripts.

    This engine provides a sandboxed environment for running Python scripts
    using Monty (pydantic-monty), with controlled access to family assistant
    functionality via external functions.

    Uses the same ScriptConfig for configuration compatibility.
    """

    def __init__(
        self,
        tools_provider: "ToolsProvider | None" = None,
        config: ScriptConfig | None = None,
        default_timezone: tzinfo | None = None,
    ) -> None:
        self.tools_provider = tools_provider
        self.config = config or ScriptConfig()
        self.default_timezone = default_timezone
        self._wake_llm_contexts: list[WakeRequest] = []
        self._pending_wake_contexts: list[WakeRequest] = []
        # ast-grep-ignore: no-dict-any - Script globals are user-provided Python values keyed by name
        self._script_globals: dict[str, Any] = {}

        logger.info(
            "Initialized MontyEngine with config: max_execution_time=%s",
            self.config.max_execution_time,
        )

    async def evaluate_async(
        self,
        script: str,
        # ast-grep-ignore: no-dict-any - Script globals are user-provided Python values keyed by name
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:  # noqa: ANN401 # Scripts can return any type
        """
        Evaluate a script asynchronously using Monty's pause/resume model.

        External function calls (including async tool calls) are handled
        natively without sync/async bridging - async functions are awaited
        directly when Monty pauses at their call sites.
        """
        try:
            result = await asyncio.wait_for(
                self._evaluate_async_impl(script, globals_dict, execution_context),
                timeout=self.config.max_execution_time,
            )
            return result

        except TimeoutError as e:
            error_msg = (
                f"Script execution timed out after "
                f"{self.config.max_execution_time} seconds"
            )
            logger.error(error_msg)
            raise ScriptTimeoutError(error_msg, self.config.max_execution_time) from e

    async def _evaluate_async_impl(
        self,
        script: str,
        # ast-grep-ignore: no-dict-any - Script globals are user-provided Python values keyed by name
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:  # noqa: ANN401
        """Internal async implementation using manual start/resume loop."""
        try:
            self._wake_llm_contexts.clear()
            self._script_globals = globals_dict or {}

            ext_fn_impls, inputs = await self._build_execution_context_async(
                globals_dict, execution_context
            )

            m = pydantic_monty.Monty(
                script,
                inputs=list(inputs.keys()) if inputs else [],
            )

            limits = self._build_resource_limits()
            print_cb = self._create_print_callback()
            loop = asyncio.get_running_loop()

            # Start execution in thread pool (Monty execution is CPU-bound)
            progress = await loop.run_in_executor(
                None,
                partial(
                    m.start,
                    inputs=inputs or None,
                    limits=limits,
                    print_callback=print_cb,
                ),
            )

            # Resume loop: handle external function calls
            while not isinstance(progress, pydantic_monty.MontyComplete):
                if not isinstance(progress, pydantic_monty.FunctionSnapshot):
                    raise ScriptExecutionError(
                        f"Unexpected Monty progress type: {type(progress)}"
                    )
                snapshot: pydantic_monty.FunctionSnapshot = progress

                fn_name = snapshot.function_name
                fn = ext_fn_impls.get(fn_name)

                if fn is None:
                    name_error_result: pydantic_monty.ExternalResult = {
                        "exception": NameError(f"name '{fn_name}' is not defined")
                    }
                    progress = await loop.run_in_executor(
                        None,
                        partial(snapshot.resume, name_error_result),
                    )
                    continue

                try:
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(*snapshot.args, **snapshot.kwargs)
                    else:
                        result = fn(*snapshot.args, **snapshot.kwargs)
                except Exception as e:
                    exception_result: pydantic_monty.ExternalResult = {"exception": e}
                    progress = await loop.run_in_executor(
                        None,
                        partial(snapshot.resume, exception_result),
                    )
                else:
                    return_result: pydantic_monty.ExternalResult = {
                        "return_value": result
                    }
                    progress = await loop.run_in_executor(
                        None,
                        partial(snapshot.resume, return_result),
                    )

            self._pending_wake_contexts = self._wake_llm_contexts.copy()
            return progress.output

        except pydantic_monty.MontySyntaxError as e:
            error_str = str(e)
            line = None
            match = re.search(r"line (\d+)", error_str)
            if match:
                line = int(match.group(1))
            raise ScriptSyntaxError(error_str, line=line) from e

        except pydantic_monty.MontyRuntimeError as e:
            raise ScriptExecutionError(f"Script execution failed: {e}") from e

        except pydantic_monty.MontyError as e:
            raise ScriptExecutionError(f"Script execution failed: {e}") from e

        except (ScriptSyntaxError, ScriptExecutionError, ScriptTimeoutError):
            raise

        except Exception as e:
            error_msg = f"Script execution failed: {e}"
            logger.error(error_msg, exc_info=True)
            raise ScriptExecutionError(error_msg) from e

    async def _build_execution_context_async(
        self,
        # ast-grep-ignore: no-dict-any - Script globals are user-provided Python values keyed by name
        globals_dict: dict[str, Any] | None,
        execution_context: "ToolExecutionContext | None",
        # ast-grep-ignore: no-dict-any - Inputs dict carries user-provided Python values keyed by name
    ) -> tuple[dict[str, Callable[..., Any]], dict[str, Any]]:
        """
        Build external function implementations and inputs for async evaluation.

        Unlike _build_execution_context, this creates async tool wrapper functions
        that directly await the ToolsProvider, bypassing the sync/async bridge
        entirely. This is the key architectural advantage of Monty's pause/resume
        model: when Monty pauses at a tool call, we can await the async tool
        execution directly without thread pools or event loop bridging.

        Returns:
            Tuple of (function_implementations, inputs)
        """
        ext_fn_impls: dict[str, Callable[..., Any]] = {}
        # ast-grep-ignore: no-dict-any - Inputs carry user-provided Python values keyed by name
        inputs: dict[str, Any] = {}

        if globals_dict:
            for key, value in globals_dict.items():
                if callable(value):
                    ext_fn_impls[key] = value
                else:
                    inputs[key] = value

        ext_fn_impls["wake_llm"] = self._create_wake_llm_function()

        if self.config.enable_json_api:
            self._add_json_api(ext_fn_impls)
        self._add_base64_api(ext_fn_impls)
        if self.config.enable_time_api:
            self._add_time_api(ext_fn_impls, inputs, execution_context)
        if self.config.enable_llm_api:
            self._add_llm_api(ext_fn_impls)

        if execution_context and execution_context.attachment_registry:
            try:
                self._add_attachment_api_async(ext_fn_impls, execution_context)
                logger.debug("Added async attachment API to Monty engine")
            except Exception as e:
                logger.warning(f"Failed to add attachment API: {e}")

        if self.tools_provider and execution_context:
            await self._add_tools_async(ext_fn_impls, execution_context)

        return ext_fn_impls, inputs

    async def _add_tools_async(
        self,
        impls: dict[str, Callable[..., Any]],
        execution_context: "ToolExecutionContext",
    ) -> None:
        """
        Add async tool functions that directly await the ToolsProvider.

        Tool calls from
        scripts are handled via Monty's pause/resume model: when a script calls
        a tool function, Monty pauses, we await the async ToolsProvider directly,
        then resume Monty with the result.
        """
        assert self.tools_provider is not None

        def is_tool_allowed(tool_name: str) -> bool:
            if self.config.deny_all_tools:
                return False
            if self.config.allowed_tools is not None:
                return tool_name in self.config.allowed_tools
            return True

        if self.config.deny_all_tools:
            impls["tools_list"] = lambda: []
            impls["tools_get"] = lambda name: None
            logger.debug("All tools denied - added empty tool stubs")
            return

        tool_definitions = await self.tools_provider.get_tool_definitions()

        tool_info_map: dict[str, ToolInfo] = {}
        for tool_def in tool_definitions:
            function = tool_def.get("function", {})
            name = function.get("name", "unknown")
            if not is_tool_allowed(name):
                continue
            tool_info_map[name] = ToolInfo(
                name=name,
                description=function.get("description", "No description available"),
                parameters=function.get("parameters", {}),
            )

        def tools_list() -> list[ToolInfo]:
            return list(tool_info_map.values())

        impls["tools_list"] = tools_list

        def tools_get(tool_name: str) -> ToolInfo | None:
            return tool_info_map.get(tool_name)

        impls["tools_get"] = tools_get

        raw_definitions = self._get_raw_tool_definitions_sync()

        async def execute_tool_async(
            tool_name: str,
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            assert self.tools_provider is not None

            if not is_tool_allowed(tool_name):
                raise PermissionError(
                    f"Tool '{tool_name}' is not allowed for execution"
                )

            if args:
                info = tool_info_map.get(tool_name)
                if info and info.get("parameters"):
                    required = info["parameters"].get("required", [])
                    for i, arg in enumerate(args):
                        if i < len(required):
                            kwargs[required[i]] = arg

            logger.info(
                f"Executing tool '{tool_name}' from Monty (async) with args: {kwargs}"
            )

            tool_definition = _find_raw_definition(tool_name)
            processed_kwargs = await process_attachment_arguments(
                kwargs, execution_context, tool_definition
            )

            result = await self.tools_provider.execute_tool(
                name=tool_name,
                arguments=processed_kwargs,
                context=execution_context,
            )

            logger.debug(f"Tool '{tool_name}' executed successfully (async)")
            return await self._format_tool_result_async(
                result, tool_name, execution_context
            )

        def _find_raw_definition(tool_name: str) -> "ToolDefinition | None":
            for d in raw_definitions:
                if d.get("function", {}).get("name") == tool_name:
                    return d
            return None

        async def tools_execute(
            tool_name: str,
            *args: Any,  # noqa: ANN401
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            return await execute_tool_async(tool_name, *args, **kwargs)

        impls["tools_execute"] = tools_execute

        async def tools_execute_json(
            tool_name: str,
            args_json: str,
        ) -> Any:  # noqa: ANN401
            parsed_args = json.loads(args_json)
            if not isinstance(parsed_args, dict):
                raise ValueError(
                    f"Arguments: expected JSON object, got {type(parsed_args).__name__}"
                )
            return await execute_tool_async(tool_name, **parsed_args)

        impls["tools_execute_json"] = tools_execute_json

        for tool_name in tool_info_map:

            def make_async_tool_wrapper(
                name: str,
            ) -> Callable[..., Any]:
                async def tool_wrapper(
                    *args: Any,  # noqa: ANN401
                    **kwargs: Any,  # noqa: ANN401
                ) -> Any:  # noqa: ANN401
                    return await execute_tool_async(name, *args, **kwargs)

                return tool_wrapper

            impls[tool_name] = make_async_tool_wrapper(tool_name)
            impls[f"tool_{tool_name}"] = make_async_tool_wrapper(tool_name)

        logger.debug(
            "Added async tools API to Monty engine (allowed_tools=%s, "
            "deny_all_tools=%s, direct_tools=%d)",
            self.config.allowed_tools,
            self.config.deny_all_tools,
            len(tool_info_map),
        )

    def _add_attachment_api_async(
        self,
        impls: dict[str, Callable[..., Any]],
        execution_context: "ToolExecutionContext",
    ) -> None:
        """
        Add async attachment functions that bypass the sync bridge.

        Creates async functions for attachment_get, attachment_read, and
        attachment_create that work directly with the AttachmentRegistry.
        """
        api = create_attachment_api(execution_context)

        async def attachment_get(
            attachment_id: str,
        ) -> AttachmentInfoDict | None:
            return await api._get_async(attachment_id)

        async def attachment_read(attachment_id: str) -> str | None:
            return await api._read_async(attachment_id)

        async def attachment_read_bytes(attachment_id: str) -> bytes | None:
            return await api._read_bytes_async(attachment_id)

        async def attachment_create(
            content: bytes | str,
            filename: str,
            description: str = "",
            mime_type: str = "application/octet-stream",
        ) -> AttachmentResultInfo:
            metadata = await api._create_async(
                content, filename, description, mime_type
            )
            return AttachmentResultInfo(
                id=metadata.attachment_id,
                filename=metadata.metadata.get("original_filename", "unknown")
                if metadata.metadata
                else "unknown",
                mime_type=metadata.mime_type,
                size=metadata.size,
                description=metadata.description,
            )

        for name, fn in [
            ("attachment_get", attachment_get),
            ("attachment_read", attachment_read),
            ("attachment_read_bytes", attachment_read_bytes),
            ("attachment_create", attachment_create),
        ]:
            impls[name] = fn

    async def _format_tool_result_async(
        self,
        result: Any,  # noqa: ANN401
        tool_name: str,
        execution_context: "ToolExecutionContext",
    ) -> (
        str
        | AttachmentResultInfo
        | AttachmentResultWithText
        # ast-grep-ignore: no-dict-any - ToolResult.data can be an arbitrary dict from tool implementations
        | dict[str, Any]
        | list[Any]
        | int
        | float
        | bool
    ):
        """
        Format a tool execution result for script consumption.

        Handles ToolResult objects with attachments by storing them via the
        registry (async, no bridge needed) and returning script-friendly dicts.
        """
        if isinstance(result, ToolResult):
            if result.attachments:
                attachment_registry = execution_context.attachment_registry
                if not attachment_registry:
                    logger.warning(
                        f"Tool '{tool_name}' returned attachments but "
                        "attachment_registry not available"
                    )
                    return result.to_string()

                attachment_ids = []
                for attachment in result.attachments:
                    if attachment.content:
                        try:
                            file_ext = (
                                mimetypes.guess_extension(attachment.mime_type)
                                or ".bin"
                            )
                            filename = f"tool_result_{uuid.uuid4()}{file_ext}"
                            registered_metadata = await attachment_registry.store_and_register_tool_attachment(
                                file_content=attachment.content,
                                filename=filename,
                                content_type=attachment.mime_type,
                                tool_name=tool_name,
                                description=attachment.description
                                or f"Output from {tool_name}",
                                conversation_id=execution_context.conversation_id,
                                metadata={
                                    "source": "script_tool_call",
                                    "auto_display": True,
                                },
                            )
                            attachment.attachment_id = registered_metadata.attachment_id
                            attachment_ids.append(registered_metadata.attachment_id)
                            logger.info(
                                f"Stored tool attachment from '{tool_name}': "
                                f"{registered_metadata.attachment_id}"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to store attachment from tool "
                                f"'{tool_name}': {e}",
                                exc_info=True,
                            )
                    elif attachment.attachment_id:
                        attachment_ids.append(attachment.attachment_id)

                script_attachments = []
                for attachment_id in attachment_ids:
                    script_att = await fetch_attachment_object(
                        attachment_id, execution_context
                    )
                    if script_att:
                        script_attachments.append(script_att)
                    else:
                        logger.warning(
                            f"Could not fetch stored attachment {attachment_id}"
                        )

                if len(script_attachments) == 1 and not (
                    result.text and result.text.strip()
                ):
                    att = script_attachments[0]
                    return AttachmentResultInfo(
                        id=att.get_id(),
                        mime_type=att.get_mime_type(),
                        description=att.get_description(),
                        size=att.get_size(),
                        filename=att.get_filename(),
                    )
                elif script_attachments:
                    return AttachmentResultWithText(
                        text=result.text,
                        attachments=[
                            AttachmentResultInfo(
                                id=att.get_id(),
                                mime_type=att.get_mime_type(),
                                description=att.get_description(),
                                size=att.get_size(),
                                filename=att.get_filename(),
                            )
                            for att in script_attachments
                        ],
                    )
                else:
                    if result.data is not None:
                        return result.data
                    return result.to_string()
            else:
                if result.data is not None:
                    return result.data
                return result.to_string()

        elif isinstance(result, dict | list):
            return result
        else:
            return str(result)

    def _get_raw_tool_definitions_sync(self) -> "list[ToolDefinition]":
        """
        Get raw tool definitions from the provider without async bridge.

        Raw definitions (without LLM translation) are needed to detect
        attachment-type parameters for argument processing.
        """
        if self.tools_provider is None:
            return []

        if isinstance(self.tools_provider, LocalToolsProvider):
            return self.tools_provider.get_raw_tool_definitions()
        elif isinstance(self.tools_provider, CompositeToolsProvider):
            all_raw: list[ToolDefinition] = []
            for provider in self.tools_provider.get_providers():
                if isinstance(provider, LocalToolsProvider):
                    all_raw.extend(provider.get_raw_tool_definitions())
            return all_raw
        elif hasattr(self.tools_provider, "get_raw_tool_definitions"):
            raw_method = getattr(self.tools_provider, "get_raw_tool_definitions", None)
            if raw_method:
                return raw_method() or []
        return []

    def _add_json_api(
        self,
        impls: dict[str, Callable[..., Any]],
    ) -> None:
        """Add JSON encode/decode functions."""
        for name, fn in [
            ("json_encode", json.dumps),
            ("json_decode", _safe_json_decode),
        ]:
            impls[name] = fn

    def _add_base64_api(
        self,
        impls: dict[str, Callable[..., Any]],
    ) -> None:
        """Add base64 encode/decode functions."""
        for name, fn in [
            ("base64_encode", _base64_encode),
            ("base64_decode", _base64_decode),
            ("base64_decode_bytes", _base64_decode_bytes),
        ]:
            impls[name] = fn

    def _add_time_api(
        self,
        impls: dict[str, Callable[..., Any]],
        # ast-grep-ignore: no-dict-any - Inputs carry user-provided Python values keyed by name
        inputs: dict[str, Any],
        execution_context: "ToolExecutionContext | None" = None,
    ) -> None:
        """Add time API functions and constants.

        ``time_now``, ``time_from_timestamp`` and ``time_parse`` are bound
        to the configured timezone so scripts calling them without arguments
        see wall-clock time in that zone rather than UTC — scripts (and, by
        extension, the LLM and user) must never see UTC wall-clock times.
        Explicit ``tz=`` overrides are respected.

        The timezone is resolved from (in order):
        1. ``execution_context.timezone`` (per-call context), or
        2. ``self.default_timezone`` (engine-level default, e.g. for event conditions).

        If neither is available, a ``ValueError`` is raised to prevent
        silent fallback to UTC.
        """
        default_tz = (
            execution_context.timezone if execution_context is not None else None
        ) or self.default_timezone

        if default_tz is None:
            raise ValueError(
                "Time API is enabled but no timezone was provided. "
                "Pass a timezone via execution_context.timezone or "
                "MontyEngine(default_timezone=...)."
            )

        def time_now_bound(
            tz: "tzinfo | str | None" = None,
        ) -> time_api.TimeDict:
            return time_api.time_now(tz if tz is not None else default_tz)

        def time_from_timestamp_bound(
            seconds: float,
            nanoseconds: int = 0,
            tz: "tzinfo | str | None" = None,
        ) -> time_api.TimeDict:
            return time_api.time_from_timestamp(
                seconds,
                nanoseconds,
                tz if tz is not None else default_tz,
            )

        def time_parse_bound(
            time_string: str,
            format_string: str = "",
            timezone_name: str = "",
        ) -> time_api.TimeDict:
            return time_api.time_parse(
                time_string,
                format_string,
                timezone_name,
                _default_tz=default_tz,
            )

        def is_between_bound(
            start_hour: int,
            end_hour: int,
            time_dict: time_api.TimeDict | None = None,
        ) -> bool:
            if time_dict is None:
                time_dict = time_now_bound()
            return time_api.is_between(start_hour, end_hour, time_dict)

        def is_weekend_bound(
            time_dict: time_api.TimeDict | None = None,
        ) -> bool:
            if time_dict is None:
                time_dict = time_now_bound()
            return time_api.is_weekend(time_dict)

        time_functions: list[tuple[str, Callable[..., Any]]] = [
            # Time creation
            ("time_now", time_now_bound),
            ("time_now_utc", time_api.time_now_utc),
            ("time_create", time_api.time_create),
            ("time_from_timestamp", time_from_timestamp_bound),
            ("time_parse", time_parse_bound),
            # Time manipulation
            ("time_in_location", time_api.time_in_location),
            ("time_format", time_api.time_format),
            ("time_add", time_api.time_add),
            ("time_add_duration", time_api.time_add_duration),
            # Time components
            ("time_year", time_api.time_year),
            ("time_month", time_api.time_month),
            ("time_day", time_api.time_day),
            ("time_hour", time_api.time_hour),
            ("time_minute", time_api.time_minute),
            ("time_second", time_api.time_second),
            ("time_weekday", time_api.time_weekday),
            # Time comparison
            ("time_before", time_api.time_before),
            ("time_after", time_api.time_after),
            ("time_equal", time_api.time_equal),
            ("time_diff", time_api.time_diff),
            # Duration
            ("duration_parse", time_api.duration_parse),
            ("duration_human", time_api.duration_human),
            # Timezone
            ("timezone_is_valid", time_api.timezone_is_valid),
            ("timezone_offset", time_api.timezone_offset),
            # Utility
            ("is_between", is_between_bound),
            ("is_weekend", is_weekend_bound),
        ]

        for name, fn in time_functions:
            impls[name] = fn

        # Duration constants as inputs
        inputs.update({
            "NANOSECOND": time_api.NANOSECOND,
            "MICROSECOND": time_api.MICROSECOND,
            "MILLISECOND": time_api.MILLISECOND,
            "SECOND": time_api.SECOND,
            "MINUTE": time_api.MINUTE,
            "HOUR": time_api.HOUR,
            "DAY": time_api.DAY,
            "WEEK": time_api.WEEK,
        })

    def _add_llm_api(
        self,
        impls: dict[str, Callable[..., Any]],
    ) -> None:
        """Add LLM API functions (llm, llm_json)."""
        from .apis.llm import llm_call_async, llm_call_json_async  # noqa: PLC0415

        impls["llm"] = llm_call_async
        impls["llm_json"] = llm_call_json_async

    def _build_resource_limits(self) -> pydantic_monty.ResourceLimits:
        """Build Monty resource limits from config.

        Monty supports additional resource limits beyond execution time:
        max_memory, max_allocations, max_recursion_depth, gc_interval.
        These are set to sensible defaults here. To expose them via config,
        add fields to ScriptConfig or create a MontyConfig subclass.
        """
        return pydantic_monty.ResourceLimits(
            max_duration_secs=self.config.max_execution_time,
            max_memory=256 * 1024 * 1024,  # 256 MB
            max_recursion_depth=100,
        )

    def _create_print_callback(
        self,
    ) -> Callable[[str, str], None]:
        """Create a print callback that logs output.

        When enable_print is False, the callback raises an error to prevent
        print() usage.
        """
        if not self.config.enable_print:

            def deny_print(stream: str, text: str) -> None:
                raise RuntimeError("print() is not available in this context")

            return deny_print

        def print_callback(stream: str, text: str) -> None:
            stripped = text.rstrip("\n")
            if stripped:
                logger.info("Script output: %s", stripped)
                if self.config.enable_debug:
                    print(f"[SCRIPT] {stripped}")

        return print_callback

    def _create_wake_llm_function(self) -> Callable[..., None]:
        """Create a wake_llm function for scripts."""

        # ast-grep-ignore: no-dict-any - Script wake context values are user-provided and vary by key
        def wake_llm(context: dict[str, Any] | str, include_event: bool = True) -> None:
            if isinstance(context, str):
                context_dict: dict[str, str | list[str]] = {"message": context}
            elif isinstance(context, dict):
                context_dict = dict(context)
            else:
                raise TypeError(
                    f"wake_llm context: expected dict or str, got {type(context).__name__}"
                )

            if "attachments" in context_dict:
                attachments = context_dict["attachments"]
                if not isinstance(attachments, list):
                    raise TypeError(
                        f"attachments: expected list, got {type(attachments).__name__}"
                    )

                for attachment_id in attachments:
                    if not isinstance(attachment_id, str):
                        raise TypeError(
                            f"attachment ID: expected str, got {type(attachment_id).__name__}"
                        )
                    try:
                        uuid.UUID(attachment_id)
                    except ValueError as e:
                        raise ValueError(
                            f"Invalid attachment ID format: {attachment_id}"
                        ) from e

            wake_request = WakeRequest(
                context=context_dict,
                include_event=include_event,
            )
            self._wake_llm_contexts.append(wake_request)
            logger.debug(f"Script requested LLM wake with context: {context_dict}")

        return wake_llm

    def get_pending_wake_contexts(self) -> list[WakeRequest]:
        """Get any pending wake_llm contexts from the last script execution."""
        return self._pending_wake_contexts
