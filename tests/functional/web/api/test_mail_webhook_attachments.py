"""Functional tests for email-attachment registration in AttachmentRegistry.

These tests verify that the Mailgun webhook:
1. Registers each saved attachment in ``attachment_metadata_table`` with
   ``source_type="email"``.
2. Persists the generated ``attachment_id`` back into
   ``received_emails.attachment_info``.
3. Makes attachment bytes accessible through ``get_attachment_content``.

They also verify the lazy-backfill path in
``get_full_document_content_tool`` so emails predating registry integration
get their attachment IDs registered on first read.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert, select

from family_assistant.config_models import AppConfig, EmailIntakeConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import AttachmentData, received_emails_table
from family_assistant.tools.documents import resolve_email_attachments
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
    registry: AttachmentRegistry | None,
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
    monkeypatch.setattr(
        fastapi_app.state, "attachment_registry", registry, raising=False
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_webhook_registers_email_attachment(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
    _configure_app(monkeypatch, tmp_path, registry)

    message_id = f"<mailgun-{uuid.uuid4()}@example.com>"
    form = _mailgun_form(message_id=message_id)
    form["attachment-count"] = "1"
    attachment_bytes = b"PDF bytes here"

    response = await api_client.post(
        "/webhook/mail",
        data=form,
        files={
            "attachment-1": ("ticket.pdf", attachment_bytes, "application/pdf"),
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
        stored_attachments = [
            AttachmentData.model_validate(item) for item in email_row["attachment_info"]
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
        assert registry_row["mime_type"] == "application/pdf"
        assert registry_row["size"] == len(attachment_bytes)
        assert registry_row["storage_path"] == stored_attachments[0].storage_path

        content = await registry.get_attachment_content(db_context, attachment_id)
        assert content == attachment_bytes


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_webhook_tolerates_missing_registry(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Webhook must still accept emails when no registry is configured."""
    _configure_app(monkeypatch, tmp_path, registry=None)

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

    assert response.status_code == 200

    async with DatabaseContext(engine=db_engine) as db_context:
        email_row = await db_context.fetch_one(
            select(received_emails_table.c.attachment_info).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
        assert email_row is not None
        stored_attachments = [
            AttachmentData.model_validate(item) for item in email_row["attachment_info"]
        ]
        assert stored_attachments[0].attachment_id is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_resolve_email_attachments_backfills_missing_ids(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A legacy email with no attachment_ids should get IDs assigned on read."""
    external_file = tmp_path / "invoice.txt"
    external_file.write_bytes(b"invoice body")

    registry = AttachmentRegistry(
        storage_path=str(tmp_path / "registry"),
        db_engine=db_engine,
        config=None,
    )
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
            attachment_registry=registry,
        )

        assert summary is not None and len(summary) == 1
        attachment_id = summary[0]["attachment_id"]
        assert attachment_id is not None
        assert summary[0]["filename"] == "invoice.txt"

        second = await resolve_email_attachments(
            db_context=db_context,
            message_id_header=legacy_message_id,
            attachment_registry=registry,
        )
        assert second is not None
        assert second[0]["attachment_id"] == attachment_id

        registry_row = await db_context.fetch_one(
            select(attachment_metadata_table.c.source_type).where(
                attachment_metadata_table.c.attachment_id == attachment_id
            )
        )
        assert registry_row is not None
        assert registry_row["source_type"] == "email"

        content = await registry.get_attachment_content(db_context, attachment_id)
        assert content == b"invoice body"
