"""Tests for the frontend error reporting API endpoint."""

import logging
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.utils.logging_handler import SQLAlchemyErrorHandler
from family_assistant.web.frontend_telemetry import (
    get_frontend_telemetry_buffer,
    reset_frontend_telemetry_buffer,
)
from family_assistant.web.routers.errors_api import (
    ERROR_INTAKE_RATE_LIMIT,
    RATE_LIMIT_MAX_ADDRESSES,
    check_error_intake_rate_limit,
    expire_rate_limit_addresses,
    rate_limit_tracked_addresses,
    reset_error_intake_rate_limiter,
)

# The frontend.javascript logger that the API endpoint uses
FRONTEND_LOGGER_NAME = "frontend.javascript"


@pytest.fixture(autouse=True)
def _reset_telemetry_buffer() -> None:
    """The telemetry ring buffer is a process-global singleton; reset it around
    each test so buffered breadcrumbs do not leak between tests."""
    reset_frontend_telemetry_buffer()


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
async def test_info_severity_routes_to_telemetry_not_error_log(
    web_only_assistant: Assistant,
    frontend_error_handler: SQLAlchemyErrorHandler,
) -> None:
    """A breadcrumb (severity=info) is recorded in the telemetry buffer and does
    NOT reach the error log, so it cannot drown genuine errors."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        breadcrumb_message = "iOS chat sync breadcrumb telemetry-only 24680"
        response = await client.post(
            "/api/errors/",
            json={
                "message": breadcrumb_message,
                "url": "familyassistant://ios/Chat.streamDisconnect",
                "component_name": "Chat.streamDisconnect",
                "error_type": "component_error",
                "severity": "info",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "recorded"

        # It appears in the telemetry lane...
        telemetry = await client.get("/api/errors/telemetry")
        assert telemetry.status_code == 200
        telemetry_messages = [r["message"] for r in telemetry.json()["records"]]
        assert breadcrumb_message in telemetry_messages

        # ...but NOT in the error log.
        await frontend_error_handler.wait_for_pending_logs()
        errors = await client.get(
            "/api/errors/", params={"logger": "frontend.javascript", "days": 1}
        )
        assert errors.status_code == 200
        error_messages = [e["message"] for e in errors.json()["errors"]]
        assert breadcrumb_message not in error_messages


@pytest.mark.asyncio
async def test_explicit_error_severity_still_logs_to_error_log(
    web_only_assistant: Assistant,
    frontend_error_handler: SQLAlchemyErrorHandler,
) -> None:
    """An explicit severity=error report still lands in the error log (parity with
    the default, absent-severity behaviour)."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        message = "Explicit error severity still logged 13579"
        response = await client.post(
            "/api/errors/",
            json={
                "message": message,
                "url": "http://localhost:3000/",
                "error_type": "manual",
                "severity": "error",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "reported"

        await frontend_error_handler.wait_for_pending_logs()
        errors = await client.get(
            "/api/errors/", params={"logger": "frontend.javascript", "days": 1}
        )
        error_messages = [e["message"] for e in errors.json()["errors"]]
        assert message in error_messages

        # And it is not duplicated into the telemetry lane.
        telemetry = await client.get("/api/errors/telemetry")
        telemetry_messages = [r["message"] for r in telemetry.json()["records"]]
        assert message not in telemetry_messages


@pytest.mark.asyncio
async def test_get_frontend_telemetry_filters_by_component(
    web_only_assistant: Assistant,
) -> None:
    """The telemetry read endpoint honours the component filter."""
    assert web_only_assistant.fastapi_app is not None
    transport = httpx.ASGITransport(app=web_only_assistant.fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for component in ("Chat.streamRestart", "Chat.resync"):
            await client.post(
                "/api/errors/",
                json={
                    "message": f"breadcrumb for {component}",
                    "url": f"familyassistant://ios/{component}",
                    "component_name": component,
                    "error_type": "component_error",
                    "severity": "info",
                },
            )

        response = await client.get(
            "/api/errors/telemetry", params={"component": "Chat.resync"}
        )
        assert response.status_code == 200
        records = response.json()["records"]
        assert records
        assert {r["component_name"] for r in records} == {"Chat.resync"}


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


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    reset_error_intake_rate_limiter()


class _FakeAuthService:
    """Auth-enabled stand-in; configurable token acceptance."""

    auth_enabled = True
    oauth = None

    def __init__(self, accept_tokens: bool) -> None:
        self._accept_tokens = accept_tokens

    async def get_user_from_api_token(
        self, auth_header: str, request: object
    ) -> dict | None:
        if self._accept_tokens:
            return {
                "sub": "token-user",
                "name": "token-user",
                "email": "token-user",
                "source": "api_token",
                "token_id": 1,
            }
        return None


def _client_for(assistant: Assistant) -> httpx.AsyncClient:
    assert assistant.fastapi_app is not None
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=assistant.fastapi_app),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_report_rejects_oversized_payload(
    web_only_assistant: Assistant,
) -> None:
    async with _client_for(web_only_assistant) as client:
        response = await client.post(
            "/api/errors/",
            json={"message": "x" * 9000, "url": "http://localhost/chat"},
        )
        assert response.status_code in {413, 422}

        # A body beyond the hard streaming cap is rejected before parsing.
        response = await client.post(
            "/api/errors/",
            content=b"x" * (200 * 1024),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_unauthenticated_reports_never_persist(
    web_only_assistant: Assistant,
) -> None:
    """With auth enabled and no credential, claimed errors land in the ring."""
    assert web_only_assistant.fastapi_app is not None
    original = getattr(web_only_assistant.fastapi_app.state, "auth_service", None)
    web_only_assistant.fastapi_app.state.auth_service = _FakeAuthService(False)
    try:
        async with _client_for(web_only_assistant) as client:
            response = await client.post(
                "/api/errors/",
                json={
                    "message": "claimed error",
                    "url": "http://x/",
                    "severity": "error",
                },
            )
    finally:
        web_only_assistant.fastapi_app.state.auth_service = original

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    records = get_frontend_telemetry_buffer().get_recent()
    assert len(records) == 1
    assert records[0].to_dict()["severity"] == "info"


@pytest.mark.asyncio
async def test_authenticated_error_report_keeps_error_lane(
    web_only_assistant: Assistant,
) -> None:
    assert web_only_assistant.fastapi_app is not None
    original = getattr(web_only_assistant.fastapi_app.state, "auth_service", None)
    web_only_assistant.fastapi_app.state.auth_service = _FakeAuthService(True)
    try:
        async with _client_for(web_only_assistant) as client:
            response = await client.post(
                "/api/errors/",
                json={"message": "real error", "url": "http://x/"},
                headers={"Authorization": "Bearer some-opaque-token"},
            )
    finally:
        web_only_assistant.fastapi_app.state.auth_service = original

    assert response.status_code == 200
    assert response.json()["status"] == "reported"


@pytest.mark.asyncio
async def test_intake_rate_limit_returns_429(
    web_only_assistant: Assistant,
) -> None:
    async with _client_for(web_only_assistant) as client:
        statuses = []
        for _ in range(ERROR_INTAKE_RATE_LIMIT + 1):
            response = await client.post(
                "/api/errors/",
                json={"message": "spam", "url": "http://x/"},
            )
            statuses.append(response.status_code)
    assert all(status == 200 for status in statuses[:-1])
    assert statuses[-1] == 429


@pytest.mark.asyncio
async def test_rate_limiter_map_stays_bounded(
    web_only_assistant: Assistant,
) -> None:
    """Rotating client addresses cannot grow the limiter map without bound."""

    for i in range(RATE_LIMIT_MAX_ADDRESSES + 500):
        check_error_intake_rate_limit(f"10.0.0.{i % 256}.{i}")
        if rate_limit_tracked_addresses() > RATE_LIMIT_MAX_ADDRESSES:
            break
        if i % 512 == 0:
            expire_rate_limit_addresses(200)
    assert rate_limit_tracked_addresses() <= RATE_LIMIT_MAX_ADDRESSES
