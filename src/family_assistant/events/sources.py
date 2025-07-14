"""
Base event source protocol and implementations.
"""

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

from family_assistant.events.validation import ValidationResult

logger = logging.getLogger(__name__)


@runtime_checkable
class EventSource(Protocol):
    """Base protocol for event sources."""

    @abstractmethod
    async def start(self, processor: "EventProcessor") -> None:
        """Start listening for events and register the processor."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop listening for events."""
        ...

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for this source."""
        ...


class BaseEventSource:
    """Base class for event sources with default implementations."""

    async def validate_match_conditions(
        self, match_conditions: dict[str, Any]
    ) -> ValidationResult:
        """
        Validate match conditions for this source type.

        Default implementation returns valid=True for backward compatibility.

        Args:
            match_conditions: The match conditions to validate

        Returns:
            ValidationResult with validation status and any errors/warnings
        """
        return ValidationResult(valid=True)
