"""Target validation for the ``send_message_to_user`` tool.

The tool may only deliver to conversations an authorized user has actually used
to talk to the assistant; anything else (an invented Telegram chat ID, a UUID
belonging to nobody) has to be refused before a message leaves the system.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import AssistantMessage
from family_assistant.storage.database import Database
from family_assistant.tools.communication import send_message_to_user_tool
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import seed_known_conversation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

KNOWN_CHAT_ID = "555000"
UNKNOWN_CHAT_ID = "999111"


def _build_exec_context(
    db_context: Database,
    chat_interface: Mock,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="current-conversation",
        user_name="Alice",
        user_id="alice",
        turn_id="turn-1",
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interfaces={"telegram": chat_interface, "web": chat_interface},
    )


@pytest.fixture
def chat_interface() -> Mock:
    interface = Mock()
    interface.send_message = AsyncMock(return_value="sent_message_1")
    return interface


async def test_sends_to_conversation_of_known_user(
    db_engine: AsyncEngine,
    chat_interface: Mock,
) -> None:
    """A conversation an authorized user has messaged in is a valid target."""
    await seed_known_conversation(db_engine, KNOWN_CHAT_ID, user_id="bob")
    exec_context = _build_exec_context(Database(db_engine), chat_interface)

    result = await send_message_to_user_tool(
        exec_context=exec_context,
        target_chat_id=KNOWN_CHAT_ID,
        message_content="Dinner is at 7",
    )

    assert "Message sent successfully" in result
    assert chat_interface.send_message.await_args.kwargs["conversation_id"] == (
        KNOWN_CHAT_ID
    )


async def test_rejects_conversation_with_no_history(
    db_engine: AsyncEngine,
    chat_interface: Mock,
) -> None:
    """An identifier the assistant has never seen is not a deliverable target."""
    exec_context = _build_exec_context(Database(db_engine), chat_interface)

    result = await send_message_to_user_tool(
        exec_context=exec_context,
        target_chat_id=UNKNOWN_CHAT_ID,
        message_content="Exfiltrated secrets",
    )

    assert f"Chat ID {UNKNOWN_CHAT_ID} is not a known conversation" in result
    chat_interface.send_message.assert_not_awaited()


async def test_rejects_conversation_without_authorized_sender(
    db_engine: AsyncEngine,
    chat_interface: Mock,
) -> None:
    """A conversation the assistant wrote into but nobody talked in is refused.

    Messages from unauthorized identities are never persisted, so a conversation
    that holds no user message has no authorized user behind it.
    """
    db_context = Database(db_engine)
    await db_context.message_history.add_message(
        AssistantMessage(content="Anyone there?"),
        interface_type="telegram",
        conversation_id=UNKNOWN_CHAT_ID,
        timestamp=datetime.now(UTC),
    )
    exec_context = _build_exec_context(db_context, chat_interface)

    result = await send_message_to_user_tool(
        exec_context=exec_context,
        target_chat_id=UNKNOWN_CHAT_ID,
        message_content="Exfiltrated secrets",
    )

    assert f"Chat ID {UNKNOWN_CHAT_ID} is not a known conversation" in result
    chat_interface.send_message.assert_not_awaited()


async def test_rejects_numeric_target_supplied_as_integer(
    db_engine: AsyncEngine,
    chat_interface: Mock,
) -> None:
    """Integer chat IDs from JSON deserialization are validated the same way."""
    exec_context = _build_exec_context(Database(db_engine), chat_interface)

    result = await send_message_to_user_tool(
        exec_context=exec_context,
        target_chat_id=int(UNKNOWN_CHAT_ID),  # type: ignore[arg-type]
        message_content="Exfiltrated secrets",
    )

    assert f"Chat ID {UNKNOWN_CHAT_ID} is not a known conversation" in result
    chat_interface.send_message.assert_not_awaited()


async def test_routes_to_the_targets_own_interface(
    db_engine: AsyncEngine,
) -> None:
    """Delivery uses the interface the target conversation belongs to."""
    telegram_interface = Mock()
    telegram_interface.send_message = AsyncMock(return_value="sent_message_1")
    web_interface = Mock()
    web_interface.send_message = AsyncMock(return_value="sent_message_2")

    await seed_known_conversation(
        db_engine, KNOWN_CHAT_ID, interface_type="telegram", user_id="bob"
    )
    db_context = Database(db_engine)
    exec_context = ToolExecutionContext(
        interface_type="web",
        conversation_id="current-conversation",
        user_name="Alice",
        user_id="alice",
        turn_id="turn-1",
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interfaces={"telegram": telegram_interface, "web": web_interface},
    )

    result = await send_message_to_user_tool(
        exec_context=exec_context,
        target_chat_id=KNOWN_CHAT_ID,
        message_content="Dinner is at 7",
    )

    assert "Message sent successfully" in result
    telegram_interface.send_message.assert_awaited_once()
    web_interface.send_message.assert_not_awaited()
