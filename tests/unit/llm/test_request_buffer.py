"""Unit tests for the LLM request buffer."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from family_assistant.llm.request_buffer import (
    LLMRequestBuffer,
    LLMRequestRecord,
    get_request_buffer,
    reset_request_buffer,
)
from family_assistant.tools.types import ToolDefinition


@pytest.fixture(autouse=True)
def reset_buffer() -> None:
    """Reset the global buffer before each test."""
    reset_request_buffer()


def create_test_record(
    request_id: str = "test123",
    model_id: str = "test-model",
    timestamp: datetime | None = None,
) -> LLMRequestRecord:
    """Create a test record with default values."""
    return LLMRequestRecord(
        timestamp=timestamp or datetime.now(UTC),
        request_id=request_id,
        model_id=model_id,
        messages=[{"role": "user", "content": "test"}],
        tools=None,
        tool_choice=None,
        response={"content": "response"},
        duration_ms=100.0,
        error=None,
    )


class TestLLMRequestRecord:
    """Tests for LLMRequestRecord dataclass."""

    def test_to_dict_serialization(self) -> None:
        """Test that to_dict produces correct JSON-serializable output."""
        timestamp = datetime(2025, 1, 15, 10, 30, 0, tzinfo=UTC)
        record = LLMRequestRecord(
            timestamp=timestamp,
            request_id="abc123",
            model_id="gpt-4",
            messages=[{"role": "user", "content": "Hello"}],
            tools=[
                ToolDefinition(
                    type="function",
                    function={
                        "name": "test_tool",
                        "description": "A test tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                )
            ],
            tool_choice="auto",
            response={"content": "Hi there"},
            duration_ms=150.5,
            error=None,
        )

        result = record.to_dict()

        assert result["timestamp"] == "2025-01-15T10:30:00+00:00"
        assert result["request_id"] == "abc123"
        assert result["model_id"] == "gpt-4"
        assert result["messages"] == [{"role": "user", "content": "Hello"}]
        assert result["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        assert result["tool_choice"] == "auto"
        assert result["response"] == {"content": "Hi there"}
        assert result["duration_ms"] == 150.5
        assert result["error"] is None

    def test_to_dict_with_error(self) -> None:
        """Test to_dict with an error field."""
        record = create_test_record()
        record = LLMRequestRecord(
            timestamp=record.timestamp,
            request_id=record.request_id,
            model_id=record.model_id,
            messages=record.messages,
            tools=None,
            tool_choice=None,
            response=None,
            duration_ms=50.0,
            error="Connection timeout",
        )

        result = record.to_dict()

        assert result["error"] == "Connection timeout"
        assert result["response"] is None


class TestLLMRequestBuffer:
    """Tests for LLMRequestBuffer class."""

    def test_add_and_get_recent(self) -> None:
        """Test adding records and retrieving them."""
        buffer = LLMRequestBuffer(max_size=10)

        record1 = create_test_record(request_id="r1")
        record2 = create_test_record(request_id="r2")

        buffer.add(record1)
        buffer.add(record2)

        recent = buffer.get_recent(limit=10)

        assert len(recent) == 2
        # Newest first
        assert recent[0].request_id == "r2"
        assert recent[1].request_id == "r1"

    def test_buffer_overflow_evicts_oldest(self) -> None:
        """Test that oldest records are evicted when buffer is full."""
        buffer = LLMRequestBuffer(max_size=3)

        for i in range(5):
            buffer.add(create_test_record(request_id=f"r{i}"))

        recent = buffer.get_recent(limit=10)

        # Only last 3 should remain
        assert len(recent) == 3
        assert recent[0].request_id == "r4"
        assert recent[1].request_id == "r3"
        assert recent[2].request_id == "r2"

    def test_get_recent_with_limit(self) -> None:
        """Test that limit parameter works correctly."""
        buffer = LLMRequestBuffer(max_size=10)

        for i in range(5):
            buffer.add(create_test_record(request_id=f"r{i}"))

        recent = buffer.get_recent(limit=2)

        assert len(recent) == 2
        assert recent[0].request_id == "r4"
        assert recent[1].request_id == "r3"

    def test_get_recent_with_time_filter(self) -> None:
        """Test filtering by time window."""
        buffer = LLMRequestBuffer(max_size=10)

        # Old record (2 hours ago)
        old_time = datetime.now(UTC) - timedelta(hours=2)
        buffer.add(create_test_record(request_id="old", timestamp=old_time))

        # Recent record (5 minutes ago)
        recent_time = datetime.now(UTC) - timedelta(minutes=5)
        buffer.add(create_test_record(request_id="recent", timestamp=recent_time))

        # Filter to last 30 minutes
        recent = buffer.get_recent(limit=10, since_minutes=30)

        assert len(recent) == 1
        assert recent[0].request_id == "recent"

    def test_clear(self) -> None:
        """Test clearing the buffer."""
        buffer = LLMRequestBuffer(max_size=10)

        buffer.add(create_test_record())
        buffer.add(create_test_record())

        assert len(buffer) == 2

        buffer.clear()

        assert len(buffer) == 0
        assert buffer.get_recent() == []

    def test_len(self) -> None:
        """Test __len__ method."""
        buffer = LLMRequestBuffer(max_size=10)

        assert len(buffer) == 0

        buffer.add(create_test_record())
        assert len(buffer) == 1

        buffer.add(create_test_record())
        assert len(buffer) == 2

    def test_thread_safety(self) -> None:
        """Test that buffer is thread-safe for concurrent writes."""
        buffer = LLMRequestBuffer(max_size=100)
        num_threads = 10
        records_per_thread = 10

        def add_records(thread_id: int) -> None:
            for i in range(records_per_thread):
                buffer.add(create_test_record(request_id=f"t{thread_id}_r{i}"))

        threads = [
            threading.Thread(target=add_records, args=(i,)) for i in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All records should be added (no data corruption)
        assert len(buffer) == num_threads * records_per_thread


class TestGlobalBuffer:
    """Tests for global buffer singleton."""

    def test_get_request_buffer_returns_singleton(self) -> None:
        """Test that get_request_buffer returns the same instance."""
        buffer1 = get_request_buffer()
        buffer2 = get_request_buffer()

        assert buffer1 is buffer2

    def test_reset_request_buffer(self) -> None:
        """Test that reset_request_buffer clears the singleton."""
        buffer1 = get_request_buffer()
        buffer1.add(create_test_record())

        reset_request_buffer()

        buffer2 = get_request_buffer()

        # Should be a new instance
        assert buffer1 is not buffer2
        assert len(buffer2) == 0

    def test_get_request_buffer_with_custom_size(self) -> None:
        """Test that max_size is respected on first call only."""
        # First call sets the size
        buffer1 = get_request_buffer(max_size=5)

        # Add 10 records
        for i in range(10):
            buffer1.add(create_test_record(request_id=f"r{i}"))

        # Should only keep 5
        assert len(buffer1) == 5

        # Second call with different size should be ignored
        buffer2 = get_request_buffer(max_size=100)
        assert buffer1 is buffer2
        assert len(buffer2) == 5
