"""How the artifact review page names an artifact, and when it asks for review.

A leaf module, so code that links to the page (chat cards, the task worker)
need not import the repositories package to do it.
"""

from typing import Literal

from family_assistant.security.taint import SourceTrustTier

ArtifactKind = Literal["note", "script", "event", "schedule"]

_REVIEWED_TIERS = frozenset({
    SourceTrustTier.TRUSTED_USER.config_value,
    SourceTrustTier.TRUSTED_INTERNAL.config_value,
    SourceTrustTier.MACHINE_REVIEWED.config_value,
})


def needs_review(trust_tier: str) -> bool:
    """Whether the review page lists an artifact at ``trust_tier`` as needing review."""
    return trust_tier not in _REVIEWED_TIERS


def review_path(kind: ArtifactKind, artifact_id: int) -> str:
    """The artifact review page, opened at one artifact."""
    return f"/artifacts?kind={kind}&id={artifact_id}"
