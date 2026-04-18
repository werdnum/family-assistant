"""Functional tests for inbound Mailgun email webhook security checks."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from family_assistant.config_models import AppConfig, EmailIntakeConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table
from family_assistant.web.app_creator import app as fastapi_app

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine


def _signature(timestamp: str, token: str, signing_key: str) -> str:
    return hmac.new(
        key=signing_key.encode("utf-8"),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _mailgun_form(
    *,
    sender: str = "buyer@example.com",
    recipient: str = "orders@example.net",
    dmarc: str | None = "pass",
    spf: str | None = "pass",
    dkim: str | None = "pass",
    signing_key: str | None = "mailgun-test-key",
    signature: str | None = None,
    message_id: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    token = f"token-{uuid.uuid4().hex}"
    resolved_signature = (
        signature
        if signature is not None
        else (_signature(timestamp, token, signing_key) if signing_key else "")
    )

    form = {
        "subject": "Order confirmation",
        "stripped-text": "Your order ABC-123 is confirmed for pickup tomorrow.",
        "sender": sender,
        "recipient": recipient,
        "Message-Id": message_id or f"<mailgun-security-{uuid.uuid4()}@example.com>",
        "From": f"Buyer <{sender}>",
        "To": f"Orders <{recipient}>",
        "timestamp": timestamp,
        "token": token,
        "signature": resolved_signature,
        "message-headers": (
            f'[["From", "Buyer <{sender}>"], ["To", "Orders <{recipient}>"]]'
        ),
    }
    if dmarc is not None:
        form["dmarc"] = dmarc
    if spf is not None:
        form["SPF"] = spf
    if dkim is not None:
        form["Dkim"] = dkim
    return form


def _configure_email_intake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email_intake: EmailIntakeConfig,
) -> None:
    monkeypatch.setattr(
        fastapi_app.state,
        "config",
        AppConfig(
            attachment_storage_path=str(tmp_path / "attachments"),
            mailbox_raw_dir=str(tmp_path / "raw"),
            email_intake=email_intake,
        ),
        raising=False,
    )


async def _email_exists(engine: AsyncEngine, message_id: str) -> bool:
    async with DatabaseContext(engine=engine) as db_context:
        row = await db_context.fetch_one(
            select(received_emails_table.c.id).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
    return row is not None


def _raw_mail_files(tmp_path: Path) -> list[Path]:
    raw_dir = tmp_path / "raw"
    if not raw_dir.exists():
        return []
    return list(raw_dir.iterdir())


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_accepts_signed_authorized_authenticated_sender(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key="mailgun-test-key",
            allowed_sender_addresses=["buyer@example.com"],
            allowed_recipient_addresses=["orders@example.net"],
            require_authenticated_sender=True,
        ),
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])
    assert _raw_mail_files(tmp_path)


@pytest.mark.asyncio
async def test_mail_webhook_rejects_invalid_mailgun_signature(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(mailgun_webhook_signing_key="mailgun-test-key"),
    )
    form = _mailgun_form(signature="not-a-valid-signature")

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "Invalid Mailgun signature" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])
    assert _raw_mail_files(tmp_path) == []


@pytest.mark.asyncio
async def test_mail_webhook_rejects_policy_without_mailgun_signature_configuration(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(allowed_sender_addresses=["buyer@example.com"]),
    )
    form = _mailgun_form(signing_key=None)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "Mailgun signature verification must be configured" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])
    assert _raw_mail_files(tmp_path) == []


@pytest.mark.asyncio
async def test_mail_webhook_rejects_unlisted_sender(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key="mailgun-test-key",
            allowed_sender_addresses=["authorized@example.com"],
        ),
    )
    form = _mailgun_form(sender="attacker@example.com")

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "not authorized" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])
    assert _raw_mail_files(tmp_path) == []


@pytest.mark.asyncio
async def test_mail_webhook_rejects_dmarc_failure_even_when_spf_passes(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key="mailgun-test-key",
            require_authenticated_sender=True,
        ),
    )
    form = _mailgun_form(dmarc="fail", spf="pass", dkim="fail")

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "DMARC" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_accepts_authentication_results_from_message_headers(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key="mailgun-test-key",
            require_authenticated_sender=True,
        ),
    )
    form = _mailgun_form(dmarc=None, spf=None, dkim=None)
    form["message-headers"] = (
        '[["Authentication-Results", "mx.example.net; dmarc=pass spf=pass dkim=pass"]]'
    )

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_accepts_explicit_spf_fallback_when_dmarc_missing(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key="mailgun-test-key",
            require_authenticated_sender=True,
            require_dmarc_pass=False,
            allow_spf_or_dkim_fallback_when_dmarc_missing=True,
        ),
    )
    form = _mailgun_form(dmarc=None, spf="pass", dkim="fail")

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
async def test_mail_webhook_rejects_oversized_raw_payload(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(max_raw_request_bytes=512),
    )
    form = _mailgun_form(signing_key=None)
    form["stripped-text"] = "x" * 2048

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 413
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
async def test_mail_webhook_rejects_oversized_content_length_before_form_parsing(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(max_raw_request_bytes=8),
    )

    response = await api_client.post(
        "/webhook/mail",
        content=b"this is larger than eight bytes",
        headers={"content-type": "application/octet-stream"},
    )

    assert response.status_code == 413
    assert _raw_mail_files(tmp_path) == []


@pytest.mark.asyncio
async def test_mail_webhook_rejects_oversized_attachment(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(max_attachment_bytes=3),
    )
    form = _mailgun_form(signing_key=None)
    form["attachment-count"] = "1"

    response = await api_client.post(
        "/webhook/mail",
        data=form,
        files={
            "attachment-1": (
                "ticket.pdf",
                b"larger than three bytes",
                "application/pdf",
            )
        },
    )

    assert response.status_code == 413
    assert not await _email_exists(db_engine, form["Message-Id"])
