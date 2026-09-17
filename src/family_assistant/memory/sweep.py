"""The recurring task that turns the due predicate into review work.

See docs/design/conversation-memory.md, "Reviews are scheduled from state, not
from events". This handler evaluates :func:`~family_assistant.memory.due.
select_due_conversations` and enqueues one review per due conversation, keyed
on the conversation so the same conversation never has two reviews in flight.

**Dedup is the queue's, not this module's.** The review task id is derived from
the conversation, and a duplicate non-system id is refused by the tasks
repository, so "one review per conversation at a time" is held by a primary key
rather than by a check this handler performs and could race. What the handler
does first is clear away a *finished* row with the same id -- a terminal row
keeps its id reserved -- so the next sweep can enqueue again once a review is
over.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from family_assistant.memory.due import DueReason, select_due_conversations
from family_assistant.observability.metrics import (
    record_memory_conversations_due,
    record_memory_review_enqueued,
)
from family_assistant.storage.repositories.tasks import TaskAlreadyExistsError
from family_assistant.storage.tasks import TaskPriority
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection

    from family_assistant.memory.review_settings import MemoryReviewSettings
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

MEMORY_REVIEW_SWEEP_TASK_TYPE = "memory_review_sweep"
MEMORY_REVIEW_SWEEP_TASK_ID = "system_memory_review_sweep"
MEMORY_REVIEW_TASK_TYPE = "memory_review"


def memory_review_task_id(interface_type: str, conversation_id: str) -> str:
    """The one task id a review of this conversation may hold.

    Deterministic on purpose: the queue's uniqueness on it is what serialises
    reviews of a conversation.
    """
    return f"memory_review:{interface_type}:{conversation_id}"


def make_memory_review_sweep_handler(
    *,
    settings: MemoryReviewSettings,
    configured_contributors: Collection[str],
    # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
) -> Callable[[ToolExecutionContext, dict[str, Any]], Awaitable[None]]:
    """Bind the sweep to the configuration this process was started with.

    The settings and the set of profiles an operator configured to contribute
    are process-level facts, and a change to either needs a restart in any
    case; what the handler reads per run is the stored enablement, which is the
    half that moves.
    """

    async def handle_memory_review_sweep(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
        payload: dict[str, Any],
    ) -> None:
        del payload
        await run_memory_review_sweep(
            exec_context,
            settings=settings,
            configured_contributors=configured_contributors,
        )

    return handle_memory_review_sweep


async def run_memory_review_sweep(
    exec_context: ToolExecutionContext,
    *,
    settings: MemoryReviewSettings,
    configured_contributors: Collection[str],
) -> int:
    """Enqueue a review for every due conversation. Returns how many it wrote.

    Cheap to run when memory is off or nothing contributes, because that is the
    shipped state and a previously seeded sweep survives the switch being
    turned off.
    """
    if not settings.enabled or not configured_contributors:
        record_memory_conversations_due(dict.fromkeys(DueReason, 0))
        return 0

    db = exec_context.db_context
    now = (exec_context.clock or SystemClock()).now()
    enablement = await db.memory_review.get_enablement()
    contributing = {
        profile_id: enabled_at
        for profile_id, enabled_at in enablement.items()
        if profile_id in configured_contributors
    }

    due = await select_due_conversations(
        db, now=now, settings=settings, contributing_profiles=contributing
    )
    counts = Counter(conversation.reason for conversation in due)
    record_memory_conversations_due({
        reason: counts.get(reason, 0) for reason in DueReason
    })

    enqueued = 0
    for conversation in due:
        if await _enqueue_review(
            exec_context, conversation.interface_type, conversation.conversation_id
        ):
            enqueued += 1
    if due:
        logger.info(
            f"Memory review sweep: {len(due)} conversation(s) due, "
            f"{enqueued} review(s) enqueued."
        )
    return enqueued


async def _enqueue_review(
    exec_context: ToolExecutionContext, interface_type: str, conversation_id: str
) -> bool:
    """Write one review task, unless one for this conversation is in flight."""
    db = exec_context.db_context
    task_id = memory_review_task_id(interface_type, conversation_id)
    # A terminal row keeps its id reserved; clearing it is what lets the next
    # stretch of the same conversation be reviewed. A pending or processing row
    # is left alone, and the enqueue below is what reports it.
    await db.tasks.delete_finished(task_id)
    try:
        await db.tasks.enqueue(
            task_id=task_id,
            task_type=MEMORY_REVIEW_TASK_TYPE,
            payload={
                "interface_type": interface_type,
                "conversation_id": conversation_id,
            },
            priority=TaskPriority.BACKGROUND,
        )
    except TaskAlreadyExistsError:
        record_memory_review_enqueued(
            interface_type=interface_type, outcome="in_flight"
        )
        return False
    record_memory_review_enqueued(interface_type=interface_type, outcome="enqueued")
    return True
