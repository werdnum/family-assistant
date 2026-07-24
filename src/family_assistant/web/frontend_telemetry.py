"""In-memory ring buffer for non-error frontend telemetry (breadcrumbs).

Frontend clients (notably the iOS app) emit a stream of diagnostic breadcrumbs — stream
restarts/disconnects, resync phases, per-operation transport events — so intermittent connection
problems are diagnosable in production. These are telemetry, not errors, so they must not land in
the ``error_logs`` table the engineer profile reads. Reports that arrive at ``POST /api/errors/``
with a non-error ``severity`` are recorded here instead.

Modelled on ``family_assistant.llm.request_buffer``: a thread-safe, bounded ``deque`` global
singleton with automatic oldest-entry eviction. Deliberately in-memory and non-persistent — a
process restart drops it, which is acceptable for live-debugging telemetry.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any


@dataclass
class FrontendTelemetryRecord:
    """A single non-error frontend telemetry report (breadcrumb)."""

    timestamp: datetime
    severity: str
    message: str
    component_name: str | None = None
    error_type: str | None = None
    url: str | None = None
    user_agent: str | None = None
    # ast-grep-ignore: no-dict-any - Freeform extra_data attached by the client
    extra_data: dict[str, Any] | None = None

    # ast-grep-ignore: no-dict-any - JSON serialization output
    def to_dict(self) -> dict[str, Any]:
        """Convert record to a JSON-serializable dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "severity": self.severity,
            "message": self.message,
            "component_name": self.component_name,
            "error_type": self.error_type,
            "url": self.url,
            "user_agent": self.user_agent,
            "extra_data": self.extra_data,
        }


@dataclass
class FrontendTelemetryBuffer:
    """Thread-safe ring buffer for recent non-error frontend telemetry.

    Uses ``collections.deque`` with ``maxlen`` for automatic oldest-entry eviction.
    """

    max_size: int = 500
    _buffer: deque[FrontendTelemetryRecord] = field(default_factory=deque)
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        """Initialize the buffer with the correct max size."""
        self._buffer = deque(maxlen=self.max_size)

    def add(self, record: FrontendTelemetryRecord) -> None:
        """Add a telemetry record. Thread-safe; evicts the oldest entry when full."""
        with self._lock:
            self._buffer.append(record)

    def get_recent(
        self,
        limit: int = 100,
        since_minutes: int | None = None,
        component: str | None = None,
    ) -> list[FrontendTelemetryRecord]:
        """Get recent telemetry records, newest first.

        Args:
            limit: Maximum number of records to return.
            since_minutes: Optional filter to only include records from the last N minutes.
            component: Optional exact-match filter on ``component_name``.

        Returns:
            List of telemetry records, newest first.
        """
        with self._lock:
            records = list(self._buffer)

        if since_minutes is not None:
            cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
            records = [r for r in records if r.timestamp >= cutoff]

        if component is not None:
            records = [r for r in records if r.component_name == component]

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


_global_buffer: FrontendTelemetryBuffer | None = None
_buffer_lock = Lock()


def get_frontend_telemetry_buffer(max_size: int = 500) -> FrontendTelemetryBuffer:
    """Get the global frontend telemetry buffer, creating it on first access.

    Args:
        max_size: Maximum number of records to store (only used on first call).

    Returns:
        The global FrontendTelemetryBuffer instance.
    """
    global _global_buffer
    with _buffer_lock:
        if _global_buffer is None:
            _global_buffer = FrontendTelemetryBuffer(max_size=max_size)
        return _global_buffer


def reset_frontend_telemetry_buffer() -> None:
    """Reset the global buffer (primarily for testing)."""
    global _global_buffer
    with _buffer_lock:
        _global_buffer = None
