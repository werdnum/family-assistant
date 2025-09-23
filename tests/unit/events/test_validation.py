"""
Unit tests for event validation data structures.
"""

from typing import TYPE_CHECKING

import pytest

from family_assistant.events.sources import BaseEventSource, EventSource
from family_assistant.events.validation import ValidationError, ValidationResult

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor


class TestValidationError:
    """Test ValidationError dataclass."""

    def test_creation_minimal(self) -> None:
        """Test creating ValidationError with minimal fields."""
        error = ValidationError(
            field="entity_id",
            value="invalid.entity",
            error="Entity does not exist",
        )

        assert error.field == "entity_id"
        assert error.value == "invalid.entity"
        assert error.error == "Entity does not exist"
        assert error.suggestion is None
        assert error.similar_values is None

    def test_creation_full(self) -> None:
        """Test creating ValidationError with all fields."""
        error = ValidationError(
            field="new_state.state",
            value="On",
            error="Invalid state for binary sensor",
            suggestion="Use 'on' (lowercase)",
            similar_values=["on", "off"],
        )

        assert error.field == "new_state.state"
        assert error.value == "On"
        assert error.error == "Invalid state for binary sensor"
        assert error.suggestion == "Use 'on' (lowercase)"
        assert error.similar_values == ["on", "off"]


class TestValidationResult:
    """Test ValidationResult dataclass."""

    def test_creation_valid(self) -> None:
        """Test creating valid ValidationResult."""
        result = ValidationResult(valid=True)

        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []

    def test_creation_with_errors(self) -> None:
        """Test creating ValidationResult with errors."""
        errors = [
            ValidationError(
                field="entity_id",
                value="person.teija",
                error="Entity does not exist",
                suggestion="Did you mean 'person.tiia'?",
                similar_values=["person.tiia", "person.andrew"],
            ),
            ValidationError(
                field="new_state.state",
                value="Chatswood",
                error="Invalid state for person entity",
                suggestion="Use 'chatswood' (lowercase)",
            ),
        ]

        result = ValidationResult(valid=False, errors=errors)

        assert result.valid is False
        assert len(result.errors) == 2
        assert result.errors[0].field == "entity_id"
        assert result.errors[1].field == "new_state.state"
        assert result.warnings == []

    def test_creation_with_warnings(self) -> None:
        """Test creating ValidationResult with warnings."""
        warnings = [
            "Cannot validate state without entity_id",
            "Entity type 'sensor' has dynamic states",
        ]

        result = ValidationResult(valid=True, warnings=warnings)

        assert result.valid is True
        assert result.errors == []
        assert len(result.warnings) == 2
        assert result.warnings[0] == "Cannot validate state without entity_id"

    def test_to_dict_valid(self) -> None:
        """Test converting valid result to dict."""
        result = ValidationResult(valid=True)
        data = result.to_dict()

        assert data == {
            "valid": True,
            "errors": [],
            "warnings": [],
        }

    def test_to_dict_with_errors(self) -> None:
        """Test converting result with errors to dict."""
        errors = [
            ValidationError(
                field="entity_id",
                value="invalid.entity",
                error="Entity does not exist",
                suggestion="Check entity format",
                similar_values=["valid.entity1", "valid.entity2"],
            )
        ]
        warnings = ["Some warning"]

        result = ValidationResult(valid=False, errors=errors, warnings=warnings)
        data = result.to_dict()

        assert data == {
            "valid": False,
            "errors": [
                {
                    "field": "entity_id",
                    "value": "invalid.entity",
                    "error": "Entity does not exist",
                    "suggestion": "Check entity format",
                    "similar_values": ["valid.entity1", "valid.entity2"],
                }
            ],
            "warnings": ["Some warning"],
        }

    def test_to_dict_none_values(self) -> None:
        """Test converting result with None values in errors."""
        errors = [
            ValidationError(
                field="test_field",
                value=None,
                error="Value is None",
            )
        ]

        result = ValidationResult(valid=False, errors=errors)
        data = result.to_dict()

        assert data["errors"][0]["value"] is None
        assert data["errors"][0]["suggestion"] is None
        assert data["errors"][0]["similar_values"] is None


class TestEventSourceValidation:
    """Test EventSource protocol validation method."""

    @pytest.mark.asyncio
    async def test_default_validation_implementation(self) -> None:
        """Test that BaseEventSource has default validate_match_conditions implementation."""

        # Create a minimal implementation of EventSource using BaseEventSource
        class TestSource(BaseEventSource, EventSource):
            async def start(self, processor: "EventProcessor") -> None:
                pass

            async def stop(self) -> None:
                pass

            @property
            def source_id(self) -> str:
                return "test_source"

        source = TestSource()

        # The default implementation should return valid=True
        result = await source.validate_match_conditions({"entity_id": "test.entity"})

        assert isinstance(result, ValidationResult)
        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []

    @pytest.mark.asyncio
    async def test_base_event_source_validation(self) -> None:
        """Test that BaseEventSource provides validate_match_conditions."""

        base_source = BaseEventSource()
        result = await base_source.validate_match_conditions({"test": "value"})

        assert isinstance(result, ValidationResult)
        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []

    @pytest.mark.asyncio
    async def test_protocol_instance_check(self) -> None:
        """Test that EventSource protocol works with isinstance."""

        class ValidSource(BaseEventSource, EventSource):
            async def start(self, processor: "EventProcessor") -> None:
                pass

            async def stop(self) -> None:
                pass

            @property
            def source_id(self) -> str:
                return "valid_source"

        class InvalidSource:
            pass

        valid_source = ValidSource()
        invalid_source = InvalidSource()

        assert isinstance(valid_source, EventSource)
        assert not isinstance(invalid_source, EventSource)
