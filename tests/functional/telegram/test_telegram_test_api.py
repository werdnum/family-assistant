"""Tests to verify telegram-test-api integration works correctly.

These tests verify that the TelegramTestServer and TelegramTestClient
work correctly before we migrate other tests to use them.

Note: server startup and get_bot_api_url() are exercised by every other
telegram functional test, since tests/functional/telegram/conftest.py
builds the real python-telegram-bot Bot against get_bot_api_url() and
every send_message/send_command call in this suite depends on the
server actually being up, so no dedicated smoke test for those is kept
here.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.mocks.telegram_test_server import TelegramTestServer


@pytest.mark.asyncio
async def test_telegram_test_client_records_sent_message_and_command(
    telegram_test_server_session: TelegramTestServer,
) -> None:
    """Sent messages and commands round-trip through the mock server's history."""
    # Token must be in format <numeric_id>:<secret> for telegram-bot-api-mock; the
    # secret is randomized so this test doesn't see history left behind by other
    # runs against the session-scoped mock server.
    token = f"222222222:{uuid.uuid4().hex}"
    client = telegram_test_server_session.get_client(
        token=token,
        user_id=123,
        chat_id=456,
        first_name="TestUser",
    )

    message_result = await client.send_message("Hello bot!")
    sent_message = message_result.get("result", {}).get("message", {})
    assert sent_message.get("text") == "Hello bot!"
    assert sent_message.get("from", {}).get("id") == 123

    command_result = await client.send_command("/start")
    command_message = command_result.get("result", {}).get("message", {})
    assert command_message.get("text") == "/start"
    entities = command_message.get("entities", [])
    assert any(entity.get("type") == "bot_command" for entity in entities)

    history = await client.get_updates_history()
    texts = [update.get("message", {}).get("text") for update in history]
    assert texts == ["Hello bot!", "/start"]
