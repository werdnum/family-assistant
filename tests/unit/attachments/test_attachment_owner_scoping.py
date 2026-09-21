"""Unit tests for owner-scoped attachment enforcement in the registry.

An attachment with ``owner_user_id IS NULL`` is "ownerless" and behaves exactly
as it did before this feature for every caller. An owned attachment is visible
and operable ONLY when the caller's acting user matches; a mismatch (including a
``None`` actor) reads as not-found (``None``/``False``), never a distinguishable
"forbidden".
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

OWNER = "user_owner"
OTHER = "user_other"


async def _register_owned(
    registry: AttachmentRegistry,
    db_context: Database,
    *,
    owner_user_id: str | None,
    conversation_id: str | None = None,
    content: bytes = b"secret bytes",
) -> str:
    """Store a file and register a tool attachment with the given owner."""
    metadata = await registry.store_and_register_tool_attachment(
        file_content=content,
        filename="owned.txt",
        content_type="text/plain",
        tool_name="gmail_get_attachment",
        conversation_id=conversation_id,
        owner_user_id=owner_user_id,
        db_context=db_context,
    )
    return metadata.attachment_id


class TestOwnerScopedSingularReads:
    @pytest.mark.asyncio
    async def test_ownerless_visible_to_every_actor(
        self, db_engine: AsyncEngine
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            att_id = await _register_owned(registry, db_context, owner_user_id=None)

            for actor in (None, OWNER, OTHER):
                metadata = await registry.get_attachment(
                    db_context, att_id, acting_user_id=actor
                )
                assert metadata is not None
                content = await registry.get_attachment_content(
                    db_context, att_id, acting_user_id=actor
                )
                assert content == b"secret bytes"

    @pytest.mark.asyncio
    async def test_owned_visible_only_to_owner(self, db_engine: AsyncEngine) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            att_id = await _register_owned(registry, db_context, owner_user_id=OWNER)

            assert (
                await registry.get_attachment(db_context, att_id, acting_user_id=OWNER)
                is not None
            )
            assert (
                await registry.get_attachment(db_context, att_id, acting_user_id=OTHER)
                is None
            )
            assert (
                await registry.get_attachment(db_context, att_id, acting_user_id=None)
                is None
            )

    @pytest.mark.asyncio
    async def test_owned_content_hidden_from_non_owner(
        self, db_engine: AsyncEngine
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            att_id = await _register_owned(registry, db_context, owner_user_id=OWNER)

            assert (
                await registry.get_attachment_content(
                    db_context, att_id, acting_user_id=OWNER
                )
                == b"secret bytes"
            )
            assert (
                await registry.get_attachment_content(
                    db_context, att_id, acting_user_id=OTHER
                )
                is None
            )
            assert (
                await registry.get_attachment_content(
                    db_context, att_id, acting_user_id=None
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_with_context_and_resolve_path_scoped(
        self, db_engine: AsyncEngine
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            att_id = await _register_owned(registry, db_context, owner_user_id=OWNER)

            assert (
                await registry.get_attachment_with_context(att_id, acting_user_id=OWNER)
                is not None
            )
            assert (
                await registry.get_attachment_with_context(att_id, acting_user_id=OTHER)
                is None
            )
            assert (
                await registry.resolve_attachment_path(att_id, acting_user_id=OWNER)
                is not None
            )
            assert (
                await registry.resolve_attachment_path(att_id, acting_user_id=OTHER)
                is None
            )


class TestOwnerScopedDelete:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_delete(self, db_engine: AsyncEngine) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            att_id = await _register_owned(registry, db_context, owner_user_id=OWNER)

            assert (
                await registry.delete_attachment(
                    db_context, att_id, acting_user_id=OTHER
                )
                is False
            )
            assert (
                await registry.delete_attachment(
                    db_context, att_id, acting_user_id=None
                )
                is False
            )
            # Still present for the owner after failed non-owner deletes.
            assert (
                await registry.get_attachment(db_context, att_id, acting_user_id=OWNER)
                is not None
            )

            assert (
                await registry.delete_attachment(
                    db_context, att_id, acting_user_id=OWNER
                )
                is True
            )
            assert (
                await registry.get_attachment(db_context, att_id, acting_user_id=OWNER)
                is None
            )


class TestOwnerScopedBulkQueriesFilter:
    @pytest.mark.asyncio
    async def test_get_attachments_filters_owned_rows(
        self, db_engine: AsyncEngine
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            ownerless = await _register_owned(registry, db_context, owner_user_id=None)
            owned = await _register_owned(registry, db_context, owner_user_id=OWNER)
            ids = [ownerless, owned]

            owner_view = await registry.get_attachments(
                db_context, ids, acting_user_id=OWNER
            )
            assert set(owner_view.keys()) == {ownerless, owned}

            # Non-owner sees only the ownerless row (filter, not error).
            other_view = await registry.get_attachments(
                db_context, ids, acting_user_id=OTHER
            )
            assert set(other_view.keys()) == {ownerless}

            none_view = await registry.get_attachments(
                db_context, ids, acting_user_id=None
            )
            assert set(none_view.keys()) == {ownerless}

    @pytest.mark.asyncio
    async def test_list_attachments_filters_owned_rows(
        self, db_engine: AsyncEngine
    ) -> None:
        conversation_id = "conv_bulk"
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            ownerless = await _register_owned(
                registry,
                db_context,
                owner_user_id=None,
                conversation_id=conversation_id,
            )
            owned = await _register_owned(
                registry,
                db_context,
                owner_user_id=OWNER,
                conversation_id=conversation_id,
            )

            owner_ids = {
                a.attachment_id
                for a in await registry.list_attachments(
                    db_context,
                    acting_user_id=OWNER,
                    conversation_id=conversation_id,
                )
            }
            assert owner_ids == {ownerless, owned}

            other_ids = {
                a.attachment_id
                for a in await registry.list_attachments(
                    db_context,
                    acting_user_id=OTHER,
                    conversation_id=conversation_id,
                )
            }
            assert other_ids == {ownerless}

    @pytest.mark.asyncio
    async def test_recent_for_conversation_filters_owned_rows(
        self, db_engine: AsyncEngine
    ) -> None:
        conversation_id = "conv_recent"
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            ownerless = await _register_owned(
                registry,
                db_context,
                owner_user_id=None,
                conversation_id=conversation_id,
            )
            await _register_owned(
                registry,
                db_context,
                owner_user_id=OWNER,
                conversation_id=conversation_id,
            )

            other_ids = {
                a.attachment_id
                for a in await registry.get_recent_attachments_for_conversation(
                    db_context,
                    conversation_id,
                    cutoff,
                    acting_user_id=OTHER,
                )
            }
            assert other_ids == {ownerless}


class TestRegisterUserAttachmentStaysOwnerless:
    @pytest.mark.asyncio
    async def test_uploads_are_ownerless(self, db_engine: AsyncEngine) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            db_context = Database(engine=db_engine)
            record = await registry.register_user_attachment(
                db_context=db_context,
                content=b"uploaded bytes",
                filename="upload.txt",
                mime_type="text/plain",
                user_id=OWNER,
            )
            assert record.owner_user_id is None
            # Visible to a different actor precisely because it is ownerless.
            assert (
                await registry.get_attachment(
                    db_context, record.attachment_id, acting_user_id=OTHER
                )
                is not None
            )
