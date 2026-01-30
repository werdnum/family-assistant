"""Tests to verify telegram-test-api integration works correctly.

These tests verify that the TelegramTestServer and TelegramTestClient
work correctly before we migrate other tests to use them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.mocks.telegram_test_server import TelegramTestServer


@pytest.mark.asyncio
async def test_telegram_test_server_starts_and_stops(
    telegram_test_server_session: TelegramTestServer,
) -> None:
    """Test that the telegram-test-api server starts and is accessible."""
    # The server should already be running from the session fixture
    assert telegram_test_server_session.port > 0
    assert telegram_test_server_session.api_url.startswith("http://")

    # Verify we can get a client
    token = "test_token_123"
    client = telegram_test_server_session.get_client(token)
    assert client is not None
    assert client.token == token


@pytest.mark.asyncio
async def test_telegram_test_client_send_message(
    telegram_test_server_session: TelegramTestServer,
) -> None:
    """Test that the client can send messages to the test server."""
    token = "test_bot_token_456"
    client = telegram_test_server_session.get_client(
        token=token,
        user_id=123,
        chat_id=456,
        first_name="TestUser",
    )

    # Send a message - this simulates a user sending a message to the bot
    result = await client.send_message("Hello bot!")

    # The server should accept the message
    assert result is not None
    # The result should indicate success
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_telegram_test_client_send_command(
    telegram_test_server_session: TelegramTestServer,
) -> None:
    """Test that the client can send commands to the test server."""
    token = "test_bot_token_789"
    client = telegram_test_server_session.get_client(
        token=token,
        user_id=789,
        chat_id=101112,
    )

    # Send a command - this simulates a user sending /start to the bot
    result = await client.send_command("/start")

    # The server should accept the command
    assert result is not None
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_bot_api_url_generation(
    telegram_test_server_session: TelegramTestServer,
) -> None:
    """Test that the bot API URL is correctly generated.

    The get_bot_api_url() method returns the base URL ending with '/bot',
    matching Telegram's default format. python-telegram-bot will append
    the token, resulting in '{base_url}{token}'.
    """
    expected_url = f"{telegram_test_server_session.api_url}/bot"

    actual_url = telegram_test_server_session.get_bot_api_url()

    assert actual_url == expected_url
