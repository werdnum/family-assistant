"""Functional tests for inbound Mailgun email webhook security checks.

Authentication (DKIM/SPF/DMARC) is evaluated locally against the raw MIME that Mailgun
forwards in the ``body-mime`` field. A fake in-memory DNS resolver is injected via
``app.state.email_intake_dns_resolver`` so the tests do not rely on real DNS.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from authheaders import SPFAuthenticationResult
from sqlalchemy import select

from family_assistant.config_models import (
    AppConfig,
    EmailIntakeConfig,
    EmailIntakeUserMapping,
)
from family_assistant.email_intake.actions import EMAIL_INTAKE_ACTION_TASK_TYPE
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.email import received_emails_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.web.app_creator import app as fastapi_app
from tests.mocks.email_auth import (
    FakeDnsResolver,
    build_dns_for,
    build_signed_message,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine


SIGNING_KEY = "mailgun-test-key"
SENDER = "buyer@example.com"
RECIPIENT = "orders@example.net"
SENDER_DOMAIN = "example.com"
MAILGUN_MIME_FORWARDING_FIXTURE = (
    Path(__file__).parents[3]
    / "fixtures"
    / "email_intake"
    / "mailgun_mime_forwarding_request.json"
)


def _signature(timestamp: str, token: str, signing_key: str) -> str:
    return hmac.new(
        key=signing_key.encode("utf-8"),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _mailgun_form(
    *,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
    signing_key: str | None = SIGNING_KEY,
    signature: str | None = None,
    message_id: str | None = None,
    include_body_mime: bool = True,
    raw_mime: bytes | None = None,
    subject: str = "Order confirmation",
    from_header: str | None = None,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    token = f"token-{uuid.uuid4().hex}"
    resolved_signature = (
        signature
        if signature is not None
        else (_signature(timestamp, token, signing_key) if signing_key else "")
    )

    resolved_message_id = message_id or f"<mailgun-security-{uuid.uuid4()}@example.com>"
    resolved_from = from_header or f"Buyer <{sender}>"

    form = {
        "subject": subject,
        "stripped-text": "Your order ABC-123 is confirmed for pickup tomorrow.",
        "sender": sender,
        "recipient": recipient,
        "Message-Id": resolved_message_id,
        "From": resolved_from,
        "To": f"Orders <{recipient}>",
        "timestamp": timestamp,
        "token": token,
        "signature": resolved_signature,
        "message-headers": (
            f'[["From", "{resolved_from}"], ["To", "Orders <{recipient}>"]]'
        ),
    }
    if include_body_mime:
        mime = (
            raw_mime
            if raw_mime is not None
            else build_signed_message(
                from_address=sender,
                to_address=recipient,
                subject=subject,
                message_id=resolved_message_id,
            )
        )
        form["body-mime"] = mime.decode("utf-8", errors="surrogateescape")
    return form


def _mailgun_mime_forwarding_fixture_form() -> dict[str, str]:
    request_payload = json.loads(MAILGUN_MIME_FORWARDING_FIXTURE.read_text())
    body = request_payload["body"]
    assert isinstance(body, dict)
    return {key: value for key, value in body.items() if isinstance(value, str)}


def _configure_email_intake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email_intake: EmailIntakeConfig,
    *,
    dns_resolver: FakeDnsResolver | None = None,
) -> None:
    config = AppConfig(
        attachment_storage_path=str(tmp_path / "attachments"),
        mailbox_raw_dir=str(tmp_path / "raw"),
        email_intake=email_intake,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "config",
        config,
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "user_identity_resolver",
        UserIdentityResolver(config),
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "email_intake_dns_resolver",
        dns_resolver
        if dns_resolver is not None
        else build_dns_for(domain=SENDER_DOMAIN),
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


async def _email_target_user_id(engine: AsyncEngine, message_id: str) -> str | None:
    async with DatabaseContext(engine=engine) as db_context:
        row = await db_context.fetch_one(
            select(received_emails_table.c.target_user_id).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
    return row["target_user_id"] if row else None


async def _email_dmarc_result(engine: AsyncEngine, message_id: str) -> str | None:
    async with DatabaseContext(engine=engine) as db_context:
        row = await db_context.fetch_one(
            select(received_emails_table.c.dmarc_result).where(
                received_emails_table.c.message_id_header == message_id
            )
        )
    return row["dmarc_result"] if row else None


async def _email_body_fields(
    engine: AsyncEngine, message_id: str
) -> tuple[str | None, str | None, str | None, str | None]:
    async with DatabaseContext(engine=engine) as db_context:
        row = await db_context.fetch_one(
            select(
                received_emails_table.c.body_plain,
                received_emails_table.c.body_html,
                received_emails_table.c.stripped_text,
                received_emails_table.c.stripped_html,
            ).where(received_emails_table.c.message_id_header == message_id)
        )
    if row is None:
        return None, None, None, None
    return (
        row["body_plain"],
        row["body_html"],
        row["stripped_text"],
        row["stripped_html"],
    )


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
            mailgun_webhook_signing_key=SIGNING_KEY,
            allowed_sender_addresses=[SENDER],
            allowed_recipient_addresses=[RECIPIENT],
            require_authenticated_sender=True,
            require_user_mapping=True,
            user_mappings=[
                EmailIntakeUserMapping(
                    user_id="alice",
                    sender_addresses={SENDER},
                )
            ],
        ),
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])
    assert await _email_target_user_id(db_engine, form["Message-Id"]) == "alice"
    assert await _email_dmarc_result(db_engine, form["Message-Id"]) == "pass"
    assert _raw_mail_files(tmp_path)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_enqueues_action_task_for_mapped_email_when_enabled(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            allowed_sender_addresses=[SENDER],
            allowed_recipient_addresses=[RECIPIENT],
            require_authenticated_sender=True,
            require_user_mapping=True,
            enable_actions=True,
            user_mappings=[
                EmailIntakeUserMapping(
                    user_id="alice",
                    sender_addresses={SENDER},
                )
            ],
        ),
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    async with DatabaseContext(engine=db_engine) as db_context:
        email_row = await db_context.fetch_one(
            select(received_emails_table.c.id).where(
                received_emails_table.c.message_id_header == form["Message-Id"]
            )
        )
        assert email_row is not None
        task_row = await db_context.fetch_one(
            select(tasks_table.c.task_type, tasks_table.c.payload).where(
                tasks_table.c.task_id == f"email_intake_action_{email_row['id']}"
            )
        )

    assert task_row is not None
    assert task_row["task_type"] == EMAIL_INTAKE_ACTION_TASK_TYPE
    assert task_row["payload"]["email_db_id"] == email_row["id"]
    assert task_row["payload"]["conversation_id"] == f"email:{email_row['id']}"
    assert task_row["payload"]["user_name"] == "alice"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_maps_target_user_from_unified_users_config(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    email_intake = EmailIntakeConfig(
        mailgun_webhook_signing_key=SIGNING_KEY,
        allowed_sender_addresses=[SENDER],
        allowed_recipient_addresses=[RECIPIENT],
        require_authenticated_sender=True,
        require_user_mapping=True,
    )
    config = AppConfig.model_validate({
        "attachment_storage_path": str(tmp_path / "attachments"),
        "mailbox_raw_dir": str(tmp_path / "raw"),
        "email_intake": email_intake.model_dump(),
        "users": [
            {
                "id": "andrew@example.com",
                "oidc": {"emails": ["andrew@example.com"]},
                "email_intake": {
                    "sender_addresses": [SENDER],
                    "recipient_addresses": [RECIPIENT],
                },
            }
        ],
    })
    monkeypatch.setattr(fastapi_app.state, "config", config, raising=False)
    monkeypatch.setattr(
        fastapi_app.state,
        "user_identity_resolver",
        UserIdentityResolver(config),
        raising=False,
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "email_intake_dns_resolver",
        build_dns_for(domain=SENDER_DOMAIN),
        raising=False,
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_target_user_id(db_engine, form["Message-Id"]) == (
        "andrew@example.com"
    )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_mime_alias_accepts_same_payload(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The ``/webhook/mail/mime`` alias exists so Mailgun forwards raw MIME.

    Mailgun only populates the ``body-mime`` form field when the destination URL
    path ends in ``mime`` or ``raw-mime``. We expose the same handler at both
    ``/webhook/mail`` and ``/webhook/mail/mime`` so operators can migrate by
    swapping the Destination in Mailgun without changing the legacy path.
    """
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            allowed_sender_addresses=[SENDER],
            allowed_recipient_addresses=[RECIPIENT],
            require_authenticated_sender=True,
        ),
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail/mime", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])
    assert await _email_dmarc_result(db_engine, form["Message-Id"]) == "pass"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_mime_alias_extracts_missing_body_fields_from_raw_mime(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    form = _mailgun_mime_forwarding_fixture_form()
    assert "body-mime" in form
    assert "body-plain" not in form
    assert "body-html" not in form
    assert "stripped-text" not in form
    assert "stripped-html" not in form

    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            allowed_sender_addresses=[form["sender"]],
        ),
    )
    form["Message-Id"] = f"<mailgun-mime-body-{uuid.uuid4()}@example.com>"
    form["timestamp"] = str(int(time.time()))
    form["token"] = f"token-{uuid.uuid4().hex}"
    form["signature"] = _signature(form["timestamp"], form["token"], SIGNING_KEY)

    response = await api_client.post("/webhook/mail/mime", data=form)

    assert response.status_code == 200, response.text
    message_id = form["Message-Id"]
    body_plain, body_html, stripped_text, stripped_html = await _email_body_fields(
        db_engine, message_id
    )
    assert body_plain is not None
    assert body_plain.startswith("Hi Alice,\n\nThis is Bob.")
    assert body_html is not None
    assert "<html>" in body_html
    assert "This is Bob." in body_html
    assert stripped_text is not None
    assert stripped_text == body_plain
    assert stripped_html is not None
    assert stripped_html == body_html


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
        EmailIntakeConfig(mailgun_webhook_signing_key=SIGNING_KEY),
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
        EmailIntakeConfig(allowed_sender_addresses=[SENDER]),
    )
    form = _mailgun_form(signing_key=None)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "Mailgun signature verification must be configured" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])
    assert _raw_mail_files(tmp_path) == []


@pytest.mark.asyncio
async def test_mail_webhook_rejects_user_mapping_without_mailgun_signature_configuration(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            require_user_mapping=True,
            user_mappings=[
                EmailIntakeUserMapping(
                    user_id="alice",
                    sender_addresses={SENDER},
                )
            ],
        ),
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
            mailgun_webhook_signing_key=SIGNING_KEY,
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
@pytest.mark.postgres
async def test_mail_webhook_maps_recipient_alias_to_target_user(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            allowed_sender_addresses=[SENDER],
            allowed_recipient_addresses=["assistant+alice@mg.example.com"],
            require_user_mapping=True,
            user_mappings=[
                EmailIntakeUserMapping(
                    user_id="alice",
                    recipient_addresses={"assistant+alice@mg.example.com"},
                )
            ],
        ),
    )
    form = _mailgun_form(recipient="assistant+alice@mg.example.com")

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_target_user_id(db_engine, form["Message-Id"]) == "alice"


@pytest.mark.asyncio
async def test_mail_webhook_rejects_unmapped_user_when_mapping_required(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_user_mapping=True,
            user_mappings=[
                EmailIntakeUserMapping(
                    user_id="alice",
                    sender_addresses={"alice@example.com"},
                )
            ],
        ),
    )
    form = _mailgun_form(sender=SENDER)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "does not map to a configured user" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
async def test_mail_webhook_rejects_ambiguous_user_mapping(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_user_mapping=True,
            user_mappings=[
                EmailIntakeUserMapping(
                    user_id="alice",
                    sender_addresses={SENDER},
                ),
                EmailIntakeUserMapping(
                    user_id="bob",
                    recipient_addresses={"assistant+bob@mg.example.com"},
                ),
            ],
        ),
    )
    form = _mailgun_form(recipient="assistant+bob@mg.example.com")

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "maps to multiple users" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_passes_dmarc_via_spf_alignment(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Covers the SPF-only DMARC alignment path.

    A message without DKIM still passes DMARC when SPF passes and the envelope
    sender domain aligns with the From: domain. We need ``extract_client_ip`` to
    pull the trusted IP from the Mailgun-added ``X-Mailgun-Sending-Ip`` header
    and feed it into SPF, then ``check_dmarc`` must propagate the aligned SPF
    pass to the overall DMARC result. Previously this path had no coverage
    because every test message omitted ``X-Mailgun-Sending-Ip``/``Received:``,
    so ``client_ip`` was always ``None`` and SPF evaluation was skipped.
    """
    client_ip = "203.0.113.7"
    message_id = f"<spf-only-{uuid.uuid4()}@example.com>"
    raw = (
        f"X-Mailgun-Sending-Ip: {client_ip}\r\n"
        f"From: {SENDER}\r\n"
        f"To: {RECIPIENT}\r\n"
        "Subject: SPF-only DMARC alignment\r\n"
        "Date: Mon, 21 Apr 2026 12:00:00 +0000\r\n"
        f"Message-ID: {message_id}\r\n"
        "\r\n"
        "body\r\n"
    ).encode()

    observed: dict[str, object] = {}

    def fake_check_spf(ip: str, mail_from: str, helo: str) -> SPFAuthenticationResult:
        observed["ip"] = ip
        observed["mail_from"] = mail_from
        observed["helo"] = helo
        return SPFAuthenticationResult(
            result="pass",
            reason="test fixture",
            smtp_mailfrom=mail_from,
            smtp_helo=helo,
        )

    monkeypatch.setattr(
        "family_assistant.email_intake.authentication.check_spf",
        fake_check_spf,
    )

    dns = FakeDnsResolver({
        f"_dmarc.{SENDER_DOMAIN}": "v=DMARC1; p=reject; adkim=s; aspf=s",
    })
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_authenticated_sender=True,
        ),
        dns_resolver=dns,
    )
    form = _mailgun_form(raw_mime=raw, message_id=message_id)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert observed == {
        "ip": client_ip,
        "mail_from": SENDER,
        "helo": SENDER_DOMAIN,
    }
    assert await _email_dmarc_result(db_engine, message_id) == "pass"


@pytest.mark.asyncio
async def test_mail_webhook_rejects_tampered_dkim_signature(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_authenticated_sender=True,
        ),
    )
    tampered = build_signed_message(from_address=SENDER).replace(
        b"Your order is confirmed.", b"Evil payload."
    )
    form = _mailgun_form(raw_mime=tampered)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "DMARC" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
async def test_mail_webhook_rejects_unsigned_message_when_dmarc_required(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_authenticated_sender=True,
        ),
    )
    unsigned = (
        b"From: buyer@example.com\r\n"
        b"To: orders@example.net\r\n"
        b"Subject: Plain\r\n"
        b"Date: Mon, 21 Apr 2026 12:00:00 +0000\r\n"
        b"Message-ID: <plain@example.com>\r\n"
        b"\r\n"
        b"Plain body\r\n"
    )
    form = _mailgun_form(raw_mime=unsigned)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "DMARC" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_swallows_permissive_auth_errors(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail-open telemetry: DNS/library errors during permissive evaluation must not 5xx.

    With ``require_authenticated_sender=False`` the webhook evaluates DKIM/DMARC only
    for telemetry, so operators can observe the result before flipping enforcement on.
    An unreachable DNS resolver or a crash inside ``authheaders`` must not bubble up
    and reject messages that would otherwise have been accepted.
    """

    def raising_resolver(_: str) -> str | None:
        msg = "DNS unavailable"
        raise RuntimeError(msg)

    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(mailgun_webhook_signing_key=SIGNING_KEY),
        dns_resolver=FakeDnsResolver(),
    )
    monkeypatch.setattr(
        fastapi_app.state,
        "email_intake_dns_resolver",
        raising_resolver,
        raising=False,
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])
    assert await _email_dmarc_result(db_engine, form["Message-Id"]) is None


@pytest.mark.asyncio
async def test_mail_webhook_rejects_missing_body_mime_when_auth_required(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_authenticated_sender=True,
        ),
    )
    form = _mailgun_form(include_body_mime=False)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "body-mime" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_accepts_missing_body_mime_without_auth_required(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(mailgun_webhook_signing_key=SIGNING_KEY),
    )
    form = _mailgun_form(include_body_mime=False)

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 200, response.text
    assert await _email_exists(db_engine, form["Message-Id"])
    assert await _email_dmarc_result(db_engine, form["Message-Id"]) is None


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mail_webhook_rejects_from_domain_without_dmarc_policy(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dns = FakeDnsResolver({
        # DKIM key for a domain with no DMARC record.
        f"test._domainkey.{SENDER_DOMAIN}": build_dns_for(
            domain=SENDER_DOMAIN
        )._records[f"test._domainkey.{SENDER_DOMAIN}"],
    })
    _configure_email_intake(
        monkeypatch,
        tmp_path,
        EmailIntakeConfig(
            mailgun_webhook_signing_key=SIGNING_KEY,
            require_authenticated_sender=True,
        ),
        dns_resolver=dns,
    )
    form = _mailgun_form()

    response = await api_client.post("/webhook/mail", data=form)

    assert response.status_code == 401
    assert "DMARC" in response.text
    assert not await _email_exists(db_engine, form["Message-Id"])


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
    form = _mailgun_form(signing_key=None, include_body_mime=False)
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
    form = _mailgun_form(signing_key=None, include_body_mime=False)
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
