"""The task that reviews one conversation's unreviewed stretch.

See docs/design/conversation-memory.md, "A curator reviews each conversation
when it goes idle". The sweep decides *which* conversation; this handler does
the review, and everything it needs it re-reads from stored state. The payload
names a conversation and nothing else, because a task row written minutes ago
is not evidence about the store now: the watermark may have moved, contribution
may have been turned off, and rows may have arrived.

**Where the terminal outcomes come from.** A review ends in exactly one of
four ways, and each advances the watermark past its chunk so no stretch is
looked at twice:

- ``applied`` -- the curator proposed edits and the apply path took them. The
  watermark moved inside that same transaction, not here.
- ``no_changes`` -- the curator read the stretch and proposed nothing.
- ``skipped`` -- the stretch has no admissible message from a person, or
  nobody spoke in it.
- ``abandoned`` -- the review was given up on: two refused proposals, two
  revision conflicts, or a turn that failed on the task's last attempt.

A review that finds no complete chunk yet advances nothing and records nothing.
The conversation stays due and the next sweep finds it again, which is what
keeps an unfinished turn from being reviewed before its outcome exists.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from family_assistant.memory.actor import MemoryActor, MemoryActorKind
from family_assistant.memory.due import NO_WATERMARK, select_review_rows
from family_assistant.memory.edits import EvidenceScope
from family_assistant.memory.review_context import MemoryReviewContext
from family_assistant.memory.transcript import (
    RenderedStretch,
    render_stretch,
    sender_labeller,
)
from family_assistant.observability.metrics import (
    record_memory_review_excluded_rows,
    record_memory_review_outcome,
    record_memory_review_skip,
)
from family_assistant.processing.service import ProcessingService
from family_assistant.security.taint import (
    TurnTaintState,
    merge_history_taint,
)
from family_assistant.security.taint_audit import taint_audit_sources
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence
    from datetime import datetime

    from family_assistant.memory.limits import MemoryLimits
    from family_assistant.memory.review_settings import MemoryReviewSettings
    from family_assistant.security.taint import TaintMetadata
    from family_assistant.storage.database import Database
    from family_assistant.storage.memory_change_log import MemoryChangeOutcome
    from family_assistant.storage.repositories.notes import MemoryTopicNote
    from family_assistant.storage.types import MessageHistoryRow
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

MEMORY_CURATOR_PROFILE_ID = "memory_curator"

MAX_REVIEW_ATTEMPTS = 2
"""The review itself, and one re-run against a store a person changed under it.

docs/design/conversation-memory.md: "the apply fails and the review is retried
against the fresh store". A second conflict means a person is editing while the
review runs, and waiting our turn is not worth a third model call.
"""

TAINT_SKIP_REASON = "external_taint"
NO_USER_MESSAGES_REASON = "no_user_messages"


def _no_configured_name(_user_id: str) -> str | None:
    """The name source of a deployment that configured none.

    A review still runs, and its transcript names each speaker by the id its
    rows carry, which is what keeps two speakers in one chat apart.
    """
    return None


class MemoryReviewResult(StrEnum):
    """How one run of the handler ended."""

    APPLIED = "applied"
    NO_CHANGES = "no_changes"
    SKIPPED = "skipped"
    ABANDONED = "abandoned"
    DEFERRED = "deferred"
    """Nothing was reviewable yet; the conversation stays due and unrecorded."""


class MemoryReviewRetry(Exception):
    """A review failed in a way the queue should try again.

    Raised rather than handled, so the task queue's own backoff decides when to
    re-run it. On the task's last attempt the handler abandons instead, which
    is what keeps a permanently failing conversation from blocking its own
    watermark for ever.
    """


def make_memory_review_handler(
    *,
    settings: MemoryReviewSettings,
    configured_contributors: Collection[str],
    limits: MemoryLimits,
    name_for_user_id: Callable[[str], str | None] = _no_configured_name,
    curator_profile_id: str = MEMORY_CURATOR_PROFILE_ID,
    # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
) -> Callable[[ToolExecutionContext, dict[str, Any]], Awaitable[None]]:
    """Bind the review to the configuration this process was started with.

    The same split as the sweep's: the settings, the configured contributors,
    the store bounds and the sender names are process-level facts that need a
    restart to change, while the stored enablement and the watermark are read
    per run.
    """

    async def handle_memory_review(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
        payload: dict[str, Any],
    ) -> None:
        interface_type = payload.get("interface_type")
        conversation_id = payload.get("conversation_id")
        if not interface_type or not conversation_id:
            raise ValueError(
                "A memory_review task needs both interface_type and "
                f"conversation_id in its payload; got {sorted(payload)}."
            )
        await run_memory_review(
            exec_context,
            interface_type=str(interface_type),
            conversation_id=str(conversation_id),
            settings=settings,
            configured_contributors=configured_contributors,
            limits=limits,
            name_for_user_id=name_for_user_id,
            curator_profile_id=curator_profile_id,
        )

    return handle_memory_review


async def run_memory_review(
    exec_context: ToolExecutionContext,
    *,
    interface_type: str,
    conversation_id: str,
    settings: MemoryReviewSettings,
    configured_contributors: Collection[str],
    limits: MemoryLimits,
    name_for_user_id: Callable[[str], str | None] = _no_configured_name,
    curator_profile_id: str = MEMORY_CURATOR_PROFILE_ID,
) -> MemoryReviewResult:
    """Review one conversation's next chunk. Returns how it ended."""
    db = exec_context.db_context
    now = (exec_context.clock or SystemClock()).now()

    chunk = await _next_chunk(
        db,
        interface_type=interface_type,
        conversation_id=conversation_id,
        settings=settings,
        configured_contributors=configured_contributors,
        limits=limits,
        name_for_user_id=name_for_user_id,
    )
    if chunk is None:
        return MemoryReviewResult.DEFERRED

    actor = MemoryActor(
        kind=MemoryActorKind.CURATOR,
        identity=curator_profile_id,
        interface_type=interface_type,
        conversation_id=conversation_id,
    )
    record_memory_review_excluded_rows(chunk.excluded_row_count)

    if chunk.user_row_count > 0 and chunk.admissible_user_row_count == 0:
        taint = _merged_chunk_taint(chunk.rows)
        # With no admissible household message there is nothing to curate.
        await _record_taint_audit(
            exec_context,
            interface_type=interface_type,
            conversation_id=conversation_id,
            chunk=chunk,
            taint=taint,
        )
        await _end_without_review(
            db,
            actor=actor,
            chunk=chunk,
            outcome="skipped",
            reason=(
                "The stretch carries unreviewed content authored outside the household "
                f"(taint tier {taint.max_tier.config_value}); memory holds "
                "nothing that originates outside it."
            ),
            now=now,
            skip_reason=TAINT_SKIP_REASON,
        )
        return MemoryReviewResult.SKIPPED

    if chunk.user_row_count == 0:
        await _end_without_review(
            db,
            actor=actor,
            chunk=chunk,
            outcome="skipped",
            reason="Nobody spoke in this stretch; there was nothing to curate.",
            now=now,
            skip_reason=NO_USER_MESSAGES_REASON,
        )
        return MemoryReviewResult.SKIPPED

    taint = _merged_chunk_taint([
        row
        for row in chunk.rows
        if int(row["internal_id"]) in chunk.rendered_message_ids
    ])
    curator = _resolve_curator(exec_context, curator_profile_id)
    review = await _run_curator_attempts(
        exec_context,
        curator=curator,
        interface_type=interface_type,
        conversation_id=conversation_id,
        chunk=chunk,
        taint=taint,
        limits=limits,
    )

    if review.progress.applied:
        record_memory_review_outcome(MemoryReviewResult.APPLIED)
        logger.info(
            "Memory review of %s:%s applied edits; store at revision %s.",
            interface_type,
            conversation_id,
            review.progress.applied_revision,
        )
        return MemoryReviewResult.APPLIED

    if review.progress.refused_proposals == 0:
        await _end_without_review(
            db,
            actor=actor,
            chunk=chunk,
            outcome="no_changes",
            reason="The curator read the stretch and proposed no edits.",
            now=now,
            skip_reason=None,
        )
        return MemoryReviewResult.NO_CHANGES

    await _end_without_review(
        db,
        actor=actor,
        chunk=chunk,
        outcome="abandoned",
        reason=_abandon_reason(review),
        now=now,
        skip_reason=None,
    )
    return MemoryReviewResult.ABANDONED


# ---------------------------------------------------------------------------
# Choosing the chunk
# ---------------------------------------------------------------------------


async def _next_chunk(
    db: Database,
    *,
    interface_type: str,
    conversation_id: str,
    settings: MemoryReviewSettings,
    configured_contributors: Collection[str],
    limits: MemoryLimits,
    name_for_user_id: Callable[[str], str | None],
) -> RenderedStretch | None:
    """The stretch this review covers, or None when there is nothing to review."""
    if not settings.enabled or not configured_contributors:
        return None

    enablement = await db.memory_review.get_enablement()
    contributing = {
        profile_id: enabled_at
        for profile_id, enabled_at in enablement.items()
        if profile_id in configured_contributors
    }
    if not contributing:
        return None

    watermark = await db.memory_review.get_watermark(
        interface_type=interface_type, conversation_id=conversation_id
    )
    rows = await select_review_rows(
        db,
        interface_type=interface_type,
        conversation_id=conversation_id,
        watermark=(
            watermark.last_reviewed_internal_id
            if watermark is not None
            else NO_WATERMARK
        ),
        settings=settings,
        contributing_profiles=contributing,
    )
    if not rows:
        return None

    chunk = render_stretch(
        rows,
        budget_chars=limits.review_transcript_max_chars,
        completed_turn_ids=await _completed_turn_ids(db, rows),
        sender_label=sender_labeller(name_for_user_id),
    )
    return None if chunk.is_empty else chunk


async def _completed_turn_ids(
    db: Database, rows: Sequence[MessageHistoryRow]
) -> set[str]:
    """Which of these rows' turns have reached a terminal reply.

    Asked of the whole turn rather than of the rows in hand: a turn can start
    before the watermark, and its terminal reply is what says it finished, not
    which of its rows are unreviewed.
    """
    turn_ids = sorted({
        str(row["turn_id"]) for row in rows if row.get("turn_id") is not None
    })
    completed: set[str] = set()
    for turn_id in turn_ids:
        if await db.message_history.has_terminal_reply_for_turn(turn_id):
            completed.add(turn_id)
    return completed


# ---------------------------------------------------------------------------
# The curator turn
# ---------------------------------------------------------------------------


async def _run_curator_attempts(
    exec_context: ToolExecutionContext,
    *,
    curator: ProcessingService,
    interface_type: str,
    conversation_id: str,
    chunk: RenderedStretch,
    taint: TurnTaintState,
    limits: MemoryLimits,
) -> MemoryReviewContext:
    """Run the review, re-running it once if a person moved the store under it.

    Returns the context of the last attempt, which carries what that attempt
    did. Each attempt re-reads the revision and the entries, so the re-run
    proposes against what the person left behind rather than against what the
    review first saw.
    """
    db = exec_context.db_context
    review = _fresh_review_context(
        interface_type=interface_type,
        conversation_id=conversation_id,
        chunk=chunk,
        expected_revision=0,
    )

    for attempt in range(1, MAX_REVIEW_ATTEMPTS + 1):
        expected_revision = await db.memory_store.get_revision()
        topics = await db.notes.get_memory_topic_notes(
            read_policy=_curator_read_policy(curator)
        )
        review = _fresh_review_context(
            interface_type=interface_type,
            conversation_id=conversation_id,
            chunk=chunk,
            expected_revision=expected_revision,
        )
        await _run_curator_turn(
            exec_context,
            curator=curator,
            interface_type=interface_type,
            conversation_id=conversation_id,
            request=_render_request(chunk, topics, limits=limits),
            taint=taint,
            review=review,
        )
        if review.progress.applied or not review.progress.conflicted:
            return review
        if attempt < MAX_REVIEW_ATTEMPTS:
            record_memory_review_outcome("conflict_retry")
            logger.info(
                "Memory review of %s:%s conflicted with a change made while it "
                "ran; re-running against the fresh store.",
                interface_type,
                conversation_id,
            )

    return review


def _fresh_review_context(
    *,
    interface_type: str,
    conversation_id: str,
    chunk: RenderedStretch,
    expected_revision: int,
) -> MemoryReviewContext:
    """A review context for one attempt, with an untouched proposal budget.

    Each attempt gets its own: a re-run after a conflict is a fresh review of
    the same stretch against a store somebody else has changed, so it gets its
    own list and its own retry rather than inheriting a spent budget.
    """
    return MemoryReviewContext(
        evidence_scope=EvidenceScope.for_stretch(
            interface_type=interface_type,
            conversation_id=conversation_id,
            first_internal_id=chunk.first_internal_id,
            last_internal_id=chunk.last_internal_id,
            allowed_message_ids=chunk.rendered_message_ids,
        ),
        expected_revision=expected_revision,
        batch_id=str(uuid.uuid4()),
        interface_type=interface_type,
        conversation_id=conversation_id,
        watermark_target=chunk.last_internal_id,
    )


async def _run_curator_turn(
    exec_context: ToolExecutionContext,
    *,
    curator: ProcessingService,
    interface_type: str,
    conversation_id: str,
    request: str,
    taint: TurnTaintState,
    review: MemoryReviewContext,
) -> None:
    """One curator turn, in an internal subconversation of its own.

    The subconversation is what keeps these rows out of the user's prompt
    window, out of the conversation list and out of its own future reviews --
    the eligibility predicate admits only rows with no subconversation. They
    stay under the reviewed conversation's own identifiers, so diagnostics can
    find a review beside the conversation it was about.

    No chat interface is passed and the curator has no messaging tools, so its
    reply is persisted and goes nowhere.

    Raises:
        MemoryReviewRetry: when the turn failed and the queue still has an
            attempt left to give it.
    """
    result = await curator.handle_chat_interaction(
        db_context=exec_context.db_context,
        interface_type=interface_type,
        conversation_id=conversation_id,
        trigger_content_parts=[{"type": "text", "text": request}],
        trigger_interface_message_id=None,
        user_name=exec_context.user_name,
        user_id=None,
        # A bare uuid: the column is 36 characters wide, which a prefix would
        # overflow on PostgreSQL. The subconversation is identified by the
        # rows' profile, not by its name.
        subconversation_id=str(uuid.uuid4()),
        # Nobody wrote this request, so it stays out of user-facing history and
        # the turn runs at the profile's configured tier rather than routed.
        trigger_is_internal=True,
        initial_taint_sources=taint.sources,
        memory_review=review,
    )
    if result.error_traceback is None:
        return

    attempt = exec_context.task_attempt
    if attempt is not None and not attempt.is_final:
        raise MemoryReviewRetry(
            f"The curator turn for {interface_type}:{conversation_id} failed; "
            f"the queue will try it again. {result.error_traceback}"
        )
    # Out of attempts: fall through so the caller abandons the chunk rather
    # than leaving this conversation's watermark stuck behind it for ever.
    review.progress.refused_proposals += 1
    logger.warning(
        "Memory review of %s:%s failed on its last attempt: %s",
        interface_type,
        conversation_id,
        result.error_traceback,
    )


def _curator_read_policy(curator: ProcessingService) -> NoteReadPolicy:
    """The confinement the curator's own note reads run under.

    Derived from the profile's config through the same constructor the tool
    layer uses, so the request this task renders shows the curator exactly the
    notes its own ``get_note`` would return -- a note labelled beyond its
    grants is hidden by both or by neither.
    """
    config = curator.service_config
    return NoteReadPolicy.for_profile(
        visibility_grants=config.visibility_grants,
        required_labels=config.required_note_read_labels,
        memory_read=config.memory_read,
    )


def _resolve_curator(
    exec_context: ToolExecutionContext, curator_profile_id: str
) -> ProcessingService:
    """The local profile a review runs under.

    Raises:
        RuntimeError: when it is not registered, which is a deployment fault
            rather than a review outcome -- abandoning the stretch would hide a
            misconfiguration behind a growing pile of abandoned reviews.
    """
    service = exec_context.processing_service
    registry = service.processing_services_registry if service is not None else None
    candidate = registry.get(curator_profile_id) if registry is not None else None
    if not isinstance(candidate, ProcessingService):
        raise RuntimeError(
            f"Memory review cannot run: no local processing profile "
            f"'{curator_profile_id}' is registered."
        )
    return candidate


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def _render_request(
    chunk: RenderedStretch,
    topics: Sequence[MemoryTopicNote],
    *,
    limits: MemoryLimits,
) -> str:
    """The curator's whole input: the stretch, and the entries it may update.

    Data, not instructions: what the curator is asked to do lives in its system
    prompt, and everything here is material it reasons over. The core memory
    note is deliberately absent -- the notes context provider puts it in front
    of every turn on this profile already.

    Topics are shown whole while they fit in what the transcript left of
    ``review_input_max_chars``, most recently changed first. There is no
    relevance selection in v1: picking the "relevant" topics would need a
    retrieval step over the corpus the curator is confined away from, and the
    caps are small enough that the whole store usually fits.
    """
    sections = [
        "## Conversation under review",
        "",
        chunk.text,
    ]
    remaining = limits.review_input_max_chars - len(chunk.text)
    shown = _topics_that_fit(topics, remaining)
    if shown:
        sections.extend(["", "## Memory entries you may update", ""])
        for topic in shown:
            sections.extend([f"### {topic.title}", "", topic.content, ""])
    if len(shown) < len(topics):
        sections.append(
            f"({len(topics) - len(shown)} further memory topic note(s) exist "
            "and are not shown here; read one with get_note if you need it.)"
        )
    return "\n".join(sections).strip()


def _topics_that_fit(
    topics: Sequence[MemoryTopicNote], budget_chars: int
) -> list[MemoryTopicNote]:
    """As many topics as the remaining budget holds, in the order given."""
    shown: list[MemoryTopicNote] = []
    used = 0
    for topic in topics:
        cost = len(topic.title) + len(topic.content) + 8
        if used + cost > budget_chars:
            break
        shown.append(topic)
        used += cost
    return shown


# ---------------------------------------------------------------------------
# Terminal outcomes
# ---------------------------------------------------------------------------


async def _end_without_review(
    db: Database,
    *,
    actor: MemoryActor,
    chunk: RenderedStretch,
    outcome: MemoryChangeOutcome,
    reason: str,
    now: datetime,
    skip_reason: str | None,
) -> None:
    """Record the outcome and move the watermark past the chunk.

    Not one transaction: the change-log row is a record and the watermark is
    the state, and a crash between them leaves at worst a recorded outcome for
    a stretch the next review looks at again -- which is the harmless order.
    Doing it the other way round would lose the record of a stretch nobody will
    ever look at again.
    """
    await db.memory_change_log.add_review_outcome(
        batch_id=str(uuid.uuid4()),
        actor=actor,
        outcome=outcome,
        reason=reason,
        now=now,
    )
    await db.memory_review.advance_watermark(
        interface_type=actor.interface_type or "",
        conversation_id=actor.conversation_id or "",
        last_reviewed_internal_id=chunk.last_internal_id,
        now=now,
    )
    record_memory_review_outcome(outcome)
    if skip_reason is not None:
        record_memory_review_skip(
            reason=skip_reason,
            rows=len(chunk.rows),
            user_chars=chunk.user_char_count,
        )
    logger.info(
        "Memory review of %s:%s ended as %s: %s",
        actor.interface_type,
        actor.conversation_id,
        outcome,
        reason,
    )


def _abandon_reason(review: MemoryReviewContext) -> str:
    """Why this review was given up on, for the recent-changes view."""
    if review.progress.conflicted:
        return (
            "Memory was changed by somebody else while this review ran, twice "
            "over; the review was given up on rather than proposed against a "
            "store that keeps moving."
        )
    return (
        f"The curator's proposals were refused {review.progress.refused_proposals} "
        "time(s), which is the whole of a review's budget: one list and one "
        "retry with the reasons fed back."
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TaintCarrier:
    """A row's stored taint, in the shape ``merge_history_taint`` reads.

    That function takes messages rather than rows, because every other caller
    has already turned rows into messages. A review has not: it renders rows
    directly, and only the provenance travels.
    """

    taint_metadata: TaintMetadata | None


def _merged_chunk_taint(rows: Sequence[MessageHistoryRow]) -> TurnTaintState:
    """The provenance the whole chunk carries, tool results included."""
    return merge_history_taint([
        _TaintCarrier(row.get("taint_metadata")) for row in rows
    ])


async def _record_taint_audit(
    exec_context: ToolExecutionContext,
    *,
    interface_type: str,
    conversation_id: str,
    chunk: RenderedStretch,
    taint: TurnTaintState,
) -> None:
    """Leave an audit record of a stretch that was excluded on its provenance.

    The skip is silent otherwise: nothing was run, so no turn carries it, and
    the design makes measuring this loss part of the first milestone.
    """
    await exec_context.db_context.taint_audit_events.add(
        event_id=str(uuid.uuid4()),
        event_type="memory_review_skipped",
        conversation_id=conversation_id,
        turn_id=None,
        processing_profile_id=MEMORY_CURATOR_PROFILE_ID,
        subconversation_id=None,
        tool_name="memory_review",
        tool_call_id=None,
        sink_class=None,
        max_tier=taint.max_tier.config_value,
        sources=taint_audit_sources(taint),
        requested_outcome="review",
        effective_outcome="skipped",
        mode="enforce",
        reason=(
            f"Memory review of {interface_type}:{conversation_id} skipped "
            f"messages #{chunk.first_internal_id}-#{chunk.last_internal_id}: "
            "the stretch carries content authored outside the household."
        ),
        arguments_summary=None,
    )
