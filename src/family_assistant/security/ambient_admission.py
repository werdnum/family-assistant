"""Admission of a note or skill into every future prompt.

A write that would place material on a full-content ambient surface crosses the
``ambient_prompt_write`` sink once, synchronously, against the complete
resolved candidate. The decision is what the persisted stamp records: an
admitting decision promotes an external candidate to ``machine_reviewed``, and
nothing else can. See docs/design/ambient-note-admission-at-write-time.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from family_assistant.security.taint import SourceTrustTier

AMBIENT_ADMISSION_EVENT_TYPE = "ambient_note_admission"
AMBIENT_ADMISSION_SINK_NAME = "ambient_prompt_write"


class AdmissionOutcome(StrEnum):
    """How an ambient write left the admission gate."""

    NOT_GATED = "not_gated"
    """The cell allowed it for a candidate that needs no promotion."""
    ADMITTED = "admitted"
    """A reviewer, a human or an operator override admitted it."""
    NOT_ADMITTED = "not_admitted"
    """Persisted, but as reference material that no prompt includes."""
    REFUSED = "refused"
    """Not persisted at all."""


@dataclass(frozen=True, slots=True)
class CandidateAttachment:
    """One attachment as the prompt would render it."""

    attachment_id: str
    description: str | None
    mime_type: str | None


@dataclass(frozen=True, slots=True)
class AmbientCandidate:
    """The complete note a write would persist, resolved before review.

    An append or a partial edit is resolved into the whole resulting note, so
    what the reviewer (or a confirming human) sees is exactly what is
    persisted, never the raw call arguments.
    """

    title: str
    content: str
    include_in_prompt: bool
    is_skill: bool
    skill_name: str | None
    skill_description: str | None
    attachments: tuple[CandidateAttachment, ...]
    imported_from: str | None = None

    def review_payload(self) -> dict[str, object]:
        """The candidate as the reviewer and the confirmation prompt render it."""
        payload: dict[str, object] = {
            "title": self.title,
            "content": self.content,
            "include_in_prompt": self.include_in_prompt,
            "attachments": [
                {
                    "attachment_id": attachment.attachment_id,
                    "description": attachment.description,
                    "mime_type": attachment.mime_type,
                }
                for attachment in self.attachments
            ],
        }
        if self.is_skill:
            payload["skill_catalog_entry"] = {
                "name": self.skill_name,
                "description": self.skill_description,
            }
        if self.imported_from is not None:
            payload["imported_from_workspace_file"] = self.imported_from
        return payload


@dataclass(frozen=True, slots=True)
class AmbientAdmissionDecision:
    """The gate's decision for one candidate."""

    outcome: AdmissionOutcome
    reason: str
    decided_by: str | None = None
    """What admitted it -- a reviewer verdict id, a human, an operator override."""


def is_external_candidate(tier: SourceTrustTier) -> bool:
    """Whether a candidate at this tier needs admission to become reusable.

    ``machine_reviewed`` needs none: transforming reviewed material alone is
    what curing it is for, and the write persists at that tier.
    """
    return tier > SourceTrustTier.MACHINE_REVIEWED
