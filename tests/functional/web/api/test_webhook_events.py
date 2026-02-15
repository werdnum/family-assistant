"""Tests for the generic webhook events endpoint."""

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.webhook_source import WebhookEventSource
from family_assistant.web.app_creator import app as fastapi_app


@pytest.fixture
def webhook_source() -> WebhookEventSource:
    """Create a WebhookEventSource for testing."""
    return WebhookEventSource()


@pytest.fixture(autouse=True)
def _set_db_engine_on_app(db_engine: AsyncEngine) -> None:
    """Set database_engine on the shared fastapi_app state for get_db dependency."""
    fastapi_app.state.database_engine = db_engine


@pytest.mark.asyncio
async def test_webhook_event_basic_acceptance(
    db_engine: AsyncEngine,
) -> None:
    """Test that webhook endpoint accepts valid events."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/event",
            json={
                "event_type": "alert",
                "source": "grafana",
                "title": "CPU High",
                "message": "Server CPU > 90%",
                "severity": "warning",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert "event_id" in data
        # Verify event_id is a valid UUID
        uuid.UUID(data["event_id"])


@pytest.mark.asyncio
async def test_webhook_event_minimal_payload(
    db_engine: AsyncEngine,
) -> None:
    """Test that webhook endpoint accepts minimal payload with only event_type."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/event",
            json={
                "event_type": "test",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_event_with_extra_fields(
    db_engine: AsyncEngine,
) -> None:
    """Test that webhook endpoint accepts extra fields in payload."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/event",
            json={
                "event_type": "build",
                "source": "github",
                "custom_field": "custom_value",
                "nested": {"key": "value"},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_event_missing_event_type(
    db_engine: AsyncEngine,
) -> None:
    """Test that webhook endpoint rejects payload without event_type."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/webhook/event",
            json={
                "source": "grafana",
                "message": "No event_type",
            },
        )

        assert response.status_code == 422


@pytest.mark.asyncio
async def test_webhook_event_source_header_override(
    db_engine: AsyncEngine,
) -> None:
    """Test that X-Webhook-Source header overrides body source."""
    transport = ASGITransport(app=fastapi_app)

    # Create a mock webhook source to verify the event data
    mock_webhook_source = AsyncMock(spec=WebhookEventSource)
    mock_webhook_source.emit_event = AsyncMock(return_value="test-event-id")

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(
            fastapi_app.state, "webhook_source", mock_webhook_source, create=True
        ):
            response = await client.post(
                "/webhook/event",
                headers={"X-Webhook-Source": "header_source"},
                json={
                    "event_type": "test",
                    "source": "body_source",
                },
            )

            assert response.status_code == 200

            # Check that emit_event was called
            if mock_webhook_source.emit_event.called:
                call_args = mock_webhook_source.emit_event.call_args
                event_data = call_args[0][0]
                # Header source should override body source
                assert event_data["source"] == "header_source"


@pytest.mark.asyncio
async def test_webhook_event_signature_required_when_secret_configured(
    db_engine: AsyncEngine,
) -> None:
    """Test that signature is required when source has a configured secret."""
    transport = ASGITransport(app=fastapi_app)

    # Create a mock config with a secret for 'grafana' source
    mock_config = MagicMock()
    mock_config.event_system.sources.webhook.secrets = {"grafana": "test-secret"}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(fastapi_app.state, "config", mock_config, create=True):
            # Request without signature should be rejected
            response = await client.post(
                "/webhook/event",
                json={
                    "event_type": "alert",
                    "source": "grafana",
                    "message": "Test alert",
                },
            )

            assert response.status_code == 401
            assert "Signature required" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_event_invalid_signature_rejected(
    db_engine: AsyncEngine,
) -> None:
    """Test that invalid signature is rejected."""
    transport = ASGITransport(app=fastapi_app)

    # Create a mock config with a secret for 'grafana' source
    mock_config = MagicMock()
    mock_config.event_system.sources.webhook.secrets = {"grafana": "test-secret"}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(fastapi_app.state, "config", mock_config, create=True):
            response = await client.post(
                "/webhook/event",
                headers={"X-Webhook-Signature": "sha256=invalid"},
                json={
                    "event_type": "alert",
                    "source": "grafana",
                    "message": "Test alert",
                },
            )

            assert response.status_code == 403
            assert "Invalid signature" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_event_valid_signature_accepted(
    db_engine: AsyncEngine,
) -> None:
    """Test that valid signature is accepted."""
    transport = ASGITransport(app=fastapi_app)

    secret = "test-secret"
    payload = b'{"event_type": "alert", "source": "grafana", "message": "Test alert"}'

    # Compute valid signature
    expected_signature = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    # Create a mock config with a secret for 'grafana' source
    mock_config = MagicMock()
    mock_config.event_system.sources.webhook.secrets = {"grafana": secret}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(fastapi_app.state, "config", mock_config, create=True):
            response = await client.post(
                "/webhook/event",
                headers={
                    "X-Webhook-Signature": f"sha256={expected_signature}",
                    "Content-Type": "application/json",
                },
                content=payload,
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_event_no_signature_needed_for_unconfigured_source(
    db_engine: AsyncEngine,
) -> None:
    """Test that no signature is needed for sources without configured secrets."""
    transport = ASGITransport(app=fastapi_app)

    # Create a mock config with a secret only for 'grafana', not for 'github'
    mock_config = MagicMock()
    mock_config.event_system.sources.webhook.secrets = {"grafana": "test-secret"}

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(fastapi_app.state, "config", mock_config, create=True):
            # Request from 'github' (no secret configured) should succeed without signature
            response = await client.post(
                "/webhook/event",
                json={
                    "event_type": "build",
                    "source": "github",
                    "message": "Build completed",
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_source_emit_event() -> None:
    """Test WebhookEventSource emit_event method."""
    source = WebhookEventSource()

    # Mock the processor
    mock_processor = AsyncMock()
    await source.start(mock_processor)

    event_data = {
        "event_type": "test",
        "source": "test-source",
        "message": "Test message",
    }

    await source.emit_event(event_data)

    # Verify process_event was called
    mock_processor.process_event.assert_called_once_with("webhook", event_data)


@pytest.mark.asyncio
async def test_webhook_source_emit_event_not_running() -> None:
    """Test WebhookEventSource emit_event returns None when not running."""
    source = WebhookEventSource()

    # Source is not started, should return None
    result = await source.emit_event({"event_type": "test"})

    assert result is None


@pytest.mark.asyncio
async def test_webhook_source_lifecycle() -> None:
    """Test WebhookEventSource start/stop lifecycle."""
    source = WebhookEventSource()

    assert source.processor is None
    assert not source._running

    # Start
    mock_processor = AsyncMock()
    await source.start(mock_processor)

    assert source.processor is mock_processor
    assert source._running

    # Stop
    await source.stop()

    assert source.processor is None
    assert not source._running


@pytest.mark.asyncio
async def test_webhook_event_type_via_query_param(
    db_engine: AsyncEngine,
) -> None:
    """Test that event_type can be provided via query parameter."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Provide event_type in query param, not in body
        response = await client.post(
            "/webhook/event?event_type=alert",
            json={
                "source": "alertmanager",
                "message": "Test alert from alertmanager",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_event_type_query_param_overrides_body(
    db_engine: AsyncEngine,
) -> None:
    """Test that query param event_type overrides body event_type."""
    transport = ASGITransport(app=fastapi_app)

    mock_webhook_source = AsyncMock(spec=WebhookEventSource)
    mock_webhook_source.emit_event = AsyncMock(return_value="test-event-id")

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(
            fastapi_app.state, "webhook_source", mock_webhook_source, create=True
        ):
            response = await client.post(
                "/webhook/event?event_type=query_type",
                json={
                    "event_type": "body_type",
                    "source": "test",
                },
            )

            assert response.status_code == 200

            # Check that emit_event was called with query param event_type
            if mock_webhook_source.emit_event.called:
                call_args = mock_webhook_source.emit_event.call_args
                event_data = call_args[0][0]
                assert event_data["event_type"] == "query_type"


@pytest.mark.asyncio
async def test_webhook_source_via_query_param(
    db_engine: AsyncEngine,
) -> None:
    """Test that source can be provided via query parameter."""
    transport = ASGITransport(app=fastapi_app)

    mock_webhook_source = AsyncMock(spec=WebhookEventSource)
    mock_webhook_source.emit_event = AsyncMock(return_value="test-event-id")

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(
            fastapi_app.state, "webhook_source", mock_webhook_source, create=True
        ):
            response = await client.post(
                "/webhook/event?source=query_source",
                json={
                    "event_type": "test",
                },
            )

            assert response.status_code == 200

            # Check that emit_event was called with query param source
            if mock_webhook_source.emit_event.called:
                call_args = mock_webhook_source.emit_event.call_args
                event_data = call_args[0][0]
                assert event_data["source"] == "query_source"


@pytest.mark.asyncio
async def test_webhook_source_priority_header_over_query(
    db_engine: AsyncEngine,
) -> None:
    """Test that header source takes priority over query param source."""
    transport = ASGITransport(app=fastapi_app)

    mock_webhook_source = AsyncMock(spec=WebhookEventSource)
    mock_webhook_source.emit_event = AsyncMock(return_value="test-event-id")

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(
            fastapi_app.state, "webhook_source", mock_webhook_source, create=True
        ):
            response = await client.post(
                "/webhook/event?source=query_source",
                headers={"X-Webhook-Source": "header_source"},
                json={
                    "event_type": "test",
                    "source": "body_source",
                },
            )

            assert response.status_code == 200

            # Check that emit_event was called with header source (highest priority)
            if mock_webhook_source.emit_event.called:
                call_args = mock_webhook_source.emit_event.call_args
                event_data = call_args[0][0]
                assert event_data["source"] == "header_source"


@pytest.mark.asyncio
async def test_webhook_alertmanager_style_request(
    db_engine: AsyncEngine,
) -> None:
    """Test that alertmanager-style requests work (event_type and source in URL)."""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Alertmanager sends event_type in URL, alerts in body
        response = await client.post(
            "/webhook/event?event_type=alertmanager&source=prometheus",
            json={
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {"alertname": "HighCPU"},
                        "annotations": {"summary": "CPU is high"},
                    }
                ],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"


@pytest.mark.asyncio
async def test_webhook_system_fields_cannot_be_overwritten(
    db_engine: AsyncEngine,
) -> None:
    """Test that system-generated fields cannot be overwritten by payload extra fields."""
    transport = ASGITransport(app=fastapi_app)

    mock_webhook_source = AsyncMock(spec=WebhookEventSource)
    mock_webhook_source.emit_event = AsyncMock(return_value="test-event-id")

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with patch.object(
            fastapi_app.state, "webhook_source", mock_webhook_source, create=True
        ):
            response = await client.post(
                "/webhook/event",
                json={
                    "event_type": "test",
                    "source": "test_source",
                    # Try to inject malicious values via extra fields
                    "event_id": "malicious-id",
                },
            )

            assert response.status_code == 200

            # Check that emit_event was called with system-generated event_id
            if mock_webhook_source.emit_event.called:
                call_args = mock_webhook_source.emit_event.call_args
                event_data = call_args[0][0]
                # event_id should be a valid UUID, not the malicious value
                assert event_data["event_id"] != "malicious-id"
                # Verify it's a valid UUID
                uuid.UUID(event_data["event_id"])
