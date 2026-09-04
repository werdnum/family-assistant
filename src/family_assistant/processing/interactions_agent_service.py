"""Pollable local ProcessingService for Google Interactions API agents.

A Deep Research or Antigravity turn can run for many minutes (longer for the
research "max" tier, and for an Antigravity run that plans and executes a
multi-step task in its sandbox). When delegated to via
``delegate_to_service``, the default inline delegation path
(``handle_chat_interaction``) would block a TaskWorker slot for the entire
run. This subclass additionally implements ``PollableDelegationService`` so
the delegation worker submits the interaction and polls it to terminal on a
schedule instead — the same submit-then-poll pattern already used for remote
A2A targets, applied here to a local target with a long-running provider
call. Direct (non-delegated) usage via ``/research``/``/research_max``/
``/coder`` is unaffected: ``handle_chat_interaction`` (inherited,
unchanged) still streams the interaction live for that interactive path.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import TYPE_CHECKING, TypedDict

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_interaction_terminal_error_status,
)
from family_assistant.observability.metrics import record_llm_call
from family_assistant.processing.protocol import (
    PENDING,
    DelegationPermanentError,
    DelegationTransientError,
    PendingPoll,
    RemoteSubmission,
    TaintedSinkRefusedError,
)
from family_assistant.processing.service import ProcessingService
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.tools import (
    TaintTrackingToolsProvider,
    ToolExecutionContext,
    ToolPolicyDeniedError,
    find_provider_by_type,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from google.genai.interactions import Interaction

    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import LLMMessage, MessageReasoningInfo
    from family_assistant.services.attachment_registry import AttachmentMetadata
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

# Ceiling on the total attachment bytes one run may carry into the sandbox.
# The API takes them base64-encoded inside the create request body, so this
# bounds the request rather than any sandbox limit. A constant until there is
# evidence about what real files look like.
MAX_ROUTED_ATTACHMENT_BYTES = 20 * 1024 * 1024

# Where mounted attachments appear inside the sandbox.
_SANDBOX_MOUNT_DIR = "/workspace"

# Filenames come from user-supplied metadata, so they are reduced to a safe
# basename: no directory traversal, no separators, nothing that could land the
# file outside the mount directory.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class EnvironmentSourceDict(TypedDict):
    """An ``environment.sources`` entry mounting one file into the sandbox."""

    type: str
    content: str
    encoding: str
    target: str


def _sandbox_target_path(
    attachment_id: str, metadata: AttachmentMetadata, taken: set[str]
) -> str:
    """Mount path for one attachment: readable, traversal-safe, and unique.

    Two attachments can share an ``original_filename``, and two different names
    can sanitize to the same one. A repeated ``target`` would leave the sandbox
    holding one file where the task expects two, so a colliding name gains a
    ``-2``/``-3`` suffix before its extension. The first file of a given name
    keeps it, so the common single-attachment case reads naturally.
    """
    raw_name = str(metadata.metadata.get("original_filename") or "").strip()
    basename = raw_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", basename).lstrip(".")
    if not cleaned:
        cleaned = f"attachment-{attachment_id}"

    candidate = cleaned
    if candidate in taken:
        stem, dot, suffix = cleaned.partition(".")
        counter = 2
        while candidate in taken:
            candidate = f"{stem}-{counter}{dot}{suffix}"
            counter += 1
    taken.add(candidate)
    return f"{_SANDBOX_MOUNT_DIR}/{candidate}"


def _attachment_taint_sources(
    attachment_id: str, provenance: Mapping[str, object]
) -> tuple[TaintSource, ...]:
    """Derive an attachment's taint from its stored provenance.

    Reads the same fields as ``merge_artifact_taint_into_context`` and, like
    it, contributes nothing when an attachment carries no provenance at all.
    That is the convention the taint system already runs on: an artifact
    produced from untrusted input is labelled at the point it is created (see
    ``email_intake/taint.py``), and an unlabelled artifact is one no untrusted
    path touched. Defaulting an unlabelled attachment to ``unknown_external``
    instead would deny every ordinary user upload, which is the common case
    this exists to serve; being stricter here than ``read_text_attachment`` is
    on the same file would also be incoherent.
    """
    raw_state = provenance.get("taint_metadata")
    if raw_state is not None:
        state = TurnTaintState.from_metadata(raw_state)
        if state.sources:
            return state.sources

    raw_tier = provenance.get("source_trust_tier")
    if raw_tier is None:
        return ()
    try:
        tier = SourceTrustTier.from_value(raw_tier)
    except ValueError:
        tier = SourceTrustTier.UNKNOWN_EXTERNAL
    raw_labels = provenance.get("provenance_labels")
    labels = (
        frozenset(str(label) for label in raw_labels)
        if isinstance(raw_labels, list)
        else frozenset()
    )
    return (
        TaintSource(
            source_type=TaintSourceType.ATTACHMENT,
            source_id=attachment_id,
            tier=tier,
            labels=labels,
            reason="Attachment routed into the sandbox.",
        ),
    )


def _interaction_duration_seconds(interaction: Interaction) -> float:
    """How long the provider ran the interaction for, in seconds.

    Both timestamps are the provider's, so this measures the run rather than
    the poll that observed it finishing. Zero when either is missing: a run
    with no duration is better than a wrong one, and the call still counts.
    """
    created = getattr(interaction, "created", None)
    updated = getattr(interaction, "updated", None)
    if created is None or updated is None:
        return 0.0
    try:
        return max(0.0, (updated - created).total_seconds())
    except (TypeError, AttributeError):
        return 0.0


def _describe_interaction_errors(interaction: Interaction) -> str:
    """Render ``interaction.errors`` for logs/tracebacks, or "" when absent.

    The Interactions API records diagnostic faults / platform errors on the
    interaction (e.g. the concurrency-limit cancellation it returns while a
    same-account agent run is already in flight), and without them a terminal
    status alone says nothing about *why*. Errors are optional on the SDK
    model, and each carries only an optional code and message.
    """
    rendered = []
    for error in interaction.errors or []:
        fields = []
        if error.code:
            fields.append(f"code={error.code}")
        if error.message:
            fields.append(f"message={error.message}")
        if fields:
            rendered.append("{" + ", ".join(fields) + "}")
    return "; ".join(rendered)


class InteractionsAgentProcessingService(ProcessingService):
    """A ``ProcessingService`` for an Interactions API agent, also pollable.

    Only constructed (see ``assistant.py``'s registry setup) for profiles
    whose resolved model names an Interactions agent —
    ``PollableDelegationService`` is a ``runtime_checkable`` Protocol, so
    defining these methods unconditionally on the base ``ProcessingService``
    would incorrectly make every local profile "pollable"; scoping the
    subclass to these profiles keeps ordinary local delegation targets on the
    existing inline path.
    """

    # These agents collapse the prompt into a single `input` string (plus, for
    # Antigravity, a `system_instruction`) and the client drops scaffolding on
    # the way, so the block never reaches the model on either the interactive
    # or the submit-then-poll path.
    sends_turn_context_block: bool = False

    def format_system_prompt(self, *, user_name: str) -> str:
        """Fold the clock into the prompt, since no block survives to carry it.

        Work grounded on live web results needs a date more than most work
        does -- "the latest on X this week" is unanswerable without one. Putting
        it in the prompt is what the rest of the codebase moved away from, but
        the reason not to is a cache prefix, and a single-shot agent
        submission has none.
        """
        prompt = super().format_system_prompt(user_name=user_name)
        return f"{prompt}\n\nCurrent time: {self.current_time_str()}".strip()

    def _google_client(self) -> GoogleGenAIClient:
        client = self.llm_client
        if not isinstance(client, GoogleGenAIClient):
            raise TypeError(
                "InteractionsAgentProcessingService requires a GoogleGenAIClient, "
                f"got {type(client).__name__}"
            )
        return client

    def remote_context_id(
        self, conversation_id: str, subconversation_id: str | None
    ) -> str | None:
        """The Interactions API has no separate context-grouping concept.

        Continuation is chained explicitly via ``previous_interaction_id``
        (see ``submit_async``), not via a context id known ahead of submit.
        """
        _ = (conversation_id, subconversation_id)
        return None

    async def _authorize_profile_sink(
        self,
        state: TurnTaintState,
        *,
        conversation_id: str,
        subconversation_id: str | None,
        user_name: str,
        acting_user_id: str | None,
        db_context: Database,
        messages: Sequence[LLMMessage],
    ) -> None:
        """Authorize a pollable submit through the shared reviewer chokepoint.

        A turn that runs the LLM loop is gated there instead (see
        ``ProcessingService.sink_refusal_reason``), against the turn's complete
        taint. This path never runs the loop, so it evaluates what it has: the
        parent turn's state -- carrying any approval the delegation gate
        recorded -- plus the attachments it is about to mount.

        Production providers include :class:`TaintTrackingToolsProvider`, whose
        named-sink authorization records policy audit, launches observe-mode
        review in the background, and applies enforce-mode reviewer verdicts.
        The legacy refusal remains only as a conservative fallback for embedded
        callers and tests that supply a provider chain without taint tracking.
        """
        sink_class = self.service_config.taint_sink_class
        if sink_class is None:
            return

        taint_provider = find_provider_by_type(
            self.tools_provider, TaintTrackingToolsProvider
        )
        if taint_provider is None:
            refusal = self.sink_refusal_reason(state)
            if refusal is not None:
                raise TaintedSinkRefusedError(refusal)
            return

        context = ToolExecutionContext(
            interface_type="delegation",
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=None,
            db_context=db_context,
            processing_service=self,
            clock=self.clock,
            home_assistant_client=self.home_assistant_client,
            event_sources=self.event_sources,
            attachment_registry=self.attachment_registry,
            camera_backend=self.camera_backend,
            credential_resolvers=self.credential_resolvers,
            api_backend=self.api_backend,
            timezone=self.service_config.timezone,
            user_id=acting_user_id,
            processing_profile_id=self.service_config.id,
            subconversation_id=subconversation_id,
            tools_provider=self.tools_provider,
            taint_tracker=InMemoryTurnTaintTracker(state),
            taint_policy_snapshot=state,
            tool_call_review_messages=tuple(messages),
        )
        try:
            await taint_provider.authorize_taint_sink(
                name=f"profile:{self.service_config.id}",
                sink_class=sink_class,
                arguments={
                    "profile_id": self.service_config.id,
                    "submission_mode": "pollable",
                },
                context=context,
                call_id="profile_sink:submit_async",
                taint_policy=self.taint_policy,
            )
        except ToolPolicyDeniedError as exc:
            raise TaintedSinkRefusedError(str(exc)) from exc

    async def _build_environment_sources(
        self,
        content_parts: list[ContentPartDict],
        *,
        db_context: Database,
        acting_user_id: str | None,
    ) -> tuple[list[EnvironmentSourceDict], tuple[TaintSource, ...]]:
        """Mount delegated attachments into the sandbox, with their taint.

        ``delegate_to_service`` turns ``attachment_ids`` into ``attachment``
        content parts. An interaction takes a single ``input`` string, so the
        attachment cannot ride in the request text -- but the agent runs in a
        filesystem, and the API mounts inline sources into it. Handing the
        agent a real file is also what a coding agent actually wants: it can
        open, parse and rewrite it rather than parse a blob out of its prompt.

        Each attachment's stored provenance becomes a ``TaintSource`` so the
        sink evaluation above sees it. That is what makes routing safe rather
        than merely possible: a spreadsheet from an unknown sender raises the
        turn's tier and the matrix denies the run, while the user's own file
        does not.
        """
        sources: list[EnvironmentSourceDict] = []
        taint_sources: list[TaintSource] = []
        taken_names: set[str] = set()
        total_bytes = 0

        for part in content_parts:
            part_type = part.get("type")
            if part_type == "text":
                continue
            if part_type != "attachment":
                raise DelegationPermanentError(
                    f"Profile '{self.service_config.id}' cannot accept "
                    f"'{part_type}' content: it runs a sandboxed agent that "
                    "receives a text request plus mounted files. Send the "
                    "content as an attachment or as text."
                )
            attachment_id = str(part.get("attachment_id") or "")
            if not attachment_id:
                raise DelegationPermanentError(
                    "Attachment content part is missing required 'attachment_id'"
                )
            registry = self.attachment_registry
            if registry is None:
                raise DelegationPermanentError(
                    f"Profile '{self.service_config.id}' received an attachment "
                    "but no attachment registry is configured."
                )

            metadata = await registry.get_attachment(
                db_context, attachment_id, acting_user_id=acting_user_id
            )
            content = await registry.get_attachment_content(
                db_context, attachment_id, acting_user_id=acting_user_id
            )
            if metadata is None or content is None:
                raise DelegationPermanentError(
                    f"Attachment '{attachment_id}' was not found, or belongs to "
                    "another user."
                )

            total_bytes += len(content)
            if total_bytes > MAX_ROUTED_ATTACHMENT_BYTES:
                limit_mb = MAX_ROUTED_ATTACHMENT_BYTES // (1024 * 1024)
                raise DelegationPermanentError(
                    f"Attachments exceed the {limit_mb}MB a single sandbox run "
                    "may carry. Send fewer or smaller files."
                )

            sources.append({
                "type": "inline",
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
                "target": _sandbox_target_path(attachment_id, metadata, taken_names),
            })
            taint_sources.extend(
                _attachment_taint_sources(attachment_id, metadata.metadata)
            )

        return sources, tuple(taint_sources)

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
        """Start the agent interaction without blocking on its result.

        Builds the same system prompt as a direct turn (via the inherited
        ``format_system_prompt``) plus the delegated content as input text.
        No ``<turn_context>`` block is appended: these profiles aggregate no
        context, and a single-shot submission has no cache prefix to protect.
        Chains onto the prior delegation's
        interaction (if this is a resumed run — see
        ``DelegationRunsRepository.get_latest_completed_run``), and submits
        in the background.

        Attachments are mounted into the sandbox and their taint folded into
        the sink evaluation, which happens *after* they are resolved so a file
        the caller routed counts toward the tier that gates the run.
        """
        environment_sources, attachment_taint = await self._build_environment_sources(
            content_parts, db_context=db_context, acting_user_id=acting_user_id
        )
        # The state carries the parent's sources *and* any approval recorded on
        # it, so it wins outright where both are supplied -- re-adding the
        # sources would only duplicate them in the audit trail.
        state = initial_taint_state
        if state is None:
            state = TurnTaintState.empty()
            for source in initial_taint_sources or ():
                state = state.add_source(source)
        for source in attachment_taint:
            state = state.add_source(source)

        system_prompt = self.format_system_prompt(user_name=user_name)
        user_text = self._extract_user_content_for_history(content_parts)
        messages: list[LLMMessage] = []
        if system_prompt:
            messages.append(self._build_system_message(system_prompt))
        messages.append(
            UserMessage(content=user_text, taint_metadata=state.to_metadata())
        )
        await self._authorize_profile_sink(
            state,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            user_name=user_name,
            acting_user_id=acting_user_id,
            db_context=db_context,
            messages=messages,
        )

        previous_interaction_id: str | None = None
        if subconversation_id is not None:
            prior_run = await db_context.delegation_runs.get_latest_completed_run(
                conversation_id=conversation_id,
                subconversation_id=subconversation_id,
                target_service_id=self.service_config.id,
            )
            if prior_run is not None:
                previous_interaction_id = prior_run["remote_task_id"]

        interaction = await self._google_client().start_agent_interaction(
            messages,
            previous_interaction_id=previous_interaction_id,
            environment_sources=environment_sources or None,
        )
        if not interaction.id:
            raise DelegationTransientError(
                "Interactions API create response carried no interaction id"
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
        interaction = await self._google_client().get_agent_interaction(remote_task_id)
        if interaction.status == "completed":
            self._record_run_metrics(interaction, outcome="success")
            return ChatInteractionResult.success(
                text_reply=interaction.output_text or ""
            )
        if is_interaction_terminal_error_status(interaction.status):
            self._record_run_metrics(interaction, outcome="error")
            error_detail = _describe_interaction_errors(interaction)
            logger.warning(
                "Interactions API run on '%s' ended with status %s (interaction %s)%s",
                self.service_config.id,
                interaction.status,
                remote_task_id,
                f"; errors: {error_detail}" if error_detail else "",
            )
            return ChatInteractionResult.error(
                text_reply=f"The {self.service_config.id} run {interaction.status}.",
                error_traceback=(
                    f"Interaction {remote_task_id} ended with status "
                    f"{interaction.status!r}."
                    + (f" Errors: {error_detail}" if error_detail else "")
                ),
            )
        return PENDING

    def _record_run_metrics(
        self,
        interaction: Interaction,
        *,
        outcome: str,
    ) -> None:
        """Count a finished agent run and the tokens it spent.

        Recorded once, when the run reaches a terminal state, rather than on
        every poll: a poll is a cheap status read, and counting one per poll
        would report a long run as hundreds of calls it never made.

        The duration is the provider's own -- the wall-clock between the
        interaction being created and last updated -- because the run outlives
        the task-worker invocation that submitted it, and no timer on this side
        spans it.
        """
        client = self._google_client()
        record_llm_call(
            profile=self.service_config.id,
            provider="google",
            model=client.model_name,
            resolved_model=interaction.model,
            operation="agent",
            outcome=outcome,
            error_type=interaction.status if outcome == "error" else None,
            duration_seconds=_interaction_duration_seconds(interaction),
            time_to_first_output_seconds=None,
            reasoning_info=client.reasoning_info_from_interaction(interaction),
        )

    async def cancel_async(self, remote_task_id: str) -> None:
        """Best-effort cancellation; mirrors ``RemoteA2AService.cancel_async``."""
        try:
            await self._google_client().cancel_agent_interaction(remote_task_id)
        except Exception as exc:
            logger.warning(
                "Failed to cancel interaction %s on '%s': %s",
                remote_task_id,
                self.service_config.id,
                exc,
            )
        finally:
            await self._record_cancelled_run(remote_task_id)

    async def _record_cancelled_run(self, remote_task_id: str) -> None:
        """Account for a run that was cancelled rather than polled to an end.

        The worker's timeout path cancels and never polls again, so without
        this the longest runs -- the ones that reached the timeout, and so the
        most expensive -- would be the only ones missing from the metrics.

        Read after cancelling, when the provider's totals have settled, and
        best-effort throughout: a run whose usage cannot be fetched is still
        counted, because a call with no tokens beats no call at all.
        """
        usage: MessageReasoningInfo | None = None
        resolved_model: str | None = None
        duration_seconds = 0.0
        try:
            interaction = await self._google_client().get_agent_interaction(
                remote_task_id
            )
        except Exception as exc:
            logger.debug(
                "Could not read usage for cancelled interaction %s: %s",
                remote_task_id,
                exc,
            )
        else:
            usage = self._google_client().reasoning_info_from_interaction(interaction)
            resolved_model = interaction.model
            duration_seconds = _interaction_duration_seconds(interaction)

        record_llm_call(
            profile=self.service_config.id,
            provider="google",
            model=self._google_client().model_name,
            resolved_model=resolved_model,
            operation="agent",
            outcome="cancelled",
            error_type=None,
            duration_seconds=duration_seconds,
            time_to_first_output_seconds=None,
            reasoning_info=usage,
        )
