"""Functional tests for email-attachment registration in AttachmentRegistry.

These tests verify that:

1. The Mailgun webhook accepts emails with attachments (no direct registration).
2. ``EmailIndexer.handle_index_email`` registers each attachment in
   ``attachment_metadata_table`` with ``source_type="email"`` and persists the
   generated ``attachment_id`` back into ``received_emails.attachment_info``.
3. ``resolve_email_attachments`` is strictly read-only and simply surfaces
   whatever IDs are already stored on the email row.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select
from sqlalchemy import text as sa_text

from family_assistant.config_models import AppConfig, EmailIntakeConfig
from family_assistant.indexing.email_indexer import EmailIndexer
from family_assistant.indexing.pipeline import IndexingPipeline
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import AttachmentData, received_emails_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.tools.documents import (
    reindex_email_tool,
    resolve_email_attachments,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.web.app_creator import app as fastapi_app
from tests.mocks.email_auth import build_dns_for

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine

SIGNING_KEY = "mailgun-test-key"
SENDER = "buyer@example.com"
RECIPIENT = "orders@example.net"
SENDER_DOMAIN = "example.com"


def _signature(timestamp: str, token: str, signing_key: str) -> str:
    return hmac.new(
        key=signing_key.encode("utf-8"),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _mailgun_form(*, message_id: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    token = f"token-{uuid.uuid4().hex}"
    return {
        "subject": "Ticket",
        "stripped-text": "Please find your ticket attached.",
        "sender": SENDER,
        "recipient": RECIPIENT,
        "Message-Id": message_id,
        "From": f"Buyer <{SENDER}>",
        "To": f"Orders <{RECIPIENT}>",
        "timestamp": timestamp,
        "token": token,
        "signature": _signature(timestamp, token, SIGNING_KEY),
        "message-headers": (
            f'[["From", "Buyer <{SENDER}>"], ["To", "Orders <{RECIPIENT}>"]]'
        ),
    }


def _configure_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        fastapi_app.state,
        "config",
        AppConfig(
            attachment_storage_path=str(tmp_path / "mailbox"),
            mailbox_raw_dir=str(tmp_path / "raw"),
            email_intake=EmailIntakeConfig(
                mailgun_webhook_signing_key=SIGNING_KEY,
                allowed_sender_addresses=[SENDER],
                allowed_recipient_addresses=[RECIPIENT],
                require_authenticated_sender=False,
            ),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "email_intake_dns_resolver",
        build_dns_for(domain=SENDER_DOMAIN),
        raising=False,
    )


def _build_indexer_context(
    db_context: DatabaseContext,
    attachment_registry: AttachmentRegistry,
) -> ToolExecutionContext:
    """Minimal ToolExecutionContext suitable for driving EmailIndexer in tests."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test-conversation",
        user_name="test-user",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_registers_email_attachment(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """The indexer registers attachments and writes IDs back to the email row."""
    external_file = tmp_path / "ticket.pdf"
    external_file.write_bytes(b"PDF bytes here")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"

    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Ticket",
                stripped_text="Body",
                attachment_info=[
                    {
                        "filename": "ticket.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )

        updated_row = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert updated_row is not None
        stored_attachments = [
            AttachmentData.model_validate(item)
            for item in updated_row["attachment_info"]
        ]
        assert len(stored_attachments) == 1
        attachment_id = stored_attachments[0].attachment_id
        assert attachment_id is not None

        registry_row = await db_context.fetch_one(
            select(
                attachment_metadata_table.c.source_type,
                attachment_metadata_table.c.source_id,
                attachment_metadata_table.c.storage_path,
                attachment_metadata_table.c.mime_type,
                attachment_metadata_table.c.size,
            ).where(attachment_metadata_table.c.attachment_id == attachment_id)
        )
        assert registry_row is not None
        assert registry_row["source_type"] == "email"
        assert registry_row["source_id"] == message_id
        assert registry_row["storage_path"] == str(external_file)
        assert registry_row["mime_type"] == "application/pdf"

        content = await registry.get_attachment_content(db_context, attachment_id)
        assert content == b"PDF bytes here"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_registration_is_idempotent(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Running the indexer twice does not produce duplicate registry rows."""
    external_file = tmp_path / "invoice.txt"
    external_file.write_bytes(b"bytes")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"

    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Invoice",
                stripped_text="Body",
                attachment_info=[
                    {
                        "filename": "invoice.txt",
                        "content_type": "text/plain",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )

        registry_rows = await db_context.fetch_all(
            select(attachment_metadata_table.c.attachment_id).where(
                attachment_metadata_table.c.source_id == message_id
            )
        )
        assert len(registry_rows) == 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_webhook_accepts_email_with_attachment(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The webhook persists the email; registration happens later in the indexer."""
    _configure_app(monkeypatch, tmp_path)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    form = _mailgun_form(message_id=message_id)
    form["attachment-count"] = "1"

    response = await api_client.post(
        "/webhook/mail",
        data=form,
        files={
            "attachment-1": ("ticket.pdf", b"PDF", "application/pdf"),
        },
    )

    assert response.status_code == 200, response.text

    async with DatabaseContext(engine=db_engine) as db_context:
        email_row = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
        assert email_row is not None
        stored = [
            AttachmentData.model_validate(item) for item in email_row["attachment_info"]
        ]
        # Webhook stores the file; indexing (which runs separately) assigns the ID.
        assert stored[0].storage_path.endswith("ticket.pdf")
        assert stored[0].attachment_id is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolve_email_attachments_is_read_only(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """``resolve_email_attachments`` must not write to the database.

    A legacy email without attachment_ids stays in that state; the helper
    merely surfaces what is stored. Registration only happens in the indexer.
    """
    external_file = tmp_path / "invoice.txt"
    external_file.write_bytes(b"invoice body")

    legacy_message_id = f"<legacy-{uuid.uuid4()}@example.com>"

    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.execute_with_retry(
            insert(received_emails_table).values(
                message_id_header=legacy_message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Legacy",
                attachment_info=[
                    {
                        "filename": "invoice.txt",
                        "content_type": "text/plain",
                        "size": len(b"invoice body"),
                        "storage_path": str(external_file),
                    }
                ],
            )
        )

        summary = await resolve_email_attachments(
            db_context=db_context,
            message_id_header=legacy_message_id,
        )

        assert summary is not None and len(summary) == 1
        # ID is None because the indexer has not run for this email.
        assert summary[0]["attachment_id"] is None
        assert summary[0]["filename"] == "invoice.txt"

        # No registry rows should exist for this message id.
        registry_rows = await db_context.fetch_all(
            select(attachment_metadata_table.c.attachment_id).where(
                attachment_metadata_table.c.source_id == legacy_message_id
            )
        )
        assert registry_rows == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_dedups_on_retry(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """If a registry row already exists for (source_id, storage_path), the
    indexer reuses its attachment_id instead of creating a duplicate. This
    guards against the retry-after-partial-failure scenario where
    ``register_attachment`` succeeds but the final update to
    ``received_emails.attachment_info`` fails.
    """
    external_file = tmp_path / "ticket.pdf"
    external_file.write_bytes(b"PDF")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"

    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Ticket",
                stripped_text="Body",
                attachment_info=[
                    {
                        "filename": "ticket.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        # Simulate the partial-failure case by pre-registering a row without
        # writing the id back to received_emails.attachment_info.
        orphan_id = str(uuid.uuid4())
        await registry.register_attachment(
            db_context=db_context,
            attachment_id=orphan_id,
            source_type="email",
            source_id=message_id,
            mime_type="application/pdf",
            description="orphan",
            size=external_file.stat().st_size,
            storage_path=str(external_file),
        )

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )

        registry_rows = await db_context.fetch_all(
            select(attachment_metadata_table.c.attachment_id).where(
                attachment_metadata_table.c.source_id == message_id
            )
        )
        assert len(registry_rows) == 1
        assert registry_rows[0]["attachment_id"] == orphan_id

        updated_row = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert updated_row is not None
        stored = [
            AttachmentData.model_validate(item)
            for item in updated_row["attachment_info"]
        ]
        assert stored[0].attachment_id == orphan_id


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_applies_chunk_index_offset_per_attachment(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """An email with multiple attachments must produce disjoint
    ``chunk_index`` ranges per attachment so content_chunk rows for
    different attachments don't collide on the
    (document_id, chunk_index, embedding_type) unique constraint.
    """
    first_pdf = tmp_path / "first.pdf"
    first_pdf.write_bytes(b"first")
    second_pdf = tmp_path / "second.pdf"
    second_pdf.write_bytes(b"second")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Two attachments",
                stripped_text="Body",
                attachment_info=[
                    {
                        "filename": "first.pdf",
                        "content_type": "application/pdf",
                        "size": first_pdf.stat().st_size,
                        "storage_path": str(first_pdf),
                    },
                    {
                        "filename": "second.pdf",
                        "content_type": "application/pdf",
                        "size": second_pdf.stat().st_size,
                        "storage_path": str(second_pdf),
                    },
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )

    pipeline.run.assert_called_once()
    pipeline_kwargs = pipeline.run.call_args.kwargs
    initial_items = pipeline_kwargs["initial_items"]
    attachment_items = [
        item for item in initial_items if item.embedding_type == "email_attachment_file"
    ]
    assert len(attachment_items) == 2
    offsets = [item.metadata["chunk_index_offset"] for item in attachment_items]
    assert offsets[0] != offsets[1]
    # Offsets must be large enough that no realistic per-attachment chunk
    # count could bridge them.
    assert abs(offsets[0] - offsets[1]) >= 1_000_000


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_reindex_email_tool_respects_visibility_grants(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """``reindex_email_tool`` must treat docs the caller can't see as
    "not found" — otherwise callers can distinguish hidden document IDs
    and trigger indexing work for emails they shouldn't access.
    """
    external_file = tmp_path / "ticket.pdf"
    external_file.write_bytes(b"PDF")

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        email_insert = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Hidden",
                stripped_text="Body",
                attachment_info=[
                    {
                        "filename": "ticket.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = email_insert.scalar_one()

        # Insert a document row with a restricted visibility label.
        doc_row = await db_context.execute_with_retry(
            sa_text(
                "INSERT INTO documents "
                "(source_type, source_id, visibility_labels) "
                "VALUES ('email', :sid, '[\"secret\"]') RETURNING id"
            ),
            {"sid": message_id},
        )
        document_id = doc_row.scalar_one()

        registry = AttachmentRegistry(
            storage_path=str(tmp_path / "registry"),
            db_engine=db_engine,
            config=None,
        )
        exec_context = _build_indexer_context(db_context, registry)
        # Caller lacks the 'secret' visibility label.
        exec_context.visibility_grants = {"public"}

        result = await reindex_email_tool(
            exec_context=exec_context, document_id=document_id
        )

        data = result.get_data()
        assert isinstance(data, dict)
        assert data.get("error") == f"Document {document_id} not found"

        # Also confirm no reindex task was enqueued.
        task_prefix = f"index_email_{email_db_id}_"
        existing = await db_context.fetch_all(
            select(tasks_table.c.task_id).where(
                tasks_table.c.task_id.startswith(task_prefix)
            )
        )
        assert existing == []


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_delete_email_attachment_clears_email_row(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Deleting an email attachment's registry row must clear its
    ``attachment_id`` from ``received_emails.attachment_info`` so the next
    read doesn't surface a broken handle."""
    external_file = tmp_path / "ticket.pdf"
    external_file.write_bytes(b"PDF")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"

    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Ticket",
                stripped_text="Body",
                attachment_info=[
                    {
                        "filename": "ticket.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )

        # Grab the attachment_id produced by the indexer, then delete it.
        row = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert row is not None
        attachments = [
            AttachmentData.model_validate(item) for item in row["attachment_info"]
        ]
        attachment_id = attachments[0].attachment_id
        assert attachment_id is not None

        await registry.delete_attachment(db_context, attachment_id)

        # External file is preserved (owned by the email record), but the
        # ``attachment_id`` entry in received_emails is cleared.
        assert external_file.exists()
        refreshed = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert refreshed is not None
        refreshed_atts = [
            AttachmentData.model_validate(item) for item in refreshed["attachment_info"]
        ]
        assert refreshed_atts[0].attachment_id is None
