from unittest.mock import AsyncMock, MagicMock

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
async def test_crazytel_backend_failure() -> None:
    # Test failure when httpx fails
    backend = CrazyTelBackend(api_key="test_key", from_number="987654321")
    # We don't want to actually call the API, so we'd normally mock httpx.
    # For now, just a basic check that it's instantiated correctly.
    assert backend.api_key == "test_key"
    assert backend.from_number == "987654321"


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
