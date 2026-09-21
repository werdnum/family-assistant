"""Delivery-path tests for owner-scoped attachments over a chat interface.

Delivering an owned (personal-data) attachment reads it back by ID from the
registry. Without threading the requester through ``on_behalf_of_user_id`` the
strict owner check would drop the attachment even when sending it back to its
own requester. These tests exercise ``TelegramChatInterface._send_attachments``:
with a matching ``on_behalf_of_user_id`` the owned attachment is delivered; with
``None`` (no user context) it is skipped like any missing attachment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.telegram.interface import TelegramChatInterface

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

OWNER = "owner_user"


async def _register_owned(registry: AttachmentRegistry, db_engine: AsyncEngine) -> str:
    db_context = Database(db_engine)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"fake document content",
        filename="report.pdf",
        content_type="application/pdf",
        tool_name="gmail_get_attachment",
        description="Personal report",
        owner_user_id=OWNER,
        db_context=db_context,
    )
    return attachment.attachment_id


@pytest.mark.asyncio
async def test_owned_attachment_delivered_with_matching_actor(
    db_engine: AsyncEngine, tmp_path: object
) -> None:
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot
    mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=301))

    registry = AttachmentRegistry(storage_path=str(tmp_path), db_engine=db_engine)
    attachment_id = await _register_owned(registry, db_engine)

    chat_interface = TelegramChatInterface(
        application=mock_app, attachment_registry=registry
    )
    await chat_interface._send_attachments(
        chat_id=123,
        attachment_ids=[attachment_id],
        reply_to_msg_id=None,
        on_behalf_of_user_id=OWNER,
    )

    mock_bot.send_document.assert_called_once()


@pytest.mark.asyncio
async def test_owned_attachment_skipped_without_actor(
    db_engine: AsyncEngine, tmp_path: object
) -> None:
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot
    mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=302))

    registry = AttachmentRegistry(storage_path=str(tmp_path), db_engine=db_engine)
    attachment_id = await _register_owned(registry, db_engine)

    chat_interface = TelegramChatInterface(
        application=mock_app, attachment_registry=registry
    )
    # No acting user: the owned attachment reads as not-found and is skipped
    # exactly like any missing attachment (graceful skip, no send).
    await chat_interface._send_attachments(
        chat_id=123,
        attachment_ids=[attachment_id],
        reply_to_msg_id=None,
        on_behalf_of_user_id=None,
    )

    mock_bot.send_document.assert_not_called()
