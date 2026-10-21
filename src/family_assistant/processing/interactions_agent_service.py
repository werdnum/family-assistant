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

import logging
import mimetypes
import re
from datetime import datetime
from typing import TYPE_CHECKING

from family_assistant.llm.call_context import (
    CallAttribution,
    reset_processing_profile,
    set_processing_profile,
)
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    SandboxFile,
    is_interaction_terminal_error_status,
)
from family_assistant.observability.metrics import record_llm_call
from family_assistant.processing.protocol import (
    PENDING,
    CollectedOutputs,
    DelegationPermanentError,
    DelegationTransientError,
    PendingPoll,
    RemoteDisposition,
    RemoteObservation,
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
from family_assistant.tools.infrastructure import (
    TaintTrackingToolsProvider,
    ToolPolicyDeniedError,
    find_provider_by_type,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from google.genai.interactions import Interaction

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import LLMMessage, MessageReasoningInfo
    from family_assistant.llm.model_selection import ResolvedModelSelection
    from family_assistant.services.attachment_registry import AttachmentMetadata
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

# Ceiling on the total attachment bytes one run may carry into the sandbox.
# Files are uploaded to the run's environment ahead of the create request, so
# this bounds what one delegation uploads rather than any API limit. A constant
# until there is evidence about what real files look like.
MAX_ROUTED_ATTACHMENT_BYTES = 20 * 1024 * 1024

# Where mounted attachments appear inside the sandbox.
_SANDBOX_MOUNT_DIR = "/workspace"

# Where the agent is told to leave files for the user, as the environments API
# addresses it (relative to the environment root), and how much of it one run
# may return. The prompt in defaults.yaml states the same limits to the agent.
_SANDBOX_OUTPUT_DIR = "workspace/outputs"
MAX_RETURNED_OUTPUT_FILES = 20
MAX_RETURNED_OUTPUT_BYTES = 50 * 1024 * 1024

# Added to a sandboxed agent's prompt on the submit path only: that is the path
# whose completion collects outputs. A run streamed directly into a chat does
# not, so its prompt must not promise the files.
_OUTPUTS_INSTRUCTION = (
    "Files you are sent are in /workspace. Save every file meant for "
    "{user_name} (a model, image, document or data file) under "
    "/workspace/outputs: the files there are sent to {user_name} as "
    "attachments with your reply, up to "
    f"{MAX_RETURNED_OUTPUT_FILES} files and "
    f"{MAX_RETURNED_OUTPUT_BYTES // (1024 * 1024)} MB in all. Nothing else in "
    "the sandbox is returned."
)

# Filenames come from user-supplied metadata, so they are reduced to a safe
# basename: no directory traversal, no separators, nothing that could land the
# file outside the mount directory.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _output_over_limit(size: int, total_so_far: int, max_file_size: int) -> str | None:
    """Why a returned file of ``size`` bytes does not fit, or ``None`` if it does."""
    if size > max_file_size:
        return f"{size:,} bytes, over the attachment size limit"
    if total_so_far + size > MAX_RETURNED_OUTPUT_BYTES:
        limit_mb = MAX_RETURNED_OUTPUT_BYTES // (1024 * 1024)
        return f"{size:,} bytes, over the {limit_mb} MB a run returns"
    return None


def _output_mime_type(reported: str | None, filename: str) -> str:
    """A bare MIME type for a returned file, without the API's charset suffix."""
    bare = (reported or "").split(";", 1)[0].strip()
    if bare and bare != "application/octet-stream":
        return bare
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or bare or "application/octet-stream"


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


def _interaction_timestamp(value: str | datetime | None) -> datetime | None:
    """Parse one Interactions API timestamp.

    The SDK types these as ISO-8601 strings, not datetimes, so they have to be
    parsed rather than subtracted. Accepts a datetime too, in case a later SDK
    starts returning one, and returns None for anything unparseable -- which
    is logged rather than silently treated as zero. ``TypeError`` counts as
    unparseable alongside ``ValueError``: a provider that returns a number, or
    a field the SDK models loosely, must not fail the read that carries it.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning("Unparseable Interactions API timestamp: %r", value)
        return None


def _interaction_duration_seconds(interaction: Interaction) -> float:
    """How long the provider ran the interaction for, in seconds.

    Both timestamps are the provider's, so this measures the run rather than
    the poll that observed it finishing. Zero when either is missing: a run
    with no duration is better than a wrong one, and the call still counts.
    """
    created = _interaction_timestamp(interaction.created)
    updated = _interaction_timestamp(interaction.updated)
    if created is None or updated is None:
        return 0.0
    return max(0.0, (updated - created).total_seconds())


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

    # An Interactions agent profile exists to hand a result back into the
    # conversation, so a "completed" run with nothing in it is a failure to
    # answer rather than an answer. Declared as an attribute so a profile whose
    # empty completion would be legitimate can say so.
    expects_output: bool = True

    def format_system_prompt(self, *, user_name: str) -> str:
        """Fold the clock into the prompt, since only the latest message survives.

        These agents collapse the prompt into a single ``input`` string, so the
        send-time stamps on earlier messages never reach them.

        Work grounded on live web results needs a date more than most work
        does -- "the latest on X this week" is unanswerable without one. Putting
        it in the prompt is what the rest of the codebase moved away from, but
        the reason not to is a cache prefix, and a single-shot agent
        submission has none.
        """
        prompt = super().format_system_prompt(user_name=user_name)
        return f"{prompt}\n\nCurrent time: {self.current_time_str()}".strip()

    def _google_client(self, client: LLMInterface | None = None) -> GoogleGenAIClient:
        """The Google client *client* is, the default tier's when omitted.

        Reading, polling and cancelling an interaction go by its id alone, so
        any of the profile's clients serves them; only starting one depends on
        the run's tier, which picks the agent's reasoning model.
        """
        client = client or self.llm_client
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
        turn_id: str | None,
        conversation_id: str,
        subconversation_id: str | None,
        user_name: str,
        acting_user_id: str | None,
        db_context: Database,
        messages: Sequence[LLMMessage],
        llm_client: LLMInterface,
    ) -> None:
        """Authorize a pollable submit through the shared reviewer chokepoint.

        A turn that runs the LLM loop is gated there instead (see
        ``ProcessingService.sink_refusal_reason``), against the turn's complete
        taint. This path never runs the loop, so it evaluates what it has: the
        parent turn's state -- carrying any approval the delegation gate
        recorded -- plus the attachments it is about to mount. It audits under
        the delegating turn's id, since that is the turn it decides for.

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
            turn_id=turn_id,
            db_context=db_context,
            processing_service=self,
            llm_client=llm_client,
            clock=self.clock,
            plugins=self.plugins,
            event_sources=self.event_sources,
            attachment_registry=self.attachment_registry,
            credential_resolvers=self.credential_resolvers,
            api_backend=self.api_backend,
            timezone=self.service_config.timezone,
            user_id=acting_user_id,
            processing_profile_id=self.service_config.id,
            subconversation_id=subconversation_id,
            tools_provider=self.tools_provider,
            calendar_config=self.service_config.calendar_config,
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

    async def _build_sandbox_files(
        self,
        content_parts: list[ContentPartDict],
        *,
        db_context: Database,
        acting_user_id: str | None,
    ) -> tuple[list[SandboxFile], tuple[TaintSource, ...]]:
        """Mount delegated attachments into the sandbox, with their taint.

        ``delegate_to_service`` turns ``attachment_ids`` into ``attachment``
        content parts. An interaction takes a single ``input`` string, so the
        attachment cannot ride in the request text -- but the agent runs in a
        filesystem, and the client uploads files into it. Handing the
        agent a real file is also what a coding agent actually wants: it can
        open, parse and rewrite it rather than parse a blob out of its prompt.

        Each attachment's stored provenance becomes a ``TaintSource`` so the
        sink evaluation above sees it. That is what makes routing safe rather
        than merely possible: a spreadsheet from an unknown sender raises the
        turn's tier and the matrix denies the run, while the user's own file
        does not.
        """
        files: list[SandboxFile] = []
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

            files.append(
                SandboxFile(
                    path=_sandbox_target_path(attachment_id, metadata, taken_names),
                    content=content,
                    mime_type=metadata.mime_type,
                )
            )
            taint_sources.extend(
                _attachment_taint_sources(attachment_id, metadata.metadata)
            )

        return files, tuple(taint_sources)

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
        source_turn_id: str | None = None,
        model_selection: ResolvedModelSelection | None = None,
    ) -> RemoteSubmission:
        """Start the agent interaction without blocking on its result.

        Builds the same system prompt as a direct turn (via the inherited
        ``format_system_prompt``) plus the delegated content as input text.
        No household context is added: these profiles aggregate none, and the
        clock is folded into the prompt because a single-shot submission has no
        cache prefix to protect.
        Chains onto the prior delegation's
        interaction (if this is a resumed run — see
        ``DelegationRunsRepository.get_latest_completed_run``), and submits
        in the background.

        Attachments are mounted into the sandbox and their taint folded into
        the sink evaluation, which happens *after* they are resolved so a file
        the caller routed counts toward the tier that gates the run.

        ``model_selection`` is the run's frozen tier envelope; on an Antigravity
        profile the tier is the model the agent reasons with.
        """
        _, llm_client = self._run_binding(model_selection)
        sandbox_files, attachment_taint = await self._build_sandbox_files(
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
        if self._google_client(llm_client).runs_in_sandbox:
            system_prompt = "\n\n".join(
                part
                for part in (
                    system_prompt,
                    _OUTPUTS_INSTRUCTION.format(user_name=user_name),
                )
                if part
            )
        user_text = self._extract_user_content_for_history(content_parts)
        messages: list[LLMMessage] = []
        if system_prompt:
            messages.append(self._build_system_message(system_prompt))
        messages.append(
            UserMessage(content=user_text, taint_metadata=state.to_metadata())
        )
        await self._authorize_profile_sink(
            state,
            turn_id=source_turn_id,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            user_name=user_name,
            acting_user_id=acting_user_id,
            db_context=db_context,
            messages=messages,
            llm_client=llm_client,
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

        # The client counts the submission itself, at the point the request
        # actually reaches Google. This path never enters the streaming loop,
        # so the profile it should be attributed to is established here.
        token = set_processing_profile(self.service_config.id)
        try:
            interaction = await self._google_client(llm_client).start_agent_interaction(
                messages,
                previous_interaction_id=previous_interaction_id,
                sandbox_files=sandbox_files,
            )
        finally:
            reset_processing_profile(token)
        # The client raises rather than returning an interaction without an id,
        # having counted the failed submission; this narrows the SDK's Optional
        # so the invariant is checked rather than assumed.
        if not interaction.id:
            raise DelegationTransientError(
                "Interactions API create response carried no interaction id"
            )
        return RemoteSubmission(
            remote_task_id=interaction.id,
            remote_context_id=None,
            terminal_result=None,
        )

    async def collect_outputs(
        self,
        observation: RemoteObservation,
        *,
        owner_user_id: str | None,
        conversation_id: str,
        taint_state: TurnTaintState,
        db_context: Database,
    ) -> CollectedOutputs:
        """Return what the agent left under ``/workspace/outputs`` as attachments.

        The run's environment outlives it, so its files can be read back after
        it finishes -- whether on the terminal poll or when a late completion
        is recovered. Files over the caps are named in ``omitted`` rather than
        dropped silently. Each file carries ``taint_state``, the provenance of
        the run's own reply, since the agent produced both from the same
        inputs.
        """
        environment_id = observation.environment_id
        if not environment_id:
            return CollectedOutputs(attachment_ids=[], omitted=[])
        client = self._google_client()
        entries = await client.list_sandbox_files(environment_id, _SANDBOX_OUTPUT_DIR)
        if not entries:
            return CollectedOutputs(attachment_ids=[], omitted=[])
        registry = self.attachment_registry
        if registry is None:
            return CollectedOutputs(
                attachment_ids=[],
                omitted=[
                    f"{len(entries)} output file(s): no attachment registry is "
                    "configured to return them."
                ],
            )

        # Everything is downloaded before anything is registered. A transient
        # failure partway through is retried from the start, and registering
        # as we went would store another copy of every earlier file on each
        # retry, with nothing holding the ids of the last attempt's.
        staged: list[tuple[str, bytes, str]] = []
        omitted: list[str] = []
        total_bytes = 0
        for entry in sorted(entries, key=lambda e: e.path):
            relative = entry.path.removeprefix(f"{_SANDBOX_OUTPUT_DIR}/")
            if len(staged) >= MAX_RETURNED_OUTPUT_FILES:
                omitted.append(
                    f"{relative} (beyond the first {MAX_RETURNED_OUTPUT_FILES} files)"
                )
                continue
            # The listed size, when given, spares downloading a file that
            # cannot be returned; the downloaded size is what decides.
            listed = entry.size_bytes
            reason = (
                _output_over_limit(listed, total_bytes, registry.max_file_size)
                if listed is not None
                else None
            )
            if reason is None:
                content = await client.download_sandbox_file(environment_id, entry.path)
                reason = _output_over_limit(
                    len(content), total_bytes, registry.max_file_size
                )
            if reason is not None:
                omitted.append(f"{relative} ({reason})")
                continue
            total_bytes += len(content)
            staged.append((
                relative,
                content,
                _output_mime_type(entry.mime_type, relative),
            ))

        attachment_ids: list[str] = []
        for relative, content, mime_type in staged:
            metadata = await registry.store_and_register_tool_attachment(
                file_content=content,
                filename=relative.replace("/", "_"),
                content_type=mime_type,
                tool_name=self.service_config.id,
                description=f"{relative}, produced by the {self.service_config.id} run",
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                db_context=db_context,
                taint_state=taint_state,
            )
            attachment_ids.append(metadata.attachment_id)
        return CollectedOutputs(attachment_ids=attachment_ids, omitted=omitted)

    async def observe_async(self, remote_task_id: str) -> RemoteObservation:
        """Read the interaction once and classify it. The only classifier.

        Both polling and reconciliation go through here, so "which statuses are
        terminal" and "which completions are empty" are decided in one place
        and cannot drift apart as the provider grows states.

        Classifies by deny-listing known terminal-error statuses (mirrors the
        streaming path's own ``interaction.status_update`` handling) rather
        than allow-listing "pending" ones, so a status this SDK doesn't
        enumerate (e.g. a capacity-queueing ``queued`` state) is treated as
        still pending instead of failing the delegation outright.
        """
        # Stamped before the read, not after. A reading describes the provider
        # state at some instant between the request and its response, and the
        # only bound this side can prove is that the state is no older than
        # when the request went out. Stamping on return would give a slow read
        # -- one that snapshotted an older state but finished last -- the later
        # timestamp, which is exactly what lets it overwrite a fresher reading
        # through ``record_remote_observation``'s stale-write guard.
        observed_at = self.clock.now()
        interaction = await self._google_client().get_agent_interaction(remote_task_id)
        status = str(interaction.status)
        output_text = interaction.output_text or ""
        step_count = len(interaction.steps) if interaction.steps is not None else None
        usage = self._google_client().reasoning_info_from_interaction(interaction)
        total_tokens = usage.get("total_tokens") if usage is not None else None
        error_summary = _describe_interaction_errors(interaction) or None

        if status == "completed":
            disposition = (
                RemoteDisposition.COMPLETED
                if self._completion_is_useful(
                    output_text, step_count=step_count, total_tokens=total_tokens
                )
                else RemoteDisposition.EMPTY_COMPLETION
            )
        elif status == "cancelled":
            disposition = RemoteDisposition.CANCELLED
        elif is_interaction_terminal_error_status(status):
            disposition = RemoteDisposition.FAILED
        else:
            disposition = RemoteDisposition.PENDING

        return RemoteObservation(
            remote_task_id=remote_task_id,
            status=status,
            disposition=disposition,
            observed_at=observed_at,
            result=(
                ChatInteractionResult.success(text_reply=output_text)
                if disposition is RemoteDisposition.COMPLETED
                else None
            ),
            remote_created_at=_interaction_timestamp(interaction.created),
            remote_updated_at=_interaction_timestamp(interaction.updated),
            output_chars=len(output_text),
            step_count=step_count,
            total_tokens=total_tokens,
            resolved_model=interaction.model,
            error_summary=error_summary,
            event_cursor=interaction.previous_interaction_id,
            environment_id=interaction.environment_id,
        )

    def _completion_is_useful(
        self,
        output_text: str,
        *,
        step_count: int | None,
        total_tokens: int | None,
    ) -> bool:
        """Whether a provider ``completed`` actually carries a result.

        A profile whose job is to answer has not answered if it returns no
        text, ran no steps and spent no tokens: that shape has been observed
        from runs that plainly never executed, and delivering it as a success
        shows the user a blank reply from an assistant that claims to have
        finished. Any one of the three is enough evidence -- a run that did
        work and returned nothing is a different (real) answer from a run that
        did nothing at all.

        Profiles that are not expected to return output opt out via
        ``expects_output``, which keeps this from judging a target whose empty
        completion is legitimate.
        """
        if not self.expects_output:
            return True
        return bool(output_text) or bool(step_count) or bool(total_tokens)

    async def poll_async(
        self,
        remote_task_id: str,
        remote_context_id: str | None,
    ) -> ChatInteractionResult | PendingPoll:
        """Poll the interaction once; PENDING until it reaches a terminal state."""
        _ = remote_context_id
        observation = await self.observe_async(remote_task_id)
        return self.result_for_observation(observation)

    def result_for_observation(
        self, observation: RemoteObservation
    ) -> ChatInteractionResult | PendingPoll:
        """Turn a classified observation into what a poller should do with it."""
        if observation.disposition is RemoteDisposition.PENDING:
            return PENDING
        if observation.result is not None:
            return observation.result
        logger.warning(
            "Interactions API run on '%s' ended with status %s (interaction %s)%s",
            self.service_config.id,
            observation.status,
            observation.remote_task_id,
            f"; errors: {observation.error_summary}"
            if observation.error_summary
            else "",
        )
        if observation.disposition is RemoteDisposition.EMPTY_COMPLETION:
            return ChatInteractionResult.error(
                text_reply=(
                    f"The {self.service_config.id} run reported that it finished "
                    "but returned no result."
                ),
                error_traceback=(
                    f"Interaction {observation.remote_task_id} reported status "
                    "'completed' with no output, no steps and no token usage."
                ),
            )
        return ChatInteractionResult.error(
            text_reply=f"The {self.service_config.id} run {observation.status}.",
            error_traceback=(
                f"Interaction {observation.remote_task_id} ended with status "
                f"{observation.status!r}."
                + (
                    f" Errors: {observation.error_summary}"
                    if observation.error_summary
                    else ""
                )
            ),
        )

    async def record_terminal_metrics(
        self,
        remote_task_id: str,
        *,
        outcome: str = "success",
        cancelled: bool = False,
    ) -> None:
        """Count the finished run, called once the terminal CAS has committed.

        ``outcome`` comes from the caller, which learned it from the result it
        just committed, rather than from the interaction read below: the read
        is enrichment and may fail, while the outcome is already known.

        ``cancelled`` is the timeout path, which cancels the remote run and
        never polls it again: the run has an outcome but no terminal poll to
        read it from, and those are the longest runs, so the most expensive.

        Re-reads the interaction rather than carrying one out of the poll: the
        poll that saw the terminal state is not necessarily the attempt that
        commits it, and counting there would count a run again for every
        retried poll. One extra read per run is not worth avoiding against a
        run that took minutes to produce, and the provider's totals have
        settled by now.
        """
        if cancelled:
            await self._record_cancelled_run(remote_task_id)
            return
        interaction: Interaction | None = None
        try:
            interaction = await self._google_client().get_agent_interaction(
                remote_task_id
            )
        except Exception:
            # The read is for the tokens and the duration, not for whether the
            # run happened -- and the caller has already committed the terminal
            # transition, so no later poll will reach this hook again. Dropping
            # the call here would lose the whole run rather than its detail.
            logger.warning(
                "Could not read interaction %s to count its usage; recording the "
                "run without it.",
                remote_task_id,
                exc_info=True,
            )
        self._record_run_metrics(interaction, outcome=outcome)

    def _record_run_metrics(
        self,
        interaction: Interaction | None,
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
            # The terminal hooks see the interaction, not the run, so the
            # attribution carries no model selection.
            attribution=CallAttribution(profile_id=self.service_config.id),
            provider="google",
            model=client.model_name,
            resolved_model=interaction.model if interaction else None,
            operation=client.agent_operation_name,
            outcome=outcome,
            error_type=(
                (interaction.status if interaction else "unknown")
                if outcome == "error"
                else None
            ),
            duration_seconds=(
                _interaction_duration_seconds(interaction) if interaction else 0.0
            ),
            time_to_first_output_seconds=None,
            reasoning_info=(
                client.reasoning_info_from_interaction(interaction)
                if interaction
                else None
            ),
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
            # The terminal hooks see the interaction, not the run, so the
            # attribution carries no model selection.
            attribution=CallAttribution(profile_id=self.service_config.id),
            provider="google",
            model=self._google_client().model_name,
            resolved_model=resolved_model,
            operation=self._google_client().agent_operation_name,
            outcome="cancelled",
            error_type=None,
            duration_seconds=duration_seconds,
            time_to_first_output_seconds=None,
            reasoning_info=usage,
        )
