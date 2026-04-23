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
                attachment_id, stored_path=str(external_file)
            )

            assert resolved == external_file

    @pytest.mark.asyncio
    async def test_falls_back_to_sharded_when_stored_path_missing(
        self, db_engine: AsyncEngine
    ) -> None:
        """When the external file does not exist, fall back to sharded lookup."""
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
                attachment_id, stored_path="/nonexistent/file.bin"
            )

            assert resolved == sharded_file

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
                    db_context, attachment_id
                )

            assert content == payload

    @pytest.mark.asyncio
    async def test_delete_attachment_removes_external_file(
        self, db_engine: AsyncEngine, tmp_path: Path
    ) -> None:
        external_file = tmp_path / "remove_me.txt"
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

                deleted = await registry.delete_attachment(db_context, attachment_id)

            assert deleted is True
            assert not external_file.exists()
