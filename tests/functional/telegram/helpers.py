"""Test helpers for realistic Telegram testing.

Follows patterns from tests/functional/calendar/ and tests/integration/home_assistant/.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.mocks.telegram_test_server import TelegramTestClient, TestServerUpdate

logger = logging.getLogger(__name__)


async def wait_for_bot_response(
    client: TelegramTestClient,
    timeout: float = 5.0,
    poll_interval: float = 0.1,
    min_messages: int = 1,
) -> list[TestServerUpdate]:
    """Wait for bot to send responses, polling until timeout.

    Like _wait_for_ha_entity() in HA fixture.

    Args:
        client: The TelegramTestClient to poll.
        timeout: Maximum time to wait for responses.
        poll_interval: Time between poll attempts.
        min_messages: Minimum number of messages to wait for.

    Returns:
        List of updates from the bot.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        updates = await client.get_updates(timeout=0.1, poll_interval=poll_interval)
        if len(updates) >= min_messages:
            return updates
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling for bot responses requires delay
        await asyncio.sleep(poll_interval)

    return []


async def assert_bot_sent_message(
    client: TelegramTestClient,
    expected_text: str,
    timeout: float = 5.0,
    partial_match: bool = True,
) -> TestServerUpdate:
    """Assert bot sent a message containing expected text.

    Args:
        client: The TelegramTestClient to poll.
        expected_text: Text to look for in bot messages.
        timeout: Maximum time to wait.
        partial_match: If True, check if expected_text is contained in message.
                       If False, require exact match.

    Returns:
        The matching update/message.

    Raises:
        AssertionError: If no matching message is found.
    """
    updates = await wait_for_bot_response(client, timeout)

    for update in updates:
        message_text = update.get("message", {}).get("text", "")
        if partial_match:
            if expected_text in message_text:
                return update
        elif message_text == expected_text:
            return update

    actual_texts = [u.get("message", {}).get("text", "") for u in updates]
    raise AssertionError(
        f"Expected message containing '{expected_text}', got: {actual_texts}"
    )


async def assert_bot_sent_message_with_keyboard(
    client: TelegramTestClient,
    timeout: float = 5.0,
) -> TestServerUpdate:
    """Assert bot sent a message with an inline keyboard.

    Args:
        client: The TelegramTestClient to poll.
        timeout: Maximum time to wait.

    Returns:
        The matching update/message with keyboard.

    Raises:
        AssertionError: If no message with keyboard is found.
    """
    updates = await wait_for_bot_response(client, timeout)

    for update in updates:
        message = update.get("message", {})
        reply_markup = message.get("reply_markup", {})
        if reply_markup.get("inline_keyboard"):
            return update

    raise AssertionError(
        f"Expected message with inline keyboard, got {len(updates)} messages without keyboards"
    )


def extract_callback_data_from_keyboard(
    update: TestServerUpdate,
    button_index: int = 0,
    row_index: int = 0,
) -> str:
    """Extract callback_data from an inline keyboard button.

    Args:
        update: The update containing a message with inline keyboard.
        button_index: Index of the button in the row (default: 0).
        row_index: Index of the keyboard row (default: 0).

    Returns:
        The callback_data string from the button.

    Raises:
        KeyError: If the button or keyboard doesn't exist.
    """
    message = update.get("message", {})
    reply_markup = message.get("reply_markup", {})
    inline_keyboard = reply_markup.get("inline_keyboard", [])

    if not inline_keyboard:
        raise KeyError("No inline_keyboard found in message")

    if row_index >= len(inline_keyboard):
        raise KeyError(
            f"Row index {row_index} out of range (have {len(inline_keyboard)} rows)"
        )

    row = inline_keyboard[row_index]
    if button_index >= len(row):
        raise KeyError(
            f"Button index {button_index} out of range (have {len(row)} buttons in row)"
        )

    button = row[button_index]
    return button.get("callback_data", "")
