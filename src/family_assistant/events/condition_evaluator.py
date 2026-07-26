"""
Event condition evaluator for script-based conditions.
"""

import logging
import textwrap
from datetime import tzinfo
from typing import Any

from family_assistant.scripting import ScriptExecutionError, ScriptSyntaxError
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.scripting.validator import ScriptValidator
from family_assistant.storage.types import EventConditionEvaluatorConfig

logger = logging.getLogger(__name__)


class EventConditionEvaluator:
    """Evaluates condition scripts for event matching."""

    def __init__(
        self,
        config: EventConditionEvaluatorConfig | None = None,
        timezone: tzinfo | None = None,
    ) -> None:
        """
        Initialize the event condition evaluator.

        Args:
            config: Optional configuration dictionary with settings like
                   script_execution_timeout_ms and script_size_limit_bytes
            timezone: Timezone for time API functions in condition scripts.
                     Required so that time_now() returns local wall-clock time
                     rather than silently falling back to UTC.
        """
        # Restricted config for event conditions.
        # Note: We intentionally create a new MontyEngine instance here rather than
        # using dependency injection to ensure complete isolation and security.
        # Tool access is denied. The cheap pure-Python helpers (json, time) are
        # exposed so conditions can do simple things like check the current time,
        # but the LLM API is not — event conditions are evaluated on every
        # incoming event, must run within a tight (~100ms) timeout, and an LLM
        # call would be both impractical and an exfiltration vector.
        timeout_ms = (config or {}).get("script_execution_timeout_ms", 100)
        self.config = ScriptConfig(
            max_execution_time=timeout_ms / 1000.0,  # Convert to seconds
            enable_print=False,
            enable_debug=False,
            deny_all_tools=True,
            enable_json_api=True,
            enable_time_api=True,
            enable_llm_api=False,
        )
        self.engine = MontyEngine(
            tools_provider=None, config=self.config, default_timezone=timezone
        )

    # ast-grep-ignore: no-dict-any - event_data is arbitrary JSON from external sources (Home Assistant, webhooks) with no fixed schema
    async def evaluate_condition(self, script: str, event_data: dict[str, Any]) -> bool:
        """
        Evaluate a condition script against event data.

        Args:
            script: The script to evaluate
            event_data: The event data to make available to the script

        Returns:
            Boolean indicating whether the condition matches

        Raises:
            ScriptSyntaxError: If the script has invalid syntax
            ScriptExecutionError: If the script fails during execution
        """
        try:
            # For event conditions, wrap simple expressions in return statement
            # or use the script as-is if it already contains return

            # If script doesn't contain 'return', treat it as an expression
            if "return" not in script:
                wrapped_script = f"""
def _evaluate():
    return {script}

_evaluate()
"""
            else:
                # Script already has return statements, just wrap in function
                # Use textwrap.indent to safely indent multi-line scripts
                indented_script = textwrap.indent(script, "    ")
                wrapped_script = f"""
def _evaluate():
{indented_script}

_evaluate()
"""

            result = await self.engine.evaluate_async(
                wrapped_script,
                globals_dict={"event": event_data},
                execution_context=None,
            )

            # Ensure boolean result
            if not isinstance(result, bool):
                raise ScriptExecutionError(
                    f"Script must return boolean, got {type(result).__name__}"
                )

            return result

        except (ScriptSyntaxError, ScriptExecutionError):
            # Re-raise script errors as-is
            raise
        except Exception as e:
            # Wrap other errors
            raise ScriptExecutionError(f"Script execution failed: {e!s}") from e

    async def validate_script(self, script: str) -> tuple[bool, str | None]:
        """
        Validate a condition script without executing it.

        Args:
            script: The script to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            # Test with sample event data
            sample_event = {
                "entity_id": "test.entity",
                "event_type": "state_changed",
                "old_state": {"state": "off", "attributes": {}},
                "new_state": {"state": "on", "attributes": {}},
            }
            await self.evaluate_condition(script, sample_event)

            # Script executed successfully and returned boolean (already checked in evaluate_condition)
            return True, None

        except ScriptSyntaxError as e:
            return False, f"Syntax error: {e!s}"
        except ScriptExecutionError as e:
            return False, f"Execution error: {e!s}"
        except Exception as e:
            return False, f"Validation error: {e!s}"


class EventConditionValidator:
    """Validates condition scripts before saving."""

    def __init__(
        self,
        evaluator: EventConditionEvaluator | None = None,
        config: EventConditionEvaluatorConfig | None = None,
        timezone: tzinfo | None = None,
    ) -> None:
        """
        Initialize the validator.

        Args:
            evaluator: Optional evaluator instance to use
            config: Optional configuration dictionary
            timezone: Timezone for time API functions in condition scripts.
        """
        # Use provided evaluator or create one
        self.evaluator = evaluator or EventConditionEvaluator(config, timezone=timezone)
        self.size_limit = (config or {}).get("script_size_limit_bytes", 10240)

    async def validate_script(self, script: str) -> tuple[bool, str | None]:
        """
        Validate a condition script.

        Args:
            script: The script to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check size
        if len(script.encode("utf-8")) > self.size_limit:
            return False, f"Script too large (max {self.size_limit} bytes)"

        # Static type checking (catches syntax and type errors without execution).
        # Wrap the script the same way evaluate_condition() does, so that
        # `return` statements are valid (they're inside a function body at runtime).
        # Use the evaluator's ScriptConfig so validation runs in the same
        # environment as execution: same built-in APIs (time/json enabled, llm
        # disabled) and no tools_*/attachment_* surface, since event conditions
        # run with tools_provider=None and no attachment registry.
        if "return" not in script:
            wrapped = f"def _evaluate():\n    return {script}\n\n_evaluate()"
        else:
            indented = textwrap.indent(script, "    ")
            wrapped = f"def _evaluate():\n{indented}\n\n_evaluate()"

        type_result = ScriptValidator(config=self.evaluator.config).validate(
            wrapped,
            input_names=["event"],
            include_tools_api=False,
            include_attachment_api=False,
        )
        if not type_result.is_valid:
            # Categorize as syntax or type error for consistent error messages
            has_syntax = any(
                "pars" in d.message.lower()
                or "syntax" in d.message.lower()
                or "eof" in d.message.lower()
                for d in type_result.diagnostics
            )
            prefix = "Syntax error" if has_syntax else "Type error"
            return False, f"{prefix}: {type_result.error_message}"

        # Delegate to evaluator for runtime validation with sample data
        return await self.evaluator.validate_script(script)
