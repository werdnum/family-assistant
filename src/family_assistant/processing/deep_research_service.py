"""Pollable local ProcessingService for Google Deep Research profiles.

Deep Research turns can run for many minutes (longer for the "max" tier).
When delegated to via ``delegate_to_service``, the default inline delegation
path (``handle_chat_interaction``) would block a TaskWorker slot for the
entire run. This subclass additionally implements ``PollableDelegationService``
so the delegation worker submits the interaction and polls it to terminal on
a schedule instead — the same submit-then-poll pattern already used for
remote A2A targets, applied here to a local target with a long-running
provider call. Direct (non-delegated) usage via ``/research``/``/research_max``
is unaffected: ``handle_chat_interaction`` (inherited, unchanged) still
streams the interaction live for that interactive path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_deep_research_terminal_error_status,
)
from family_assistant.processing.protocol import (
    PENDING,
    DelegationTransientError,
    PendingPoll,
    RemoteSubmission,
)
from family_assistant.processing.service import ProcessingService
from family_assistant.processing.types import ChatInteractionResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.security.taint import TaintSource
    from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


class DeepResearchProcessingService(ProcessingService):
    """A ``ProcessingService`` for a Deep Research profile, also pollable.

    Only constructed (see ``assistant.py``'s registry setup) for profiles
    whose resolved model is a Deep Research model — ``PollableDelegationService``
    is a ``runtime_checkable`` Protocol, so defining these methods unconditionally
    on the base ``ProcessingService`` would incorrectly make every local
    profile "pollable"; scoping the subclass to Deep Research profiles keeps
    ordinary local delegation targets on the existing inline path.
    """

    def _google_client(self) -> GoogleGenAIClient:
        client = self.llm_client
        if not isinstance(client, GoogleGenAIClient):
            raise TypeError(
                "DeepResearchProcessingService requires a GoogleGenAIClient, "
                f"got {type(client).__name__}"
            )
        return client

    def remote_context_id(
        self, conversation_id: str, subconversation_id: str | None
    ) -> str | None:
        """Deep Research has no separate context-grouping concept.

        Continuation is chained explicitly via ``previous_interaction_id``
        (see ``submit_async``), not via a context id known ahead of submit.
        """
        _ = (conversation_id, subconversation_id)
        return None

    async def submit_async(
        self,
        content_parts: list[ContentPartDict],
        *,
        conversation_id: str,
        subconversation_id: str | None,
        user_name: str,
        db_context: DatabaseContext,
        initial_taint_sources: Sequence[TaintSource] | None = None,
    ) -> RemoteSubmission:
        """Start a Deep Research interaction without blocking on its result.

        Builds the same system prompt as a direct turn (via the inherited
        ``_render_system_prompt``; research profiles have no context
        providers, so there's no other context to aggregate) plus the
        delegated content as input text, chains onto the prior delegation's
        interaction (if this is a resumed run — see
        ``DelegationRunsRepository.get_latest_completed_run``), and submits
        in the background.
        """
        _ = initial_taint_sources
        system_prompt = self._render_system_prompt(
            user_name=user_name, aggregated_other_context_str=""
        )
        user_text = self._extract_user_content_for_history(content_parts)
        messages: list[LLMMessage] = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(UserMessage(content=user_text))

        previous_interaction_id: str | None = None
        if subconversation_id is not None:
            prior_run = await db_context.delegation_runs.get_latest_completed_run(
                conversation_id=conversation_id,
                subconversation_id=subconversation_id,
                target_service_id=self.service_config.id,
            )
            if prior_run is not None:
                previous_interaction_id = prior_run["remote_task_id"]

        interaction = await self._google_client().start_deep_research_interaction(
            messages, previous_interaction_id=previous_interaction_id
        )
        if not interaction.id:
            raise DelegationTransientError(
                "Deep Research interaction create response carried no interaction id"
            )
        return RemoteSubmission(
            remote_task_id=interaction.id,
            remote_context_id=None,
            terminal_result=None,
        )

    async def poll_async(
        self,
        remote_task_id: str,
        remote_context_id: str | None,
    ) -> ChatInteractionResult | PendingPoll:
        """Poll the interaction once; PENDING until it reaches a terminal state.

        Classifies by deny-listing known terminal-error statuses (mirrors the
        streaming path's own ``interaction.status_update`` handling) rather
        than allow-listing "pending" ones, so a status this SDK doesn't
        enumerate (e.g. a capacity-queueing ``queued`` state) is treated as
        still pending instead of failing the delegation outright.
        """
        _ = remote_context_id
        interaction = await self._google_client().get_deep_research_interaction(
            remote_task_id
        )
        if interaction.status == "completed":
            return ChatInteractionResult.success(
                text_reply=interaction.output_text or ""
            )
        if is_deep_research_terminal_error_status(interaction.status):
            return ChatInteractionResult.error(
                text_reply=f"Deep research {interaction.status}.",
                error_traceback=f"Deep Research interaction {remote_task_id} ended with status {interaction.status!r}.",
            )
        return PENDING

    async def cancel_async(self, remote_task_id: str) -> None:
        """Best-effort cancellation; mirrors ``RemoteA2AService.cancel_async``."""
        try:
            await self._google_client().cancel_deep_research_interaction(remote_task_id)
        except Exception as exc:  # noqa: BLE001 - best-effort, must never raise
            logger.warning(
                "Failed to cancel Deep Research interaction %s on '%s': %s",
                remote_task_id,
                self.service_config.id,
                exc,
            )
