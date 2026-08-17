"""What happens when a delegation result cannot be delivered.

A terminal run that cannot be delivered used to be retried every hour forever:
the transport reported failure without saying whether the same text could ever
succeed, so retrying was the only response available. A permanently refused
message therefore failed identically every hour and the requester never learned
their work had finished.

Now a permanent failure is handed back to the delegating profile -- the one
thing here that can decide what to send instead -- bounded by a delivery stage
so a dead channel cannot spin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from family_assistant.interfaces import ChatDeliveryError, ChatInterface
from family_assistant.storage.database import Database
from family_assistant.storage.delegation_runs import delegation_runs_table
from family_assistant.task_worker import DelegationRunCleanupPayload, TaskWorker
from family_assistant.utils.clock import SystemClock

from .test_async_delegation import (
    FakeDelegatableService,
    FakeWakeCapableSourceService,
    _create_run,
    _tool_context,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing import ProcessingService

_CLEANUP_PAYLOAD = DelegationRunCleanupPayload(running_timeout_seconds=0)


class _TriggerRecordingSourceService(FakeWakeCapableSourceService):
    """Records the trigger each wake was given, so tests can read it back."""

    def __init__(self, target_service: FakeDelegatableService) -> None:
        super().__init__(target_service)
        self.wake_triggers: list[str] = []

    async def handle_chat_interaction(self, **kwargs: Any) -> Any:  # noqa: ANN401 - test fake mirrors the ProcessingService keyword surface
        parts = cast("list[dict[str, str]]", kwargs["trigger_content_parts"])
        self.wake_triggers.append(" ".join(part.get("text", "") for part in parts))
        return await super().handle_chat_interaction(**kwargs)


def _worker(
    db_engine: AsyncEngine,
    processing_service: ProcessingService,
    chat_interface: ChatInterface,
) -> TaskWorker:
    return TaskWorker(
        processing_service=processing_service,
        chat_interface=chat_interface,
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
    )


async def _terminal_run(db_engine: AsyncEngine, delegation_id: str) -> None:
    """A completed, handed-off run waiting to be delivered."""
    db_context = Database(engine=db_engine)
    clock = SystemClock()
    await _create_run(db_context, delegation_id=delegation_id)
    await db_context.delegation_runs.mark_handed_off(delegation_id, clock.now())
    await db_context.delegation_runs.mark_completed(
        delegation_id=delegation_id,
        result_text="the delegated work, in full",
        result_attachment_ids=[],
        completed_at=clock.now(),
    )


async def _run_cleanup(
    db_engine: AsyncEngine,
    processing_service: ProcessingService,
    chat_interface: ChatInterface,
) -> None:
    worker = _worker(db_engine, processing_service, chat_interface)
    await worker.handle_delegation_run_cleanup(
        _tool_context(Database(engine=db_engine), processing_service, chat_interface),
        _CLEANUP_PAYLOAD,
    )


@pytest.mark.asyncio
async def test_a_permanent_failure_asks_the_model_for_something_deliverable(
    db_engine: AsyncEngine,
) -> None:
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = [
        ChatDeliveryError("Message is too long", transient=False),
        "delivered_after_fail_forward",
    ]
    await _terminal_run(db_engine, "delegation_permanent")

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    # Woken twice: once with the result, once with the failure to deliver it.
    assert processing_service.wake_call_count == 2
    assert "could not be delivered" in processing_service.wake_triggers[1]
    assert "Message is too long" in processing_service.wake_triggers[1]

    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        "delegation_permanent"
    )
    assert run is not None
    assert run["notified_at"] is not None
    assert run["notify_stage"] == "failed_forward"


@pytest.mark.asyncio
async def test_a_transient_failure_is_left_for_a_later_retry(
    db_engine: AsyncEngine,
) -> None:
    """Rewriting a fine reply because the network blipped would burn a turn."""
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = ChatDeliveryError(
        "connection reset", transient=True
    )
    await _terminal_run(db_engine, "delegation_transient")

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        "delegation_transient"
    )
    assert run is not None
    assert run["notified_at"] is None
    assert run["notify_stage"] == "initial"
    assert run["notify_attempts"] == 1
    assert run["notify_first_failed_at"] is not None


@pytest.mark.asyncio
async def test_a_failing_run_is_not_retried_every_hour(db_engine: AsyncEngine) -> None:
    """A channel refusing for hours is not persuaded by asking again on the hour."""
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = ChatDeliveryError(
        "connection reset", transient=True
    )
    await _terminal_run(db_engine, "delegation_backoff")
    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )
    sends_after_first_failure = chat_interface.send_message.await_count

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    assert chat_interface.send_message.await_count == sends_after_first_failure


@pytest.mark.asyncio
async def test_a_transient_failure_that_never_recovers_stops_being_treated_as_one(
    db_engine: AsyncEngine,
) -> None:
    """A day of 'try again later' is an outage, not a blip."""
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = [
        # The reply, then the standard notice it falls back to: the whole
        # channel is down, not just this one message.
        ChatDeliveryError("connection reset", transient=True),
        ChatDeliveryError("connection reset", transient=True),
        "delivered_after_fail_forward",
    ]
    await _terminal_run(db_engine, "delegation_long_outage")
    db_context = Database(engine=db_engine)
    await db_context.execute(
        update(delegation_runs_table)
        .where(delegation_runs_table.c.delegation_id == "delegation_long_outage")
        .values(notify_first_failed_at=datetime.now(UTC) - timedelta(days=3))
    )

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    assert processing_service.wake_call_count == 2
    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        "delegation_long_outage"
    )
    assert run is not None
    assert run["notify_stage"] == "failed_forward"


@pytest.mark.asyncio
async def test_an_undeliverable_run_ends_in_gave_up_rather_than_notified(
    db_engine: AsyncEngine,
) -> None:
    """Nothing got through, so the run must not count as delivered.

    A run marked notified would stop being retried while claiming the requester
    was reached, which is the silent failure this whole path exists to remove.
    """
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = ChatDeliveryError(
        "Forbidden: bot was blocked by the user", transient=False
    )
    await _terminal_run(db_engine, "delegation_dead_channel")

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        "delegation_dead_channel"
    )
    assert run is not None
    assert run["notify_stage"] == "gave_up"
    assert run["notified_at"] is None
    assert "blocked" in (run["notify_error"] or "")
    # The result reply, the fail-forward reply, and the canned notice: no more.
    assert chat_interface.send_message.await_count == 3
    assert processing_service.wake_call_count == 2


@pytest.mark.asyncio
async def test_a_gave_up_run_is_not_picked_up_again(db_engine: AsyncEngine) -> None:
    """Otherwise the hourly pass resumes exactly the loop this replaced."""
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = ChatDeliveryError(
        "chat not found", transient=False
    )
    await _terminal_run(db_engine, "delegation_given_up")
    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )
    sends_after_giving_up = chat_interface.send_message.await_count

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    assert chat_interface.send_message.await_count == sends_after_giving_up


@pytest.mark.asyncio
async def test_a_transient_failure_delivering_the_fail_forward_reply_does_not_rerun_it(
    db_engine: AsyncEngine,
) -> None:
    """The fail-forward turn's tools commit as it runs, so it runs once.

    Its turn id comes from the persisted stage rather than an attempt count,
    precisely so a retried delivery resumes at delivery instead of waking the
    model a second time and repeating everything it did.
    """
    processing_service = _TriggerRecordingSourceService(FakeDelegatableService())
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.side_effect = [
        ChatDeliveryError("Message is too long", transient=False),
        ChatDeliveryError("connection reset", transient=True),
        "delivered_on_the_next_pass",
    ]
    await _terminal_run(db_engine, "delegation_resumes_at_delivery")

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )
    assert processing_service.wake_call_count == 2

    await _run_cleanup(
        db_engine, cast("ProcessingService", processing_service), chat_interface
    )

    # Still two: the second pass delivered the reply the first pass generated.
    assert processing_service.wake_call_count == 2
    run = await Database(engine=db_engine).delegation_runs.get_by_delegation_id(
        "delegation_resumes_at_delivery"
    )
    assert run is not None
    assert run["notified_at"] is not None
