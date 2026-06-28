from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.datastructures import FormData

    from family_assistant.config_models import AppConfig


class UserIdentityResolutionError(ValueError):
    """Raised when an external identity cannot be mapped to an application user."""


@dataclass(frozen=True)
class ResolvedUserIdentity:
    """A canonical application user resolved from an interface-specific identity."""

    user_id: str
    source: str
    source_identifier: str
    email: str | None = None
    subject: str | None = None
    label: str | None = None


class UserIdentityResolver:
    """Resolves interface-specific identities to canonical application user ids."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._users_configured = bool(config.users)
        self._oidc_email_to_user_id: dict[str, str] = {}
        self._oidc_subject_to_user_id: dict[str, str] = {}
        self._telegram_user_id_to_user_id: dict[int, str] = {}
        self._developer_telegram_user_ids: set[int] = set()
        self._email_sender_to_user_id: dict[str, str] = {}
        self._email_recipient_to_user_id: dict[str, str] = {}
        self._user_id_to_label: dict[str, str] = {}

        for user in config.users:
            if user.label is not None:
                self._user_id_to_label[user.id] = user.label
            for email in user.oidc.emails:
                self._oidc_email_to_user_id[email] = user.id
            for subject in user.oidc.subjects:
                self._oidc_subject_to_user_id[subject] = user.id
            for telegram_user_id in user.telegram.user_ids:
                self._telegram_user_id_to_user_id[telegram_user_id] = user.id
                if user.telegram.developer:
                    self._developer_telegram_user_ids.add(telegram_user_id)
            for sender in user.email_intake.sender_addresses:
                self._email_sender_to_user_id[sender] = user.id
            for recipient in user.email_intake.recipient_addresses:
                self._email_recipient_to_user_id[recipient] = user.id

    @property
    def users_configured(self) -> bool:
        return self._users_configured

    def get_user_label(self, user_id: str) -> str | None:
        """Return the configured human-friendly label for a canonical user, if any."""
        return self._user_id_to_label.get(user_id)

    def resolve_oidc_user(self, user_info: dict[str, object]) -> ResolvedUserIdentity:
        """Resolve an OIDC session or userinfo payload to a canonical user."""
        email = normalize_email_address(_string_or_none(user_info.get("email")))
        subject = _string_or_none(user_info.get("sub"))

        if self._users_configured:
            matched_user_ids: set[str] = set()
            if email is not None and email in self._oidc_email_to_user_id:
                matched_user_ids.add(self._oidc_email_to_user_id[email])
            if subject is not None and subject in self._oidc_subject_to_user_id:
                matched_user_ids.add(self._oidc_subject_to_user_id[subject])
            if len(matched_user_ids) > 1:
                sorted_user_ids = ", ".join(sorted(matched_user_ids))
                msg = f"OIDC identity maps to multiple users: {sorted_user_ids}"
                raise UserIdentityResolutionError(msg)
            if matched_user_ids:
                matched_user_id = next(iter(matched_user_ids))
                return ResolvedUserIdentity(
                    user_id=matched_user_id,
                    source="oidc",
                    source_identifier=email or subject or "unknown",
                    email=email,
                    subject=subject,
                    label=self._user_id_to_label.get(matched_user_id),
                )
            msg = (
                "OIDC identity is not mapped to a configured user "
                f"(email={email or '<missing>'}, sub={subject or '<missing>'})"
            )
            raise UserIdentityResolutionError(msg)

        user_id = subject or email
        if not user_id:
            msg = "OIDC identity did not contain a subject or email"
            raise UserIdentityResolutionError(msg)
        return ResolvedUserIdentity(
            user_id=user_id,
            source="oidc",
            source_identifier=user_id,
            email=email,
            subject=subject,
        )

    def resolve_api_token_user(self, user_identifier: str) -> ResolvedUserIdentity:
        """Resolve an already-issued API token owner id."""
        normalized_user_identifier = user_identifier.strip()
        if not normalized_user_identifier:
            msg = "API token user identifier is empty"
            raise UserIdentityResolutionError(msg)
        if self._users_configured:
            email = normalize_email_address(normalized_user_identifier)
            matched_user_ids: set[str] = set()
            if email is not None and email in self._oidc_email_to_user_id:
                matched_user_ids.add(self._oidc_email_to_user_id[email])
            if normalized_user_identifier in self._oidc_subject_to_user_id:
                matched_user_ids.add(
                    self._oidc_subject_to_user_id[normalized_user_identifier]
                )
            configured_user_ids = {user.id for user in self._config.users}
            if normalized_user_identifier in configured_user_ids:
                matched_user_ids.add(normalized_user_identifier)
            if len(matched_user_ids) > 1:
                sorted_user_ids = ", ".join(
                    repr(user_id) for user_id in sorted(matched_user_ids)
                )
                msg = (
                    "API token owner maps to multiple configured users: "
                    f"{sorted_user_ids}"
                )
                raise UserIdentityResolutionError(msg)
            if not matched_user_ids:
                msg = (
                    "API token owner is not mapped to a configured user: "
                    f"{normalized_user_identifier}"
                )
                raise UserIdentityResolutionError(msg)
            normalized_user_identifier = next(iter(matched_user_ids))
        return ResolvedUserIdentity(
            user_id=normalized_user_identifier,
            source="api_token",
            source_identifier=user_identifier,
            label=self._user_id_to_label.get(normalized_user_identifier),
        )

    def resolve_telegram_user(self, telegram_user_id: int) -> ResolvedUserIdentity:
        """Resolve a Telegram user id to a canonical user."""
        if self._users_configured:
            user_id = self._telegram_user_id_to_user_id.get(telegram_user_id)
            if user_id is None:
                msg = (
                    "Telegram user id is not mapped to a configured user: "
                    f"{telegram_user_id}"
                )
                raise UserIdentityResolutionError(msg)
            return ResolvedUserIdentity(
                user_id=user_id,
                source="telegram",
                source_identifier=str(telegram_user_id),
                label=self._user_id_to_label.get(user_id),
            )

        if (
            self._config.allowed_user_ids
            and telegram_user_id not in self._config.allowed_user_ids
        ):
            msg = f"Telegram user id is not authorized: {telegram_user_id}"
            raise UserIdentityResolutionError(msg)
        return ResolvedUserIdentity(
            user_id=str(telegram_user_id),
            source="telegram",
            source_identifier=str(telegram_user_id),
        )

    def canonicalize_owner_id(self, raw_owner_id: str) -> str:
        """Map a stored ``user_id`` to its canonical application user id.

        Conversation owner ids come from the raw ``user_id`` persisted on each
        user message. Most interfaces already store the canonical id, but a
        Telegram conversation may be stored under the numeric Telegram user id,
        which must map to the same canonical id a web/API session resolves to.
        Unresolvable ids are returned unchanged so distinct unknown owners stay
        distinct (rather than collapsing together).
        """
        if raw_owner_id.isdigit():
            try:
                return self.resolve_telegram_user(int(raw_owner_id)).user_id
            except UserIdentityResolutionError:
                return raw_owner_id
        try:
            return self.resolve_api_token_user(raw_owner_id).user_id
        except UserIdentityResolutionError:
            return raw_owner_id

    def is_telegram_user_allowed(self, telegram_user_id: int) -> bool:
        try:
            self.resolve_telegram_user(telegram_user_id)
        except UserIdentityResolutionError:
            return False
        return True

    def is_developer_telegram_user(self, telegram_user_id: int) -> bool:
        if self._users_configured:
            return telegram_user_id in self._developer_telegram_user_ids
        return self._config.developer_chat_id == telegram_user_id

    def get_primary_telegram_user_id(self, user_id: str) -> int | None:
        """Return the primary Telegram user id for a canonical user, if configured."""
        if self._users_configured:
            for user in self._config.users:
                if user.id == user_id and user.telegram.user_ids:
                    return sorted(user.telegram.user_ids)[0]
            return None

        try:
            telegram_user_id = int(user_id)
        except ValueError:
            return None
        if self.is_telegram_user_allowed(telegram_user_id):
            return telegram_user_id
        return None

    def is_email_sender_authorized_for_user(
        self,
        sender_address: str,
        user_id: str,
    ) -> bool:
        """Return whether an email sender is explicitly mapped to a user."""
        sender = normalize_email_address(sender_address)
        if sender is None:
            return False
        if self._users_configured:
            return self._email_sender_to_user_id.get(sender) == user_id
        return any(
            sender in mapping.sender_addresses and mapping.user_id == user_id
            for mapping in self._config.email_intake.user_mappings
        )

    def resolve_email_intake_user(self, form_data: FormData) -> str | None:
        """Resolve an accepted inbound email to a canonical user id."""
        sender = normalize_email_address(_string_or_none(form_data.get("sender")))
        recipient = normalize_email_address(_string_or_none(form_data.get("recipient")))

        if self._users_configured:
            configured_matched_user_ids: set[str] = set()
            if sender is not None and sender in self._email_sender_to_user_id:
                configured_matched_user_ids.add(self._email_sender_to_user_id[sender])
            if recipient is not None and recipient in self._email_recipient_to_user_id:
                configured_matched_user_ids.add(
                    self._email_recipient_to_user_id[recipient]
                )
            if len(configured_matched_user_ids) > 1:
                sorted_user_ids = ", ".join(sorted(configured_matched_user_ids))
                msg = f"Inbound email maps to multiple users: {sorted_user_ids}"
                raise UserIdentityResolutionError(msg)
            if configured_matched_user_ids:
                return next(iter(configured_matched_user_ids))
            if self._config.email_intake.require_user_mapping:
                msg = "Inbound email does not map to a configured user"
                raise UserIdentityResolutionError(msg)
            return None

        matched_user_ids: set[str] = set()
        for mapping in self._config.email_intake.user_mappings:
            if (sender is not None and sender in mapping.sender_addresses) or (
                recipient is not None and recipient in mapping.recipient_addresses
            ):
                matched_user_ids.add(mapping.user_id)

        if len(matched_user_ids) > 1:
            sorted_user_ids = ", ".join(sorted(matched_user_ids))
            msg = f"Inbound email maps to multiple users: {sorted_user_ids}"
            raise UserIdentityResolutionError(msg)

        if matched_user_ids:
            return next(iter(matched_user_ids))

        if self._config.email_intake.require_user_mapping:
            msg = "Inbound email does not map to a configured user"
            raise UserIdentityResolutionError(msg)

        return None


def normalize_email_address(raw_address: str | None) -> str | None:
    """Extract and normalize an email address from a header or form field."""
    if not raw_address:
        return None
    _, parsed_address = parseaddr(raw_address)
    normalized = parsed_address.strip().lower()
    return normalized or None


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
