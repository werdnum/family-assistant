"""Relaunch web streaming turns that an earlier process was running.

Registered with the ``TurnLeaseRegistry`` under :data:`WEB_STREAM_RESUMER`. A
resumed turn goes through the same producer as a new one, under its original
``turn_id``: it registers in the hub (so a client's follow stream sees
``turn_started`` and Stop/steer work), and continues from the rows the
interrupted run persisted.
"""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.datastructures import State

from family_assistant.llm.model_selection import (
    ResolvedModelSelection,
    model_selection_from_reasoning,
)
from family_assistant.services.turn_resumption import (
    TurnLeaseRegistry,
    TurnResumePayload,
)
from family_assistant.storage.database import Database
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
    persist_stopped_reply,
    trigger_content_parts_for,
)
from family_assistant.web.web_mid_turn_controller import WebMidTurnController

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService

logger = logging.getLogger(__name__)

WEB_STREAM_RESUMER = "web_stream"


async def resumed_model_selection(
    db: Database, payload: TurnResumePayload
) -> ResolvedModelSelection | None:
    """The tier the resumed turn runs on: the one its earlier rows ran on.

    The lease holds the envelope the endpoint admitted, which under Auto is
    the unrouted default. Once a model call has run, the routed envelope is
    stamped on its row, and the continuation must stay on it rather than
    switch tiers partway through the turn. If no call ran, nothing was decided
    yet, so the admitted envelope goes through exactly as the endpoint passed
    it -- still open to routing.
    """
    for reasoning in reversed(
        await db.message_history.get_assistant_reasoning_infos_for_turn(payload.turn_id)
    ):
        stamped = model_selection_from_reasoning(reasoning)
        if stamped is not None:
            return stamped
    if payload.model_selection is None:
        return None
    admitted = ResolvedModelSelection.from_json(payload.model_selection)
    return replace(admitted, frozen=False)


class WebTurnResumer:
    """Resumes ``POST /v1/chat/turns`` turns from the app's shared state."""

    def __init__(self, app_state: State) -> None:
        self._app_state = app_state

    def _processing_service(self, profile_id: str) -> "ProcessingService":
        state = self._app_state
        registry: dict[str, ProcessingService] = (
            getattr(state, "processing_services", None) or {}
        )
        service = registry.get(profile_id)
        if service is None:
            default = getattr(state, "processing_service", None)
            if default is not None and default.service_config.id == profile_id:
                service = default
        if service is None:
            raise RuntimeError(
                f"Cannot resume turn: processing profile '{profile_id}' is not "
                "configured"
            )
        return service

    async def resume(
        self, payload: TurnResumePayload, registry: TurnLeaseRegistry
    ) -> bool:
        state = self._app_state
        hub: ConversationStreamHub = state.conversation_stream_hub
        processing_service = self._processing_service(payload.processing_profile_id)
        db = Database(state.database_engine)
        user_row = await db.message_history.get_user_row_by_turn_id(payload.turn_id)
        if user_row is None:
            raise RuntimeError(f"Turn {payload.turn_id} has no user message to resume")
        model_selection = await resumed_model_selection(db, payload)

        mid_turn_controller = WebMidTurnController()
        try:
            await hub.start_turn(
                payload.conversation_id,
                turn_id=payload.turn_id,
                user_id=payload.user_id,
                started_at=datetime.now(UTC),
                mid_turn_controller=mid_turn_controller,
                reject_if_running=True,
            )
        except (ConversationTurnRunningError, TurnAlreadyExistsError):
            return False

        try:
            taint = await initial_turn_taint(
                db,
                processing_service,
                interface_type=payload.interface_type,
                conversation_id=payload.conversation_id,
            )
            lease = await registry.arm(db.tasks, payload.next_attempt())
        except Exception:
            # The task queue retries this resume. The record holds no producer
            # yet, so drop it once ended -- a retained failed record would make
            # the retry's start_turn refuse the turn_id and strand the turn.
            try:
                await hub.end_turn(
                    payload.conversation_id,
                    turn_id=payload.turn_id,
                    status="failed",
                    error="An internal error occurred.",
                )
            finally:
                await hub.discard_turn(payload.conversation_id, payload.turn_id)
            raise

        await hub.publish_activity(
            payload.conversation_id,
            user_id=payload.user_id,
            reason="turn_started",
        )

        async def persist_orphan_stopped_reply() -> None:
            await persist_stopped_reply(
                state.database_engine,
                interface_type=payload.interface_type,
                conversation_id=payload.conversation_id,
                turn_id=payload.turn_id,
                user_id=payload.user_id,
                reply_text="",
                processing_profile_id=payload.processing_profile_id,
                initial_history_taint_metadata=taint.history,
                initial_context_taint_metadata=taint.context,
                live_taint_metadata=taint.live,
            )

        launch_turn_producer(
            app_state=state,
            hub=hub,
            processing_service=processing_service,
            web_chat_interface=state.web_chat_interface,
            confirmation_service=confirmation_service_for_state(state),
            confirmation_result_waiters=confirmation_result_waiters_for_state(state),
            attachment_registry=getattr(state, "attachment_registry", None),
            conversation_id=payload.conversation_id,
            turn_id=payload.turn_id,
            user_id=payload.user_id,
            user_name=payload.user_name,
            interface_type=payload.interface_type,
            trigger_content_parts=trigger_content_parts_for(
                user_row["content"] or "", user_row["attachments"]
            ),
            trigger_attachments=user_row["attachments"],
            initial_history_taint_metadata=taint.history,
            initial_context_taint_metadata=taint.context,
            mid_turn_controller=mid_turn_controller,
            model_selection=model_selection,
            on_orphan_cancel=persist_orphan_stopped_reply,
            lease_registry=registry,
            lease=lease,
            resume=True,
        )
        logger.info(
            "Resumed turn %s in conversation %s (attempt %d)",
            payload.turn_id,
            payload.conversation_id,
            payload.attempt + 1,
        )
        return True

    async def deliver_pending_reply(self, payload: TurnResumePayload) -> None:
        """Nothing to send: web clients read a finished turn's reply from history."""
