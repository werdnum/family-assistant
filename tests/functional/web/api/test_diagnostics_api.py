"""Functional tests for the diagnostics export API."""

from datetime import UTC, datetime

import httpx
import pytest

from family_assistant.llm.request_buffer import (
    LLMRequestRecord,
    get_request_buffer,
    reset_request_buffer,
)


@pytest.fixture(autouse=True)
def reset_llm_buffer() -> None:
    """Reset the global LLM request buffer before each test."""
    reset_request_buffer()


@pytest.mark.asyncio
async def test_diagnostics_export_returns_json(api_client: httpx.AsyncClient) -> None:
    """Test that the diagnostics export endpoint returns valid JSON structure."""
    response = await api_client.get("/api/diagnostics/export")

    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]

    data = response.json()

    # Verify top-level structure
    assert "export_timestamp" in data
    assert "time_window_minutes" in data
    assert "system_info" in data
    assert "error_logs" in data
    assert "llm_requests" in data
    assert "message_history" in data
    assert "summary" in data

    # Verify system_info structure
    assert "python_version" in data["system_info"]
    assert "platform" in data["system_info"]
    assert "database_type" in data["system_info"]

    # Verify summary structure
    assert "error_count" in data["summary"]
    assert "llm_request_count" in data["summary"]
    assert "message_count" in data["summary"]


@pytest.mark.asyncio
async def test_diagnostics_export_includes_llm_requests(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that LLM requests from the buffer are included in the export."""
    # Add a test record to the buffer
    buffer = get_request_buffer()
    test_record = LLMRequestRecord(
        timestamp=datetime.now(UTC),
        request_id="test123",
        model_id="test-model",
        messages=[{"role": "user", "content": "Hello"}],
        tools=None,
        tool_choice=None,
        response={"content": "Hi there"},
        duration_ms=100.0,
        error=None,
    )
    buffer.add(test_record)

    response = await api_client.get("/api/diagnostics/export")

    assert response.status_code == 200
    data = response.json()

    # Should have our test record
    assert data["summary"]["llm_request_count"] >= 1

    llm_requests = data["llm_requests"]
    assert len(llm_requests) >= 1

    # Find our test record
    test_request = next((r for r in llm_requests if r["request_id"] == "test123"), None)
    assert test_request is not None
    assert test_request["model_id"] == "test-model"
    assert test_request["duration_ms"] == 100.0


@pytest.mark.asyncio
async def test_diagnostics_export_respects_time_filter(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that the minutes parameter filters results correctly."""
    # Request with 5 minute window
    response = await api_client.get("/api/diagnostics/export?minutes=5")

    assert response.status_code == 200
    data = response.json()
    assert data["time_window_minutes"] == 5


@pytest.mark.asyncio
async def test_diagnostics_export_markdown_format(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that markdown format returns plain text."""
    response = await api_client.get("/api/diagnostics/export?format=markdown")

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]

    content = response.text
    assert "# Diagnostic Export" in content
    assert "## System Info" in content
    assert "## Error Logs" in content
    assert "## LLM Requests" in content
    assert "## Message History" in content


@pytest.mark.asyncio
async def test_diagnostics_export_limits_results(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that max_* parameters limit the number of results."""
    # Add multiple records to the buffer
    buffer = get_request_buffer()
    for i in range(10):
        buffer.add(
            LLMRequestRecord(
                timestamp=datetime.now(UTC),
                request_id=f"req{i}",
                model_id="test-model",
                messages=[{"role": "user", "content": f"Message {i}"}],
                tools=None,
                tool_choice=None,
                response={"content": f"Response {i}"},
                duration_ms=float(i * 10),
                error=None,
            )
        )

    response = await api_client.get("/api/diagnostics/export?max_llm_requests=3")

    assert response.status_code == 200
    data = response.json()

    # Should be limited to 3 requests
    assert len(data["llm_requests"]) <= 3


@pytest.mark.asyncio
async def test_diagnostics_export_with_error_record(
    api_client: httpx.AsyncClient,
) -> None:
    """Test that error records are properly exported."""
    buffer = get_request_buffer()
    buffer.add(
        LLMRequestRecord(
            timestamp=datetime.now(UTC),
            request_id="error123",
            model_id="test-model",
            messages=[{"role": "user", "content": "Test"}],
            tools=None,
            tool_choice=None,
            response=None,
            duration_ms=50.0,
            error="Connection timeout",
        )
    )

    response = await api_client.get("/api/diagnostics/export")

    assert response.status_code == 200
    data = response.json()

    error_request = next(
        (r for r in data["llm_requests"] if r["request_id"] == "error123"), None
    )
    assert error_request is not None
    assert error_request["error"] == "Connection timeout"
    assert error_request["response"] is None
