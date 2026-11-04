"""Run a web thread's first turn through the stream hub.

The turn goes through the same producer as a turn the user sends: registered in
the hub under the run's turn id, so the thread streams it live and Stop and
steering reach it, with the request the thread was started with as its prompt.
See ``family_assistant.threads.turns``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from family_assistant.storage.database import Database
from family_assistant.threads.turns import StartedThreadTurn, ThreadTurnOutcome
from family_assistant.web.conversation_stream_hub import (
    ConversationStreamHub,
    ConversationTurnRunningError,
    TurnAlreadyExistsError,
)
from family_assistant.web.turn_producer import (
    confirmation_result_waiters_for_state,
    confirmation_service_for_state,
    initial_turn_taint,
    launch_turn_producer,
)
from family_assistant.web.web_mid_turn_controller import WebMidTurnController

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.datastructures import State

    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.model_selection import ResolvedModelSelection
    from family_assistant.processing import ProcessingService
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.repositories.delegation_runs import (
        DelegationRunDict,
    )

logger = logging.getLogger(__name__)


class WebThreadTurnRunner:
    """Runs web threads' first turns from the app's shared state."""

    def __init__(self, app_state: State) -> None:
        self._app_state = app_state

    def handles(self, run: DelegationRunDict) -> bool:
        return run["interface_type"] == "web"

    async def start_first_turn(
        self,
        run: DelegationRunDict,
        *,
        target: ProcessingService,
        model_selection: ResolvedModelSelection,
        tool_call_review_trigger: TriggerReviewInput | None,
        claim: Callable[[], Awaitable[bool]],
        resume: bool,
    ) -> StartedThreadTurn | None:
        state = self._app_state
        hub: ConversationStreamHub = state.conversation_stream_hub
        conversation_id = run["conversation_id"]
        turn_id = run["turn_id"]
        user_id = run["user_id"]
        if turn_id is None or user_id is None:
            raise ValueError(
                f"Thread run {run['delegation_id']} has no turn id or owner to run as."
            )
        db = Database(state.database_engine)
        seed = await db.message_history.get_user_row_by_turn_id(turn_id)
        if seed is None:
            raise RuntimeError(f"Thread run {run['delegation_id']} has no seed row.")

        controller = WebMidTurnController()
        try:
            await hub.start_turn(
                conversation_id,
                turn_id=turn_id,
                user_id=user_id,
                started_at=datetime.now(UTC),
                mid_turn_controller=controller,
                reject_if_running=True,
            )
        except ConversationTurnRunningError, TurnAlreadyExistsError:
            return None

        try:
            claimed = await claim()
            # The seed is already in the turn, so the taint it carries is read
            # as this turn's own, as an interrupted turn's rows are.
            taint = (
                await initial_turn_taint(
                    db,
                    target,
                    interface_type=run["interface_type"],
                    conversation_id=conversation_id,
                    resumed_turn_id=turn_id,
                )
                if claimed
                else None
            )
        except BaseException:
            await self._abandon(hub, conversation_id, turn_id)
            raise
        if taint is None:
            await self._abandon(hub, conversation_id, turn_id)
            return None

        await hub.publish_activity(
            conversation_id, user_id=user_id, reason="turn_started"
        )
        producer = launch_turn_producer(
            app_state=state,
            hub=hub,
            processing_service=target,
            web_chat_interface=state.web_chat_interface,
            confirmation_service=confirmation_service_for_state(state),
            confirmation_result_waiters=confirmation_result_waiters_for_state(state),
            attachment_registry=getattr(state, "attachment_registry", None),
            conversation_id=conversation_id,
            turn_id=turn_id,
            user_id=user_id,
            user_name=run["user_name"] or "",
            interface_type=run["interface_type"],
            # The parts the run was created with: the seed records its
            # attachments only by id, which is not enough to inject them.
            trigger_content_parts=cast(
                "list[ContentPartDict]", run["content_parts_json"]
            ),
            trigger_attachments=seed["attachments"],
            initial_history_taint_metadata=taint.history,
            initial_context_taint_metadata=taint.context,
            mid_turn_controller=controller,
            model_selection=model_selection,
            on_orphan_cancel=None,
            # The worker holds the turn's lease, on the task that settles the
            # run, so the run is settled before the lease is let go.
            lease_registry=None,
            lease=None,
            resume=resume,
            tool_call_review_trigger=tool_call_review_trigger,
        )

        def request_suspend() -> None:
            # Flag the record before the controller, so a producer that halts
            # on the controller's interrupt already sees itself as suspended.
            record = hub.get_turn(conversation_id, turn_id)
            if record is not None:
                record.suspended = True
            controller.request_suspend()

        async def outcome() -> ThreadTurnOutcome | None:
            return await self._outcome(db, hub, producer, conversation_id, turn_id)

        return StartedThreadTurn(request_suspend=request_suspend, outcome=outcome)

    async def _outcome(
        self,
        db: Database,
        hub: ConversationStreamHub,
        producer: asyncio.Task[None],
        conversation_id: str,
        turn_id: str,
    ) -> ThreadTurnOutcome | None:
        # Waited on rather than awaited: Stop cancels the producer, and that
        # cancellation must end the turn, not propagate into the waiter as if
        # it were itself being cancelled.
        try:
            await asyncio.wait([producer])
        except asyncio.CancelledError:
            # The waiter itself is being cancelled -- a timeout or shutdown --
            # so the turn must not outlive the run it belongs to.
            producer.cancel()
            await asyncio.wait([producer])
            raise
        record = hub.get_turn(conversation_id, turn_id)
        if record is not None and record.suspended:
            return None
        # The stream reports a provider error or a refusal as an event and still
        # ends the turn complete, so completion alone is not success.
        if record is not None and (
            (record.status == "complete" and not record.error_reported)
            # Stopped by the user: the thread is theirs to continue.
            or record.status == "cancelled"
        ):
            return ThreadTurnOutcome(
                succeeded=True,
                reply_text=await self._reply_text(db, turn_id),
                error=None,
            )
        return ThreadTurnOutcome(
            succeeded=False,
            reply_text="",
            error=(
                "The thread's first turn ended without a record."
                if record is None
                else "The thread's first turn reported an error."
                if record.error_reported
                else f"The thread's first turn ended {record.status}."
            ),
        )

    @staticmethod
    async def _abandon(
        hub: ConversationStreamHub, conversation_id: str, turn_id: str
    ) -> None:
        """Drop a turn registered but never started, so it can be run later."""
        try:
            await hub.end_turn(
                conversation_id, turn_id=turn_id, status="failed", error="not started"
            )
        finally:
            await hub.discard_turn(conversation_id, turn_id)

    @staticmethod
    async def _reply_text(db: Database, turn_id: str) -> str:
        rows = await db.message_history.get_rows_by_turn_id(turn_id)
        replies = [
            row["content"]
            for row in rows
            if row["role"] == "assistant" and isinstance(row["content"], str)
        ]
        return replies[-1] if replies else ""
