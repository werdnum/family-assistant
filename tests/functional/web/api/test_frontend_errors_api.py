"""Tests for the frontend error reporting API endpoint."""

import logging
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.utils.logging_handler import SQLAlchemyErrorHandler

# The frontend.javascript logger that the API endpoint uses
FRONTEND_LOGGER_NAME = "frontend.javascript"


@pytest_asyncio.fixture
async def frontend_error_handler(
    db_engine: AsyncEngine,
) -> AsyncGenerator[SQLAlchemyErrorHandler]:
    """Create and attach an error handler for frontend error logging tests.

    The global conftest disables database error logging for tests to avoid
    connection issues. This fixture explicitly sets up a handler for tests
    that need to verify errors are logged to the database.
    """
    # Get the frontend logger and configure it
    frontend_logger = logging.getLogger(FRONTEND_LOGGER_NAME)
    frontend_logger.setLevel(logging.ERROR)

    # Create and add our test handler
    handler = SQLAlchemyErrorHandler(db_engine, min_level=logging.ERROR)
    frontend_logger.addHandler(handler)

    yield handler

    # Cleanup
    await handler.wait_for_pending_logs()
    handler.close()
    frontend_logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_report_frontend_error_basic(web_only_assistant: Assistant) -> None:
    """Test that the frontend error reporting endpoint accepts valid error reports."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/errors/",
            json={
                "message": "Test error message",
                "url": "http://localhost:3000/chat",
                "stack": "Error: Test error\n    at test.js:1:1",
                "user_agent": "Mozilla/5.0 Test Browser",
                "component_name": "ChatApp",
                "error_type": "uncaught",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reported"


@pytest.mark.asyncio
async def test_report_frontend_error_minimal(web_only_assistant: Assistant) -> None:
    """Test that the endpoint works with only required fields."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/errors/",
            json={
                "message": "Minimal error",
                "url": "http://localhost:3000/",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reported"


@pytest.mark.asyncio
async def test_report_frontend_error_with_extra_data(
    web_only_assistant: Assistant,
) -> None:
    """Test that extra_data is properly included."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/errors/",
            json={
                "message": "Error with extra data",
                "url": "http://localhost:3000/notes",
                "error_type": "component_error",
                "extra_data": {
                    "component_stack": "    at MyComponent\n    at App",
                    "props": {"id": 123},
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reported"


@pytest.mark.asyncio
async def test_report_frontend_error_invalid_missing_message(
    web_only_assistant: Assistant,
) -> None:
    """Test that missing required field 'message' returns validation error."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/errors/",
            json={
                "url": "http://localhost:3000/",
            },
        )

        assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_report_frontend_error_invalid_missing_url(
    web_only_assistant: Assistant,
) -> None:
    """Test that missing required field 'url' returns validation error."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/errors/",
            json={
                "message": "Error without URL",
            },
        )

        assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_report_frontend_error_all_error_types(
    web_only_assistant: Assistant,
) -> None:
    """Test all supported error types."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        error_types = ["uncaught", "promise_rejection", "component_error", "manual"]

        for error_type in error_types:
            response = await client.post(
                "/api/errors/",
                json={
                    "message": f"Error of type {error_type}",
                    "url": "http://localhost:3000/",
                    "error_type": error_type,
                },
            )

            assert response.status_code == 200, f"Failed for error_type={error_type}"
            data = response.json()
            assert data["status"] == "reported"


@pytest.mark.asyncio
async def test_reported_frontend_error_appears_in_list(
    web_only_assistant: Assistant,
    frontend_error_handler: SQLAlchemyErrorHandler,
) -> None:
    """Test that reported frontend errors appear in the error logs list."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Report a unique error
        unique_message = "Unique test error for list verification 12345"
        await client.post(
            "/api/errors/",
            json={
                "message": unique_message,
                "url": "http://localhost:3000/test",
                "error_type": "manual",
            },
        )

        # Wait for async logging to complete
        await frontend_error_handler.wait_for_pending_logs()

        # Verify the error appears in the list
        response = await client.get(
            "/api/errors/",
            params={"logger": "frontend.javascript", "days": 1},
        )
        assert response.status_code == 200
        data = response.json()
        error_messages = [error["message"] for error in data["errors"]]

        assert unique_message in error_messages, (
            f"Expected '{unique_message}' in error messages, got: {error_messages}"
        )


@pytest.mark.asyncio
async def test_frontend_error_extra_data_stored_correctly(
    web_only_assistant: Assistant,
    frontend_error_handler: SQLAlchemyErrorHandler,
) -> None:
    """Test that extra_data is properly stored and retrievable."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        unique_message = "Error with extra data verification 67890"
        test_extra_data = {"custom_field": "custom_value", "nested": {"key": "value"}}

        await client.post(
            "/api/errors/",
            json={
                "message": unique_message,
                "url": "http://localhost:3000/extra-data-test",
                "component_name": "TestComponent",
                "error_type": "component_error",
                "extra_data": test_extra_data,
            },
        )

        # Wait for async logging to complete
        await frontend_error_handler.wait_for_pending_logs()

        # Verify the error is stored with correct extra_data
        response = await client.get(
            "/api/errors/",
            params={"logger": "frontend.javascript", "days": 1},
        )
        assert response.status_code == 200
        data = response.json()

        matching_errors = [
            error for error in data["errors"] if error["message"] == unique_message
        ]

        assert len(matching_errors) >= 1, (
            f"Expected to find error with message '{unique_message}'"
        )

        error = matching_errors[0]
        assert error["extra_data"] is not None
        assert error["extra_data"]["url"] == "http://localhost:3000/extra-data-test"
        assert error["extra_data"]["component_name"] == "TestComponent"
        assert error["extra_data"]["error_type"] == "component_error"
        # Client-provided extra_data is nested under "details" to prevent key collision
        assert error["extra_data"]["details"]["custom_field"] == "custom_value"
        assert error["extra_data"]["details"]["nested"]["key"] == "value"
