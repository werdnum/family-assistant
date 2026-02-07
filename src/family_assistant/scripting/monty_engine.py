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
import re
import uuid
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

import pydantic_monty

from .apis import time as time_api
from .apis.attachments import create_attachment_api
from .apis.tools import create_tools_api
from .engine import StarlarkConfig
from .errors import ScriptExecutionError, ScriptSyntaxError, ScriptTimeoutError

if TYPE_CHECKING:
    from family_assistant.tools import ToolsProvider
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class MontyEngine:
    """
    Monty scripting engine for executing user-defined scripts.

    This engine provides a sandboxed environment for running Python scripts
    using Monty (pydantic-monty), with controlled access to family assistant
    functionality via external functions.

    Uses the same StarlarkConfig for configuration compatibility.
    """

    def __init__(
        self,
        tools_provider: "ToolsProvider | None" = None,
        config: StarlarkConfig | None = None,
    ) -> None:
        self.tools_provider = tools_provider
        self.config = config or StarlarkConfig()
        # ast-grep-ignore: no-dict-any - Wake contexts and script globals are arbitrary dicts
        self._wake_llm_contexts: list[dict[str, Any]] = []
        # ast-grep-ignore: no-dict-any - Wake contexts and script globals are arbitrary dicts
        self._pending_wake_contexts: list[dict[str, Any]] = []
        # ast-grep-ignore: no-dict-any - Script globals can be arbitrary values
        self._script_globals: dict[str, Any] = {}

        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

        logger.info(
            "Initialized MontyEngine with config: max_execution_time=%s, main_loop=%s",
            self.config.max_execution_time,
            self._main_loop is not None,
        )

    def evaluate(
        self,
        script: str,
        # ast-grep-ignore: no-dict-any - Script globals/wake contexts are genuinely arbitrary
        globals_dict: dict[str, Any] | None = None,
        execution_context: "ToolExecutionContext | None" = None,
    ) -> Any:  # noqa: ANN401 # Scripts can return any type
        """
        Evaluate a script synchronously.

        For scripts without async external functions (tools), runs directly.
        For scripts with tools, uses a bridge to the main event loop.
        """
        try:
            self._wake_llm_contexts.clear()
            self._script_globals = globals_dict or {}

            ext_fn_names, ext_fn_impls, inputs = self._build_execution_context(
                globals_dict, execution_context, async_mode=False
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

            result = m.run(
                inputs=inputs or None,
                external_functions=ext_fn_impls or None,
                limits=limits,
                print_callback=print_cb,
            )

            self._pending_wake_contexts = self._wake_llm_contexts.copy()
            return result

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

            ext_fn_names, ext_fn_impls, inputs = self._build_execution_context(
                globals_dict, execution_context, async_mode=True
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

    def _build_execution_context(
        self,
        # ast-grep-ignore: no-dict-any - Script globals can be arbitrary values
        globals_dict: dict[str, Any] | None,
        execution_context: "ToolExecutionContext | None",
        *,
        async_mode: bool,
        # ast-grep-ignore: no-dict-any - Returns (fn_names, fn_impls, inputs) where inputs are arbitrary
    ) -> tuple[list[str], dict[str, Callable[..., Any]], dict[str, Any]]:
        """
        Build external function names, implementations, and inputs.

        Returns:
            Tuple of (external_function_names, function_implementations, inputs)
        """
        ext_fn_names: list[str] = []
        ext_fn_impls: dict[str, Callable[..., Any]] = {}
        # ast-grep-ignore: no-dict-any - Inputs to scripts are arbitrary values
        inputs: dict[str, Any] = {}

        # Add user-provided globals as inputs (non-callable) or external functions (callable)
        if globals_dict:
            for key, value in globals_dict.items():
                if callable(value):
                    ext_fn_names.append(key)
                    ext_fn_impls[key] = value
                else:
                    inputs[key] = value

        # Add print function
        if self.config.enable_print:
            # print is handled via print_callback, not as external function
            pass

        # Add wake_llm
        wake_fn = self._create_wake_llm_function()
        ext_fn_names.append("wake_llm")
        ext_fn_impls["wake_llm"] = wake_fn

        # Add APIs (JSON, time, etc.) unless disabled
        if not self.config.disable_apis:
            self._add_json_api(ext_fn_names, ext_fn_impls)
            self._add_time_api(ext_fn_names, ext_fn_impls, inputs)

        # Add attachment API if available
        if execution_context and execution_context.attachment_registry:
            try:
                attachment_api = create_attachment_api(
                    execution_context, main_loop=self._main_loop
                )
                for name, method in [
                    ("attachment_get", attachment_api.get),
                    ("attachment_read", attachment_api.read),
                    ("attachment_create", attachment_api.create),
                ]:
                    ext_fn_names.append(name)
                    ext_fn_impls[name] = method
                logger.debug("Added attachment API to Monty engine")
            except Exception as e:
                logger.warning(f"Failed to add attachment API: {e}")

        # Add tools API if available
        if self.tools_provider and execution_context:
            tools_api = create_tools_api(
                self.tools_provider,
                execution_context,
                allowed_tools=self.config.allowed_tools,
                deny_all_tools=self.config.deny_all_tools,
                main_loop=self._main_loop,
            )

            # Add functional API
            for name, method in [
                ("tools_list", tools_api.list),
                ("tools_get", tools_api.get),
                ("tools_execute", tools_api.execute),
                ("tools_execute_json", tools_api.execute_json),
            ]:
                ext_fn_names.append(name)
                ext_fn_impls[name] = method

            # Add direct tool callables
            available_tools = tools_api.list()
            for tool_info in available_tools:
                tool_name = tool_info["name"]

                def make_tool_wrapper(
                    name: str,
                ) -> Callable[..., Any]:
                    def tool_wrapper(
                        *args: Any,  # noqa: ANN401
                        **kwargs: Any,  # noqa: ANN401
                    ) -> Any:  # noqa: ANN401
                        if args:
                            return tools_api.execute(name, *args, **kwargs)
                        return tools_api.execute(name, **kwargs)

                    return tool_wrapper

                ext_fn_names.append(tool_name)
                ext_fn_impls[tool_name] = make_tool_wrapper(tool_name)

                prefixed = f"tool_{tool_name}"
                ext_fn_names.append(prefixed)
                ext_fn_impls[prefixed] = make_tool_wrapper(tool_name)

            logger.debug(
                "Added tools API to Monty engine (allowed_tools=%s, "
                "deny_all_tools=%s, direct_tools=%d)",
                self.config.allowed_tools,
                self.config.deny_all_tools,
                len(available_tools),
            )

        return ext_fn_names, ext_fn_impls, inputs

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
        """Build Monty resource limits from config."""
        return pydantic_monty.ResourceLimits(
            max_duration_secs=self.config.max_execution_time,
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
