"""Unit tests for AttachmentRegistry.get_attachment_path honoring an externally
supplied storage_path.

This behavior is used by email attachments which live in the mailbox directory
rather than the registry-managed sharded storage.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class TestGetAttachmentPathExternal:
    """Exercise the optional ``stored_path`` argument."""

    @pytest.mark.asyncio
    async def test_returns_external_path_when_file_exists(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        external_file = external_dir / "ticket.pdf"
        external_file.write_bytes(b"fake pdf bytes")

        with tempfile.TemporaryDirectory() as registry_dir:
            registry = AttachmentRegistry(
                storage_path=registry_dir, db_engine=db_engine, config=None
            )
            attachment_id = str(uuid.uuid4())

            resolved = registry.get_attachment_path(
                attachment_id,
                stored_path=str(external_file),
                source_type="email",
            )

            assert resolved == external_file

    @pytest.mark.asyncio
    async def test_falls_back_to_sharded_when_stored_path_missing(
        self, db_engine: AsyncEngine
    ) -> None:
        """When the external email file does not exist, fall back to sharded lookup."""
        with tempfile.TemporaryDirectory() as registry_dir:
            registry = AttachmentRegistry(
                storage_path=registry_dir, db_engine=db_engine, config=None
            )
            attachment_id = str(uuid.uuid4())
            # Write a file into the sharded storage.
            shard_dir = Path(registry_dir) / attachment_id[:2]
            shard_dir.mkdir(parents=True)
            sharded_file = shard_dir / f"{attachment_id}.bin"
            sharded_file.write_bytes(b"sharded bytes")

            resolved = registry.get_attachment_path(
                attachment_id,
                stored_path="/nonexistent/file.bin",
                source_type="email",
            )

            assert resolved == sharded_file

    @pytest.mark.asyncio
    async def test_non_email_source_ignores_stored_path(
        self,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Non-email rows must not serve files via ``stored_path``.

        A poisoned ``attachment_metadata.storage_path`` pointing at an
        arbitrary absolute path would otherwise let ``get_attachment_content``
        and ``/api/attachments/{id}`` read files from outside the managed
        directory for ``source_type`` values like ``"user"`` / ``"tool"`` /
        ``"script"``.
        """
        outside_file = tmp_path / "outside" / "secret.txt"
        outside_file.parent.mkdir()
        outside_file.write_bytes(b"should not be served")

        with tempfile.TemporaryDirectory() as registry_dir:
            registry = AttachmentRegistry(
                storage_path=registry_dir, db_engine=db_engine, config=None
            )
            attachment_id = str(uuid.uuid4())

            # No sharded file exists, so the sharded lookup returns None.
            # The external ``stored_path`` must also be rejected for
            # non-email source types.
            for source_type in (None, "user", "tool", "script"):
                resolved = registry.get_attachment_path(
                    attachment_id,
                    stored_path=str(outside_file),
                    source_type=source_type,
                )
                assert resolved is None, (
                    f"source_type={source_type!r} unexpectedly resolved"
                )

    @pytest.mark.asyncio
    async def test_get_attachment_content_reads_external_file(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """get_attachment_content should read bytes from an externally registered path."""
        external_file = tmp_path / "invoice.txt"
        payload = b"line 1\nline 2\n"
        external_file.write_bytes(payload)

        with tempfile.TemporaryDirectory() as registry_dir:
            registry = AttachmentRegistry(
                storage_path=registry_dir, db_engine=db_engine, config=None
            )
            attachment_id = str(uuid.uuid4())

            async with DatabaseContext(engine=db_engine) as db_context:
                await registry.register_attachment(
                    db_context=db_context,
                    attachment_id=attachment_id,
                    source_type="email",
                    source_id="<msg@example.com>",
                    mime_type="text/plain",
                    description="Test email attachment",
                    size=len(payload),
                    storage_path=str(external_file),
                )

                content = await registry.get_attachment_content(
                    db_context, attachment_id, acting_user_id=None
                )

            assert content == payload

    @pytest.mark.asyncio
    async def test_delete_attachment_unlinks_managed_file(
        self, db_engine: AsyncEngine
    ) -> None:
        """Registry-managed uploads must still have their files unlinked."""
        with tempfile.TemporaryDirectory() as registry_dir:
            registry = AttachmentRegistry(
                storage_path=registry_dir, db_engine=db_engine, config=None
            )

            async with DatabaseContext(engine=db_engine) as db_context:
                metadata = await registry.register_user_attachment(
                    db_context=db_context,
                    content=b"user upload",
                    filename="doc.txt",
                    mime_type="text/plain",
                )

                managed_path = registry.get_attachment_path(metadata.attachment_id)
                assert managed_path is not None and managed_path.exists()

                deleted = await registry.delete_attachment(
                    db_context, metadata.attachment_id, acting_user_id=None
                )

            assert deleted is True
            assert not managed_path.exists()

    @pytest.mark.asyncio
    async def test_delete_attachment_preserves_external_file(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        """Deleting an email-attachment registry row must NOT unlink the
        externally-managed file — the email record still references it."""
        external_file = tmp_path / "keep_me.txt"
        external_file.write_bytes(b"bytes")

        with tempfile.TemporaryDirectory() as registry_dir:
            registry = AttachmentRegistry(
                storage_path=registry_dir, db_engine=db_engine, config=None
            )
            attachment_id = str(uuid.uuid4())

            async with DatabaseContext(engine=db_engine) as db_context:
                await registry.register_attachment(
                    db_context=db_context,
                    attachment_id=attachment_id,
                    source_type="email",
                    source_id="<msg@example.com>",
                    mime_type="text/plain",
                    description="Test email attachment",
                    size=5,
                    storage_path=str(external_file),
                )

                deleted = await registry.delete_attachment(
                    db_context, attachment_id, acting_user_id=None
                )

                # Registry row was removed but the external file survives.
                assert deleted is True
                assert external_file.exists()
                assert (
                    await registry.get_attachment(
                        db_context, attachment_id, acting_user_id=None
                    )
                    is None
                )
