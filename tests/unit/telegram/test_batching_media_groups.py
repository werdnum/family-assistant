"""Tests for Telegram media group batching behavior.

These tests verify that ``DefaultMessageBatcher`` and ``NoBatchMessageBatcher``
correctly group Telegram album messages (those sharing a ``media_group_id``)
into a single batch, so the assistant receives them as one combined message
rather than several fragmented messages.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, PhotoSize, Update, User

from family_assistant.telegram.batching import (
    DefaultMessageBatcher,
    NoBatchMessageBatcher,
)
from tests.helpers import wait_for_condition

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from family_assistant.telegram.types import AttachmentData


def _make_context() -> ContextTypes.DEFAULT_TYPE:
    return cast("ContextTypes.DEFAULT_TYPE", SimpleNamespace())


def _make_photo_update(
    update_id: int,
    message_id: int,
    media_group_id: str | None,
    chat_id: int = 123,
) -> Update:
    user = User(id=12345, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    photo = PhotoSize(
        file_id=f"photo_{message_id}",
        file_unique_id=f"photo_{message_id}_uniq",
        width=10,
        height=10,
        file_size=64,
    )
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        photo=[photo],
        media_group_id=media_group_id,
    )
    return Update(update_id=update_id, message=message)


def _make_text_update(
    update_id: int,
    message_id: int,
    text: str,
    chat_id: int = 123,
) -> Update:
    user = User(id=12345, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


@pytest.mark.asyncio
async def test_no_batch_batcher_groups_album_into_single_batch() -> None:
    """All four photos in an album are delivered as a single batch."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_delay_seconds=0.1,
    )
    context = _make_context()

    for i in range(4):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="group-A"
        )
        await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=5.0,
        description="media group flushed",
    )

    assert processor.process_batch.await_count == 1
    chat_id, batch, _ctx = processor.process_batch.await_args.args
    assert chat_id == 123
    assert len(batch) == 4
    assert [u.update_id for u, _ in batch] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_no_batch_batcher_processes_non_album_messages_immediately() -> None:
    """Plain text messages (no media_group_id) skip buffering."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_delay_seconds=5.0,
    )
    context = _make_context()

    update = _make_text_update(update_id=1, message_id=101, text="hello")
    await batcher.add_to_batch(update, context, attachments=None)

    assert processor.process_batch.await_count == 1
    chat_id, batch, _ctx = processor.process_batch.await_args.args
    assert chat_id == 123
    assert len(batch) == 1
    assert batch[0][0].update_id == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_flushes_album_when_text_arrives() -> None:
    """A non-album message arriving mid-album flushes the album first."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_delay_seconds=5.0,
    )
    context = _make_context()

    for i in range(3):
        photo = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="group-B"
        )
        await batcher.add_to_batch(photo, context, attachments=None)

    text = _make_text_update(update_id=10, message_id=200, text="caption follow-up")
    await batcher.add_to_batch(text, context, attachments=None)

    assert processor.process_batch.await_count == 2
    first_call = processor.process_batch.await_args_list[0]
    second_call = processor.process_batch.await_args_list[1]
    assert len(first_call.args[1]) == 3
    assert [u.update_id for u, _ in first_call.args[1]] == [1, 2, 3]
    assert len(second_call.args[1]) == 1
    assert second_call.args[1][0][0].update_id == 10


@pytest.mark.asyncio
async def test_no_batch_batcher_notify_pending_arms_timer() -> None:
    """notify_pending_media_group sets a flush timer even before any add_to_batch."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_delay_seconds=0.1,
    )
    context = _make_context()

    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-C", context=context
    )
    # No messages have been added; flushing an empty buffer is a no-op.
    # ast-grep-ignore: no-asyncio-sleep-in-tests - asserting timer expiry is idempotent
    await asyncio.sleep(0.2)
    assert processor.process_batch.await_count == 0

    # Now add a single album message before the timer is re-armed.
    update = _make_photo_update(update_id=1, message_id=100, media_group_id="group-C")
    await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=5.0,
        description="media group flushed after notify_pending",
    )
    assert processor.process_batch.await_count == 1
    _, batch, _ = processor.process_batch.await_args.args
    assert len(batch) == 1


@pytest.mark.asyncio
async def test_default_batcher_uses_longer_delay_for_media_groups() -> None:
    """Album messages use ``media_group_delay_seconds`` instead of the normal delay."""
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_delay_seconds=0.5,
    )
    context = _make_context()

    update_album = _make_photo_update(
        update_id=1, message_id=100, media_group_id="group-D"
    )
    await batcher.add_to_batch(update_album, context, attachments=None)

    # Wait longer than batch_delay_seconds (0.05) but shorter than
    # media_group_delay_seconds (0.5). The batch should still be pending.
    # ast-grep-ignore: no-asyncio-sleep-in-tests - asserting batch does NOT fire early
    await asyncio.sleep(0.2)
    assert processor.process_batch.await_count == 0

    update_album_2 = _make_photo_update(
        update_id=2, message_id=101, media_group_id="group-D"
    )
    await batcher.add_to_batch(update_album_2, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=5.0,
        description="media group batch flushed",
    )
    assert processor.process_batch.await_count == 1
    _, batch, _ = processor.process_batch.await_args.args
    assert len(batch) == 2
    assert [u.update_id for u, _ in batch] == [1, 2]


@pytest.mark.asyncio
async def test_default_batcher_notify_pending_extends_delay() -> None:
    """notify_pending_media_group makes the timer use the longer media-group delay."""
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_delay_seconds=0.4,
    )
    context = _make_context()

    await batcher.notify_pending_media_group(
        chat_id=123, media_group_id="group-E", context=context
    )

    update_album = _make_photo_update(
        update_id=1, message_id=100, media_group_id="group-E"
    )
    await batcher.add_to_batch(update_album, context, attachments=None)

    # Without the longer media-group delay, the batch would have fired by now.
    # ast-grep-ignore: no-asyncio-sleep-in-tests - asserting batch does NOT fire early
    await asyncio.sleep(0.15)
    assert processor.process_batch.await_count == 0

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=5.0,
        description="batch flushed after media-group delay",
    )
    assert processor.process_batch.await_count == 1


@pytest.mark.asyncio
async def test_default_batcher_uses_short_delay_for_non_group_messages() -> None:
    """Plain text messages still flush quickly using ``batch_delay_seconds``."""
    processor = AsyncMock()
    batcher = DefaultMessageBatcher(
        batch_processor=processor,
        batch_delay_seconds=0.05,
        media_group_delay_seconds=2.0,
    )
    context = _make_context()

    update = _make_text_update(update_id=1, message_id=200, text="hi")
    await batcher.add_to_batch(update, context, attachments=None)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=1.0,
        description="batch flushed using short delay",
    )
    assert processor.process_batch.await_count == 1


@pytest.mark.asyncio
async def test_no_batch_batcher_attachments_preserved() -> None:
    """Per-message attachments survive media-group buffering."""
    processor = AsyncMock()
    batcher = NoBatchMessageBatcher(
        batch_processor=processor,
        media_group_delay_seconds=0.05,
    )
    context = _make_context()

    expected_attachments: list[list[AttachmentData] | None] = []
    for i in range(3):
        update = _make_photo_update(
            update_id=i + 1, message_id=100 + i, media_group_id="group-F"
        )
        attachments = [
            cast(
                "AttachmentData",
                SimpleNamespace(
                    content=b"data",
                    filename=f"photo_{i}.jpg",
                    mime_type="image/jpeg",
                    description=None,
                ),
            )
        ]
        expected_attachments.append(attachments)
        await batcher.add_to_batch(update, context, attachments=attachments)

    await wait_for_condition(
        lambda: processor.process_batch.await_count >= 1,
        timeout=5.0,
        description="media group attachments flushed",
    )

    _, batch, _ = processor.process_batch.await_args.args
    actual_attachments = [a for _, a in batch]
    assert actual_attachments == expected_attachments
