"""The sweep as a task the worker actually runs.

Slice 4 of docs/design/conversation-memory.md. The predicate and the enqueue
have their own tests; what this one proves is the seam between them and the
queue -- that the handler the assistant registers matches the registry's
signature, and that a sweep occurrence run by a real worker leaves a review
task behind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.memory.review_settings import MemoryReviewSettings
from family_assistant.memory.sweep import (
    MEMORY_REVIEW_SWEEP_TASK_ID,
    MEMORY_REVIEW_SWEEP_TASK_TYPE,
    MEMORY_REVIEW_TASK_TYPE,
    make_memory_review_sweep_handler,
    memory_review_task_id,
)
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from tests.helpers import wait_for_condition

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from family_assistant.task_worker import TaskWorker

CONTRIBUTOR = "default_assistant"
CONVERSATION = "conv-1"

SETTINGS = MemoryReviewSettings(
    idle_window_minutes={"web": 30},
    contributing_interfaces=frozenset({"web"}),
)


@pytest.mark.asyncio
async def test_a_sweep_occurrence_enqueues_a_review(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    worker, new_task_event, _ = task_worker_manager(
        processing_service=MagicMock(), chat_interface=MagicMock()
    )
    engine = worker.engine
    assert engine is not None
    worker.register_task_handler(
        MEMORY_REVIEW_SWEEP_TASK_TYPE,
        make_memory_review_sweep_handler(
            settings=SETTINGS, configured_contributors={CONTRIBUTOR}
        ),
    )

    db = Database(engine=engine)
    now = datetime.now(UTC)
    await db.memory_review.record_enablement(
        profile_ids_contributing={CONTRIBUTOR}, now=now - timedelta(days=1)
    )
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="we always take the tram"),
        interface_type="web",
        conversation_id=CONVERSATION,
        timestamp=now - timedelta(minutes=45),
        turn_id="turn-1",
        processing_profile_id=CONTRIBUTOR,
        user_id="alice",
    )
    await db.tasks.enqueue(
        task_id=MEMORY_REVIEW_SWEEP_TASK_ID,
        task_type=MEMORY_REVIEW_SWEEP_TASK_TYPE,
        payload={},
        recurrence_rule=f"FREQ=MINUTELY;INTERVAL={SETTINGS.sweep_interval_minutes}",
        priority=TaskPriority.BACKGROUND,
    )
    new_task_event.set()

    async def review_enqueued() -> bool:
        tasks = await Database(engine=engine).tasks.get_all(
            task_type=MEMORY_REVIEW_TASK_TYPE
        )
        return any(
            task["task_id"] == memory_review_task_id("web", CONVERSATION)
            for task in tasks
        )

    await wait_for_condition(
        review_enqueued, description="the sweep to enqueue a memory review"
    )
