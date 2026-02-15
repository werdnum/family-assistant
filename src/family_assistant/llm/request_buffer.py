"""Ring buffer for capturing recent LLM requests for diagnostic purposes.

This module provides a thread-safe ring buffer that captures LLM request/response
data for debugging and diagnostic export. All LLM client instances write to a
global singleton buffer.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from family_assistant.tools.types import ToolDefinition


@dataclass
class LLMRequestRecord:
    """Record of a single LLM request/response pair."""

    timestamp: datetime
    request_id: str
    model_id: str
    # ast-grep-ignore: no-dict-any - Serialized LLM messages from external APIs
    messages: list[dict[str, Any]]
    tools: list[ToolDefinition] | None = None
    tool_choice: str | None = None
    # ast-grep-ignore: no-dict-any - Serialized LLM response from external APIs
    response: dict[str, Any] | None = None
    duration_ms: float = 0.0
    error: str | None = None

    # ast-grep-ignore: no-dict-any - JSON serialization output
    def to_dict(self) -> dict[str, Any]:
        """Convert record to a JSON-serializable dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "request_id": self.request_id,
            "model_id": self.model_id,
            "messages": self.messages,
            "tools": self.tools,
            "tool_choice": self.tool_choice,
            "response": self.response,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class LLMRequestBuffer:
    """Thread-safe ring buffer for storing recent LLM requests.

    Uses collections.deque with maxlen for automatic oldest-entry eviction.
    """

    max_size: int = 100
    _buffer: deque[LLMRequestRecord] = field(default_factory=deque)
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        """Initialize the buffer with the correct max size."""
        self._buffer = deque(maxlen=self.max_size)

    def add(self, record: LLMRequestRecord) -> None:
        """Add a request record to the buffer.

        Thread-safe. Automatically evicts oldest entries when full.
        """
        with self._lock:
            self._buffer.append(record)

    def get_recent(
        self,
        limit: int = 50,
        since_minutes: int | None = None,
    ) -> list[LLMRequestRecord]:
        """Get recent request records.

        Args:
            limit: Maximum number of records to return.
            since_minutes: Optional filter to only include records from the last N minutes.

        Returns:
            List of request records, newest first.
        """
        with self._lock:
            records = list(self._buffer)

        if since_minutes is not None:
            cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
            records = [r for r in records if r.timestamp >= cutoff]

        records.reverse()
        return records[:limit]

    def clear(self) -> None:
        """Clear all records from the buffer."""
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        """Return the current number of records in the buffer."""
        with self._lock:
            return len(self._buffer)


_global_buffer: LLMRequestBuffer | None = None
_buffer_lock = Lock()


def get_request_buffer(max_size: int = 100) -> LLMRequestBuffer:
    """Get the global LLM request buffer.

    Creates the buffer on first access. Thread-safe.

    Args:
        max_size: Maximum number of records to store (only used on first call).

    Returns:
        The global LLMRequestBuffer instance.
    """
    global _global_buffer
    with _buffer_lock:
        if _global_buffer is None:
            _global_buffer = LLMRequestBuffer(max_size=max_size)
        return _global_buffer


def reset_request_buffer() -> None:
    """Reset the global buffer (primarily for testing)."""
    global _global_buffer
    with _buffer_lock:
        _global_buffer = None
