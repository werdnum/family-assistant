"""Runtime taint classification for inbound email sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.email_intake.security import normalize_email_address
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.config_models import AppConfig


def configured_sender_set(raw_addresses: list[str]) -> set[str]:
    """Normalize configured sender addresses, dropping invalid entries."""
    return {
        normalized
        for address in raw_addresses
        if (normalized := normalize_email_address(address)) is not None
    }


def email_authentication_passed(email_row: Mapping[str, object]) -> bool:
    """Return whether the stored inbound email authentication result passed."""
    return email_row.get("dmarc_result") == "pass"


def email_initial_taint_source(
    *,
    email_db_id: int,
    email_row: Mapping[str, object],
    app_config: AppConfig,
) -> TaintSource:
    """Classify an inbound email row as a runtime taint source."""
    sender = normalize_email_address(
        str(email_row["sender_address"]) if email_row.get("sender_address") else None
    )
    auth_passed = email_authentication_passed(email_row)
    known_contacts = configured_sender_set(
        app_config.email_intake.known_contact_sender_addresses
    )
    recognized_machines = configured_sender_set(
        app_config.email_intake.recognized_machine_sender_addresses
    )

    if sender is not None and sender in known_contacts and auth_passed:
        tier = SourceTrustTier.KNOWN_CONTACT
        reason = (
            f"Inbound email sender {sender!r} matched the known-contact "
            "allowlist and passed DMARC."
        )
    elif sender is not None and sender in recognized_machines and auth_passed:
        tier = SourceTrustTier.RECOGNIZED_MACHINE
        reason = (
            f"Inbound email sender {sender!r} matched the recognized-machine "
            "allowlist and passed DMARC."
        )
    else:
        tier = SourceTrustTier.UNKNOWN_EXTERNAL
        reason = (
            "Inbound email content is sender-controlled and did not match an "
            "authenticated lower-trust-tier source."
        )

    return TaintSource(
        source_type=TaintSourceType.EMAIL,
        source_id=str(email_db_id),
        tier=tier,
        labels=frozenset(app_config.taint_policy.artifact_labels.get(tier, [])),
        reason=reason,
    )


def email_taint_metadata(
    *,
    email_db_id: int,
    email_row: Mapping[str, object],
    app_config: AppConfig,
) -> TaintMetadata:
    """Return compact taint metadata for an inbound email row."""
    source = email_initial_taint_source(
        email_db_id=email_db_id,
        email_row=email_row,
        app_config=app_config,
    )
    return TurnTaintState.empty().add_source(source).to_metadata()


def email_provenance_metadata(
    *,
    email_db_id: int,
    email_row: Mapping[str, object],
    app_config: AppConfig,
) -> dict[str, object]:
    """Return durable provenance metadata for indexed email artifacts."""
    source = email_initial_taint_source(
        email_db_id=email_db_id,
        email_row=email_row,
        app_config=app_config,
    )
    taint_metadata = TurnTaintState.empty().add_source(source).to_metadata()
    return {
        "source_trust_tier": source.tier.config_value,
        "source_type": source.source_type.value,
        "source_id": source.source_id,
        "source_trust_reason": source.reason,
        "provenance_labels": sorted(source.labels),
        "taint_metadata": taint_metadata,
    }
