"""Tell the user when script review refuses to run one of their automations.

An unattended firing of a definition nobody vouched for is reviewed once
before it runs, and nobody is there to confirm it (see
docs/design/script-execution-review-boundary.md). A refusal therefore needs a
human to act later: confirming the definition on the artifact review page cures
it, and its next firing runs. The card this module writes is that hand-off. It
is deterministic: the reviewer's reason is shown to the user, never to a model.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from family_assistant.actions import resolve_home_target
from family_assistant.storage.artifact_review_status import ArtifactKind, needs_review
from family_assistant.storage.types import (
    ReviewRejectedArtifactDict,
    ReviewRejectionNoticeDict,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from family_assistant.config_models import AppConfig
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.artifact_review import ArtifactReview

logger = logging.getLogger(__name__)

REVIEW_REJECTION_REASON_MAX_CHARS = 600


def _notice_key(
    automation_name: str, source: str, artifacts: Sequence[ArtifactReview]
) -> str:
    """Identify the refused content; editing or confirming any part changes it."""
    material = json.dumps(
        [
            automation_name,
            hashlib.sha256(source.encode()).hexdigest(),
            sorted(f"{a.kind}:{a.id}:{a.content_hash}" for a in artifacts),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _content(automation_name: str, artifacts: Sequence[ArtifactReview]) -> str:
    text = f"Script review stopped “{automation_name}” from running."
    if artifacts:
        names = ", ".join(f"“{a.name}”" for a in artifacts)
        text += f" Review and confirm {names} to let it run."
    return text


async def post_review_rejection_notice(
    db: Database,
    app_config: AppConfig | None,
    *,
    interface_type: str,
    conversation_id: str,
    home_owner_user_id: str | None,
    artifact_refs: Sequence[tuple[ArtifactKind, int]],
    fallback_name: str,
    source: str,
    reason: str,
    taint_metadata: TaintMetadata,
    now: datetime,
) -> int | None:
    """Post a card naming what to review; returns its id, or ``None`` if already posted.

    ``artifact_refs`` is the firing's executable closure, invoking definition
    first. The card names the automation after that definition, and links to
    every member the review page still lists as needing review. A firing with
    nothing reviewable (a one-shot action rides its task) gets the card with no
    links: it will not fire again, but the user still learns it did not run.
    """
    reviews: list[ArtifactReview] = []
    for kind, artifact_id in dict.fromkeys(artifact_refs):
        review = await db.artifact_review.get(kind, artifact_id)
        if review is not None:
            reviews.append(review)
    automation_name = reviews[0].name if reviews else fallback_name
    pending = [review for review in reviews if needs_review(review.trust_tier)]
    notice = ReviewRejectionNoticeDict(
        automation_name=automation_name,
        reason=reason[:REVIEW_REJECTION_REASON_MAX_CHARS],
        artifacts=[
            ReviewRejectedArtifactDict(kind=a.kind, id=a.id, name=a.name)
            for a in pending
        ],
    )
    if home_owner_user_id is not None:
        interface_type, conversation_id = await resolve_home_target(
            db,
            app_config,
            interface_type=interface_type,
            conversation_id=conversation_id,
            home_owner_user_id=home_owner_user_id,
            now=now,
        )
    notice_id = await db.message_history.add_review_rejection_notice(
        interface_type=interface_type,
        conversation_id=conversation_id,
        notice=notice,
        key=_notice_key(automation_name, source, pending),
        content=_content(automation_name, pending),
        taint_metadata=taint_metadata,
        now=now,
    )
    if notice_id is None:
        logger.info(
            "Review rejection of %r already posted to %s:%s",
            automation_name,
            interface_type,
            conversation_id,
        )
    return notice_id
