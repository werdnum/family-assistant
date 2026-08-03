"""Unit tests for reaping attachments that nothing references.

An upload commits its ``attachment_metadata`` row and file before the message
that would reference it exists, so every send that never persists a message —
an abandoned compose, a failed kickoff, a refused concurrent turn — leaves both
behind. The reaper collects those rows once they are past the grace period, and
only those: anything a message or a note names, anything still inside the grace
period, and anything a producer other than an upload owns stays put.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, update

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import add_message_to_history
from family_assistant.storage.repositories.notes import NoteWritePolicy

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.ext.asyncio import AsyncEngine

USER = "user_uploader"
CONVERSATION = "conversation-1"
GRACE = timedelta(hours=24)


async def _upload(
    registry: AttachmentRegistry,
    db_context: Database,
    *,
    age: timedelta,
    filename: str = "photo.png",
) -> str:
    """Register a user upload and backdate it by ``age``."""
    metadata = await registry.register_user_attachment(
        db_context=db_context,
        content=b"file bytes",
        filename=filename,
        mime_type="image/png",
        user_id=USER,
    )
    await _backdate(registry, db_context, metadata.attachment_id, age)
    return metadata.attachment_id


async def _backdate(
    registry: AttachmentRegistry,
    db_context: Database,
    attachment_id: str,
    age: timedelta,
) -> None:
    """Age both the row and its file, as the passage of time would."""
    created_at = datetime.now(UTC) - age
    await db_context.execute(
        update(attachment_metadata_table)
        .where(attachment_metadata_table.c.attachment_id == attachment_id)
        .values(created_at=created_at)
    )
    file_path = registry.get_attachment_path(attachment_id)
    if file_path and file_path.exists():
        timestamp = created_at.timestamp()
        os.utime(file_path, (timestamp, timestamp))


async def _row_exists(db_context: Database, attachment_id: str) -> bool:
    row = await db_context.fetch_one(
        select(attachment_metadata_table.c.attachment_id).where(
            attachment_metadata_table.c.attachment_id == attachment_id
        )
    )
    return row is not None


def _file_exists(registry: AttachmentRegistry, attachment_id: str) -> bool:
    file_path = registry.get_attachment_path(attachment_id)
    return file_path is not None and file_path.exists()


@pytest.fixture
def registry_and_db(
    db_engine: AsyncEngine,
) -> Generator[tuple[AttachmentRegistry, Database]]:
    with tempfile.TemporaryDirectory() as temp_dir:
        yield (
            AttachmentRegistry(storage_path=temp_dir, db_engine=db_engine, config=None),
            Database(engine=db_engine),
        )


class TestReapUnreferencedAttachments:
    @pytest.mark.asyncio
    async def test_abandoned_upload_is_collected(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        attachment_id = await _upload(registry, db_context, age=timedelta(days=2))

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 1
        assert not await _row_exists(db_context, attachment_id)
        assert not _file_exists(registry, attachment_id)

    @pytest.mark.asyncio
    async def test_upload_inside_grace_period_survives(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        attachment_id = await _upload(registry, db_context, age=timedelta(hours=1))

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 0
        assert await _row_exists(db_context, attachment_id)
        assert _file_exists(registry, attachment_id)

    @pytest.mark.asyncio
    async def test_attachment_a_message_references_survives(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        sent = await _upload(registry, db_context, age=timedelta(days=2))
        abandoned = await _upload(
            registry, db_context, age=timedelta(days=2), filename="abandoned.png"
        )

        await add_message_to_history(
            db_context,
            interface_type="web",
            conversation_id=CONVERSATION,
            interface_message_id=None,
            turn_id=None,
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="user",
            content="here is a photo",
            attachments=[
                {
                    "type": "image",
                    "attachment_id": sent,
                    "mime_type": "image/png",
                }
            ],
        )
        # A message with no attachments at all must not upset the scan.
        await add_message_to_history(
            db_context,
            interface_type="web",
            conversation_id=CONVERSATION,
            interface_message_id=None,
            turn_id=None,
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="assistant",
            content="nice photo",
        )

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 1
        assert await _row_exists(db_context, sent)
        assert _file_exists(registry, sent)
        assert not await _row_exists(db_context, abandoned)

    @pytest.mark.asyncio
    async def test_attachment_a_note_references_survives(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        attached_to_note = await _upload(registry, db_context, age=timedelta(days=2))

        await db_context.notes.add_or_update(
            title="Receipts",
            content="scanned receipt",
            attachment_ids=[attached_to_note],
            write_policy=NoteWritePolicy.UNCONSTRAINED,
        )

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 0
        assert await _row_exists(db_context, attached_to_note)
        assert _file_exists(registry, attached_to_note)

    @pytest.mark.asyncio
    async def test_attachment_linked_to_a_message_row_survives(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        attachment_id = await _upload(registry, db_context, age=timedelta(days=2))
        message_id = await add_message_to_history(
            db_context,
            interface_type="web",
            conversation_id=CONVERSATION,
            interface_message_id=None,
            turn_id=None,
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            role="user",
            content="linked by column",
        )
        await db_context.execute(
            update(attachment_metadata_table)
            .where(attachment_metadata_table.c.attachment_id == attachment_id)
            .values(message_id=message_id)
        )

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 0
        assert await _row_exists(db_context, attachment_id)

    @pytest.mark.asyncio
    async def test_tool_attachment_is_never_a_candidate(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        metadata = await registry.store_and_register_tool_attachment(
            file_content=b"tool output",
            filename="chart.png",
            content_type="image/png",
            tool_name="make_chart",
            db_context=db_context,
        )
        await _backdate(registry, db_context, metadata.attachment_id, timedelta(days=9))

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 0
        assert await _row_exists(db_context, metadata.attachment_id)
        assert _file_exists(registry, metadata.attachment_id)

    @pytest.mark.asyncio
    async def test_sent_uploads_do_not_starve_the_limit(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        """Older sent uploads must not consume the pass's budget.

        Nothing back-fills ``message_id``, so every upload a message references
        stays in the candidate columns forever. If the limit were applied before
        the reference exclusion, the oldest of those would fill it on every pass
        and the orphan behind them would never be reached.
        """
        registry, db_context = registry_and_db
        for index in range(3):
            sent = await _upload(
                registry,
                db_context,
                age=timedelta(days=10),
                filename=f"sent-{index}.png",
            )
            await add_message_to_history(
                db_context,
                interface_type="web",
                conversation_id=CONVERSATION,
                interface_message_id=None,
                turn_id=None,
                thread_root_id=None,
                timestamp=datetime.now(UTC),
                role="user",
                content="here is a photo",
                attachments=[{"type": "image", "attachment_id": sent}],
            )
        abandoned = await _upload(
            registry, db_context, age=timedelta(days=2), filename="abandoned.png"
        )

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE, limit=2
        )

        assert reaped == 1
        assert not await _row_exists(db_context, abandoned)

    @pytest.mark.asyncio
    async def test_orphans_beyond_the_first_page_are_reached(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        """Paging walks past whole pages of referenced candidates."""
        registry, db_context = registry_and_db
        registry.REAP_PAGE_SIZE = 2
        for index in range(4):
            sent = await _upload(
                registry,
                db_context,
                age=timedelta(days=10),
                filename=f"sent-{index}.png",
            )
            await add_message_to_history(
                db_context,
                interface_type="web",
                conversation_id=CONVERSATION,
                interface_message_id=None,
                turn_id=None,
                thread_root_id=None,
                timestamp=datetime.now(UTC),
                role="user",
                content="here is a photo",
                attachments=[{"type": "image", "attachment_id": sent}],
            )
        abandoned = await _upload(
            registry, db_context, age=timedelta(days=2), filename="abandoned.png"
        )

        reaped = await registry.reap_unreferenced_attachments(
            db_context, grace_period=GRACE
        )

        assert reaped == 1
        assert not await _row_exists(db_context, abandoned)

    @pytest.mark.asyncio
    async def test_batch_limit_bounds_one_pass(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        for index in range(3):
            await _upload(
                registry,
                db_context,
                age=timedelta(days=2),
                filename=f"abandoned-{index}.png",
            )

        assert (
            await registry.reap_unreferenced_attachments(
                db_context, grace_period=GRACE, limit=2
            )
            == 2
        )
        assert (
            await registry.reap_unreferenced_attachments(
                db_context, grace_period=GRACE, limit=2
            )
            == 1
        )


class TestCleanupOrphanedFiles:
    @pytest.mark.asyncio
    async def test_file_without_a_row_is_collected_once_it_ages(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        attachment_id = await _upload(registry, db_context, age=timedelta(days=2))
        await db_context.execute(
            attachment_metadata_table.delete().where(
                attachment_metadata_table.c.attachment_id == attachment_id
            )
        )

        deleted = await registry.cleanup_orphaned_attachments(db_context, min_age=GRACE)

        assert deleted == 1
        assert not _file_exists(registry, attachment_id)

    @pytest.mark.asyncio
    async def test_freshly_written_file_is_left_for_its_row_to_commit(
        self, registry_and_db: tuple[AttachmentRegistry, Database]
    ) -> None:
        registry, db_context = registry_and_db
        metadata = await registry.register_user_attachment(
            db_context=db_context,
            content=b"just written",
            filename="in-flight.png",
            mime_type="image/png",
            user_id=USER,
        )
        # Stand in for the window between the file landing on disk and its row
        # being committed: the file is new, and no row names it.
        await db_context.execute(
            attachment_metadata_table.delete().where(
                attachment_metadata_table.c.attachment_id == metadata.attachment_id
            )
        )

        deleted = await registry.cleanup_orphaned_attachments(db_context, min_age=GRACE)

        assert deleted == 0
        assert _file_exists(registry, metadata.attachment_id)
