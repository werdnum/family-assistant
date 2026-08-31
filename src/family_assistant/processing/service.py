from __future__ import annotations

import json
import logging
import re
import traceback
import uuid
from string import Formatter
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from family_assistant.llm import LLMInterface, LLMStreamEvent
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    ErrorMessage,
    LLMMessage,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing.protocol import TaintedSinkRefusedError
from family_assistant.security.taint import (
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyOutcome,
    TurnTaintState,
)
from family_assistant.utils.clock import Clock, SystemClock
from family_assistant.utils.text_normalization import normalize_latex_to_unicode

from .attachments import AttachmentProcessor
from .context import ContextPreparer
from .llm_loop import LLMStreamingLoop
from .tool_execution import ToolExecutor
from .turn_context import build_turn_context_message, turn_context_guidance
from .types import (
    ChatInteractionResult,
    ProcessingServiceConfig,
    RequestConfirmationCallback,
)
from .utils import (
    _user_friendly_error_message,
    format_attachment_metadata_block,
    inject_metadata_into_user_message,
    merge_attachment_metadata,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Collection,
        Mapping,
        Sequence,
    )
    from datetime import datetime

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.config_models import AppConfig
    from family_assistant.context_providers import ContextProvider
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing.protocol import DelegatableService
    from family_assistant.processing.types import MidTurnInputProvider
    from family_assistant.security.taint import (
        TaintMetadata,
        TaintSource,
        TurnTaintTracker,
    )
    from family_assistant.services.api_backend import ApiBackend
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.services.oauth_credentials import OAuthCredentialResolver
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.database import Database
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools import OnDemandToolsView, ToolsProvider
    from family_assistant.tools.types import EventSourcesById

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# How the current time is spelled inside the <turn_context> block. Defined once
# so the surfaces that report the block (the context viewer) cannot render a
# different clock format from the one the model is handed.
DEFAULT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

# Stand-in result for a tool call whose real result was never recorded, so the
# history stays representable to providers that require every call to be
# answered. Worded for the model: it says what is and is not known, because the
# tool may well have run to completion after the turn that called it went away.
ABANDONED_TOOL_CALL_RESULT = (
    "Error: no result was recorded for this tool call. The turn that made it "
    "ended before the tool returned, so whether it took effect is unknown. "
    "Re-run it if you need the result, and check for side effects first if "
    "re-running it would not be safe to do twice."
)


def _taint_metadata_from_sources(
    sources: Sequence[TaintSource] | None,
) -> TaintMetadata:
    state = TurnTaintState.empty()
    for source in sources or ():
        state = state.add_source(source)
    return state.to_metadata()


def _is_turn_closing_assistant_message(message: LLMMessage) -> bool:
    """Whether this assistant message ends the turn rather than a tool round.

    The LLM loop emits one ``done`` event per iteration and every one of them
    repeats the turn's pending attachment ids, so the ids alone do not identify
    the row that closes the turn: an ``attach_to_response`` followed by another
    tool call would record the same reference on both assistant rows, which is
    the duplicate the reader would then render twice. Only the reply that stops
    calling tools is terminal.
    """
    return isinstance(message, AssistantMessage) and not message.tool_calls


def _tool_row_attachment_ids(message: LLMMessage) -> set[str]:
    """Attachment ids a tool result records on its own history row."""
    if not isinstance(message, ToolMessage) or not message.attachments:
        return set()
    return {
        attachment_id
        for attachment in message.attachments
        if (attachment_id := attachment.get("attachment_id"))
    }


def _response_attachment_references(
    response_attachment_ids: Sequence[str] | None,
    *,
    recorded_on_tool_rows: Collection[str],
) -> list[MessageAttachmentMetadata] | None:
    """Attachment references to store on the assistant row that ends a turn.

    A turn's response attachments are otherwise only announced live (the web
    stream's per-file events, a Telegram media send), so a client loading the
    conversation afterwards has no way to know the reply carried them. Recording
    them as references keeps them in history; the read path resolves each id to
    its mime type and URL.

    Ids a tool result already recorded on its own row are skipped: both rows are
    visible to clients, so listing the attachment on each would show it twice.
    """
    references = [
        MessageAttachmentMetadata(
            type="attachment_reference",
            attachment_id=attachment_id,
        )
        for attachment_id in response_attachment_ids or ()
        if attachment_id not in recorded_on_tool_rows
    ]
    return references or None


class ProcessingService:
    """
    Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls.
    """

    kind: Literal["local"] = "local"

    sends_turn_context_block: bool = True
    """Whether this service's requests actually carry a ``<turn_context>`` block.

    Gates the system-prompt sentence describing the block, so a subclass whose
    transport drops it does not promise the model something that never arrives.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        tools_provider: ToolsProvider,
        service_config: ProcessingServiceConfig,
        context_providers: list[ContextProvider],
        server_url: str | None,
        app_config: AppConfig,
        clock: Clock | None = None,
        attachment_registry: AttachmentRegistry | None = None,
        event_sources: EventSourcesById | None = None,
        processing_services_registry: Mapping[str, DelegatableService] | None = None,
        home_assistant_client: HomeAssistantClientWrapper | None = None,
        camera_backend: CameraBackend | None = None,
        on_demand_view: OnDemandToolsView | None = None,
        credential_resolvers: Mapping[str, OAuthCredentialResolver] | None = None,
        api_backend: ApiBackend | None = None,
        taint_policy: TaintPolicyConfig | None = None,
    ) -> None:
        self._llm_client = llm_client
        self.tools_provider = tools_provider
        self.on_demand_view = on_demand_view
        self.service_config = service_config
        self.context_providers = context_providers
        self.server_url = server_url or "http://localhost:8000"
        self.app_config = app_config
        self.clock = clock if clock is not None else SystemClock()
        self._attachment_registry = attachment_registry
        self.processing_services_registry = processing_services_registry
        self.home_assistant_client = home_assistant_client
        self.camera_backend = camera_backend
        self.event_sources = event_sources
        self.credential_resolvers = credential_resolvers
        self.api_backend = api_backend
        # Only read by a subclass whose profile declares a `taint_sink_class`;
        # an ordinary profile is not a sink in its own right and evaluates
        # taint per tool, inside the tools provider.
        self.taint_policy = taint_policy or TaintPolicyConfig()

        # Compose helpers
        self.attachment_processor = AttachmentProcessor(
            attachment_registry, llm_client, app_config, self.clock
        )
        self.context_preparer = ContextPreparer(
            context_providers, service_config, self.clock
        )
        self.tool_executor = ToolExecutor(
            tools_provider,
            service_config,
            self.attachment_processor,
            attachment_registry,
            self.clock,
            credential_resolvers=credential_resolvers,
            api_backend=api_backend,
        )
        self.llm_loop = LLMStreamingLoop(
            llm_client,
            service_config,
            app_config,
            self.tool_executor,
            self.attachment_processor,
        )

    def sink_refusal_reason(
        self,
        state: TurnTaintState,
    ) -> str | None:
        """Refusal text when a turn's taint bars this profile's declared sink.

        A profile whose whole turn is a privileged operation -- an agent that
        runs code in a sandbox -- declares a ``taint_sink_class``, and the turn
        is then evaluated the way the equivalent *tool* already is. Returns
        ``None`` (proceed) for a profile that declares no sink, which is every
        ordinary profile.

        Called from ``LLMStreamingLoop.run_stream`` with the turn's *complete*
        state: the prompt's own sources, the aggregated context's, and the
        history's, merged. Evaluating only the trigger's sources would miss a
        trusted prompt that pulls in an email-derived attachment or tainted
        history -- the sandbox would then execute exactly the content this
        exists to keep out.

        A ``confirm`` outcome is permitted only when an approval for this sink
        travelled with the taint -- recorded by whichever gate actually put the
        question to a user (today, ``delegate_to_service``'s). Reading the
        approval off the state means this gate never has to infer, from the
        shape of the call path, whether somebody was asked. ``deny`` is refused
        regardless: it is never confirmable, so no approval for it can exist.
        """
        sink_class = self.service_config.taint_sink_class
        if sink_class is None:
            return None

        evaluation = TaintPolicyEvaluator(self.taint_policy).evaluate(
            state=state, sink_class=sink_class
        )
        logger.info(
            "Profile sink taint policy evaluated: profile=%s sink=%s requested=%s "
            "effective=%s mode=%s max_tier=%s approved=%s",
            self.service_config.id,
            evaluation.sink_class.value,
            evaluation.requested_outcome.value,
            evaluation.effective_outcome.value,
            evaluation.mode.value,
            state.max_tier.config_value,
            sorted(state.approved_sinks),
        )
        permitted = {TaintPolicyOutcome.ALLOW, TaintPolicyOutcome.AUDIT}
        if state.is_sink_approved(sink_class, profile_id=self.service_config.id):
            permitted |= {TaintPolicyOutcome.CONFIRM}
            if evaluation.verdict_floor is not TaintPolicyOutcome.DENY:
                # A human approval carried with this exact turn already answers
                # a confirmable adjudication. A deny floor remains absolute.
                permitted |= {TaintPolicyOutcome.ADJUDICATE}
        if evaluation.effective_outcome in permitted:
            return None

        return (
            f"Profile '{self.service_config.id}' refused this request: it runs "
            f"code in a sandbox ({sink_class.value}), and the request carries "
            f"{state.max_tier.config_value} content, which the runtime taint "
            f"policy resolves to '{evaluation.effective_outcome.value}'. "
            "Content derived from email, web pages or other untrusted sources "
            "cannot direct a code-execution agent. Ask the user to make the "
            "request themselves if it is genuinely wanted."
        )

    @property
    def llm_client(self) -> LLMInterface:
        return self._llm_client

    @llm_client.setter
    def llm_client(self, value: LLMInterface) -> None:
        self._llm_client = value
        self.llm_loop.llm_client = value
        self.attachment_processor.llm_client = value

    @property
    def attachment_registry(self) -> AttachmentRegistry | None:
        return self._attachment_registry

    @attachment_registry.setter
    def attachment_registry(self, value: AttachmentRegistry | None) -> None:
        self._attachment_registry = value
        self.attachment_processor.attachment_registry = value
        self.tool_executor.attachment_registry = value

    async def _resolve_thread_root_id(
        self,
        db_context: Database,
        interface_type: str,
        replied_to_interface_id: str | None,
    ) -> int | None:
        """Resolve the thread root ID when the user replied to an existing message."""
        if replied_to_interface_id is None:
            return None

        replied_to_msg_row = await db_context.message_history.get_row_by_interface_id(
            interface_type=interface_type,
            interface_message_id=replied_to_interface_id,
        )
        if replied_to_msg_row:
            thread_root_id = replied_to_msg_row.get(
                "thread_root_id"
            ) or replied_to_msg_row.get("internal_id")
            logger.info(
                "Received reply to interface message %s. Thread root ID: %s",
                replied_to_interface_id,
                thread_root_id,
            )
            return thread_root_id

        logger.warning(
            "Replied-to interface message %s not found. Creating new thread.",
            replied_to_interface_id,
        )
        return None

    def _extract_user_content_for_history(
        self, trigger_content_parts: list[ContentPartDict]
    ) -> str:
        """Extract a concise text value for message-history storage."""
        if not trigger_content_parts:
            return ""

        first_text_part = next(
            (
                part.get("text")
                for part in trigger_content_parts
                if part.get("type") == "text"
            ),
            None,
        )
        if first_text_part:
            return str(first_text_part)
        if trigger_content_parts[0].get("type") == "image_url":
            return "[Media Attached]"
        return ""

    async def _build_initial_messages_for_llm(
        self,
        db_context: Database,
        interface_type: str,
        conversation_id: str,
        replied_to_interface_id: str | None,
        thread_root_id_for_turn: int | None,
        subconversation_id: str | None,
        *,
        acting_user_id: str | None,
    ) -> tuple[list[LLMMessage], str]:
        """Load history and optional full-thread context for LLM processing."""
        history_limit, history_max_age = self.context_preparer.get_history_limits(
            interface_type
        )
        raw_history_messages = await db_context.message_history.get_recent(
            interface_type=interface_type,
            conversation_id=conversation_id,
            limit=history_limit,
            max_age=history_max_age,
            processing_profile_id=self.service_config.id,
            subconversation_id=subconversation_id,
            current_time=self.clock.now(),
        )
        logger.debug("Raw history messages fetched (%d).", len(raw_history_messages))

        initial_messages_for_llm = await self.context_preparer.format_history(
            raw_history_messages
        )
        logger.debug(
            "Initial messages for LLM after formatting history (%d).",
            len(initial_messages_for_llm),
        )

        thread_attachments_context = ""
        if replied_to_interface_id and thread_root_id_for_turn:
            logger.info(
                "Fetching full thread history for root ID %s due to reply.",
                thread_root_id_for_turn,
            )
            full_thread_messages = await db_context.message_history.get_by_thread_id(
                thread_root_id=thread_root_id_for_turn,
                processing_profile_id=None,
                subconversation_id=subconversation_id,
            )
            initial_messages_for_llm = await self.context_preparer.format_history(
                full_thread_messages
            )
            logger.info(
                "Using %d messages from full thread history for LLM context.",
                len(initial_messages_for_llm),
            )

            thread_attachments_context = (
                await self.attachment_processor.extract_conversation_context(
                    db_context,
                    conversation_id,
                    self.service_config.history_max_age_hours,
                    self.service_config.prompts,
                    acting_user_id=acting_user_id,
                )
            )
            if thread_attachments_context:
                logger.debug(
                    "Extracted attachment context from thread messages for LLM."
                )

        return initial_messages_for_llm, thread_attachments_context

    async def _append_missing_pinned_history_messages(
        self,
        db_context: Database,
        messages_for_llm: list[LLMMessage],
        pinned_history_message_ids: list[int] | None,
    ) -> None:
        """Append required rows that history limits may have excluded."""
        if not pinned_history_message_ids:
            return

        pinned_messages = await db_context.message_history.get_by_internal_ids(
            tuple(pinned_history_message_ids)
        )
        pinned_messages_for_llm = await self.context_preparer.format_history(
            pinned_messages
        )
        existing_keys = {
            (message.role, self._message_content_key(message))
            for message in messages_for_llm
        }
        for pinned_message in pinned_messages_for_llm:
            pinned_key = (
                pinned_message.role,
                self._message_content_key(pinned_message),
            )
            if pinned_key not in existing_keys:
                messages_for_llm.append(pinned_message)
                existing_keys.add(pinned_key)

    @staticmethod
    def _message_content_key(message: LLMMessage) -> str:
        """Return a stable content key for duplicate detection."""
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        return json.dumps(content, default=str, sort_keys=True)

    @staticmethod
    def _prune_leading_invalid_messages(messages_for_llm: list[LLMMessage]) -> int:
        """Remove leading tool messages/tool-calling assistant messages."""
        pruned_count = 0
        while messages_for_llm:
            first_msg = messages_for_llm[0]
            is_tool_msg = isinstance(first_msg, ToolMessage)
            is_assistant_with_tools = (
                isinstance(first_msg, AssistantMessage) and first_msg.tool_calls
            )
            if is_tool_msg or is_assistant_with_tools:
                messages_for_llm.pop(0)
                pruned_count += 1
            else:
                break
        return pruned_count

    @staticmethod
    def _repair_unmatched_tool_calls(
        messages_for_llm: list[LLMMessage],
    ) -> tuple[int, int]:
        """Pair every tool call left in the history with exactly one result.

        History can contain an assistant tool call whose result was never
        written: a turn that is interrupted while a tool is still running
        persists the call as soon as the model emits it, and the result only
        lands when the tool finishes. A client that starts a second turn on
        that conversation meanwhile replays the gap, which providers that
        validate the pairing reject outright — OpenAI's Responses API fails the
        whole request with "No tool output found for function call".

        Synthesize a placeholder result for each unmatched call so the model
        can see that the call was abandoned rather than silently losing it, and
        drop tool results whose call is missing, which cannot be represented at
        all. Truncation of the history window is handled before this by
        :meth:`_prune_leading_invalid_messages`, so an unmatched call reaching
        here really is an abandoned one and not merely a call whose result fell
        outside the window.

        Every surviving result is emitted directly after its calling assistant
        message, even if history stored it further down. The incident this
        repairs produces exactly that separation — the rival turn persists its
        prompt while the slow tool is still running, so the rows land as
        ``assistant(call) → user(rival prompt) → tool(result)`` — and a provider
        that wants the results attached to the calling turn rejects it. Moving
        the result keeps the real output (the model can use it) instead of
        discarding it for a placeholder; the cost is that the intervening
        message now sorts after a result it was originally typed before.

        Returns the (synthesized, dropped) counts.
        """
        # A result only answers its call if the call came first, so both facts
        # have to be known before any message can be classified: which results
        # are usable, and therefore which calls still need one.
        seen_call_ids: set[str] = set()
        # call id -> its result, hoisted out of the stream so it can be re-emitted
        # at the call site. Only the first result for a call is kept; a duplicate
        # cannot be represented and is dropped like an orphan.
        results_by_call_id: dict[str, ToolMessage] = {}
        dropped = 0
        for message in messages_for_llm:
            if not isinstance(message, ToolMessage):
                if isinstance(message, AssistantMessage) and message.tool_calls:
                    seen_call_ids.update(
                        tool_call.id for tool_call in message.tool_calls
                    )
                continue
            if (
                message.tool_call_id not in seen_call_ids
                or message.tool_call_id in results_by_call_id
            ):
                dropped += 1
                continue
            results_by_call_id[message.tool_call_id] = message

        repaired: list[LLMMessage] = []
        synthesized = 0
        for message in messages_for_llm:
            if isinstance(message, ToolMessage):
                # Emitted at its call site below, or already counted as dropped.
                continue
            repaired.append(message)
            if not isinstance(message, AssistantMessage) or not message.tool_calls:
                continue
            for tool_call in message.tool_calls:
                answer = results_by_call_id.get(tool_call.id)
                if answer is not None:
                    repaired.append(answer)
                    continue
                repaired.append(
                    ToolMessage(
                        tool_call_id=tool_call.id,
                        name=tool_call.function.name,
                        content=ABANDONED_TOOL_CALL_RESULT,
                    )
                )
                synthesized += 1
        messages_for_llm[:] = repaired
        return synthesized, dropped

    def render_available_service_profiles(self) -> str:
        """Render the catalog of delegatable service profiles.

        Sourced from the live processing-services registry so it reflects the
        operator's configured profiles (including custom ones). Returns an empty
        string when no registry is available.
        """
        registry = self.processing_services_registry
        if not registry:
            return ""
        lines = [
            f"- ID: {profile_id}, Description: "
            f"{service.service_config.description or 'No description available.'}"
            for profile_id, service in registry.items()
        ]
        return "\n".join(lines)

    async def delegation_catalog_addition(self) -> str:
        """Return a system-prompt section listing delegation targets, or "".

        This used to live in the delegate_to_service schema. It is now appended
        to the rendered system prompt independently of the per-profile prompt
        template, so every profile that actually advertises delegate_to_service
        gets the catalog (some shipped profiles override system_prompt and would
        otherwise miss it). Returns "" when the profile cannot delegate or the
        registry is empty.
        """
        catalog = self.render_available_service_profiles()
        if not catalog:
            return ""
        from family_assistant.tools.infrastructure import (  # noqa: PLC0415
            get_tool_definitions_for_advertisement,
        )

        advertised = await get_tool_definitions_for_advertisement(
            self.tools_provider, can_confirm=True
        )
        advertised_names = {
            definition.get("function", {}).get("name") for definition in advertised
        }
        if "delegate_to_service" not in advertised_names:
            return ""
        return (
            "## Service profiles you can delegate to\n"
            "Use `delegate_to_service` with one of these IDs as `target_service_id`:\n"
            f"{catalog}"
        )

    def validate_system_prompt_renders(self) -> None:
        """Render the system prompt once, raising ValueError if the template is bad.

        Called at startup so an operator template that still asks for a removed
        placeholder fails the boot rather than the first conversation to reach
        this profile. The user name is the only per-conversation input and any
        value exercises the same code path, so a stand-in is enough.
        """
        self.format_system_prompt(user_name="startup validation")

    @staticmethod
    def _build_system_message(content: str) -> SystemMessage:
        """Wrap a rendered system prompt, marking the whole of it cacheable.

        The prompt carries no per-turn material -- the clock and the context
        providers ride in the trailing ``<turn_context>`` block instead -- so all
        of it is stable across a conversation's requests and the cache breakpoint
        sits at its end. Text appended after this point (attachment metadata,
        on-demand tool additions) lands past the offset and stays out of the
        cached block, which is what ``stable_prefix_len`` is for.
        """
        return SystemMessage(content=content, stable_prefix_len=len(content) or None)

    def current_time_str(self, *, fmt: str = DEFAULT_TIME_FORMAT) -> str:
        """Now, in the profile's timezone, as the model is shown it.

        Public because the surfaces that report or re-render the turn-context
        block -- the context viewer and the two Live API paths -- must not spell
        this out for themselves and drift from what the model actually receives.
        It reads the injected clock, so a test that pins the clock pins these too.

        *fmt* exists for telephony, which has the model speak the time aloud and
        wants a more speakable rendering than the machine-readable default.
        """
        return self.clock.now().astimezone(self.service_config.timezone).strftime(fmt)

    def format_system_prompt(self, *, user_name: str) -> str:
        """Render the system prompt template with strict placeholder validation.

        ``current_time`` and ``aggregated_other_context`` are deliberately absent
        from ``format_args``: they now ride in the trailing ``<turn_context>``
        block, and a template still asking for them would quietly reintroduce the
        cache-busting interpolation this moved away from. Leaving them out turns
        that into the unknown-placeholder error below, which
        ``validate_system_prompt_renders`` surfaces at startup.
        """
        system_prompt_template = self.service_config.prompts.get(
            "system_prompt",
            "You are a helpful assistant.",
        )
        system_prompt_docs = self.service_config.prompts.get("system_prompt_docs", "")
        format_args = {
            "user_name": user_name,
            "server_url": self.server_url,
            "profile_id": self.service_config.id,
        }

        formatter = Formatter()
        escaped_template_parts: list[str] = []
        unknown_placeholders: set[str] = set()
        simple_placeholder_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

        for literal_text, field_name, format_spec, conversion in formatter.parse(
            system_prompt_template
        ):
            escaped_template_parts.append(
                literal_text.replace("{", "{{").replace("}", "}}")
            )
            if field_name is None:
                continue

            if (
                simple_placeholder_pattern.fullmatch(field_name)
                and not format_spec
                and conversion is None
            ):
                if field_name not in format_args:
                    unknown_placeholders.add(field_name)
                    continue
                escaped_template_parts.append(f"{{{field_name}}}")
                continue

            literal_field = "{" + field_name
            if conversion is not None:
                literal_field += f"!{conversion}"
            if format_spec:
                literal_field += f":{format_spec}"
            literal_field += "}"
            escaped_template_parts.append(
                literal_field.replace("{", "{{").replace("}", "}}")
            )

        if unknown_placeholders:
            unknown_placeholder_list = ", ".join(sorted(unknown_placeholders))
            raise ValueError(
                "System prompt template contains unknown placeholders: "
                f"{unknown_placeholder_list}. Escape literal braces with '{{' and '}}'."
            )
        escaped_template = "".join(escaped_template_parts)

        try:
            final_system_prompt = escaped_template.format_map(format_args).strip()
        except ValueError as exc:
            raise ValueError(f"Failed to format system prompt template: {exc}") from exc

        if isinstance(system_prompt_docs, str) and system_prompt_docs.strip():
            if final_system_prompt:
                final_system_prompt = (
                    f"{final_system_prompt}\n{system_prompt_docs}".strip()
                )
            else:
                final_system_prompt = system_prompt_docs.strip()

        # Appended here rather than written into each profile's template, since
        # every profile that receives the block needs to be told what it is -- and
        # told accurately: a profile without the aggregated-context grant must not
        # be led to believe its notes and calendar are in there.
        if self.sends_turn_context_block:
            guidance = turn_context_guidance(
                includes_aggregated_context=(
                    self.service_config.include_aggregated_context
                ),
                placement="appended",
            )
            final_system_prompt = f"{final_system_prompt}\n\n{guidance}".strip()

        return self.context_preparer.prepend_profile_preamble(final_system_prompt)

    @staticmethod
    def _inject_trigger_attachment_metadata(
        messages_for_llm: list[LLMMessage],
        trigger_attachments: list[MessageAttachmentMetadata] | None,
    ) -> None:
        """Inject trigger-attachment metadata into the latest trigger message."""
        if not trigger_attachments:
            return

        metadata_text = format_attachment_metadata_block(trigger_attachments)
        if not metadata_text:
            return

        for i in range(len(messages_for_llm) - 1, -1, -1):
            msg = messages_for_llm[i]
            if isinstance(msg, UserMessage):
                inject_metadata_into_user_message(msg, metadata_text)
                return
            if isinstance(msg, SystemMessage):
                msg.content = f"{msg.content}\n\n{metadata_text}"
                return

    @staticmethod
    def _is_delegation_wake_trigger_content(content: str) -> bool:
        """Return whether content is a one-shot delegation wake trigger."""
        return content.startswith((
            "System: Delegated profile task completed.",
            "System: Delegated profile task failed.",
        ))

    @staticmethod
    def _replace_historical_delegation_wake_with_active_system_trigger(
        messages_for_llm: list[LLMMessage],
        trigger_content: str,
    ) -> None:
        """Make the current wake a system trigger without replaying old wakes as system."""
        delegation_reference_line = next(
            (
                line
                for line in trigger_content.splitlines()
                if line.startswith("Delegation reference:")
            ),
            None,
        )
        for index in range(len(messages_for_llm) - 1, -1, -1):
            msg = messages_for_llm[index]
            if (
                delegation_reference_line is not None
                and isinstance(msg, UserMessage)
                and isinstance(msg.content, str)
                and msg.content.startswith(
                    "Historical delegation completion event from a previous turn."
                )
                and delegation_reference_line in msg.content
            ):
                messages_for_llm.pop(index)
                break
        messages_for_llm.append(SystemMessage(content=trigger_content))

    async def _save_history_message(
        self,
        db_context: Database,
        *,
        message: LLMMessage,
        interface_type: str,
        conversation_id: str,
        turn_id: str,
        thread_root_id: int | None,
        timestamp: datetime | None = None,
        interface_message_id: str | None = None,
        subconversation_id: str | None = None,
        user_id: str | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        attachments: list[MessageAttachmentMetadata] | None = None,
        is_internal: bool = False,
    ) -> int | None:
        """Persist a history message."""
        message_timestamp = timestamp if timestamp is not None else self.clock.now()

        return await db_context.message_history.add_message(
            message=message,
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=interface_message_id,
            turn_id=turn_id,
            thread_root_id=thread_root_id,
            timestamp=message_timestamp,
            processing_profile_id=self.service_config.id,
            subconversation_id=subconversation_id,
            user_id=user_id,
            reasoning_info=reasoning_info,
            attachments=attachments,
            is_internal=is_internal,
        )

    async def _persist_error_history_message(
        self,
        db_context: Database,
        *,
        error_message: str,
        error_traceback: str,
        interface_type: str,
        conversation_id: str,
        turn_id: str,
        thread_root_id: int | None,
        subconversation_id: str | None,
        user_id: str | None,
    ) -> int | None:
        """Persist a processing error message."""
        try:
            return await self._save_history_message(
                db_context,
                message=ErrorMessage(
                    content=error_message,
                    error_traceback=error_traceback,
                ),
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=None,
                turn_id=turn_id,
                thread_root_id=thread_root_id,
                subconversation_id=subconversation_id,
                user_id=user_id,
            )
        except Exception:
            logger.exception("Failed to save error message to history")
            return None

    async def _prepare_turn_messages_for_llm(
        self,
        db_context: Database,
        *,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        turn_id: str,
        user_id: str | None,
        replied_to_interface_id: str | None,
        trigger_attachments: list[MessageAttachmentMetadata] | None,
        subconversation_id: str | None,
        thread_root_id: int | None = None,
        trigger_is_internal: bool = False,
        pinned_history_message_ids: list[int] | None = None,
        trigger_role: Literal["user", "system"] = "user",
        reuse_existing_user_row: bool = False,
        initial_taint_sources: Sequence[TaintSource] | None = None,
    ) -> tuple[int | None, list[LLMMessage], tuple[TaintSource, ...]]:
        """Build the full pre-LLM turn state shared by sync and streaming flows."""
        thread_root_id_for_turn = thread_root_id
        if thread_root_id_for_turn is None:
            thread_root_id_for_turn = await self._resolve_thread_root_id(
                db_context=db_context,
                interface_type=interface_type,
                replied_to_interface_id=replied_to_interface_id,
            )
        user_content_for_history = self._extract_user_content_for_history(
            trigger_content_parts
        )
        actual_interface_message_id = trigger_interface_message_id or f"temp_{turn_id}"

        trigger_message: LLMMessage
        trigger_taint_metadata = _taint_metadata_from_sources(initial_taint_sources)
        if trigger_role == "system":
            trigger_message = SystemMessage(content=user_content_for_history)
        else:
            trigger_message = UserMessage(
                content=user_content_for_history,
                taint_metadata=trigger_taint_metadata,
            )
        # When the caller already persisted this turn's user message, reuse it
        # instead of inserting a duplicate. The web endpoint does this before
        # launching the (cancellable) producer task, so a Stop that cancels the
        # producer before it runs still leaves the prompt durable. Only the web
        # path sets this; other callers insert here as usual, so we avoid an extra
        # read on their (often single-connection SQLite) db_context.
        existing_user_row = (
            await db_context.message_history.get_user_row_by_turn_id(turn_id)
            if reuse_existing_user_row and trigger_role == "user"
            else None
        )
        if existing_user_row is not None:
            saved_user_msg_record = existing_user_row["internal_id"]
        else:
            saved_user_msg_record = await self._save_history_message(
                db_context,
                message=trigger_message,
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=actual_interface_message_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id_for_turn,
                timestamp=self.clock.now(),
                attachments=trigger_attachments,
                subconversation_id=subconversation_id,
                user_id=user_id,
                is_internal=trigger_is_internal,
            )
        if saved_user_msg_record is not None and thread_root_id_for_turn is None:
            thread_root_id_for_turn = saved_user_msg_record
            logger.info("Established new thread_root_id: %s", thread_root_id_for_turn)

        (
            messages_for_llm,
            thread_attachments_context,
        ) = await self._build_initial_messages_for_llm(
            db_context=db_context,
            interface_type=interface_type,
            conversation_id=conversation_id,
            replied_to_interface_id=replied_to_interface_id,
            thread_root_id_for_turn=thread_root_id_for_turn,
            subconversation_id=subconversation_id,
            acting_user_id=user_id,
        )
        if trigger_role == "system" and self._is_delegation_wake_trigger_content(
            user_content_for_history
        ):
            self._replace_historical_delegation_wake_with_active_system_trigger(
                messages_for_llm,
                user_content_for_history,
            )
        await self._append_missing_pinned_history_messages(
            db_context,
            messages_for_llm,
            pinned_history_message_ids,
        )
        pruned_count = self._prune_leading_invalid_messages(messages_for_llm)
        if pruned_count > 0:
            logger.warning("Pruned %d leading messages from LLM history.", pruned_count)
        synthesized, dropped = self._repair_unmatched_tool_calls(messages_for_llm)
        if synthesized or dropped:
            logger.warning(
                "Repaired unmatched tool calls in LLM history for conversation %s: "
                "%d abandoned call(s) given a placeholder result, %d orphaned "
                "result(s) dropped.",
                conversation_id,
                synthesized,
                dropped,
            )

        # A profile opts in to the household's own data -- notes, calendar, home
        # state -- by setting include_aggregated_context. Most shipped profiles do
        # not, and the taint that comes with the context is gated with it: a
        # profile that never receives the context was never exposed to it.
        aggregated_other_context_str = ""
        context_taint_sources: tuple[TaintSource, ...] = ()
        if self.service_config.include_aggregated_context:
            aggregated_other_context_str = (
                await self.context_preparer.aggregate_context()
            )
            context_taint_sources = (
                await self.context_preparer.aggregate_context_taint_sources()
            )
            if thread_attachments_context:
                if aggregated_other_context_str:
                    aggregated_other_context_str += "\n\n" + thread_attachments_context
                else:
                    aggregated_other_context_str = thread_attachments_context

        final_system_prompt = self.format_system_prompt(user_name=user_name)
        delegation_addition = await self.delegation_catalog_addition()
        if delegation_addition:
            # Config-derived, so it is as stable as the rest of the prompt and
            # belongs inside the cached block rather than after it.
            final_system_prompt = (
                f"{final_system_prompt}\n\n{delegation_addition}".strip()
            )
        if final_system_prompt:
            messages_for_llm.insert(0, self._build_system_message(final_system_prompt))

        processed_content_parts = await self.attachment_processor.process_content_parts(
            db_context,
            conversation_id,
            trigger_content_parts,
            acting_user_id=user_id,
        )
        # Before the injection messages are appended, so the block lands on the
        # trigger rather than on the newest injection. An injection built by a
        # provider adapter can carry its payload in `parts`, which is what that
        # provider renders -- text appended to `content` there is dropped on the
        # floor.
        #
        # Injected attachments are listed alongside the trigger's own: a
        # delegated profile receives its attachments only as injections, and
        # without their ids in the block it can see the image but cannot name
        # it to transform_image or any other attachment-taking tool.
        self._inject_trigger_attachment_metadata(
            messages_for_llm=messages_for_llm,
            trigger_attachments=merge_attachment_metadata(
                trigger_attachments, processed_content_parts.attachments
            ),
        )
        messages_for_llm.extend(processed_content_parts.messages)
        # Last, and after the attachment-metadata injection above: that scans back
        # for the newest user message, and would fasten the trigger's attachment
        # list onto this block instead of onto the trigger.
        messages_for_llm.append(
            build_turn_context_message(
                current_time_str=self.current_time_str(),
                aggregated_context=aggregated_other_context_str,
            )
        )
        typed_messages_for_llm = await self.attachment_processor.convert_message_urls(
            db_context, messages_for_llm, acting_user_id=user_id
        )
        return thread_root_id_for_turn, typed_messages_for_llm, context_taint_sources

    async def process_message(
        self,
        db_context: Database,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
    ) -> tuple[list[LLMMessage], MessageReasoningInfo | None, list[str] | None]:
        """
        Non-streaming version of process_message that uses the streaming generator internally.

        Returns:
            A tuple containing:
            - A list of all typed LLMMessage objects generated during this turn.
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
            - A list of attachment IDs to send with the response (or None).
        """
        return await self.llm_loop.run(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            chat_interface=chat_interface,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=self,
            home_assistant_client=self.home_assistant_client,
            camera_backend=self.camera_backend,
            event_sources=self.event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
            taint_tracker=taint_tracker,
            tool_call_review_trigger=tool_call_review_trigger,
        )

    async def process_message_stream(
        self,
        db_context: Database,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
    ) -> AsyncIterator[tuple[LLMStreamEvent, LLMMessage | None]]:
        """
        Streaming version of process_message that yields LLMStreamEvent objects as they are generated.

        Yields tuples of (event, message) where:
        - event: The LLMStreamEvent object
        - message: The typed LLMMessage to be saved to history (for assistant/tool messages)

        This generator handles the same logic as process_message but yields events incrementally.
        """
        async for item in self.llm_loop.run_stream(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            chat_interface=chat_interface,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=self,
            home_assistant_client=self.home_assistant_client,
            camera_backend=self.camera_backend,
            event_sources=self.event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
            taint_tracker=taint_tracker,
            tool_call_review_trigger=tool_call_review_trigger,
        ):
            yield item

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
    ) -> ChatInteractionResult:
        """
        Handles a complete chat interaction from user input to final response.

        This method orchestrates the entire conversation flow:
        1. Context aggregation (messages, attachments, calendar, etc.)
        2. LLM processing with tool execution
        3. Message saving and final response extraction
        4. Error handling and recovery

        Args:
            db_context: Database context for operations
            interface_type: Type of interface (e.g., "telegram", "web")
            conversation_id: Unique conversation identifier
            trigger_content_parts: User's message content parts
            trigger_interface_message_id: Interface-specific message ID
            user_name: Name of the user
            user_id: User identifier
            replied_to_interface_id: ID of message being replied to
            chat_interface: Interface for sending messages
            chat_interfaces: All registered chat interfaces
            confirmation_ui_managers: Confirmation UI managers by interface
            request_confirmation_callback: Callback for tool confirmations
            trigger_attachments: Attachments from the user
            subconversation_id: Subconversation identifier
            thread_root_id: Existing message-history row to use as this turn's thread root
            trigger_is_internal: Hide the trigger row from user-facing history.
            pinned_history_message_ids: Message rows that must be present even if
                normal history limits would exclude them.

        Returns:
            ChatInteractionResult containing:
            - text_reply: Final LLM content to send to user (str; empty if no text)
            - assistant_message_internal_id: Internal message ID of assistant's response (int | None)
            - reasoning_info: Final reasoning information (dict | None)
            - error_traceback: Processing error traceback if any (str | None)
            - attachment_ids: Response attachment IDs (list[str] | None)
        """

        if turn_id is None:
            turn_id = str(uuid.uuid4())
        logger.info(
            f"Starting handle_chat_interaction for conversation {conversation_id}, turn {turn_id}"
        )

        thread_root_id_for_turn: int | None = None

        async def interaction_success() -> ChatInteractionResult:
            nonlocal thread_root_id_for_turn
            # --- 1-2. Persist user trigger + build LLM-ready messages ---
            (
                thread_root_id_for_turn,
                typed_messages_for_llm,
                context_taint_sources,
            ) = await self._prepare_turn_messages_for_llm(
                db_context,
                interface_type=interface_type,
                conversation_id=conversation_id,
                trigger_content_parts=trigger_content_parts,
                trigger_interface_message_id=trigger_interface_message_id,
                user_name=user_name,
                turn_id=turn_id,
                user_id=user_id,
                replied_to_interface_id=replied_to_interface_id,
                trigger_attachments=trigger_attachments,
                subconversation_id=subconversation_id,
                thread_root_id=thread_root_id,
                trigger_is_internal=trigger_is_internal,
                pinned_history_message_ids=pinned_history_message_ids,
                trigger_role=trigger_role,
                reuse_existing_user_row=reuse_existing_user_row,
                initial_taint_sources=initial_taint_sources,
            )

            # --- 3. Call Core LLM Processing (self.process_message) ---
            (
                generated_turn_messages,
                final_reasoning_info_from_process_msg,
                response_attachment_ids,
            ) = await self.process_message(
                db_context=db_context,
                messages=typed_messages_for_llm,
                interface_type=interface_type,
                conversation_id=conversation_id,
                user_name=user_name,
                user_id=user_id,
                turn_id=turn_id,
                chat_interface=chat_interface,
                chat_interfaces=chat_interfaces,
                confirmation_ui_managers=confirmation_ui_managers,
                request_confirmation_callback=request_confirmation_callback,
                subconversation_id=subconversation_id,
                mid_turn_input_provider=mid_turn_input_provider,
                initial_taint_sources=(
                    *context_taint_sources,
                    *(initial_taint_sources or ()),
                ),
                tool_call_review_trigger=tool_call_review_trigger,
            )
            final_reasoning_info = final_reasoning_info_from_process_msg

            # --- 4. Save Generated Turn Messages & Extract Final Reply ---
            final_text_reply = ""
            final_assistant_message_internal_id = None

            if generated_turn_messages:
                # The reply's attachments belong on the last assistant row of the
                # turn, and only where a tool row isn't already carrying them.
                recorded_on_tool_rows: set[str] = set()
                for turn_msg in generated_turn_messages:
                    recorded_on_tool_rows |= _tool_row_attachment_ids(turn_msg)
                final_assistant_index = max(
                    (
                        index
                        for index, message in enumerate(generated_turn_messages)
                        if _is_turn_closing_assistant_message(message)
                        and message.content
                    ),
                    default=None,
                )

                for index, turn_msg in enumerate(generated_turn_messages):
                    if (
                        isinstance(turn_msg, AssistantMessage)
                        and turn_msg.content
                        and not turn_msg.tool_calls
                    ):
                        # Skip messages that carry tool calls: their content
                        # may be cryptographically tied to a Google thought
                        # signature (see ContextPreparer.format_history) and
                        # rewriting it would break replay continuity.
                        turn_msg.content = normalize_latex_to_unicode(turn_msg.content)

                    # Each assistant message carries the call that produced it.
                    # Falling back to the turn's last call would attribute one
                    # call's tokens to every iteration of a tool loop.
                    reasoning_info_for_msg = (
                        turn_msg.reasoning_info
                        if isinstance(turn_msg, AssistantMessage)
                        else None
                    )
                    saved_turn_msg_record = await self._save_history_message(
                        db_context,
                        message=turn_msg,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        thread_root_id=thread_root_id_for_turn,
                        subconversation_id=subconversation_id,
                        user_id=user_id,
                        reasoning_info=reasoning_info_for_msg,
                        attachments=(
                            _response_attachment_references(
                                response_attachment_ids,
                                recorded_on_tool_rows=recorded_on_tool_rows,
                            )
                            if index == final_assistant_index
                            else None
                        ),
                    )

                    if isinstance(turn_msg, AssistantMessage) and turn_msg.content:
                        final_text_reply = turn_msg.content
                        if saved_turn_msg_record is not None:
                            final_assistant_message_internal_id = saved_turn_msg_record
            else:
                logger.warning(
                    f"No messages generated by self.process_message for turn {turn_id}."
                )

            return ChatInteractionResult.success(
                text_reply=final_text_reply,
                assistant_message_internal_id=final_assistant_message_internal_id,
                reasoning_info=final_reasoning_info,
                attachment_ids=response_attachment_ids,
            )

        try:
            return await interaction_success()
        except TaintedSinkRefusedError as refusal:
            # A policy decision, not a fault: render the reason and skip the
            # error-history row and traceback the generic handler would write.
            logger.warning(
                "Runtime taint policy refused a turn on profile '%s': %s",
                self.service_config.id,
                refusal,
            )
            return ChatInteractionResult.error(
                text_reply=str(refusal),
                error_traceback=f"Runtime taint policy refused the turn: {refusal}",
            )
        except Exception as exc:
            logger.exception(
                f"Error in handle_chat_interaction for conversation {conversation_id}, turn {turn_id}"
            )
            processing_error_traceback = traceback.format_exc()

            error_message = _user_friendly_error_message(exc)
            error_message_internal_id = await self._persist_error_history_message(
                db_context,
                error_message=error_message,
                error_traceback=processing_error_traceback,
                interface_type=interface_type,
                conversation_id=conversation_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id_for_turn,
                subconversation_id=subconversation_id,
                user_id=user_id,
            )

            return ChatInteractionResult.error(
                text_reply=error_message,
                error_traceback=processing_error_traceback,
                assistant_message_internal_id=error_message_internal_id,
            )

    async def handle_chat_interaction_stream(
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
        reuse_existing_user_row: bool = False,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Streaming version of handle_chat_interaction.

        Yields LLMStreamEvent objects as the interaction progresses, providing
        real-time updates on text generation, tool calls, and tool results.

        Args:
            Same as handle_chat_interaction

        Yields:
            LLMStreamEvent objects representing different stages of processing
        """
        if turn_id is None:
            turn_id = str(uuid.uuid4())

        span = tracer.start_span(
            "conversation.process",
            attributes={
                "conversation.interface": interface_type,
                "conversation.id": conversation_id,
                "conversation.user": user_name,
            },
        )
        if subconversation_id:
            span.set_attribute("conversation.subconversation_id", subconversation_id)
        logger.info(
            f"Starting streaming chat interaction. Turn ID: {turn_id}, "
            f"Interface: {interface_type}, Conversation: {conversation_id}, "
            f"User: {user_name}, Content parts: {len(trigger_content_parts)}"
        )

        for i, part in enumerate(trigger_content_parts):
            logger.info(
                f"Processing content part {i}: type={part.get('type')}, size={len(str(part))}"
            )

        thread_root_id_for_turn: int | None = None

        async def interaction_events() -> AsyncGenerator[LLMStreamEvent]:
            nonlocal thread_root_id_for_turn
            # --- 1-2. Persist user trigger + build LLM-ready messages ---
            (
                thread_root_id_for_turn,
                typed_messages_for_llm,
                context_taint_sources,
            ) = await self._prepare_turn_messages_for_llm(
                db_context,
                interface_type=interface_type,
                conversation_id=conversation_id,
                trigger_content_parts=trigger_content_parts,
                trigger_interface_message_id=trigger_interface_message_id,
                user_name=user_name,
                turn_id=turn_id,
                user_id=user_id,
                replied_to_interface_id=replied_to_interface_id,
                trigger_attachments=trigger_attachments,
                subconversation_id=subconversation_id,
                reuse_existing_user_row=reuse_existing_user_row,
            )

            # --- 3. Stream LLM Processing ---
            # Ids already recorded on a tool row of this turn, so the
            # closing assistant row doesn't repeat them.
            recorded_on_tool_rows: set[str] = set()
            async for event, stream_msg in self.process_message_stream(
                db_context=db_context,
                messages=typed_messages_for_llm,
                interface_type=interface_type,
                conversation_id=conversation_id,
                user_name=user_name,
                user_id=user_id,
                turn_id=turn_id,
                chat_interface=chat_interface,
                chat_interfaces=chat_interfaces,
                confirmation_ui_managers=confirmation_ui_managers,
                request_confirmation_callback=request_confirmation_callback,
                subconversation_id=subconversation_id,
                mid_turn_input_provider=mid_turn_input_provider,
                initial_taint_sources=(
                    *context_taint_sources,
                    *(initial_taint_sources or ()),
                ),
                taint_tracker=taint_tracker,
                tool_call_review_trigger=tool_call_review_trigger,
            ):
                # A ``user_input`` echo is the client's proof that its
                # steering message was delivered: seeing one is what
                # stops it tracking the message for recovery. Publishing
                # it before the row is written would let a failed write
                # clear the client's only copy -- the message would exist
                # nowhere, having been neither persisted nor acted on. So
                # this one event is published after its save; everything
                # else streams first, since the reply should not wait on
                # a database round trip.
                publish_after_save = event.type == "user_input"
                if not publish_after_save:
                    yield event

                # Save messages as they're generated
                if stream_msg is not None:
                    if (
                        isinstance(stream_msg, AssistantMessage)
                        and stream_msg.content
                        and not stream_msg.tool_calls
                    ):
                        # Skip messages that carry tool calls: see
                        # the matching branch in
                        # handle_chat_interaction for the
                        # thought-signature rationale.
                        stream_msg.content = normalize_latex_to_unicode(
                            stream_msg.content
                        )
                    recorded_on_tool_rows |= _tool_row_attachment_ids(stream_msg)
                    # Every assistant message in the turn carries its
                    # own call's usage and timing, not just the one that
                    # closes it -- the intermediate iterations of a tool
                    # loop are calls too, and used to save nothing.
                    reasoning_info_for_stream = (
                        stream_msg.reasoning_info
                        if isinstance(stream_msg, AssistantMessage)
                        else None
                    )
                    # The turn's closing assistant message arrives on the
                    # same event as its response attachment ids, so this
                    # is where they get recorded.
                    response_attachments = (
                        _response_attachment_references(
                            event.metadata.get("attachment_ids"),
                            recorded_on_tool_rows=recorded_on_tool_rows,
                        )
                        if _is_turn_closing_assistant_message(stream_msg)
                        and event.metadata
                        else None
                    )
                    await self._save_history_message(
                        db_context,
                        message=stream_msg,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        thread_root_id=thread_root_id_for_turn,
                        subconversation_id=subconversation_id,
                        user_id=user_id,
                        reasoning_info=reasoning_info_for_stream,
                        attachments=response_attachments,
                    )

                if publish_after_save:
                    yield event

        events = interaction_events()

        async def traced_events() -> AsyncGenerator[LLMStreamEvent]:
            while True:
                try:
                    with trace.use_span(span, end_on_exit=False):
                        stream_event = await anext(events)
                except StopAsyncIteration:
                    return
                yield stream_event

        traced_iterator = traced_events()
        try:
            async for stream_event in traced_iterator:
                yield stream_event
        except TaintedSinkRefusedError as refusal:
            with trace.use_span(span, end_on_exit=False):
                logger.warning(
                    "Runtime taint policy refused a turn on profile '%s': %s",
                    self.service_config.id,
                    refusal,
                )
                refusal_event = LLMStreamEvent(
                    type="error",
                    error=str(refusal),
                    metadata={"error_id": str(uuid.uuid4())},
                )
            yield refusal_event
        except Exception as e:
            with trace.use_span(span, end_on_exit=False):
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                logger.exception(f"Error in streaming chat interaction: {e}")
                processing_error_traceback = traceback.format_exc()
                error_message = _user_friendly_error_message(e)
                await self._persist_error_history_message(
                    db_context,
                    error_message=error_message,
                    error_traceback=processing_error_traceback,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    thread_root_id=thread_root_id_for_turn,
                    subconversation_id=subconversation_id,
                    user_id=user_id,
                )
                error_event = LLMStreamEvent(
                    type="error",
                    error=error_message,
                    metadata={"error_id": str(uuid.uuid4())},
                )
            yield error_event
        finally:
            try:
                await traced_iterator.aclose()
            finally:
                try:
                    await events.aclose()
                finally:
                    span.end()
