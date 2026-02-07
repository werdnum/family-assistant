"""
Tests for event condition evaluator.
"""

import pytest

from family_assistant.events.condition_evaluator import (
    EventConditionEvaluator,
    EventConditionValidator,
)
from family_assistant.scripting import ScriptExecutionError, ScriptSyntaxError


class TestEventConditionEvaluator:
    """Test event condition evaluator."""

    @pytest.fixture
    def evaluator(self) -> EventConditionEvaluator:
        """Create evaluator instance."""
        return EventConditionEvaluator()

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


class TestEventConditionValidator:
    """Test event condition validator."""

    @pytest.fixture
    def validator(self) -> EventConditionValidator:
        """Create validator instance."""
        return EventConditionValidator()

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
        validator = EventConditionValidator(config={"script_size_limit_bytes": 100})
        script = "# " + "x" * 100 + "\nTrue"
        is_valid, error = await validator.validate_script(script)
        assert is_valid is False
        assert error is not None and "max 100 bytes" in error
