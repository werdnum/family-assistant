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
from pathlib import Path
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
    get_full_document_content_tool,
    reindex_email_tool,
    resolve_email_attachments,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from family_assistant.web.app_creator import app as fastapi_app
from tests.mocks.email_auth import build_dns_for

if TYPE_CHECKING:
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
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

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

        content = await registry.get_attachment_content(
            db_context, attachment_id, acting_user_id=None
        )
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
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

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
async def test_webhook_persists_duplicate_filenames_as_distinct_parts(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two attachments on one email sharing the same filename must land
    at distinct storage paths so they don't overwrite on disk and don't
    collapse to one registry row after indexing dedup."""
    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config={
            "email_attachment_base_path": str(tmp_path / "mailbox"),
        },
    )
    monkeypatch.setattr(
        fastapi_app.state, "attachment_registry", registry, raising=False
    )
    _configure_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        fastapi_app.state, "attachment_registry", registry, raising=False
    )

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    form = _mailgun_form(message_id=message_id)
    form["attachment-count"] = "2"

    response = await api_client.post(
        "/webhook/mail",
        data=form,
        files={
            "attachment-1": ("image.png", b"first-bytes", "image/png"),
            "attachment-2": ("image.png", b"second-bytes-different", "image/png"),
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
        assert len(stored) == 2
        # Each part gets a distinct storage_path so the second write does
        # not overwrite the first. The persisted path is relative to the
        # configured mailbox base; the registry resolves it at read time.
        assert stored[0].storage_path != stored[1].storage_path
        assert not Path(stored[0].storage_path).is_absolute()
        assert not Path(stored[1].storage_path).is_absolute()

        # Indexer dedupes on identity_hash(source_id, storage_path). With
        # distinct storage paths the two parts must produce two distinct
        # registry rows, not collapse into one.
        pipeline = MagicMock(spec=IndexingPipeline)
        pipeline.run = AsyncMock(return_value=None)
        indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)
        email_db_row = await db_context.fetch_one(
            select(received_emails_table.c.id).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
        assert email_db_row is not None
        email_db_id = email_db_row["id"]
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
        assert len(registry_rows) == 2


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
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

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
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

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
async def test_reindex_email_then_get_full_document_content_populates_ids(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Happy-path end-to-end: a legacy email surfaces null attachment_ids;
    ``reindex_email`` enqueues a task; running the indexer populates the
    IDs; the next ``get_full_document_content`` call surfaces them.

    Exercises the public contract shipped to the LLM — reindex_email is
    the LLM's entry point for registering legacy attachments, and
    get_full_document_content is how they read the results.
    """
    external_file = tmp_path / "ticket.pdf"
    external_file.write_bytes(b"PDF body")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"

    async with DatabaseContext(engine=db_engine) as db_context:
        email_insert = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Legacy ticket",
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

        await db_context.execute_with_retry(
            sa_text(
                "INSERT INTO documents (source_type, source_id, title, "
                "visibility_labels) VALUES ('email', :sid, :title, '[]')"
            ),
            {"sid": message_id, "title": "Legacy ticket"},
        )
        doc_row = await db_context.fetch_one(
            sa_text(
                "SELECT id FROM documents WHERE source_type = 'email' "
                "AND source_id = :sid"
            ),
            {"sid": message_id},
        )
        assert doc_row is not None
        document_id = doc_row["id"]

        exec_context = _build_indexer_context(db_context, registry)

        # 1. Reading the email before any reindex surfaces attachment_id=null.
        pre_result = await get_full_document_content_tool(
            exec_context=exec_context, document_id=document_id
        )
        assert isinstance(pre_result, ToolResult)
        pre_data = pre_result.get_data()
        assert isinstance(pre_data, dict)
        assert pre_data["attachments"][0]["attachment_id"] is None

        # 2. reindex_email enqueues an index_email task and records it.
        reindex_result = await reindex_email_tool(
            exec_context=exec_context, document_id=document_id
        )
        reindex_data = reindex_result.get_data()
        assert isinstance(reindex_data, dict)
        assert reindex_data["status"] == "enqueued"
        assert reindex_data["email_db_id"] == email_db_id
        enqueued_task_id = reindex_data["task_id"]

        task_row = await db_context.fetch_one(
            select(
                tasks_table.c.task_id,
                tasks_table.c.task_type,
                tasks_table.c.status,
            ).where(tasks_table.c.task_id == enqueued_task_id)
        )
        assert task_row is not None
        assert task_row["task_type"] == "index_email"
        assert task_row["status"] == "pending"

        email_row = await db_context.fetch_one(
            select(received_emails_table.c.indexing_task_id).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert email_row is not None
        assert email_row["indexing_task_id"] == enqueued_task_id

        # 3. Simulate the task worker by running the indexer directly.
        await indexer.handle_index_email(
            exec_context=exec_context,
            payload={"email_db_id": email_db_id},
        )

        # 4. Reading the email again surfaces the registered attachment_id.
        post_result = await get_full_document_content_tool(
            exec_context=exec_context, document_id=document_id
        )
        assert isinstance(post_result, ToolResult)
        post_data = post_result.get_data()
        assert isinstance(post_data, dict)
        populated_id = post_data["attachments"][0]["attachment_id"]
        assert populated_id is not None

        # The populated ID resolves back to the external mailbox file.
        content = await registry.get_attachment_content(
            db_context, populated_id, acting_user_id=None
        )
        assert content == b"PDF body"


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
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

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

        await registry.delete_attachment(db_context, attachment_id, acting_user_id=None)

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


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_clears_stale_attachment_id_when_file_missing(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """If a previously-registered attachment's file is gone, the indexer
    must clear the stale ``attachment_id`` from
    ``received_emails.attachment_info`` — otherwise
    ``get_full_document_content`` keeps surfacing a handle that 404s on
    every download/read_text_attachment call.
    """
    external_file = tmp_path / "gone.pdf"
    external_file.write_bytes(b"PDF bytes")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Missing file",
                stripped_text="body",
                attachment_info=[
                    {
                        "filename": "gone.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        # First run: register the attachment while the file exists.
        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(exec_context, {"email_db_id": email_db_id})

        first_pass = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert first_pass is not None
        first_atts = [
            AttachmentData.model_validate(item)
            for item in first_pass["attachment_info"]
        ]
        stale_id = first_atts[0].attachment_id
        assert stale_id is not None

        # Simulate the file disappearing between reindex runs, then re-run.
        external_file.unlink()
        await indexer.handle_index_email(exec_context, {"email_db_id": email_db_id})

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


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_reindex_email_tool_syncs_indexing_task_id_when_already_in_flight(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """When an ``index_email_*`` task is already pending (e.g. queued by
    the backfill migration without touching ``received_emails.indexing_task_id``),
    ``reindex_email`` must sync the email row's ``indexing_task_id`` to
    the in-flight task rather than leaving it NULL/stale.
    """
    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        email_insert = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Backfilled",
                stripped_text="Body",
                attachment_info=[],
                indexing_task_id=None,
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = email_insert.scalar_one()

        doc_row = await db_context.execute_with_retry(
            sa_text(
                "INSERT INTO documents (source_type, source_id, visibility_labels) "
                "VALUES ('email', :sid, '[]') RETURNING id"
            ),
            {"sid": message_id},
        )
        document_id = doc_row.scalar_one()

        # Backfill-style direct enqueue: task row created, email row not updated.
        in_flight_task_id = f"index_email_{email_db_id}_{uuid.uuid4()}"
        await db_context.tasks.enqueue(
            task_id=in_flight_task_id,
            task_type="index_email",
            payload={"email_db_id": email_db_id},
        )

        registry = AttachmentRegistry(
            storage_path=str(tmp_path / "registry"),
            db_engine=db_engine,
            config=None,
        )
        exec_context = _build_indexer_context(db_context, registry)

        result = await reindex_email_tool(
            exec_context=exec_context, document_id=document_id
        )

        data = result.get_data()
        assert isinstance(data, dict)
        assert data.get("status") == "already_in_flight"
        assert data.get("task_id") == in_flight_task_id

        refreshed = await db_context.fetch_one(
            select(received_emails_table.c.indexing_task_id).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert refreshed is not None
        assert refreshed["indexing_task_id"] == in_flight_task_id


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_preserves_malformed_sibling_attachment_info(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A malformed sibling entry in ``attachment_info`` must round-trip
    through the indexer's write-back instead of being silently dropped.
    The indexer registers the valid attachment and rewrites
    ``attachment_info``; the malformed raw dict should still be there
    afterwards so we don't destroy data we can't regenerate.
    """
    good_file = tmp_path / "valid.pdf"
    good_file.write_bytes(b"PDF bytes")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

    malformed_entry = {"filename": "legacy-no-path.bin", "size": 123}
    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Mixed",
                stripped_text="body",
                attachment_info=[
                    malformed_entry,
                    {
                        "filename": "valid.pdf",
                        "content_type": "application/pdf",
                        "size": good_file.stat().st_size,
                        "storage_path": str(good_file),
                    },
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(exec_context, {"email_db_id": email_db_id})

        refreshed = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert refreshed is not None
        entries = refreshed["attachment_info"]
        assert len(entries) == 2
        # Malformed entry is preserved verbatim.
        assert entries[0] == malformed_entry
        # Valid sibling now has an attachment_id populated.
        assert entries[1]["filename"] == "valid.pdf"
        assert entries[1].get("attachment_id")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_delete_email_attachment_tolerates_malformed_sibling(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """``_clear_email_attachment_id`` must not fail (or drop siblings)
    when another ``attachment_info`` entry is malformed. Otherwise the
    registry delete would succeed and then raise a 500 while trying to
    clean the email row, leaving a stale id advertised to callers.
    """
    external_file = tmp_path / "to-delete.pdf"
    external_file.write_bytes(b"PDF bytes")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        attachment_id = str(uuid.uuid4())
        await registry.register_attachment(
            db_context=db_context,
            attachment_id=attachment_id,
            source_type="email",
            source_id=message_id,
            mime_type="application/pdf",
            description="Email attachment: to-delete.pdf",
            size=external_file.stat().st_size,
            storage_path=str(external_file),
        )
        malformed_entry = {"filename": "legacy-no-path.bin", "size": 42}
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Mixed delete",
                stripped_text="body",
                attachment_info=[
                    malformed_entry,
                    {
                        "filename": "to-delete.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                        "attachment_id": attachment_id,
                    },
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        deleted = await registry.delete_attachment(
            db_context, attachment_id, acting_user_id=None
        )
        assert deleted is True

        refreshed = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert refreshed is not None
        entries = refreshed["attachment_info"]
        assert len(entries) == 2
        assert entries[0] == malformed_entry  # raw preserved
        assert entries[1]["filename"] == "to-delete.pdf"
        assert entries[1].get("attachment_id") is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_webhook_rejects_request_when_app_config_is_missing(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``app.state.config`` is absent, the webhook must fail loudly.

    Silently accepting the request under default values would bypass
    Mailgun signature verification (no signing key configured) and
    persist attachments to a fallback directory the runtime registry
    doesn't know about, so any saved attachment would become unreadable.
    Reject with 503 instead.
    """
    # Deliberately skip ``_configure_app`` so ``app.state.config`` is unset.
    monkeypatch.delattr(fastapi_app.state, "config", raising=False)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    form = _mailgun_form(message_id=message_id)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 503, response.text
    assert "config" in response.text.lower()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_webhook_accepts_email_when_mailbox_raw_dir_is_unset(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``mailbox_raw_dir`` is optional — it drives only raw-request
    archiving for debug/replay. Deployments that don't set it must still
    be able to receive email; the webhook should skip the archive step
    instead of rejecting the request.
    """
    # Configure the app with mailbox_raw_dir deliberately unset.
    monkeypatch.setattr(
        fastapi_app.state,
        "config",
        AppConfig(
            attachment_storage_path=str(tmp_path / "mailbox"),
            mailbox_raw_dir=None,
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

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    form = _mailgun_form(message_id=message_id)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    # The email still lands in the database — only the raw-archive step
    # was skipped.
    async with DatabaseContext(engine=db_engine) as db_context:
        row = await db_context.fetch_one(
            select(received_emails_table.c.message_id_header).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
        assert row is not None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_reindex_email_tool_does_not_match_similar_email_ids(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """``reindex_email`` must not be fooled by the SQL LIKE wildcards in
    ``task_id.startswith("index_email_{email_db_id}_")``: without
    ``autoescape``, the prefix for email ``1`` also matches tasks for
    emails ``12`` / ``100`` / etc., so ``already_in_flight`` would
    report another email's task and overwrite ``indexing_task_id`` on
    the wrong row.
    """
    async with DatabaseContext(engine=db_engine) as db_context:
        # Target email: id=1 (no in-flight task of its own).
        target_id_insert = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=f"<target-{uuid.uuid4()}@example.com>",
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Target",
                stripped_text="body",
                attachment_info=[],
                indexing_task_id=None,
            )
            .returning(received_emails_table.c.id)
        )
        target_email_id = target_id_insert.scalar_one()

        # Sibling task for an email with a related numeric id (the LIKE
        # predicate ``index_email_{id}_%`` would otherwise match this).
        sibling_id = int(f"{target_email_id}9")
        sibling_task_id = f"index_email_{sibling_id}_{uuid.uuid4()}"
        await db_context.tasks.enqueue(
            task_id=sibling_task_id,
            task_type="index_email",
            payload={"email_db_id": sibling_id},
        )

        # Register a document row for the target email so
        # ``reindex_email_tool`` can resolve it.
        target_row = await db_context.fetch_one(
            select(received_emails_table.c.message_id_header).where(
                received_emails_table.c.id == target_email_id
            )
        )
        assert target_row is not None
        message_id_header = target_row["message_id_header"]
        doc_row = await db_context.execute_with_retry(
            sa_text(
                "INSERT INTO documents (source_type, source_id, visibility_labels) "
                "VALUES ('email', :sid, '[]') RETURNING id"
            ),
            {"sid": message_id_header},
        )
        target_document_id = doc_row.scalar_one()

        registry = AttachmentRegistry(
            storage_path=str(tmp_path / "registry"),
            db_engine=db_engine,
            config=None,
        )
        exec_context = _build_indexer_context(db_context, registry)

        result = await reindex_email_tool(
            exec_context=exec_context, document_id=target_document_id
        )

        data = result.get_data()
        assert isinstance(data, dict)
        # We must *not* be told the sibling's task is in flight for us.
        assert data.get("status") != "already_in_flight"
        assert data.get("task_id") != sibling_task_id
        # A fresh reindex task was enqueued for our own email.
        fresh = await db_context.fetch_one(
            select(tasks_table.c.task_id).where(
                tasks_table.c.task_id == data["task_id"]
            )
        )
        assert fresh is not None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_webhook_accepts_attachment_free_email_without_attachment_storage_path(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``attachment_storage_path`` is only needed for emails with
    attachments. An attachment-free email must still be accepted even
    when the config field is empty — the check is deferred into the
    attachment loop and gated on ``attachment-count > 0``.
    """
    monkeypatch.setattr(
        fastapi_app.state,
        "config",
        AppConfig(
            attachment_storage_path="",
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

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    form = _mailgun_form(message_id=message_id)
    # No attachment-count set → attachment-free email.

    response = await api_client.post("/webhook/mail", data=form)
    assert response.status_code == 200, response.text

    async with DatabaseContext(engine=db_engine) as db_context:
        row = await db_context.fetch_one(
            select(received_emails_table.c.message_id_header).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
        assert row is not None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_email_indexer_reregisters_when_stored_id_is_dangling(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """If the email row carries an ``attachment_id`` whose registry row
    has since been deleted (or never existed due to a partial write-back
    failure), reindexing must detect that the id is dangling and
    re-register instead of trusting it and surfacing a handle that
    perpetually 404s via ``read_text_attachment`` /
    ``/api/attachments/{id}``.
    """
    external_file = tmp_path / "doc.pdf"
    external_file.write_bytes(b"PDF bytes")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    pipeline = MagicMock(spec=IndexingPipeline)
    pipeline.run = AsyncMock(return_value=None)
    indexer = EmailIndexer(pipeline=pipeline, attachment_registry=registry)

    dangling_id = str(uuid.uuid4())
    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    async with DatabaseContext(engine=db_engine) as db_context:
        insert_result = await db_context.execute_with_retry(
            insert(received_emails_table)
            .values(
                message_id_header=message_id,
                sender_address=SENDER,
                recipient_address=RECIPIENT,
                subject="Dangling",
                stripped_text="body",
                attachment_info=[
                    {
                        "filename": "doc.pdf",
                        "content_type": "application/pdf",
                        "size": external_file.stat().st_size,
                        "storage_path": str(external_file),
                        "attachment_id": dangling_id,
                    }
                ],
            )
            .returning(received_emails_table.c.id)
        )
        email_db_id = insert_result.scalar_one()

        exec_context = _build_indexer_context(db_context, registry)
        await indexer.handle_index_email(exec_context, {"email_db_id": email_db_id})

        refreshed = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.id == email_db_id
            )
        )
        assert refreshed is not None
        attachments = [
            AttachmentData.model_validate(item) for item in refreshed["attachment_info"]
        ]
        new_id = attachments[0].attachment_id
        assert new_id is not None
        # Row was replaced, not retained.
        assert new_id != dangling_id
        # The new id resolves in the registry.
        assert (
            await registry.get_attachment(db_context, new_id, acting_user_id=None)
            is not None
        )
