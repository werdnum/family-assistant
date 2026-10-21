"""Tests for cancel_delegation: stopping a delegation nobody wants any more.

Each test drives the real tool and, where it matters, the real worker handler
that would otherwise have carried the run on, against the fakes the other
delegation tests use.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.processing import RemoteDisposition
from family_assistant.processing.delegation_turns import RunningDelegationTurns
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.storage.database import Database
from family_assistant.tools.services import (
    cancel_delegation_tool,
    list_delegations_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import SystemClock
from tests.functional.automations.test_async_delegation import (
    TEST_CONVERSATION_ID,
    TEST_INTERFACE_TYPE,
    TEST_USER_NAME,
    FakeDelegatableService,
    FakeDelegationCall,
    FakePollableService,
    _build_worker,
    _create_run,
    _payload,
    _source_processing_service,
    _tool_context,
)
from tests.functional.automations.test_delegation_reconciliation import (
    REMOTE_TASK_ID,
    FakeObservableService,
    _observation,
    _reconcile_payload,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing.service import ProcessingService
    from family_assistant.processing.types import ChatInteractionResult
    from family_assistant.task_worker import DelegationPollPayload
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.types import ConfirmationOutcome


RUN_USER_ID = "async-delegation-user"


class BlockingTurnService(FakeDelegatableService):
    """A local target whose turn runs until something stops it."""

    def __init__(
        self,
        delegation_turns: RunningDelegationTurns,
        *,
        ask_for_confirmation: bool = False,
    ) -> None:
        super().__init__()
        self.delegation_turns = delegation_turns
        self.ask_for_confirmation = ask_for_confirmation
        self.waiting = asyncio.Event()
        self.stopped = False

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        self.calls.append(cast("FakeDelegationCall", kwargs))
        try:
            if self.ask_for_confirmation:
                await self._ask_for_confirmation(kwargs)
            self.waiting.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stopped = True
            raise
        raise AssertionError("the turn was never stopped")

    @staticmethod
    # ast-grep-ignore: no-dict-any - the ProcessingService keyword surface, passed through
    async def _ask_for_confirmation(kwargs: dict[str, Any]) -> None:
        db_context: Database = kwargs["db_context"]
        turn_id = "delegated_blocking_turn"
        await db_context.message_history.add_message(
            UserMessage.from_trusted_user(content="delegated target request"),
            interface_type=kwargs["interface_type"],
            conversation_id=kwargs["conversation_id"],
            timestamp=SystemClock().now(),
            turn_id=turn_id,
            subconversation_id=kwargs["subconversation_id"],
            user_id=RUN_USER_ID,
        )
        callback = kwargs["request_confirmation_callback"]
        assert callback is not None
        await callback(
            interface_type=kwargs["interface_type"],
            conversation_id=kwargs["conversation_id"],
            turn_id=turn_id,
            tool_name="confirmable_delegated_tool",
            call_id="confirmable_call_1",
            tool_args={"action": "write"},
            timeout_seconds=600.0,
            context=ToolExecutionContext(
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                user_name=TEST_USER_NAME,
                user_id=RUN_USER_ID,
                turn_id=turn_id,
                db_context=db_context,
                processing_service=None,
                clock=SystemClock(),
                plugins=None,
                event_sources=None,
                attachment_registry=None,
                timezone=ZoneInfo("UTC"),
                credential_resolvers=None,
                api_backend=None,
            ),
        )


class DurableBlockingConfirmationManager:
    """Records a durable confirmation, as the web's manager does, then waits."""

    def __init__(self, confirmations: ConfirmationService) -> None:
        self._confirmations = confirmations
        self.asked = asyncio.Event()
        self.request_id: str | None = None

    async def request_confirmation(self, **kwargs: Any) -> ConfirmationOutcome:  # noqa: ANN401 - fake accepts the ConfirmationUIManager keyword surface
        request = await self._confirmations.create_request(
            target_user_id=kwargs["target_user_id"],
            tool_name=kwargs["tool_name"],
            tool_args=kwargs["tool_args"],
            tool_call_id=kwargs["tool_call_id"],
            source_message_internal_id=kwargs["source_message_internal_id"],
            confirmation_prompt=kwargs["prompt_text"],
            expires_at=datetime.now(UTC) + timedelta(seconds=kwargs["timeout"]),
            decision_only=True,
        )
        self.request_id = request["id"]
        self.asked.set()
        await asyncio.Event().wait()
        raise AssertionError("the confirmation was never answered")


class MeteredPollableService(FakePollableService):
    """A remote target that accounts for each run once it is over."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[tuple[str, bool]] = []

    async def record_terminal_metrics(
        self, remote_task_id: str, *, outcome: str = "success", cancelled: bool = False
    ) -> None:
        _ = outcome
        self.recorded.append((remote_task_id, cancelled))


def _poll_payload(delegation_id: str) -> DelegationPollPayload:
    return {
        "delegation_id": delegation_id,
        "interface_type": TEST_INTERFACE_TYPE,
        "conversation_id": TEST_CONVERSATION_ID,
        "user_name": TEST_USER_NAME,
    }


async def _remote_run(db_context: Database, delegation_id: str) -> None:
    await _create_run(db_context, delegation_id=delegation_id)
    await db_context.delegation_runs.mark_handed_off(delegation_id, SystemClock().now())
    await db_context.delegation_runs.mark_awaiting_remote(
        delegation_id,
        remote_task_id=REMOTE_TASK_ID,
        remote_context_id=None,
        started_at=SystemClock().now(),
    )


def _processing_service(target: FakePollableService) -> ProcessingService:
    return _source_processing_service(cast("FakeDelegatableService", target))


@pytest.mark.asyncio
async def test_cancelling_a_remote_run_stops_it_and_drops_its_result(
    db_engine: AsyncEngine,
) -> None:
    """The provider is asked to stop, and a later completion is never delivered.

    The duplicate coder job that prompted the tool finished anyway after it
    was no longer wanted; its result must not come back as a late recovery.
    """
    target = FakeObservableService([
        _observation(RemoteDisposition.COMPLETED, output_text="unwanted work")
    ])
    processing_service = _processing_service(target)
    chat_interface = AsyncMock()
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_duplicate")
    context = _tool_context(db_context, processing_service, chat_interface)

    result = await cancel_delegation_tool(context, "delegation_duplicate")

    assert result.text is not None
    assert "Cancelled delegation 'delegation_duplicate'" in result.text
    assert "asked to stop" in result.text
    assert "may still carry on" in result.text
    assert target.cancelled == [REMOTE_TASK_ID]
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_duplicate")
    assert run is not None
    assert run["status"] == "failed"
    assert run["local_failure_kind"] == "cancelled"
    assert run["cancel_requested_at"] is not None
    assert run["notified_at"] is not None

    worker = _build_worker(db_engine, processing_service, chat_interface)
    await worker.handle_delegation_poll(context, _poll_payload("delegation_duplicate"))
    await worker.handle_delegation_reconcile(
        context, _reconcile_payload("delegation_duplicate")
    )

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_duplicate")
    assert run is not None
    assert run["status"] == "failed"
    assert run["late_recovered_at"] is None
    assert target.observe_calls == []
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_run_is_listed_as_cancelled_and_no_longer_pending(
    db_engine: AsyncEngine,
) -> None:
    """The model can tell a cancelled run from one that failed on its own."""
    target = FakePollableService()
    processing_service = _processing_service(target)
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_listed")
    context = _tool_context(db_context, processing_service)

    await cancel_delegation_tool(context, "delegation_listed")
    listing = await list_delegations_tool(context)

    assert isinstance(listing.data, list)
    [summary] = listing.data
    assert summary["status"] == "failed"
    assert summary["cancelled"] is True
    assert (
        await db_context.delegation_runs.list_pending_for_conversation(
            conversation_id=TEST_CONVERSATION_ID
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_queued_run_is_never_started_once_cancelled(
    db_engine: AsyncEngine,
) -> None:
    target = FakeDelegatableService()
    processing_service = _source_processing_service(target)
    chat_interface = AsyncMock()
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_queued")
    await db_context.delegation_runs.mark_handed_off(
        "delegation_queued", SystemClock().now()
    )
    context = _tool_context(db_context, processing_service, chat_interface)

    result = await cancel_delegation_tool(context, "delegation_queued")

    assert result.text is not None
    assert "Cancelled delegation 'delegation_queued'" in result.text
    worker = _build_worker(db_engine, processing_service, chat_interface)
    await worker.handle_delegated_profile_run(context, _payload("delegation_queued"))
    assert target.calls == []
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_a_running_turn_stops_it_partway(
    db_engine: AsyncEngine,
) -> None:
    """A turn running here is cancelled, as the web Stop button cancels a chat turn."""
    delegation_turns = RunningDelegationTurns()
    target = BlockingTurnService(delegation_turns)
    processing_service = _source_processing_service(
        target, delegation_turns=delegation_turns
    )
    chat_interface = AsyncMock()
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_running")
    await db_context.delegation_runs.mark_handed_off(
        "delegation_running", SystemClock().now()
    )
    context = _tool_context(db_context, processing_service, chat_interface)
    worker = _build_worker(db_engine, processing_service, chat_interface)
    handler = asyncio.create_task(
        worker.handle_delegated_profile_run(context, _payload("delegation_running"))
    )
    await asyncio.wait_for(target.waiting.wait(), timeout=10)

    result = await cancel_delegation_tool(context, "delegation_running")
    await asyncio.wait_for(handler, timeout=10)

    assert result.text is not None
    assert "Cancelled delegation 'delegation_running'" in result.text
    assert "stopped partway" in result.text
    assert target.stopped
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_running")
    assert run is not None
    assert run["status"] == "failed"
    assert run["local_failure_kind"] == "cancelled"
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stopped_turn_withdraws_the_confirmation_it_was_waiting_on(
    db_engine: AsyncEngine,
) -> None:
    """Nobody is left to act on an approval, so the prompt is taken back."""
    delegation_turns = RunningDelegationTurns()
    target = BlockingTurnService(delegation_turns, ask_for_confirmation=True)
    processing_service = _source_processing_service(
        target, delegation_turns=delegation_turns
    )
    db_context = Database(engine=db_engine)
    confirmations = ConfirmationService(db=db_context)
    confirmation_manager = DurableBlockingConfirmationManager(confirmations)
    confirmation_ui_managers = cast(
        "dict[str, ConfirmationUIManager]", {TEST_INTERFACE_TYPE: confirmation_manager}
    )
    await _create_run(db_context, delegation_id="delegation_confirming")
    await db_context.delegation_runs.mark_handed_off(
        "delegation_confirming", SystemClock().now()
    )
    context = _tool_context(
        db_context, processing_service, AsyncMock(), confirmation_ui_managers
    )
    worker = _build_worker(
        db_engine, processing_service, AsyncMock(), confirmation_ui_managers
    )
    handler = asyncio.create_task(
        worker.handle_delegated_profile_run(context, _payload("delegation_confirming"))
    )
    await asyncio.wait_for(confirmation_manager.asked.wait(), timeout=10)
    assert len(await confirmations.list_pending_for_user(user_id=RUN_USER_ID)) == 1

    await cancel_delegation_tool(context, "delegation_confirming")
    await asyncio.wait_for(handler, timeout=10)

    assert await confirmations.list_pending_for_user(user_id=RUN_USER_ID) == []
    assert confirmation_manager.request_id is not None
    request = await db_context.confirmation_requests.get(
        confirmation_manager.request_id
    )
    assert request is not None
    assert request["status"] == "rejected"


@pytest.mark.asyncio
async def test_a_turn_whose_run_was_cancelled_before_it_registered_never_runs(
    db_engine: AsyncEngine,
) -> None:
    """The cancellation landed between the run starting and its turn registering."""
    delegation_turns = RunningDelegationTurns()
    target = BlockingTurnService(delegation_turns)
    processing_service = _source_processing_service(
        target, delegation_turns=delegation_turns
    )
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_raced")
    await db_context.delegation_runs.mark_running(
        "delegation_raced", SystemClock().now()
    )
    await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_raced"
    )
    worker = _build_worker(db_engine, processing_service, AsyncMock())

    async def turn() -> ChatInteractionResult:
        return await target.handle_chat_interaction()

    stopped = await worker._run_stoppable_delegated_turn(
        _tool_context(db_context, processing_service),
        "delegation_raced",
        cast("ProcessingService", target),
        turn(),
    )

    assert stopped is None
    assert target.calls == []


@pytest.mark.asyncio
async def test_a_run_waiting_on_its_own_delegations_is_not_cancelled(
    db_engine: AsyncEngine,
) -> None:
    """Its children would run on with nobody to answer to."""
    processing_service = _source_processing_service(FakeDelegatableService())
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_parent")
    await db_context.delegation_runs.mark_running(
        "delegation_parent", SystemClock().now()
    )
    await db_context.delegation_runs.mark_awaiting_children("delegation_parent")

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_parent"
    )

    assert result.text is not None
    assert result.text.startswith("Error:")
    assert "waiting on delegations of its own" in result.text
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_parent")
    assert run is not None
    assert run["status"] == "awaiting_children"


@pytest.mark.asyncio
async def test_a_finished_run_has_nothing_to_cancel(db_engine: AsyncEngine) -> None:
    processing_service = _source_processing_service(FakeDelegatableService())
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_done")
    await db_context.delegation_runs.mark_completed(
        delegation_id="delegation_done",
        result_text="done",
        result_attachment_ids=[],
        completed_at=SystemClock().now(),
    )

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_done"
    )

    assert result.text == (
        "Delegation 'delegation_done' has already completed; there is nothing to cancel."
    )
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_done")
    assert run is not None
    assert run["status"] == "completed"


@pytest.mark.asyncio
async def test_another_conversations_run_cannot_be_cancelled(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService()
    processing_service = _processing_service(target)
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_elsewhere")

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service, interface_type="telegram"),
        "delegation_elsewhere",
    )

    assert result.text == (
        "Error: Delegation 'delegation_elsewhere' not found in this conversation."
    )
    assert target.cancelled == []
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_elsewhere")
    assert run is not None
    assert run["status"] == "awaiting_remote"


@pytest.mark.asyncio
async def test_a_cancelled_remote_run_is_still_counted_in_the_metrics(
    db_engine: AsyncEngine,
) -> None:
    """No poll reads a cancelled run again, so the cancel owes its accounting."""
    target = MeteredPollableService()
    db_context = Database(engine=db_engine)
    await _remote_run(db_context, "delegation_metered")

    await cancel_delegation_tool(
        _tool_context(db_context, _processing_service(target)), "delegation_metered"
    )

    assert target.recorded == [(REMOTE_TASK_ID, True)]


@pytest.mark.asyncio
async def test_cancelling_a_waiting_parents_last_child_hands_it_to_the_parents_next_turn(
    db_engine: AsyncEngine,
) -> None:
    """Otherwise the parent waits forever on a child that will never finish.

    Cancelled from a person's own chat turn, which runs outside any task, so
    there is no lane to inherit for the parent's continuation.
    """
    target = FakePollableService()
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_parent")
    await db_context.delegation_runs.mark_running(
        "delegation_parent", SystemClock().now()
    )
    await _create_run(
        db_context,
        delegation_id="delegation_child",
        source_subconversation_id="sub_delegation_parent",
    )
    await db_context.delegation_runs.mark_handed_off(
        "delegation_child", SystemClock().now()
    )
    await db_context.delegation_runs.mark_awaiting_children("delegation_parent")

    chat_turn = dataclasses.replace(
        _tool_context(db_context, _processing_service(target)), task_priority=None
    )
    await cancel_delegation_tool(chat_turn, "delegation_child")

    parent = await db_context.delegation_runs.get_by_delegation_id("delegation_parent")
    assert parent is not None
    assert parent["status"] == "queued"
    # Left for the parent's next turn, which is told the branch was cancelled.
    [child] = await db_context.delegation_runs.list_undelivered_children(parent)
    assert child["delegation_id"] == "delegation_child"
    assert child["local_failure_kind"] == "cancelled"
    continuations = [
        task
        for task in await db_context.tasks.get_all()
        if task["task_id"].startswith("task_delegation_parent_continue_")
    ]
    assert len(continuations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("handed_off", [True, False], ids=["handed_off", "inline"])
async def test_a_turn_with_delegations_still_out_is_not_cancelled(
    db_engine: AsyncEngine,
    handed_off: bool,
) -> None:
    """It is still running, but stopping it would orphan the child it started,
    whether handed off or still awaited inline."""
    delegation_turns = RunningDelegationTurns()
    target = BlockingTurnService(delegation_turns)
    processing_service = _source_processing_service(
        target, delegation_turns=delegation_turns
    )
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_parent")
    await db_context.delegation_runs.mark_running(
        "delegation_parent", SystemClock().now()
    )
    await _create_run(
        db_context,
        delegation_id="delegation_child",
        source_subconversation_id="sub_delegation_parent",
    )
    if handed_off:
        await db_context.delegation_runs.mark_handed_off(
            "delegation_child", SystemClock().now()
        )

    result = await cancel_delegation_tool(
        _tool_context(db_context, processing_service), "delegation_parent"
    )

    assert result.text is not None
    assert "waiting on delegations of its own" in result.text
    parent = await db_context.delegation_runs.get_by_delegation_id("delegation_parent")
    assert parent is not None
    assert parent["status"] == "running"


@pytest.mark.asyncio
async def test_a_child_cancelled_by_its_own_parents_turn_is_not_handed_back_to_it(
    db_engine: AsyncEngine,
) -> None:
    """The parent read the cancellation from the tool; a second report would repeat it."""
    target = FakePollableService()
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_parent")
    await db_context.delegation_runs.mark_running(
        "delegation_parent", SystemClock().now()
    )
    await _create_run(
        db_context,
        delegation_id="delegation_child",
        source_subconversation_id="sub_delegation_parent",
    )
    await db_context.delegation_runs.mark_handed_off(
        "delegation_child", SystemClock().now()
    )
    parents_turn = _tool_context(db_context, _processing_service(target))
    parents_turn.subconversation_id = "sub_delegation_parent"

    await cancel_delegation_tool(parents_turn, "delegation_child")

    child = await db_context.delegation_runs.get_by_delegation_id("delegation_child")
    assert child is not None
    assert child["notified_at"] is not None
    parent = await db_context.delegation_runs.get_by_delegation_id("delegation_parent")
    assert parent is not None
    assert await db_context.delegation_runs.list_undelivered_children(parent) == []
