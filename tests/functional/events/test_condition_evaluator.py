"""
Tests for event condition evaluator.
"""

from zoneinfo import ZoneInfo

import pytest

from family_assistant.events.condition_evaluator import (
    EventConditionEvaluator,
    EventConditionValidator,
)
from family_assistant.scripting import ScriptExecutionError, ScriptSyntaxError
from family_assistant.storage.types import EventConditionEvaluatorConfig

TEST_TZ = ZoneInfo("Australia/Sydney")

# Production gives an event condition ~100ms, because conditions are evaluated on
# every incoming event. Holding the tests to that budget makes them measure how
# loaded the machine is rather than whether the API works, and they fail under a
# parallel run. No test here covers timeout behaviour, so give execution room.
TEST_SCRIPT_TIMEOUT: EventConditionEvaluatorConfig = {
    "script_execution_timeout_ms": 5000
}


class TestEventConditionEvaluator:
    """Test event condition evaluator."""

    @pytest.fixture
    def evaluator(self) -> EventConditionEvaluator:
        """Create evaluator instance."""
        return EventConditionEvaluator(config=TEST_SCRIPT_TIMEOUT, timezone=TEST_TZ)

    @pytest.mark.asyncio
    async def test_simple_boolean_condition(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """Test simple boolean return."""
        script = "True"
        result = await evaluator.evaluate_condition(script, {})
        assert result is True

        script = "False"
        result = await evaluator.evaluate_condition(script, {})
        assert result is False

    @pytest.mark.asyncio
    async def test_event_data_access(self, evaluator: EventConditionEvaluator) -> None:
        """Test accessing event data."""
        script = "event.get('entity_id') == 'person.test'"
        event_data = {"entity_id": "person.test"}
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is True

        event_data = {"entity_id": "person.other"}
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is False

    @pytest.mark.asyncio
    async def test_state_transition_detection(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """Test detecting state transitions."""
        # Zone entry detection
        script = "event.get('old_state', {}).get('state') != 'home' and event.get('new_state', {}).get('state') == 'home'"
        # Person arrives home
        event_data = {
            "entity_id": "person.test",
            "old_state": {"state": "not_home"},
            "new_state": {"state": "home"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is True

        # Person was already home
        event_data = {
            "entity_id": "person.test",
            "old_state": {"state": "home"},
            "new_state": {"state": "home"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is False

    @pytest.mark.asyncio
    async def test_any_state_change(self, evaluator: EventConditionEvaluator) -> None:
        """Test detecting any state change."""
        script = "event.get('old_state', {}).get('state') != event.get('new_state', {}).get('state')"
        # State changed
        event_data = {
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is True

        # Only attributes changed
        event_data = {
            "old_state": {"state": "on"},
            "new_state": {"state": "on"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is False

    @pytest.mark.asyncio
    async def test_complex_conditions(self, evaluator: EventConditionEvaluator) -> None:
        """Test complex condition logic."""
        # Temperature threshold - using numeric comparison
        # First test with a simpler version that doesn't rely on specific data types
        simple_script = "event.get('temperature_increased', False)"
        result = await evaluator.evaluate_condition(
            simple_script, {"temperature_increased": True}
        )
        assert result is True

        # Now test numeric comparison directly (not through validator which uses different sample data)
        script = "int(event.get('new_state', {}).get('state', '0')) > int(event.get('old_state', {}).get('state', '0')) + 5"
        # Temperature increased by more than 5
        event_data = {
            "old_state": {"state": "20"},
            "new_state": {"state": "26"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is True

        # Temperature didn't increase enough
        event_data = {
            "old_state": {"state": "20"},
            "new_state": {"state": "24"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is False

    @pytest.mark.asyncio
    async def test_non_boolean_return(self, evaluator: EventConditionEvaluator) -> None:
        """Test error on non-boolean return."""
        script = "'not a boolean'"
        with pytest.raises(ScriptExecutionError) as exc_info:
            await evaluator.evaluate_condition(script, {})
        assert "must return boolean" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_syntax_error(self, evaluator: EventConditionEvaluator) -> None:
        """Test syntax error handling."""
        script = "True ("  # Invalid syntax
        with pytest.raises(ScriptSyntaxError):
            await evaluator.evaluate_condition(script, {})

    @pytest.mark.asyncio
    async def test_runtime_error(self, evaluator: EventConditionEvaluator) -> None:
        """Test runtime error handling."""
        script = "undefined_variable"
        with pytest.raises(ScriptExecutionError):
            await evaluator.evaluate_condition(script, {})

    @pytest.mark.asyncio
    async def test_no_tool_access(self, evaluator: EventConditionEvaluator) -> None:
        """Test that tools are not accessible."""
        script = "tools_list()"
        with pytest.raises(ScriptExecutionError):
            await evaluator.evaluate_condition(script, {})

    @pytest.mark.asyncio
    async def test_no_print_function(self, evaluator: EventConditionEvaluator) -> None:
        """Test that print is not available."""
        script = "print('test') or True"
        with pytest.raises(ScriptExecutionError):
            await evaluator.evaluate_condition(script, {})

    @pytest.mark.asyncio
    async def test_time_api_available(self, evaluator: EventConditionEvaluator) -> None:
        """Time API functions like time_now/time_hour are usable in conditions."""
        script = "0 <= time_hour(time_now()) <= 23"
        result = await evaluator.evaluate_condition(script, {})
        assert result is True

    @pytest.mark.asyncio
    async def test_json_api_available(self, evaluator: EventConditionEvaluator) -> None:
        """JSON API functions (json_encode/json_decode) are usable in conditions.

        Parity with test_time_api_available: this PR is about enabling both
        `time` and `json` APIs for event conditions, so both need runtime
        coverage to guard against regression if `enable_json_api` were ever
        flipped off for the event profile.
        """
        script = (
            "json_decode(json_encode(event.get('payload'))) == event.get('payload')"
        )
        event_data = {"payload": {"temperature": 22, "unit": "C"}}
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is True

    @pytest.mark.asyncio
    async def test_time_now_uses_configured_timezone(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """time_now() returns the configured timezone, not UTC."""
        script = "time_now()['timezone'] == 'Australia/Sydney'"
        result = await evaluator.evaluate_condition(script, {})
        assert result is True

    @pytest.mark.asyncio
    async def test_no_timezone_raises_error(self) -> None:
        """Creating an evaluator without timezone raises when time API is used."""
        evaluator = EventConditionEvaluator(config=TEST_SCRIPT_TIMEOUT, timezone=None)
        with pytest.raises(ScriptExecutionError, match="no timezone was provided"):
            await evaluator.evaluate_condition("time_hour(time_now()) >= 0", {})

    @pytest.mark.asyncio
    async def test_time_api_in_realistic_condition(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """Realistic afternoon-arrival condition combining state + time."""
        script = (
            "event.get('old_state', {}).get('state') != 'Barangaroo Metro' "
            "and time_hour(time_now()) >= 0"
        )
        event_data = {
            "old_state": {"state": "Wynyard"},
            "new_state": {"state": "Barangaroo Metro"},
        }
        result = await evaluator.evaluate_condition(script, event_data)
        assert result is True

    @pytest.mark.asyncio
    async def test_llm_api_not_available(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """LLM API is not exposed to event conditions.

        Event conditions run on every incoming event under a tight timeout;
        exposing llm() would create an exfiltration vector and an unbounded
        cost path, so it must be unreachable at runtime regardless of the
        rest of the API surface.
        """
        with pytest.raises(ScriptExecutionError):
            await evaluator.evaluate_condition("llm('hi') == 'x'", {})

    @pytest.mark.asyncio
    async def test_tools_api_not_available(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """tools_* helpers are not exposed to event conditions.

        EventConditionEvaluator constructs MontyEngine with tools_provider=None,
        so no tools_* names are ever registered at runtime. Scripts that try to
        call them must fail at runtime, matching what the validator rejects
        at save-time.
        """
        with pytest.raises(ScriptExecutionError):
            await evaluator.evaluate_condition("tools_list() == []", {})

    @pytest.mark.asyncio
    async def test_attachment_api_not_available(
        self, evaluator: EventConditionEvaluator
    ) -> None:
        """attachment_* helpers are not exposed to event conditions.

        EventConditionEvaluator passes no execution_context, so the attachment
        registry is never wired up and attachment_* names are never registered
        at runtime.
        """
        with pytest.raises(ScriptExecutionError):
            await evaluator.evaluate_condition("attachment_get('x') is not None", {})


class TestEventConditionValidator:
    """Test event condition validator."""

    @pytest.fixture
    def validator(self) -> EventConditionValidator:
        """Create validator instance."""
        return EventConditionValidator(config=TEST_SCRIPT_TIMEOUT, timezone=TEST_TZ)

    @pytest.mark.asyncio
    async def test_valid_script(self, validator: EventConditionValidator) -> None:
        """Test validating a valid script."""
        script = "event.get('state') == 'on'"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is True
        assert error is None

    @pytest.mark.asyncio
    async def test_syntax_error_validation(
        self, validator: EventConditionValidator
    ) -> None:
        """Test validating script with syntax error."""
        script = "True ("
        is_valid, error = await validator.validate_script(script)
        assert is_valid is False
        assert error is not None and "Syntax error" in error

    @pytest.mark.asyncio
    async def test_non_boolean_validation(
        self, validator: EventConditionValidator
    ) -> None:
        """Test validating script that doesn't return boolean."""
        script = "'string'"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is False
        assert error is not None and "must return boolean" in error

    @pytest.mark.asyncio
    async def test_script_size_limit(self, validator: EventConditionValidator) -> None:
        """Test script size validation."""
        # Create a script larger than 10KB
        large_script = "# " + "x" * 10240 + "\nTrue"
        is_valid, error = await validator.validate_script(large_script)
        assert is_valid is False
        assert error is not None and "too large" in error

    @pytest.mark.asyncio
    async def test_custom_size_limit(self) -> None:
        """Test custom size limit configuration."""
        validator = EventConditionValidator(
            config={"script_size_limit_bytes": 100}, timezone=TEST_TZ
        )
        script = "# " + "x" * 100 + "\nTrue"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is False
        assert error is not None and "max 100 bytes" in error

    @pytest.mark.asyncio
    async def test_time_api_validates(self, validator: EventConditionValidator) -> None:
        """Validator accepts conditions that call time API functions.

        This guards against the regression where the validator's environment
        diverged from the runtime environment, causing scripts that work at
        runtime to be rejected at validation time (or vice versa).
        """
        script = "time_hour(time_now()) >= 12"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is True, f"Validation failed: {error}"

    @pytest.mark.asyncio
    async def test_json_api_validates(self, validator: EventConditionValidator) -> None:
        """Validator accepts conditions that call JSON API functions.

        Parity with test_time_api_validates: both time and json APIs are
        enabled for event conditions, so static validation must recognize
        json_encode/json_decode the same way it recognizes time_*.
        """
        script = "json_decode(json_encode(event)) == event"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is True, f"Validation failed: {error}"

    @pytest.mark.asyncio
    async def test_undefined_function_rejected_by_validator(
        self, validator: EventConditionValidator
    ) -> None:
        """Validator rejects calls to functions that don't exist at runtime."""
        script = "definitely_not_a_real_function() and True"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_validator_rejects_llm_call(
        self, validator: EventConditionValidator
    ) -> None:
        """Validator rejects llm() because it's not exposed at runtime either.

        This is the parity guard for the LLM API: the runtime denies it, so
        validation must reject it at save-time rather than letting users
        commit a script that will only fail when the event fires.
        """
        is_valid, error = await validator.validate_script("llm('x') == 'y'")
        assert is_valid is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_validator_rejects_tools_execute(
        self, validator: EventConditionValidator
    ) -> None:
        """Validator rejects tools_execute() because event-condition runtime
        has no tools_provider, so the call would fail with NameError every
        time the event fired.
        """
        is_valid, error = await validator.validate_script(
            "tools_execute('send_telegram', 'hi') == 'ok'"
        )
        assert is_valid is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_validator_rejects_tools_list(
        self, validator: EventConditionValidator
    ) -> None:
        """Validator rejects tools_list() for the same reason as tools_execute.

        The ScriptValidator defaults include_tools_api=True, so this test
        guards against regressions where the event-condition validator
        forgets to pass include_tools_api=False.
        """
        is_valid, error = await validator.validate_script("tools_list() == []")
        assert is_valid is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_validator_rejects_attachment_get(
        self, validator: EventConditionValidator
    ) -> None:
        """Validator rejects attachment_get() because event conditions have
        no attachment registry at runtime.
        """
        is_valid, error = await validator.validate_script(
            "attachment_get('x') is not None"
        )
        assert is_valid is False
        assert error is not None
