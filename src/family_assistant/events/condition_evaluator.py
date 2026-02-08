"""
Event condition evaluator for script-based conditions.
"""

import logging
import textwrap
from typing import Any

from family_assistant.scripting import ScriptExecutionError, ScriptSyntaxError
from family_assistant.scripting.config import ScriptConfig
from family_assistant.scripting.engine import StarlarkEngine

logger = logging.getLogger(__name__)


class EventConditionEvaluator:
    """Evaluates condition scripts for event matching."""

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """
        Initialize the event condition evaluator.

        Args:
            config: Optional configuration dictionary with settings like
                   script_execution_timeout_ms and script_size_limit_bytes
        """
        # Restricted config for event conditions
        # Note: We intentionally create a new StarlarkEngine instance here rather than
        # using dependency injection to ensure complete isolation and security.
        # This engine is configured with maximum restrictions and no access to tools.
        timeout_ms = (config or {}).get("script_execution_timeout_ms", 100)
        self.config = ScriptConfig(
            max_execution_time=timeout_ms / 1000.0,  # Convert to seconds
            enable_print=False,
            enable_debug=False,
            deny_all_tools=True,
            disable_apis=True,  # No JSON, time, or other APIs for security
        )
        self.engine = StarlarkEngine(tools_provider=None, config=self.config)

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
            raise ScriptExecutionError(f"Script execution failed: {str(e)}") from e

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
            return False, f"Syntax error: {str(e)}"
        except ScriptExecutionError as e:
            return False, f"Execution error: {str(e)}"
        except Exception as e:
            return False, f"Validation error: {str(e)}"


class EventConditionValidator:
    """Validates condition scripts before saving."""

    def __init__(
        self,
        evaluator: EventConditionEvaluator | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        config: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialize the validator.

        Args:
            evaluator: Optional evaluator instance to use
            config: Optional configuration dictionary
        """
        # Use provided evaluator or create one
        self.evaluator = evaluator or EventConditionEvaluator(config)
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

        # Delegate to evaluator for actual validation
        return await self.evaluator.validate_script(script)
