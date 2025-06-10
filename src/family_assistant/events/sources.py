"""
Base event source protocol and implementations.
"""

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from family_assistant.events.processor import EventProcessor

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
