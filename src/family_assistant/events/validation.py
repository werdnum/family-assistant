"""
Event listener validation data structures and utilities.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationError:
    """Represents a validation error for a specific field."""

    field: str
    value: Any
    error: str
    suggestion: str | None = None
    similar_values: list[str] | None = None


@dataclass
class ValidationResult:
    """Result of validating match conditions."""

    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def to_dict(self) -> dict[str, Any]:
        """Convert validation result to dictionary for JSON serialization."""
        return {
            "valid": self.valid,
            "errors": [
                {
                    "field": e.field,
                    "value": e.value,
                    "error": e.error,
                    "suggestion": e.suggestion,
                    "similar_values": e.similar_values,
                }
                for e in self.errors
            ],
            "warnings": self.warnings,
        }


def format_validation_errors(validation: ValidationResult) -> list[str]:
    """Format validation errors as human-readable messages."""
    messages = []

    if not validation.valid:
        messages.append("VALIDATION ISSUES FOUND:")
        for error in validation.errors:
            messages.append(f"- {error.field}: {error.error}")
            if error.suggestion:
                messages.append(f"  Suggestion: {error.suggestion}")
            if error.similar_values and len(error.similar_values) <= 5:
                messages.append(f"  Valid values: {error.similar_values}")

    if validation.warnings:
        messages.append("WARNINGS:")
        for warning in validation.warnings:
            messages.append(f"- {warning}")

    return messages


def format_validation_error_summary(validation: ValidationResult) -> str:
    """Format a concise summary of validation errors."""
    if validation.valid:
        return "Validation passed"

    error_count = len(validation.errors)
    warning_count = len(validation.warnings)

    parts = []
    if error_count > 0:
        parts.append(f"{error_count} error{'s' if error_count > 1 else ''}")
    if warning_count > 0:
        parts.append(f"{warning_count} warning{'s' if warning_count > 1 else ''}")

    return f"Validation failed: {', '.join(parts)}"
