"""
Unit tests for event validation data structures.
"""

import pytest

from family_assistant.events.sources import BaseEventSource
from family_assistant.events.validation import (
    ValidationError,
    ValidationResult,
    format_validation_errors,
)


class TestFormatValidationErrors:
    """Test the analysis lines the test_event_listener tool returns."""

    def test_valid_result_without_warnings_renders_nothing(self) -> None:
        assert format_validation_errors(ValidationResult(valid=True)) == []

    def test_error_without_suggestion_renders_only_the_error_line(self) -> None:
        result = ValidationResult(
            valid=False,
            errors=[
                ValidationError(
                    field="entity_id",
                    value="invalid.entity",
                    error="Entity does not exist",
                )
            ],
        )

        assert format_validation_errors(result) == [
            "VALIDATION ISSUES FOUND:",
            "- entity_id: Entity does not exist",
        ]

    def test_errors_render_with_their_suggestions_and_valid_values(self) -> None:
        errors = [
            ValidationError(
                field="entity_id",
                value="person.taylor",
                error="Entity does not exist",
                suggestion="Did you mean 'person.tiia'?",
                similar_values=["person.tiia", "person.alex"],
            ),
            ValidationError(
                field="new_state.state",
                value="Northtown",
                error="Invalid state for person entity",
                suggestion="Use 'northtown' (lowercase)",
            ),
        ]

        result = ValidationResult(valid=False, errors=errors)

        assert format_validation_errors(result) == [
            "VALIDATION ISSUES FOUND:",
            "- entity_id: Entity does not exist",
            "  Suggestion: Did you mean 'person.tiia'?",
            "  Valid values: ['person.tiia', 'person.alex']",
            "- new_state.state: Invalid state for person entity",
            "  Suggestion: Use 'northtown' (lowercase)",
        ]

    def test_warnings_on_valid_result_render_without_issues_header(self) -> None:
        warnings = [
            "Cannot validate state without entity_id",
            "Entity type 'sensor' has dynamic states",
        ]

        result = ValidationResult(valid=True, warnings=warnings)

        assert format_validation_errors(result) == [
            "WARNINGS:",
            "- Cannot validate state without entity_id",
            "- Entity type 'sensor' has dynamic states",
        ]


class TestValidationResult:
    """Test ValidationResult dataclass."""

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


class TestBaseEventSourceValidation:
    """Test the default validation inherited by sources that do not override it."""

    @pytest.mark.asyncio
    async def test_default_validation_accepts_any_match_conditions(self) -> None:
        result = await BaseEventSource().validate_match_conditions({
            "entity_id": "test.entity"
        })

        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []
