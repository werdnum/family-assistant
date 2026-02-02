from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from family_assistant.sms.backends.crazytel import CrazyTelBackend
from family_assistant.sms.backends.mock import MockSMSBackend
from family_assistant.sms.service import SMSService


@pytest.mark.asyncio
async def test_mock_sms_backend() -> None:
    backend = MockSMSBackend()
    result = await backend.send_sms("123456789", "Hello")
    assert result == "mock_id"


@pytest.mark.asyncio
async def test_crazytel_backend_success() -> None:
    backend = CrazyTelBackend(api_key="test_key", from_number="987654321")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "success", "message_id": "12345"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result = await backend.send_sms("0491570006", "Testing")

        assert result == "12345"
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_crazytel_backend_failure() -> None:
    backend = CrazyTelBackend(api_key="test_key", from_number="987654321")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.HTTPStatusError(
            "Error", request=MagicMock(), response=MagicMock(text="Failed")
        )
        result = await backend.send_sms("0491570006", "Testing")

        assert result is None
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_sms_service_allowed_numbers() -> None:
    backend = AsyncMock()
    processing_service = AsyncMock()

    service = SMSService(
        backend=backend,
        processing_service=processing_service,
        allowed_numbers=["12345"],
    )

    db_context = MagicMock()

    # Unauthorized number
    await service.handle_inbound_sms(db_context, "99999", "67890", "Hello")
    processing_service.handle_chat_interaction.assert_not_called()

    # Authorized number
    processing_service.handle_chat_interaction.return_value = MagicMock(
        text_reply="Hi back", error_traceback=None
    )
    await service.handle_inbound_sms(db_context, "12345", "67890", "Hello")
    processing_service.handle_chat_interaction.assert_called_once()
    backend.send_sms.assert_called_with(to_number="12345", message="Hi back")
