"""Security checks for inbound Mailgun email webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from email.utils import parseaddr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.datastructures import FormData

    from family_assistant.config_models import EmailIntakeConfig


class EmailIntakeSecurityError(ValueError):
    """Raised when an inbound email webhook fails security checks."""


class EmailIntakePayloadTooLargeError(EmailIntakeSecurityError):
    """Raised when an inbound email webhook exceeds configured size limits."""


@dataclass(frozen=True, slots=True)
class SenderAuthentication:
    """Normalized sender authentication results from Mailgun/form headers."""

    dmarc: str | None
    spf: str | None
    dkim: str | None

    @property
    def dmarc_passed(self) -> bool:
        """Return whether DMARC passed."""
        return _is_pass(self.dmarc)

    @property
    def spf_passed(self) -> bool:
        """Return whether SPF passed."""
        return _is_pass(self.spf)

    @property
    def dkim_passed(self) -> bool:
        """Return whether DKIM passed."""
        return _is_pass(self.dkim)


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
        key=signing_key.encode("utf-8"),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, signature):
        msg = "Invalid Mailgun signature"
        raise EmailIntakeSecurityError(msg)


def verify_sender_authorization(
    form_data: FormData,
    config: EmailIntakeConfig,
) -> None:
    """Verify sender/recipient allowlists and sender authentication policy."""
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
        return

    authentication = extract_sender_authentication(form_data)
    if authentication.dmarc_passed:
        return

    if authentication.dmarc is not None or config.require_dmarc_pass:
        msg = "Sender authentication failed DMARC policy"
        raise EmailIntakeSecurityError(msg)

    if (
        config.allow_spf_or_dkim_fallback_when_dmarc_missing
        and authentication.dmarc is None
        and (authentication.spf_passed or authentication.dkim_passed)
    ):
        return

    msg = "Sender authentication did not meet the configured policy"
    raise EmailIntakeSecurityError(msg)


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


def extract_sender_authentication(form_data: FormData) -> SenderAuthentication:
    """Extract DMARC/SPF/DKIM results from Mailgun fields or Authentication-Results."""
    authentication_results = _string_field(form_data, "Authentication-Results")
    if authentication_results is None:
        authentication_results = _string_field(form_data, "authentication-results")
    if authentication_results is None:
        authentication_results = _message_header_value(
            form_data, "Authentication-Results"
        )

    return SenderAuthentication(
        dmarc=_coalesce_auth_result(
            _string_field(form_data, "dmarc"),
            _string_field(form_data, "DMARC"),
            _string_field(form_data, "Dmarc"),
            _extract_authentication_results_value(authentication_results, "dmarc"),
        ),
        spf=_coalesce_auth_result(
            _string_field(form_data, "spf"),
            _string_field(form_data, "SPF"),
            _extract_authentication_results_value(authentication_results, "spf"),
        ),
        dkim=_coalesce_auth_result(
            _string_field(form_data, "dkim"),
            _string_field(form_data, "Dkim"),
            _string_field(form_data, "DKIM"),
            _extract_authentication_results_value(authentication_results, "dkim"),
        ),
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
        or config.action_planning_enabled
    )


def _coalesce_auth_result(*values: str | None) -> str | None:
    for value in values:
        if value is not None:
            return value.lower()
    return None


def _extract_authentication_results_value(
    authentication_results: str | None,
    mechanism: str,
) -> str | None:
    if authentication_results is None:
        return None

    mechanism_prefix = f"{mechanism.lower()}="
    for raw_token in authentication_results.replace(";", " ").split():
        token = raw_token.strip().lower()
        if token.startswith(mechanism_prefix):
            return token.removeprefix(mechanism_prefix)
    return None


def _message_header_value(form_data: FormData, header_name: str) -> str | None:
    headers_raw = _string_field(form_data, "message-headers")
    if headers_raw is None:
        return None

    try:
        parsed_headers = json.loads(headers_raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed_headers, list):
        return None

    normalized_header_name = header_name.lower()
    for header in parsed_headers:
        if not isinstance(header, list) or len(header) < 2:
            continue
        raw_name, raw_value = header[0], header[1]
        if not isinstance(raw_name, str):
            continue
        if raw_name.lower() == normalized_header_name and isinstance(raw_value, str):
            return raw_value
    return None


def _is_pass(value: str | None) -> bool:
    return value is not None and value.lower() in {"pass", "passed", "true", "yes"}


def get_security_fields(
    form_data: FormData,
) -> tuple[str | None, str | None, str | None]:
    """Return Mailgun timestamp/token/signature fields from form data."""
    timestamp = _string_field(form_data, "timestamp")
    token = _string_field(form_data, "token")
    signature = _string_field(form_data, "signature")
    return timestamp, token, signature
