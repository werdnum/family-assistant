"""
Monty scripting engine for Family Assistant.

This module provides a Monty-based scripting engine (pydantic-monty) that executes
user-defined scripts with access to family assistant tools and state. It implements
the same interface as StarlarkEngine but uses Monty's pause/resume model for
clean async external function handling.
"""

import asyncio
import json
import logging
import mimetypes
import re
import uuid
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

import pydantic_monty

from family_assistant.tools.attachment_utils import (
    fetch_attachment_object,
    process_attachment_arguments,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.types import ToolResult

from .apis import time as time_api
from .apis.attachments import create_attachment_api
from .config import ScriptConfig
from .errors import ScriptExecutionError, ScriptSyntaxError, ScriptTimeoutError

if TYPE_CHECKING:
    from family_assistant.tools import ToolsProvider
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.tools_provider = tools_provider
        self.config = config or ScriptConfig()
        # ast-grep-ignore: no-dict-any - Wake contexts and script globals are arbitrary dicts
        self._wake_llm_contexts: list[dict[str, Any]] = []
        # ast-grep-ignore: no-dict-any - Wake contexts and script globals are arbitrary dicts
        self._pending_wake_contexts: list[dict[str, Any]] = []
        # ast-grep-ignore: no-dict-any - Script globals can be arbitrary values
        self._script_globals: dict[str, Any] = {}

        logger.info(
            "Initialized MontyEngine with config: max_execution_time=%s",
            self.config.max_execution_time,
        )

    async def evaluate_async(
        self,
        script: str,
        # ast-grep-ignore: no-dict-any - Script globals/wake contexts are genuinely arbitrary
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
        # ast-grep-ignore: no-dict-any - Script globals/wake contexts are genuinely arbitrary
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:  # noqa: ANN401
        """Internal async implementation using manual start/resume loop."""
        try:
            self._wake_llm_contexts.clear()
            self._script_globals = globals_dict or {}

            (
                ext_fn_names,
                ext_fn_impls,
                inputs,
            ) = await self._build_execution_context_async(
                globals_dict, execution_context
            )

            m = pydantic_monty.Monty(
                script,
                inputs=list(inputs.keys()) if inputs else [],
                external_functions=ext_fn_names,
            )

            limits = self._build_resource_limits()
            print_cb = (
                self._create_print_callback() if self.config.enable_print else None
            )
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
                if not isinstance(progress, pydantic_monty.MontySnapshot):
                    raise ScriptExecutionError(
                        f"Unexpected Monty progress type: {type(progress)}"
                    )

                fn_name = progress.function_name
                fn = ext_fn_impls.get(fn_name)

                if fn is None:
                    progress = await loop.run_in_executor(
                        None,
                        partial(
                            progress.resume,
                            exception=NameError(f"name '{fn_name}' is not defined"),
                        ),
                    )
                    continue

                try:
                    if asyncio.iscoroutinefunction(fn):
                        result = await fn(*progress.args, **progress.kwargs)
                    else:
                        result = fn(*progress.args, **progress.kwargs)

                    progress = await loop.run_in_executor(
                        None,
                        partial(progress.resume, return_value=result),
                    )
                except Exception as e:
                    progress = await loop.run_in_executor(
                        None,
                        partial(progress.resume, exception=e),
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
        # ast-grep-ignore: no-dict-any - Script globals can be arbitrary values
        globals_dict: dict[str, Any] | None,
        execution_context: "ToolExecutionContext | None",
        # ast-grep-ignore: no-dict-any - Returns (fn_names, fn_impls, inputs) where inputs are arbitrary
    ) -> tuple[list[str], dict[str, Callable[..., Any]], dict[str, Any]]:
        """
        Build external function names, implementations, and inputs for async evaluation.

        Unlike _build_execution_context, this creates async tool wrapper functions
        that directly await the ToolsProvider, bypassing the sync/async bridge
        entirely. This is the key architectural advantage of Monty's pause/resume
        model: when Monty pauses at a tool call, we can await the async tool
        execution directly without thread pools or event loop bridging.

        Returns:
            Tuple of (external_function_names, function_implementations, inputs)
        """
        ext_fn_names: list[str] = []
        ext_fn_impls: dict[str, Callable[..., Any]] = {}
        # ast-grep-ignore: no-dict-any - Inputs to scripts are arbitrary values
        inputs: dict[str, Any] = {}

        if globals_dict:
            for key, value in globals_dict.items():
                if callable(value):
                    ext_fn_names.append(key)
                    ext_fn_impls[key] = value
                else:
                    inputs[key] = value

        wake_fn = self._create_wake_llm_function()
        ext_fn_names.append("wake_llm")
        ext_fn_impls["wake_llm"] = wake_fn

        if not self.config.disable_apis:
            self._add_json_api(ext_fn_names, ext_fn_impls)
            self._add_time_api(ext_fn_names, ext_fn_impls, inputs)

        if execution_context and execution_context.attachment_registry:
            try:
                self._add_attachment_api_async(
                    ext_fn_names, ext_fn_impls, execution_context
                )
                logger.debug("Added async attachment API to Monty engine")
            except Exception as e:
                logger.warning(f"Failed to add attachment API: {e}")

        if self.tools_provider and execution_context:
            await self._add_tools_async(ext_fn_names, ext_fn_impls, execution_context)

        return ext_fn_names, ext_fn_impls, inputs

    async def _add_tools_async(
        self,
        names: list[str],
        impls: dict[str, Callable[..., Any]],
        execution_context: "ToolExecutionContext",
    ) -> None:
        """
        Add async tool functions that directly await the ToolsProvider.

        This bypasses the StarlarkToolsAPI sync bridge entirely. Tool calls from
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
            names.append("tools_list")
            impls["tools_list"] = lambda: []
            names.append("tools_get")
            impls["tools_get"] = lambda name: None
            logger.debug("All tools denied - added empty tool stubs")
            return

        tool_definitions = await self.tools_provider.get_tool_definitions()

        # ast-grep-ignore: no-dict-any - Tool info dicts have mixed value types
        tool_info_map: dict[str, dict[str, Any]] = {}
        for tool_def in tool_definitions:
            function = tool_def.get("function", {})
            name = function.get("name", "unknown")
            if not is_tool_allowed(name):
                continue
            tool_info_map[name] = {
                "name": name,
                "description": function.get("description", "No description available"),
                "parameters": function.get("parameters", {}),
            }

        # ast-grep-ignore: no-dict-any - Tool info dicts have mixed value types
        def tools_list() -> list[dict[str, Any]]:
            return list(tool_info_map.values())

        names.append("tools_list")
        impls["tools_list"] = tools_list

        # ast-grep-ignore: no-dict-any - Tool info dicts have mixed value types
        def tools_get(tool_name: str) -> dict[str, Any] | None:
            return tool_info_map.get(tool_name)

        names.append("tools_get")
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

        names.append("tools_execute")
        impls["tools_execute"] = tools_execute

        async def tools_execute_json(
            tool_name: str,
            args_json: str,
        ) -> Any:  # noqa: ANN401
            parsed_args = json.loads(args_json)
            if not isinstance(parsed_args, dict):
                raise ValueError("Arguments must be a JSON object")
            return await execute_tool_async(tool_name, **parsed_args)

        names.append("tools_execute_json")
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

            names.append(tool_name)
            impls[tool_name] = make_async_tool_wrapper(tool_name)

            prefixed = f"tool_{tool_name}"
            names.append(prefixed)
            impls[prefixed] = make_async_tool_wrapper(tool_name)

        logger.debug(
            "Added async tools API to Monty engine (allowed_tools=%s, "
            "deny_all_tools=%s, direct_tools=%d)",
            self.config.allowed_tools,
            self.config.deny_all_tools,
            len(tool_info_map),
        )

    def _add_attachment_api_async(
        self,
        names: list[str],
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
            # ast-grep-ignore: no-dict-any - Attachment metadata is a generic dict
        ) -> dict[str, Any] | None:
            return await api._get_async(attachment_id)

        async def attachment_read(attachment_id: str) -> str | None:
            return await api._read_async(attachment_id)

        async def attachment_create(
            content: bytes | str,
            filename: str,
            description: str = "",
            mime_type: str = "application/octet-stream",
            # ast-grep-ignore: no-dict-any - Attachment metadata is a generic dict
        ) -> dict[str, Any]:
            metadata = await api._create_async(
                content, filename, description, mime_type
            )
            return {
                "id": metadata.attachment_id,
                "filename": metadata.metadata.get("original_filename", "unknown"),
                "mime_type": metadata.mime_type,
                "size": metadata.size,
                "description": metadata.description,
            }

        for name, fn in [
            ("attachment_get", attachment_get),
            ("attachment_read", attachment_read),
            ("attachment_create", attachment_create),
        ]:
            names.append(name)
            impls[name] = fn

    async def _format_tool_result_async(
        self,
        result: Any,  # noqa: ANN401
        tool_name: str,
        execution_context: "ToolExecutionContext",
        # ast-grep-ignore: no-dict-any - Tool results can be various types
    ) -> str | dict[str, Any] | list[Any] | int | float | bool:
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
                    return {
                        "id": att.get_id(),
                        "mime_type": att.get_mime_type(),
                        "description": att.get_description(),
                        "size": att.get_size(),
                        "filename": att.get_filename(),
                    }
                elif script_attachments:
                    return {
                        "text": result.text,
                        "attachments": [
                            {
                                "id": att.get_id(),
                                "mime_type": att.get_mime_type(),
                                "description": att.get_description(),
                                "size": att.get_size(),
                                "filename": att.get_filename(),
                            }
                            for att in script_attachments
                        ],
                    }
                else:
                    if result.data is not None:
                        return result.data
                    return result.to_string()
            else:
                if result.data is not None:
                    return result.data
                return result.to_string()

        elif isinstance(result, dict | list):
            return json.dumps(result)
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
        names: list[str],
        impls: dict[str, Callable[..., Any]],
    ) -> None:
        """Add JSON encode/decode functions."""
        for name, fn in [
            ("json_encode", json.dumps),
            ("json_decode", json.loads),
        ]:
            names.append(name)
            impls[name] = fn

    def _add_time_api(
        self,
        names: list[str],
        impls: dict[str, Callable[..., Any]],
        # ast-grep-ignore: no-dict-any - Time constants are simple numeric values
        inputs: dict[str, Any],
    ) -> None:
        """Add time API functions and constants."""
        time_functions: list[tuple[str, Callable[..., Any]]] = [
            # Time creation
            ("time_now", time_api.time_now),
            ("time_now_utc", time_api.time_now_utc),
            ("time_create", time_api.time_create),
            ("time_from_timestamp", time_api.time_from_timestamp),
            ("time_parse", time_api.time_parse),
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
            ("is_between", time_api.is_between),
            ("is_weekend", time_api.is_weekend),
        ]

        for name, fn in time_functions:
            names.append(name)
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
        """Create a print callback that logs output."""

        def print_callback(stream: str, text: str) -> None:
            # Filter out bare newlines from print()
            stripped = text.rstrip("\n")
            if stripped:
                logger.info("Script output: %s", stripped)
                if self.config.enable_debug:
                    print(f"[SCRIPT] {stripped}")

        return print_callback

    def _create_wake_llm_function(self) -> Callable[..., None]:
        """Create a wake_llm function for scripts."""

        # ast-grep-ignore: no-dict-any - Script globals/wake contexts are genuinely arbitrary
        def wake_llm(context: dict[str, Any] | str, include_event: bool = True) -> None:
            if isinstance(context, str):
                context_dict = {"message": context}
            elif isinstance(context, dict):
                context_dict = dict(context)
            else:
                raise TypeError("wake_llm context must be a dictionary or string")

            if "attachments" in context_dict:
                attachments = context_dict["attachments"]
                if not isinstance(attachments, list):
                    raise TypeError("attachments must be a list of attachment IDs")

                for attachment_id in attachments:
                    if not isinstance(attachment_id, str):
                        raise TypeError("attachment IDs must be strings")
                    try:
                        uuid.UUID(attachment_id)
                    except ValueError as e:
                        raise ValueError(
                            f"Invalid attachment ID format: {attachment_id}"
                        ) from e

            wake_request = {
                "context": context_dict,
                "include_event": include_event,
            }
            self._wake_llm_contexts.append(wake_request)
            logger.debug(f"Script requested LLM wake with context: {context_dict}")

        return wake_llm

    # ast-grep-ignore: no-dict-any - Script globals/wake contexts are genuinely arbitrary
    def get_pending_wake_contexts(self) -> list[dict[str, Any]]:
        """Get any pending wake_llm contexts from the last script execution."""
        return self._pending_wake_contexts
