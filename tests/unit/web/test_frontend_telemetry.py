"""Unit tests for the in-memory frontend telemetry ring buffer."""

from __future__ import annotations

from datetime import UTC, datetime

from family_assistant.web.frontend_telemetry import (
    MAX_EXTRA_DATA_CHARS,
    MAX_MESSAGE_CHARS,
    FrontendTelemetryBuffer,
    FrontendTelemetryRecord,
    get_frontend_telemetry_buffer,
    reset_frontend_telemetry_buffer,
)


def _record(
    message: str = "breadcrumb",
    component: str | None = "Chat.resync",
    extra_data: dict | None = None,
) -> FrontendTelemetryRecord:
    return FrontendTelemetryRecord(
        timestamp=datetime.now(tz=UTC),
        severity="info",
        message=message,
        component_name=component,
        error_type="component_error",
        extra_data=extra_data,
    )


def test_get_recent_returns_newest_first() -> None:
    buffer = FrontendTelemetryBuffer(max_size=10)
    buffer.add(_record(message="first"))
    buffer.add(_record(message="second"))

    records = buffer.get_recent()

    assert [r.message for r in records] == ["second", "first"]


def test_maxlen_evicts_oldest() -> None:
    buffer = FrontendTelemetryBuffer(max_size=2)
    buffer.add(_record(message="a"))
    buffer.add(_record(message="b"))
    buffer.add(_record(message="c"))

    messages = [r.message for r in buffer.get_recent()]

    assert messages == ["c", "b"]
    assert len(buffer) == 2


def test_component_filter() -> None:
    buffer = FrontendTelemetryBuffer(max_size=10)
    buffer.add(_record(component="Chat.streamRestart"))
    buffer.add(_record(component="Chat.resync"))

    records = buffer.get_recent(component="Chat.resync")

    assert {r.component_name for r in records} == {"Chat.resync"}


def test_oversized_message_is_truncated_on_insert() -> None:
    buffer = FrontendTelemetryBuffer(max_size=10)
    buffer.add(_record(message="x" * (MAX_MESSAGE_CHARS + 5_000)))

    stored = buffer.get_recent()[0]

    assert len(stored.message) <= MAX_MESSAGE_CHARS + len("…[truncated]")
    assert stored.message.endswith("…[truncated]")


def test_oversized_extra_data_is_dropped_for_a_size_marker() -> None:
    buffer = FrontendTelemetryBuffer(max_size=10)
    huge = {"blob": "y" * (MAX_EXTRA_DATA_CHARS + 5_000)}
    buffer.add(_record(extra_data=huge))

    stored = buffer.get_recent()[0]

    assert stored.extra_data is not None
    assert stored.extra_data["_truncated"] is True
    assert stored.extra_data["_approx_size"] > MAX_EXTRA_DATA_CHARS
    assert "blob" not in stored.extra_data


def test_small_extra_data_is_preserved() -> None:
    buffer = FrontendTelemetryBuffer(max_size=10)
    buffer.add(_record(extra_data={"reason": "grace_expired"}))

    stored = buffer.get_recent()[0]

    assert stored.extra_data == {"reason": "grace_expired"}


def test_global_singleton_is_reset() -> None:
    reset_frontend_telemetry_buffer()
    first = get_frontend_telemetry_buffer()
    first.add(_record())
    assert len(first) == 1

    reset_frontend_telemetry_buffer()
    assert len(get_frontend_telemetry_buffer()) == 0
