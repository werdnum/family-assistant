"""Tell the user when script review will not vouch for one of their automations.

Two gates review an executable definition (see
docs/design/script-execution-review-boundary.md). The **admission** review
judges a script or automation as it is saved; under ``observe`` it runs off the
critical path, so a refusal lands after the assistant has already reported the
definition saved, and leaves it uncured. The **firing** review judges an
uncured definition each time it fires unattended, and under ``enforce`` a
refusal stops it. Either way the fix is a human's: confirming the definition on
the artifact review page cures it. The card this module writes is that
hand-off. It is deterministic: the reviewer's reason is shown to the user,
never to a model.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.actions import resolve_home_target
from family_assistant.interfaces import HistoryBackedChatInterface
from family_assistant.security.definition_records import DefinitionArtifactKind
from family_assistant.storage.artifact_review_status import ArtifactKind, needs_review
from family_assistant.storage.types import (
    ReviewRejectedArtifactDict,
    ReviewRejectionNoticeDict,
    ReviewRejectionStage,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from family_assistant.config_models import AppConfig
    from family_assistant.interfaces import ChatInterface
    from family_assistant.security.definition_records import DefinitionWriteRef
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.artifact_review import ArtifactReview

logger = logging.getLogger(__name__)

REVIEW_REJECTION_REASON_MAX_CHARS = 600

_REVIEW_PAGE_KINDS: dict[DefinitionArtifactKind, ArtifactKind] = {
    DefinitionArtifactKind.SCHEDULE_AUTOMATION: "schedule",
    DefinitionArtifactKind.EVENT_LISTENER: "event",
    DefinitionArtifactKind.SCRIPT: "script",
}


@dataclass(frozen=True, slots=True)
class PostedReviewRejection:
    """Where a new card landed, so its clients can be told about it."""

    interface_type: str
    conversation_id: str
    content: str


def _notice_key(
    stage: ReviewRejectionStage,
    automation_name: str,
    source: str,
    artifacts: Sequence[ArtifactReview],
) -> str:
    """Identify the refused content; editing or confirming any part changes it."""
    material = json.dumps(
        [
            stage,
            automation_name,
            hashlib.sha256(source.encode()).hexdigest(),
            sorted(f"{a.kind}:{a.id}:{a.content_hash}" for a in artifacts),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _content(
    stage: ReviewRejectionStage,
    automation_name: str,
    artifacts: Sequence[ArtifactReview],
) -> str:
    names = ", ".join(f"“{a.name}”" for a in artifacts)
    if stage == "admission":
        return (
            f"Script review didn't approve “{automation_name}” when it was saved."
            f" Review and confirm {names} if it is what you wanted."
        )
    text = f"Script review stopped “{automation_name}” from running."
    if artifacts:
        text += f" Review and confirm {names} to let it run."
    return text


async def review_refs_for_writes(
    db: Database, writes: Sequence[DefinitionWriteRef]
) -> list[tuple[ArtifactKind, int]]:
    """The review page's keys for the definitions a gated call wrote.

    A one-shot action's record rides its task and has no review page, so it
    has no key. A stored script is recorded by name and reviewed by id.
    """
    refs: list[tuple[ArtifactKind, int]] = []
    for write in writes:
        kind = _REVIEW_PAGE_KINDS.get(write.artifact_kind)
        if kind is None:
            continue
        if kind == "script":
            script = await db.scripts.get_by_name(write.artifact_id)
            if script is not None:
                refs.append((kind, script.id))
        else:
            refs.append((kind, int(write.artifact_id)))
    return refs


async def post_review_rejection_notice(
    db: Database,
    app_config: AppConfig | None,
    *,
    stage: ReviewRejectionStage,
    interface_type: str,
    conversation_id: str,
    home_owner_user_id: str | None,
    artifact_refs: Sequence[tuple[ArtifactKind, int]],
    fallback_name: str,
    source: str,
    reason: str,
    taint_metadata: TaintMetadata,
    now: datetime,
) -> PostedReviewRejection | None:
    """Post a card naming what to review; ``None`` if there is nothing to post.

    ``artifact_refs`` is what the gate judged, the invoking definition first.
    The card names the automation after it, and links to every member the
    review page still lists as needing review. Nothing is posted when the same
    card is already in the conversation, or when an admission refusal leaves
    nothing to review. A firing with nothing reviewable (a one-shot action
    rides its task) still gets a card without links, because the user would
    otherwise never learn it did not run.
    """
    reviews: list[ArtifactReview] = []
    for kind, artifact_id in dict.fromkeys(artifact_refs):
        review = await db.artifact_review.get(kind, artifact_id)
        if review is not None:
            reviews.append(review)
    automation_name = reviews[0].name if reviews else fallback_name
    pending = [review for review in reviews if needs_review(review.trust_tier)]
    if stage == "admission" and not pending:
        return None
    notice = ReviewRejectionNoticeDict(
        stage=stage,
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
    content = _content(stage, automation_name, pending)
    notice_id = await db.message_history.add_review_rejection_notice(
        interface_type=interface_type,
        conversation_id=conversation_id,
        notice=notice,
        key=_notice_key(stage, automation_name, source, pending),
        content=content,
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
        return None
    return PostedReviewRejection(
        interface_type=interface_type,
        conversation_id=conversation_id,
        content=content,
    )


async def announce_review_rejection(
    posted: PostedReviewRejection,
    *,
    chat_interfaces: Mapping[str, ChatInterface] | None,
    chat_interface: ChatInterface | None,
) -> None:
    """Tell the conversation's clients about a card already in its history.

    Sending would save it a second time. An interface whose clients do not
    read history (Telegram) is not told.
    """
    target = (chat_interfaces or {}).get(posted.interface_type, chat_interface)
    if isinstance(target, HistoryBackedChatInterface):
        await target.announce_persisted_message(posted.conversation_id, posted.content)
