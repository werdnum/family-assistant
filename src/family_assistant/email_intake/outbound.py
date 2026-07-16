"""Outbound email delivery for the email intake interface."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from family_assistant.email_intake.security import normalize_email_address
from family_assistant.storage.context import get_db_context
from family_assistant.storage.email import received_emails_table

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.config_models import EmailIntakeConfig
    from family_assistant.services.user_identity import UserIdentityResolver

logger = logging.getLogger(__name__)


class OutboundEmailDeliveryError(RuntimeError):
    """Raised when a recipient-locked email reply cannot be delivered."""


class OutboundEmailClient(Protocol):
    """Protocol for sending outbound email replies."""

    async def send_email(
        self,
        *,
        to_address: str,
        from_address: str,
        subject: str,
        text: str,
        in_reply_to: str | None = None,
    ) -> str:
        """Send a text email and return the provider message id."""
        ...


def _single_line_email_field(value: str) -> str:
    """Remove control-line breaks from fields passed to Mailgun form data."""
    return " ".join(value.replace("\r", "\n").splitlines())


def _safe_threading_header(value: str) -> str | None:
    """Return a safe email threading header value, or None if it is unsafe."""
    if "\r" in value or "\n" in value:
        logger.warning("Dropping unsafe email threading header with line breaks")
        return None
    return value


@dataclass(frozen=True, slots=True)
class EmailConversationTarget:
    """Resolved destination for an email conversation id."""

    email_db_id: int
    target_user_id: str
    to_address: str
    subject: str
    message_id_header: str


def email_conversation_id(email_db_id: int) -> str:
    """Return the deterministic conversation id for an inbound email row."""
    return f"email:{email_db_id}"


def parse_email_conversation_id(conversation_id: str) -> int:
    """Parse an email conversation id into a database row id."""
    prefix = "email:"
    if not conversation_id.startswith(prefix):
        msg = f"Invalid email conversation id: {conversation_id!r}"
        raise ValueError(msg)
    raw_id = conversation_id[len(prefix) :]
    try:
        return int(raw_id)
    except ValueError as exc:
        msg = f"Invalid email conversation id: {conversation_id!r}"
        raise ValueError(msg) from exc


class MailgunOutboundEmailClient:
    """Mailgun Messages API client for deterministic same-sender email replies."""

    def __init__(
        self,
        *,
        api_key: str,
        domain: str,
        http_client: httpx.AsyncClient,
        timeout_seconds: float,
    ) -> None:
        self._api_key = api_key
        self._domain = domain
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    async def send_email(
        self,
        *,
        to_address: str,
        from_address: str,
        subject: str,
        text: str,
        in_reply_to: str | None = None,
    ) -> str:
        """Send a text email through Mailgun."""
        data: dict[str, str] = {
            "from": _single_line_email_field(from_address),
            "to": _single_line_email_field(to_address),
            "subject": _single_line_email_field(subject),
            "text": text,
        }
        if in_reply_to:
            safe_in_reply_to = _safe_threading_header(in_reply_to)
            if safe_in_reply_to is not None:
                data["h:In-Reply-To"] = safe_in_reply_to
                data["h:References"] = safe_in_reply_to

        try:
            response = await self._http_client.post(
                f"https://api.mailgun.net/v3/{self._domain}/messages",
                auth=("api", self._api_key),
                data=data,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OutboundEmailDeliveryError("Mailgun email delivery failed") from exc

        try:
            payload: object = response.json()
        except ValueError as exc:
            raise OutboundEmailDeliveryError(
                "Mailgun response was not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise OutboundEmailDeliveryError("Mailgun response was not a JSON object")
        message_id = payload.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise OutboundEmailDeliveryError("Mailgun response did not include an id")
        return message_id


class EmailChatInterface:
    """ChatInterface that sends recipient-locked replies to inbound email senders."""

    def __init__(
        self,
        *,
        database_engine: AsyncEngine,
        outbound_client: OutboundEmailClient | None,
        config: EmailIntakeConfig,
        user_identity_resolver: UserIdentityResolver,
    ) -> None:
        self._database_engine = database_engine
        self._outbound_client = outbound_client
        self._config = config
        self._user_identity_resolver = user_identity_resolver

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
    ) -> str | None:
        """Send a reply to the original authenticated inbound email sender."""
        _ = parse_mode
        _ = reply_to_interface_id
        _ = on_behalf_of_user_id
        if attachment_ids:
            raise OutboundEmailDeliveryError(
                "Email replies with attachments are not supported"
            )
        if self._outbound_client is None:
            logger.info("Email outbound delivery is not configured")
            return None

        target = await self._resolve_target(conversation_id)
        from_address = self._config.outbound_from_address
        if not from_address:
            raise OutboundEmailDeliveryError(
                "email_intake.outbound_from_address is required for email replies"
            )

        subject = target.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        return await self._outbound_client.send_email(
            to_address=target.to_address,
            from_address=from_address,
            subject=subject,
            text=text,
            in_reply_to=target.message_id_header,
        )

    async def _resolve_target(self, conversation_id: str) -> EmailConversationTarget:
        email_db_id = parse_email_conversation_id(conversation_id)
        async with get_db_context(engine=self._database_engine) as db:
            row = await db.fetch_one(
                received_emails_table.select().where(
                    received_emails_table.c.id == email_db_id
                )
            )
        if row is None:
            msg = f"Email conversation {conversation_id!r} does not exist"
            raise OutboundEmailDeliveryError(msg)

        sender = normalize_email_address(row["sender_address"])
        if not sender:
            msg = f"Email row {email_db_id} has no deliverable sender address"
            raise OutboundEmailDeliveryError(msg)

        target_user_id = str(row["target_user_id"] or "")
        if target_user_id and not self._sender_is_authorized_for_user(
            sender,
            target_user_id,
        ):
            msg = (
                f"Email row {email_db_id} sender {sender!r} is not an authorized "
                f"sender for user {target_user_id!r}"
            )
            raise OutboundEmailDeliveryError(msg)

        return EmailConversationTarget(
            email_db_id=email_db_id,
            target_user_id=target_user_id,
            to_address=sender,
            subject=str(row["subject"] or "Family Assistant"),
            message_id_header=str(row["message_id_header"]),
        )

    def _sender_is_authorized_for_user(self, sender: str, target_user_id: str) -> bool:
        """Return whether this sender is explicitly mapped to the target user."""
        return self._user_identity_resolver.is_email_sender_authorized_for_user(
            sender,
            target_user_id,
        )
