"""Protocol for services that can receive delegated requests.

Both local ProcessingService and remote A2A services implement this
protocol, allowing the delegation tool and registry to work with either.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import MessageAttachmentMetadata
    from family_assistant.llm.model_selection import ResolvedModelSelection
    from family_assistant.processing.types import (
        ChatInteractionResult,
        MidTurnInputProvider,
        ProcessingServiceConfig,
        RemoteServiceConfig,
        RequestConfirmationCallback,
    )
    from family_assistant.security.taint import TaintSource, TurnTaintState
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.database import Database
    from family_assistant.telegram.protocols import ConfirmationUIManager


@runtime_checkable
class DelegatableService(Protocol):
    """A service that can receive delegated requests.

    Implemented by both ProcessingService (local) and RemoteA2AService (remote).
    The delegate_to_service tool and registry use this interface.
    """

    @property
    def kind(self) -> Literal["local", "remote"]: ...

    @property
    def service_config(self) -> ProcessingServiceConfig | RemoteServiceConfig: ...

    async def handle_chat_interaction(
        self,
        db_context: Database,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        user_id: str | None = None,
        replied_to_interface_id: str | None = None,
        chat_interface: ChatInterface | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        trigger_attachments: list[MessageAttachmentMetadata] | None = None,
        subconversation_id: str | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        turn_id: str | None = None,
        thread_root_id: int | None = None,
        trigger_is_internal: bool = False,
        pinned_history_message_ids: list[int] | None = None,
        trigger_role: Literal["user", "system"] = "user",
        reuse_existing_user_row: bool = False,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
        model_selection: ResolvedModelSelection | None = None,
    ) -> ChatInteractionResult: ...

    async def resolve_model_selection_for_run(
        self,
        selection: ResolvedModelSelection,
        *,
        db_context: Database,
        interface_type: str,
        conversation_id: str,
        subconversation_id: str | None,
        trigger_content_parts: list[ContentPartDict],
        acting_user_id: str | None,
    ) -> ResolvedModelSelection:
        """Settle *selection* for a run that will execute later.

        On the protocol rather than behind a local-only check because a queued
        run persists whatever this returns, and the persisted envelope is the
        run's authorization: a target that cannot route says so by returning
        what it was given, instead of every caller remembering to ask whether
        this one can.
        """
        ...


class PendingPoll(enum.Enum):
    """Sentinel returned by ``poll_async`` when the remote task is not terminal."""

    PENDING = "pending"


PENDING = PendingPoll.PENDING


class DelegationTransientError(Exception):
    """A submit/poll failure that may succeed on retry.

    Network/timeout/5xx-shaped failures: the request may have landed, or the
    remote may recover, so the worker keeps the run ``awaiting_remote`` and
    polls/retries rather than failing it.
    """


class DelegationPermanentError(DelegationTransientError):
    """A deterministic submit/poll failure that will not succeed on retry.

    A definitive negative response from the target (bad auth / bad request /
    protocol error). The worker fails the delegation fast with this rather
    than polling until the wall-clock cap.
    """


class TaintedSinkRefusedError(DelegationPermanentError):
    """The turn's taint bars it from a profile that is itself a sink.

    A ``DelegationPermanentError`` so a delegated run fails fast with the
    reason rather than polling: re-submitting the same content would be
    refused identically. The chat entry points catch it and render the reason
    to the user instead of letting it surface as an internal error.
    """


class DelegationTaskNotFoundError(DelegationPermanentError):
    """The target reports no such task (e.g. HTTP 404 or an unknown-id error).

    Distinct because, for a run whose submit may not have landed, this is a
    cue to (idempotently) re-submit rather than fail.
    """


@dataclass
class RemoteSubmission:
    """Result of submitting a request to a remote service without blocking.

    ``terminal_result`` is populated only when the remote returned a terminal
    task on submit (a synchronous remote that ignored ``blocking=false``), in
    which case the caller can complete immediately without polling.
    """

    remote_task_id: str
    remote_context_id: str | None
    terminal_result: ChatInteractionResult | None = None


@runtime_checkable
class PollableDelegationService(Protocol):
    """A delegatable service whose work runs remotely and is polled to terminal.

    Implemented by remote services (RemoteA2AService) that submit a request,
    return a remote task id, and are polled by the worker until the task is
    terminal — so a delegated run does not hold a worker for the whole remote
    duration and can re-attach after a restart. Local services do not implement
    this; the worker checks for the capability and falls back to the inline path.
    """

    @property
    def service_config(self) -> ProcessingServiceConfig | RemoteServiceConfig: ...

    def remote_context_id(
        self, conversation_id: str, subconversation_id: str | None
    ) -> str | None:
        """Deterministic remote context id for a delegation, known before submit."""
        ...

    async def submit_async(
        self,
        content_parts: list[ContentPartDict],
        *,
        conversation_id: str,
        subconversation_id: str | None,
        user_name: str,
        db_context: Database,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        acting_user_id: str | None = None,
        initial_taint_state: TurnTaintState | None = None,
    ) -> RemoteSubmission:
        """Submit without a client-supplied task id; the remote assigns one.

        Per A2A spec §3.4.2 a client must not supply a task id when creating a
        task, so the returned :class:`RemoteSubmission` carries the remote's
        assigned id for the caller to persist and poll. ``user_name`` and
        ``db_context`` are available for implementations (e.g. local services
        with no network task of their own) that need to render a prompt
        template or look up prior delegation state; remote implementations may
        ignore them. ``acting_user_id`` is the run's owner, for an
        implementation that resolves owner-scoped artifacts (attachments)
        rather than only text.
        """
        ...

    async def poll_async(
        self,
        remote_task_id: str,
        remote_context_id: str | None,
    ) -> ChatInteractionResult | PendingPoll: ...

    async def cancel_async(self, remote_task_id: str) -> None: ...


class RemoteDisposition(enum.Enum):
    """What one read of a remote run says about it, classified once.

    Deliberately not the provider's own status: providers spell their states
    differently and add new ones, and the decisions the worker makes from a
    read (poll again? deliver? recover a late result?) are the same five
    either way. The provider's verbatim status rides alongside on
    :class:`RemoteObservation` for diagnostics.
    """

    PENDING = "pending"
    """Still running (or in a state this provider has not taught us about)."""

    COMPLETED = "completed"
    """Finished with a result worth delivering."""

    EMPTY_COMPLETION = "empty_completion"
    """Finished, but with no output and no evidence of having executed.

    Distinct from :attr:`COMPLETED` because a profile expected to return
    something has not returned it. Delivering it as a success shows the user
    an empty answer; treating it as merely "failed" would lose the fact that
    the provider believes it finished.
    """

    CANCELLED = "cancelled"
    """The provider reports the run cancelled -- the only proof of that."""

    FAILED = "failed"
    """The provider reports a terminal error."""


TERMINAL_REMOTE_DISPOSITIONS: frozenset[RemoteDisposition] = frozenset({
    RemoteDisposition.COMPLETED,
    RemoteDisposition.EMPTY_COMPLETION,
    RemoteDisposition.CANCELLED,
    RemoteDisposition.FAILED,
})


# Ceilings on the free-text an observation may persist. The observation is a
# diagnostic record attached to a run, not a copy of the provider's payload:
# an agent's errors can quote its own output, and its output can be a whole
# file, so both are truncated rather than trusted to be small.
MAX_OBSERVATION_ERROR_CHARS = 2000
MAX_OBSERVATION_STATUS_CHARS = 64
# Identifiers and model names: generous, but still a ceiling, because they are
# provider strings rather than anything this application minted.
MAX_OBSERVATION_ID_CHARS = 255


class RemoteObservationMetadata(TypedDict):
    """The bounded, JSON-safe form of a :class:`RemoteObservation`.

    A closed set of keys, built field by field rather than by copying a
    provider object, so a provider that grows a field carrying prompts,
    reasoning traces, command output or credentials cannot start persisting it
    by default. Total rather than partial: ``to_metadata`` writes every key,
    using ``None`` for what the provider did not report, so a reader never has
    to distinguish "absent" from "not reported".
    """

    remote_task_id: str
    status: str
    disposition: str
    observed_at: str
    remote_created_at: str | None
    remote_updated_at: str | None
    has_output: bool
    output_chars: int
    step_count: int | None
    total_tokens: int | None
    resolved_model: str | None
    error_summary: str | None
    event_cursor: str | None


@dataclass(frozen=True)
class RemoteObservation:
    """One bounded read of a remote run's state.

    Carries both what the worker needs to act (``disposition``, and
    ``result`` when there is one) and what a person debugging the run needs to
    see (``to_metadata``). ``result`` is never persisted on the run as part of
    the observation -- a completed run's text is persisted as the run's
    result, through the ordinary result path.
    """

    remote_task_id: str
    status: str
    disposition: RemoteDisposition
    observed_at: datetime
    result: ChatInteractionResult | None = None
    remote_created_at: datetime | None = None
    remote_updated_at: datetime | None = None
    output_chars: int = 0
    step_count: int | None = None
    total_tokens: int | None = None
    resolved_model: str | None = None
    error_summary: str | None = None
    event_cursor: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether the provider considers the run finished."""
        return self.disposition in TERMINAL_REMOTE_DISPOSITIONS

    def to_metadata(self) -> RemoteObservationMetadata:
        """Render the bounded record that gets persisted on the run.

        Every field is coerced and truncated here rather than trusted from the
        adapter that built the observation. That is what makes "bounded and
        JSON-safe" a property of this one function: a provider field that is a
        loosely-typed SDK object, or a string of unbounded length, cannot reach
        the database through any adapter.
        """
        return RemoteObservationMetadata(
            remote_task_id=_bounded_str(self.remote_task_id, MAX_OBSERVATION_ID_CHARS)
            or "",
            status=(_bounded_str(self.status) or "")[:MAX_OBSERVATION_STATUS_CHARS],
            disposition=self.disposition.value,
            observed_at=self.observed_at.isoformat(),
            remote_created_at=_isoformat_or_none(self.remote_created_at),
            remote_updated_at=_isoformat_or_none(self.remote_updated_at),
            has_output=self.output_chars > 0,
            output_chars=self.output_chars,
            step_count=_bounded_int(self.step_count),
            total_tokens=_bounded_int(self.total_tokens),
            resolved_model=_bounded_str(self.resolved_model, MAX_OBSERVATION_ID_CHARS),
            error_summary=(
                _bounded_str(self.error_summary, MAX_OBSERVATION_ERROR_CHARS)
            ),
            event_cursor=_bounded_str(self.event_cursor, MAX_OBSERVATION_ID_CHARS),
        )


def _isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _bounded_str(
    value: object, limit: int = MAX_OBSERVATION_STATUS_CHARS
) -> str | None:
    """Coerce a provider value to a bounded string, or None when it is empty."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text[:limit] or None


def _bounded_int(value: object) -> int | None:
    """Coerce a provider value to an int, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@runtime_checkable
class ObservableDelegationService(Protocol):
    """A pollable target whose remote run can be re-read after we gave up.

    Separate from :class:`PollableDelegationService` rather than folded into
    it: that Protocol decides whether a target polls at all, so adding a
    member would silently demote every implementation lacking it back to the
    inline path. A target that cannot be re-read simply does not implement
    this, and reconciliation leaves its runs alone instead of guessing at
    them.
    """

    async def observe_async(self, remote_task_id: str) -> RemoteObservation:
        """Read the remote run once and classify it.

        Raises the same delegation error taxonomy as ``poll_async``:
        :class:`DelegationTaskNotFoundError` when the provider has no such
        run, :class:`DelegationPermanentError` for a definitive negative, and
        :class:`DelegationTransientError` for anything that may recover.
        """
        ...

    def result_for_observation(
        self, observation: RemoteObservation
    ) -> ChatInteractionResult | PendingPoll:
        """Turn a classified observation into what a poller should do with it.

        Paired with :meth:`observe_async` so a poll is one read classified
        once, rather than a second classification of the same provider state
        that can disagree with the reconciler's.
        """
        ...
