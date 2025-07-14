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
