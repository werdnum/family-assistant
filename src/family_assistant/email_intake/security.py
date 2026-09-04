"""Security checks for inbound Mailgun email webhooks.

Sender authentication (DKIM/SPF/DMARC) is performed locally against the raw MIME
body forwarded by Mailgun. The legacy scheme of trusting Mailgun-populated form fields
is no longer supported because Mailgun does not reliably populate them. See
:mod:`family_assistant.email_intake.authentication` for the verification implementation.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import time
from email.utils import parseaddr
from typing import TYPE_CHECKING

from family_assistant.email_intake.authentication import (
    DnsResolver,
    EmailAuthenticationResult,
    extract_client_ip,
    verify_email_authentication,
)

if TYPE_CHECKING:
    from starlette.datastructures import FormData

    from family_assistant.config_models import EmailIntakeConfig

logger = logging.getLogger(__name__)


class EmailIntakeSecurityError(ValueError):
    """Raised when an inbound email webhook fails security checks."""


class EmailIntakePayloadTooLargeError(EmailIntakeSecurityError):
    """Raised when an inbound email webhook exceeds configured size limits."""


def enforce_raw_request_size(raw_body: bytes, config: EmailIntakeConfig) -> None:
    """Reject oversized raw webhook payloads before form parsing."""
    if len(raw_body) > config.max_raw_request_bytes:
        msg = (
            "Inbound email webhook payload exceeds configured limit "
            f"({len(raw_body)} > {config.max_raw_request_bytes} bytes)"
        )
        raise EmailIntakePayloadTooLargeError(msg)


def verify_mailgun_signature(
    *,
    timestamp: str | None,
    token: str | None,
    signature: str | None,
    config: EmailIntakeConfig,
    now: float | None = None,
) -> None:
    """Verify Mailgun's timestamp/token/signature tuple when a key is configured."""
    signing_key = config.mailgun_webhook_signing_key
    if not signing_key:
        if _requires_verified_mailgun(config):
            msg = (
                "Mailgun signature verification must be configured before enabling "
                "sender, recipient, or authentication policy"
            )
            raise EmailIntakeSecurityError(msg)
        return

    if not timestamp or not token or not signature:
        msg = "Missing Mailgun signature fields"
        raise EmailIntakeSecurityError(msg)

    try:
        timestamp_seconds = int(timestamp)
    except ValueError as exc:
        msg = "Invalid Mailgun signature timestamp"
        raise EmailIntakeSecurityError(msg) from exc

    current_time = time.time() if now is None else now
    timestamp_age = abs(current_time - timestamp_seconds)
    if timestamp_age > config.mailgun_signature_max_age_seconds:
        msg = "Mailgun signature timestamp is outside the allowed replay window"
        raise EmailIntakeSecurityError(msg)

    digest = hmac.new(
        key=signing_key.get_secret_value().encode("utf-8"),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, signature):
        msg = "Invalid Mailgun signature"
        raise EmailIntakeSecurityError(msg)


def verify_sender_authorization(
    form_data: FormData,
    config: EmailIntakeConfig,
    *,
    raw_mime: bytes | None = None,
    dns_resolver: DnsResolver | None = None,
) -> EmailAuthenticationResult | None:
    """Verify sender/recipient allowlists and (when required) DKIM/DMARC authentication.

    Returns the authentication result when authentication was evaluated, else ``None``.
    """
    sender = normalize_email_address(_string_field(form_data, "sender"))
    if config.allowed_sender_addresses:
        allowed_senders = {
            normalize_email_address(address)
            for address in config.allowed_sender_addresses
        }
        if sender is None or sender not in allowed_senders:
            msg = f"Sender {sender or '<missing>'} is not authorized"
            raise EmailIntakeSecurityError(msg)

    recipient = normalize_email_address(_string_field(form_data, "recipient"))
    if config.allowed_recipient_addresses:
        allowed_recipients = {
            normalize_email_address(address)
            for address in config.allowed_recipient_addresses
        }
        if recipient is None or recipient not in allowed_recipients:
            msg = f"Recipient {recipient or '<missing>'} is not authorized"
            raise EmailIntakeSecurityError(msg)

    if not config.require_authenticated_sender:
        if raw_mime is None:
            return None
        try:
            return _evaluate_authentication(
                raw_mime=raw_mime,
                envelope_from=sender,
                dns_resolver=dns_resolver,
            )
        except Exception:
            logger.warning(
                "Permissive inbound email authentication evaluation failed; "
                "accepting message without auth telemetry",
                exc_info=True,
            )
            return None

    if raw_mime is None:
        msg = (
            "Sender authentication is required but the Mailgun webhook did not "
            "include the raw MIME message (body-mime). Configure the Mailgun route "
            "to forward the full MIME message."
        )
        raise EmailIntakeSecurityError(msg)

    authentication = _evaluate_authentication(
        raw_mime=raw_mime,
        envelope_from=sender,
        dns_resolver=dns_resolver,
    )
    if not authentication.dmarc_passed:
        msg = (
            "Sender authentication failed DMARC policy "
            f"(dkim={authentication.dkim}, spf={authentication.spf}, "
            f"dmarc={authentication.dmarc})"
        )
        raise EmailIntakeSecurityError(msg)
    return authentication


def resolve_target_user_id(
    form_data: FormData, config: EmailIntakeConfig
) -> str | None:
    """Resolve the intended application user for an accepted inbound email."""
    sender = normalize_email_address(_string_field(form_data, "sender"))
    recipient = normalize_email_address(_string_field(form_data, "recipient"))

    matched_user_ids: set[str] = set()
    for mapping in config.user_mappings:
        if (sender is not None and sender in mapping.sender_addresses) or (
            recipient is not None and recipient in mapping.recipient_addresses
        ):
            matched_user_ids.add(mapping.user_id)

    if len(matched_user_ids) > 1:
        sorted_user_ids = ", ".join(sorted(matched_user_ids))
        msg = f"Inbound email maps to multiple users: {sorted_user_ids}"
        raise EmailIntakeSecurityError(msg)

    if matched_user_ids:
        return next(iter(matched_user_ids))

    if config.require_user_mapping:
        msg = "Inbound email does not map to a configured user"
        raise EmailIntakeSecurityError(msg)

    return None


def enforce_attachment_size_limits(
    *,
    attachment_name: str,
    attachment_size: int,
    total_attachment_size: int,
    config: EmailIntakeConfig,
) -> None:
    """Reject attachments that exceed per-file or total configured limits."""
    if attachment_size > config.max_attachment_bytes:
        msg = (
            f"Attachment {attachment_name!r} exceeds configured limit "
            f"({attachment_size} > {config.max_attachment_bytes} bytes)"
        )
        raise EmailIntakePayloadTooLargeError(msg)

    if total_attachment_size > config.max_total_attachment_bytes:
        msg = (
            "Inbound email attachments exceed configured total limit "
            f"({total_attachment_size} > {config.max_total_attachment_bytes} bytes)"
        )
        raise EmailIntakePayloadTooLargeError(msg)


def normalize_email_address(raw_address: str | None) -> str | None:
    """Extract and normalize an email address from a header or form field."""
    if not raw_address:
        return None
    _, parsed_address = parseaddr(raw_address)
    normalized = parsed_address.strip().lower()
    return normalized or None


async def extract_raw_mime(form_data: FormData) -> bytes | None:
    """Return the raw MIME message bytes from a Mailgun webhook, if present.

    Mailgun includes the raw RFC 822 message in ``body-mime`` when the inbound route
    is configured with the MIME-type forwarding option (``forward('url', 'mime')``).
    Without this configuration we cannot cryptographically verify DKIM signatures.

    Starlette parses large form fields as ``UploadFile`` objects whose ``read()`` is
    an awaitable coroutine; we await it to avoid silently returning ``None`` and
    causing authentication to fail for large emails.
    """
    value = form_data.get("body-mime")
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogateescape")
    read = getattr(value, "read", None)
    if callable(read):
        data = read()
        if inspect.isawaitable(data):
            data = await data
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8", errors="surrogateescape")
    return None


def _evaluate_authentication(
    *,
    raw_mime: bytes,
    envelope_from: str | None,
    dns_resolver: DnsResolver | None,
) -> EmailAuthenticationResult:
    client_ip = extract_client_ip(raw_mime)
    return verify_email_authentication(
        raw_mime,
        envelope_from=envelope_from,
        client_ip=client_ip,
        dns_resolver=dns_resolver,
    )


def _string_field(form_data: FormData, key: str) -> str | None:
    value = form_data.get(key)
    if isinstance(value, str):
        stripped_value = value.strip()
        if stripped_value:
            return stripped_value
    return None


def _requires_verified_mailgun(config: EmailIntakeConfig) -> bool:
    return bool(
        config.allowed_sender_addresses
        or config.allowed_recipient_addresses
        or config.require_authenticated_sender
        or config.require_user_mapping
        or config.user_mappings
    )


def get_security_fields(
    form_data: FormData,
) -> tuple[str | None, str | None, str | None]:
    """Return Mailgun timestamp/token/signature fields from form data."""
    timestamp = _string_field(form_data, "timestamp")
    token = _string_field(form_data, "token")
    signature = _string_field(form_data, "signature")
    return timestamp, token, signature
