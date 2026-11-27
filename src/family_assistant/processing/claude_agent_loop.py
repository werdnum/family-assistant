"""A turn run by the Claude Agent SDK, with this application's tools.

``ClaudeAgentLoop`` runs a profile's turns on tiers whose runtime is
``claude_code``. One turn is one Claude Code session: Claude Code runs
the agent loop, and every tool it calls is one of this application's,
offered over an in-process MCP server and executed by the same
``ToolExecutor`` the ordinary loop uses -- so tool policy, confirmation, taint
tracking and tool-call review apply to each call unchanged. The loop yields
the ordinary loop's ``(event, message)`` stream, so persistence and streaming
are unchanged too. See docs/design/claude-agent-sdk-profile.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import claude_agent_sdk
import mcp.types as mcp_types
from claude_agent_sdk import (
    AssistantMessage as SdkAssistantMessage,
)
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    MirrorErrorMessage,
    RateLimitEvent,
    ResultMessage,
    SessionStore,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_agent_sdk import (
    SystemMessage as SdkSystemMessage,
)
from claude_agent_sdk import (
    UserMessage as SdkUserMessage,
)
from claude_agent_sdk.types import (
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    McpSdkServerConfig,
)
from mcp.server import Server

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.base import (
    AuthenticationError,
    InvalidRequestError,
    LLMProviderError,
    RateLimitError,
    ServiceUnavailableError,
)
from family_assistant.llm.call_context import CallAttribution
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageReasoningInfo,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.model_selection import stamp_model_selection
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.observability.metrics import (
    record_claude_agent_ready,
    record_claude_agent_session,
    record_claude_agent_wake,
    record_claude_subscription_rate_limit,
    record_llm_call,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    TurnTaintState,
    merge_taint_state_into_tracker,
    prompt_window_taint,
)
from family_assistant.services.turn_resumption import (
    SUSPEND_INTERRUPT_AFTER_SECONDS,
)
from family_assistant.tools.infrastructure import (
    collect_system_prompt_addition,
    get_tool_definitions_for_advertisement,
)
from family_assistant.tools.on_demand import RESERVED_META_TOOL_NAMES
from family_assistant.tools.types import ToolCallReviewTurnState

from .claude_agent_prompt import (
    SessionContentBlock,
    SessionPrompt,
    build_session_prompt,
    resumed_opening,
    suspended_opening,
)
from .claude_agent_sessions import (
    WakeScope,
    database_session_store,
    record_session_turn,
    remove_local_transcript,
    session_to_resume,
    session_working_directory,
    suspended_session_to_resume,
)
from .llm_loop import LLMStreamingLoop, without_unavailable_thread_tools
from .quiet_turn import (
    END_TURN_QUIETLY_TOOL_DEFINITION,
    END_TURN_QUIETLY_TOOL_NAME,
    QUIET_END_TOOL_RESULT,
    quiet_end_reason,
    quiet_end_record,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Awaitable,
        Callable,
        Mapping,
        Sequence,
    )

    from claude_agent_sdk import Message as SdkMessage
    from claude_agent_sdk import Transport

    from family_assistant.config_models import AppConfig, ClaudeAgentConfig
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.llm.model_selection import ResolvedModelSelection
    from family_assistant.llm.model_tiers import ClaudeAgentModel
    from family_assistant.memory.review_context import MemoryReviewContext
    from family_assistant.plugins.runtime import ProfilePlugins
    from family_assistant.security.taint import TaintSource, TurnTaintTracker
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.database import Database
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.types import EventSourcesById, ToolDefinition

    from .attachments import AttachmentProcessor
    from .service import ProcessingService
    from .tool_execution import ToolExecutor
    from .types import (
        ContextLengthCompactor,
        LLMStreamingLoopConfig,
        MidTurnInputProvider,
        RequestConfirmationCallback,
    )

logger = logging.getLogger(__name__)

PROVIDER_NAME = "claude_agent_sdk"
MCP_SERVER_NAME = "family_assistant"
_MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
# Claude Code's own search over the tools it has been given. It is the only
# built-in the session gets, and it stands in for on-demand activation.
TOOL_SEARCH_TOOL_NAME = "ToolSearch"
# Claude Code prefers an API key to a subscription token, and the application's
# environment carries one for the `anthropic` provider. Blanking these is what
# lets the session fall through to CLAUDE_CODE_OAUTH_TOKEN.
# The subscription token from `claude setup-token`. Turns on a claude_code tier
# run in Claude Code only where it is set.
CLAUDE_CODE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_BLANKED_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# What a session reports when no API key is in use: a subscription login.
_SUBSCRIPTION_KEY_SOURCE = "none"
# A server has no use for Claude Code's update checks, telemetry or error
# reporting, and its own metrics come from this application instead.
_SESSION_ENV = {
    **dict.fromkeys(_BLANKED_CREDENTIAL_ENV, ""),
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

# The most one tool result may put in front of Claude Code, measured as the
# JSON it travels as: its text plus its images' base64. Well inside the API's
# request limit, and many full-size photos' worth of downscaled copies.
MAX_TOOL_RESULT_BYTES = 16 * 1024 * 1024
# The largest single message the SDK will read from Claude Code. Claude Code
# echoes a tool result back on its output, possibly more than once (the
# message and its tool_use_result), so this sits well above any result we
# send: the SDK's 1 MB default ends the whole turn on one detailed photo.
SESSION_MESSAGE_LIMIT_BYTES = 4 * MAX_TOOL_RESULT_BYTES

# How often a turn waiting on its session checks whether it has been asked to
# stop. Nothing else wakes it while a tool or the model is busy.
_STOP_POLL_SECONDS = 1.0

type ClientFactory = Callable[[ClaudeAgentOptions], ClaudeSDKClient]


class ClaudeAgentBillingError(AuthenticationError):
    """The session would be billed to an API key rather than a subscription."""


def default_client_factory(
    options: ClaudeAgentOptions, transport: Transport | None = None
) -> ClaudeSDKClient:
    return ClaudeSDKClient(options, transport=transport)


def mcp_tool_name(tool_name: str) -> str:
    """The name Claude Code knows one of this application's tools by."""
    return f"{_MCP_TOOL_PREFIX}{tool_name}"


@dataclass
class _ToolOutcome:
    """A tool call this application answered, in the order it was answered."""

    event: LLMStreamEvent
    message: ToolMessage
    attachment_updates: Callable[[list[str]], None] | None = None
    # Set once the loop's caller has taken the result, and so saved every row
    # before it. Only then is the result returned to Claude Code.
    handed_over: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Done:
    """The session's message stream has ended."""


@dataclass
class _Failed:
    error: BaseException


type _QueueItem = SdkMessage | _ToolOutcome | _Done | _Failed


@dataclass
class _PendingResponse:
    """One API response, which Claude Code reports a content block at a time."""

    message_id: str | None
    model: str
    text: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallItem] = field(default_factory=list)
    usage: dict[str, object] | None = None
    started_at: float = field(default_factory=time.monotonic)
    first_output_at: float | None = None


@dataclass(frozen=True)
class _CallTiming:
    """How long one API response took, measured once for the row and the metric."""

    duration_seconds: float
    time_to_first_output_seconds: float | None

    @classmethod
    def of(cls, response: _PendingResponse) -> _CallTiming:
        return cls(
            duration_seconds=time.monotonic() - response.started_at,
            time_to_first_output_seconds=(
                response.first_output_at - response.started_at
                if response.first_output_at is not None
                else None
            ),
        )


def _reasoning_info(
    usage: Mapping[str, object] | None,
    *,
    model: str,
    message_id: str | None,
    model_selection: ResolvedModelSelection,
    duration_seconds: float,
    time_to_first_output_seconds: float | None,
) -> MessageReasoningInfo | None:
    if usage is None:
        return None

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) else 0

    prompt = count("input_tokens")
    cached = count("cache_read_input_tokens")
    written = count("cache_creation_input_tokens")
    completion = count("output_tokens")
    info: MessageReasoningInfo = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_prompt_tokens": cached,
        "cache_write_tokens": written,
        "total_tokens": prompt + cached + written + completion,
        "provider": PROVIDER_NAME,
        "resolved_model": model,
    }
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        one_hour = cast("dict[str, object]", cache_creation).get(
            "ephemeral_1h_input_tokens"
        )
        if isinstance(one_hour, int):
            info["cache_write_1h_tokens"] = one_hour
    if message_id:
        info["response_id"] = message_id
    info["duration_ms"] = round(duration_seconds * 1000, 1)
    if time_to_first_output_seconds is not None:
        info["time_to_first_output_ms"] = round(time_to_first_output_seconds * 1000, 1)
    return stamp_model_selection(info, model_selection)


def _error_for(kind: str, text: str, model: str) -> LLMProviderError:
    """The provider error matching what Claude Code reported."""
    message = f"Claude Agent SDK session failed ({kind}): {text}"
    if kind == "rate_limit":
        return RateLimitError(message, PROVIDER_NAME, model)
    if kind in {"authentication_failed", "billing_error"}:
        return AuthenticationError(message, PROVIDER_NAME, model)
    if kind == "invalid_request":
        return InvalidRequestError(message, PROVIDER_NAME, model)
    if kind == "server_error":
        return ServiceUnavailableError(message, PROVIDER_NAME, model)
    return LLMProviderError(message, PROVIDER_NAME, model)


@dataclass
class _TurnContext:
    """What a turn's tool calls are executed with, fixed for the turn."""

    db_context: Database
    messages: list[LLMMessage]
    interface_type: str
    conversation_id: str
    user_name: str
    turn_id: str
    chat_interface: ChatInterface | None
    llm_client: LLMInterface
    model_selection: ResolvedModelSelection
    user_id: str | None
    chat_interfaces: dict[str, ChatInterface] | None
    confirmation_ui_managers: dict[str, ConfirmationUIManager] | None
    request_confirmation_callback: RequestConfirmationCallback | None
    subconversation_id: str | None
    processing_service: ProcessingService | None
    plugins: ProfilePlugins | None
    event_sources: EventSourcesById | None
    mid_turn_input_provider: MidTurnInputProvider | None
    taint_tracker: TurnTaintTracker
    tool_call_review_trigger: TriggerReviewInput | None
    memory_review: MemoryReviewContext | None
    allow_quiet_end: bool


type _Output = tuple[LLMStreamEvent, LLMMessage | None]
# An output item and the event its consumer sets on asking for the next one.
type _Handoff = tuple[_Output, asyncio.Event]


class _TurnSession:
    """One turn's Claude Code session, translated into the loop's output.

    Runs as a task feeding ``output``; the loop's generator only relays it,
    so the session's context managers are never held across a ``yield``.
    """

    def __init__(
        self,
        loop: ClaudeAgentLoop,
        turn: _TurnContext,
        model: ClaudeAgentModel,
        output: asyncio.Queue[_Handoff | _Done | _Failed],
        *,
        scope: WakeScope,
        resume: str | None,
    ) -> None:
        self.loop = loop
        self.turn = turn
        self.model = model
        self.output = output
        self.scope = scope
        self.resume = resume
        self.session_store = database_session_store(turn.db_context)
        self.session_id: str | None = None
        self.mirror_failed = False
        self.review_state = ToolCallReviewTurnState()
        # Everything the session reports and every tool result, in arrival
        # order, for the one consumer that turns them into output.
        self.inbox: asyncio.Queue[_QueueItem] = asyncio.Queue()
        # PreToolUse sees the model's tool_use id; the MCP handler that runs
        # the call does not. Calls are matched by name and arguments.
        self.tool_use_ids: defaultdict[str, deque[str]] = defaultdict(deque)
        self.recorded: dict[str, asyncio.Event] = {}
        self.outstanding: set[str] = set()
        self.response: _PendingResponse | None = None
        self.reported_usage: set[str] = set()
        self.pending_attachment_ids: list[str] = []
        self.turn_ending = False
        # A graceful shutdown asked this turn to stop: when, and whether the
        # session has been interrupted for it.
        self.suspend_started_at: float | None = None
        self.suspended = False
        self.quiet_reason: str | None = None
        self.result: ResultMessage | None = None
        self.outcome = "error"
        self.started_at = time.monotonic()

    # -- tool calls ---------------------------------------------------------

    @staticmethod
    def _call_key(name: str, arguments: object) -> str:
        return f"{name}\x00{json.dumps(arguments, sort_keys=True, default=str)}"

    def _recorded_event(self, tool_use_id: str) -> asyncio.Event:
        return self.recorded.setdefault(tool_use_id, asyncio.Event())

    async def pre_tool_use(
        self, input_data: HookInput, tool_use_id: str | None, _context: HookContext
    ) -> HookJSONOutput:
        if input_data["hook_event_name"] != "PreToolUse" or tool_use_id is None:
            return {}
        name = input_data["tool_name"]
        if name.startswith(_MCP_TOOL_PREFIX):
            key = self._call_key(
                name.removeprefix(_MCP_TOOL_PREFIX), input_data["tool_input"]
            )
            self.tool_use_ids[key].append(tool_use_id)
        return {}

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        """Run one of the session's tool calls, failing the turn if it cannot.

        The MCP server answers a handler's exception to Claude as an error
        result, so the session would carry on past a call whose result was
        never saved. The turn fails instead, as it would on the ordinary loop.
        """
        try:
            return await self._run_tool_call(name, arguments)
        except Exception as exc:
            await self.inbox.put(_Failed(exc))
            raise

    async def _run_tool_call(
        self, name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        pending = self.tool_use_ids.get(self._call_key(name, arguments))
        if not pending:
            msg = f"Tool call to '{name}' arrived without a recorded tool_use id"
            raise RuntimeError(msg)
        tool_use_id = pending.popleft()
        # The assistant message that made this call is output before its
        # result, so the saved history is valid tool-call protocol.
        await self._recorded_event(tool_use_id).wait()
        call = ToolCallItem(
            id=tool_use_id,
            type="function",
            function=ToolCallFunction(name=name, arguments=arguments),
        )
        tracker = self.turn.taint_tracker

        if self.turn_ending:
            text = "Not executed: this turn has already ended."
            await self._answer(_tool_outcome(call, text, tracker, error="turn_ended"))
            return _mcp_text(text, is_error=True)

        if self.turn.allow_quiet_end and name == END_TURN_QUIETLY_TOOL_NAME:
            self.quiet_reason = quiet_end_reason(call)
            self.turn_ending = True
            await self._answer(_tool_outcome(call, QUIET_END_TOOL_RESULT, tracker))
            return _mcp_text(QUIET_END_TOOL_RESULT)

        turn = self.turn
        result = await self.loop.tool_executor.execute(
            call,
            interface_type=turn.interface_type,
            conversation_id=turn.conversation_id,
            user_name=turn.user_name,
            user_id=turn.user_id,
            turn_id=turn.turn_id,
            db_context=turn.db_context,
            chat_interface=turn.chat_interface,
            chat_interfaces=turn.chat_interfaces,
            confirmation_ui_managers=turn.confirmation_ui_managers,
            request_confirmation_callback=turn.request_confirmation_callback,
            subconversation_id=turn.subconversation_id,
            processing_service=turn.processing_service,
            llm_client=turn.llm_client,
            plugins=turn.plugins,
            event_sources=turn.event_sources,
            taint_tracker=tracker,
            taint_policy_snapshot=tracker.snapshot(),
            tool_call_review_state=self.review_state,
            tool_call_review_messages=tuple(turn.messages),
            tool_call_review_trigger=turn.tool_call_review_trigger,
            memory_review=turn.memory_review,
        )
        if self.review_state.terminal_denial_escalation_message is not None:
            self.turn_ending = True
        await self._answer(
            _ToolOutcome(
                event=result.stream_event,
                message=result.llm_message,
                attachment_updates=result.apply_attachment_updates,
            )
        )
        return _mcp_result(result.llm_message)

    async def _answer(self, outcome: _ToolOutcome) -> None:
        """Queue a call's result, returning once its row has been handed over."""
        await self.inbox.put(outcome)
        await outcome.handed_over.wait()

    # -- output -------------------------------------------------------------

    async def _emit(self, event: LLMStreamEvent, message: LLMMessage | None) -> None:
        """Output one item, returning once the caller has asked for the next.

        The caller saves each message before asking for the next one, so when
        this returns the message is durable. That is the ordinary loop's
        back-pressure, which a queue would otherwise remove: no tool runs, and
        no result reaches Claude Code, ahead of the rows that record them.
        """
        taken = asyncio.Event()
        await self.output.put(((event, message), taken))
        await taken.wait()

    async def _emit_done(
        self, message: AssistantMessage, reasoning: MessageReasoningInfo | None
    ) -> None:
        metadata, self.pending_attachment_ids = await self.loop.build_done_metadata(
            message,
            reasoning,
            pending_attachment_ids=self.pending_attachment_ids,
            messages=self.turn.messages,
            db_context=self.turn.db_context,
            user_id=self.turn.user_id,
            llm_client=self.turn.llm_client,
        )
        await self._emit(LLMStreamEvent(type="done", metadata=metadata), message)
        # The turn so far, for the tool-call review of every later call.
        self.turn.messages.append(message)

    async def _emit_closing(self, text: str, *, ended_quietly: bool = False) -> None:
        """A closing assistant message the loop writes itself."""
        message = AssistantMessage(
            content=text,
            taint_metadata=self.turn.taint_tracker.snapshot().to_metadata(),
            ended_quietly=ended_quietly,
        )
        if not ended_quietly:
            await self._emit(LLMStreamEvent(type="content", content=text), None)
        await self._emit_done(message, None)

    async def _flush(self) -> None:
        """Output the response being assembled, if it said or did anything."""
        current, self.response = self.response, None
        if current is None:
            return
        reasoning = None
        if current.message_id not in self.reported_usage:
            # Counted even when nothing is saved for it: a call that only
            # searched for tools still spent subscription quota.
            timing = _CallTiming.of(current)
            reasoning = _reasoning_info(
                current.usage,
                model=current.model,
                message_id=current.message_id,
                model_selection=self.turn.model_selection,
                duration_seconds=timing.duration_seconds,
                time_to_first_output_seconds=timing.time_to_first_output_seconds,
            )
            if current.message_id:
                self.reported_usage.add(current.message_id)
            self._record_call(current, reasoning, outcome="success", timing=timing)
        text = "".join(current.text)
        if text.strip() or current.tool_calls:
            await self._emit_done(
                AssistantMessage(
                    content=text or None,
                    tool_calls=current.tool_calls or None,
                    taint_metadata=self.turn.taint_tracker.snapshot().to_metadata(),
                    reasoning_info=reasoning,
                ),
                reasoning,
            )
        # Each call's result reaches the inbox only after this, so it is output
        # behind the message that made it.
        for call in current.tool_calls:
            self.outstanding.add(call.id)
            self._recorded_event(call.id).set()

    def _record_call(
        self,
        response: _PendingResponse,
        reasoning: MessageReasoningInfo | None,
        *,
        outcome: str,
        error_type: str | None = None,
        timing: _CallTiming | None = None,
    ) -> None:
        """Count one of the session's model calls in the LLM call metrics."""
        if timing is None:
            timing = _CallTiming.of(response)
        record_llm_call(
            attribution=CallAttribution(
                profile_id=self.loop.config.id,
                model_selection=self.turn.model_selection,
            ),
            provider=PROVIDER_NAME,
            model=self.model.model,
            resolved_model=response.model,
            operation="agent_session",
            outcome=outcome,
            error_type=error_type,
            duration_seconds=timing.duration_seconds,
            time_to_first_output_seconds=timing.time_to_first_output_seconds,
            reasoning_info=reasoning,
        )

    async def _on_stream_event(self, item: StreamEvent) -> None:
        if item.parent_tool_use_id is not None:
            return
        event = item.event
        event_type = event.get("type")
        if event_type == "message_start":
            await self._flush()
            message = event.get("message") or {}
            self.response = _PendingResponse(
                message_id=message.get("id"),
                model=message.get("model") or self.model.model,
            )
        elif event_type == "content_block_delta":
            if self.response is not None and self.response.first_output_at is None:
                self.response.first_output_at = time.monotonic()
            delta = event.get("delta") or {}
            text = delta.get("text") if delta.get("type") == "text_delta" else None
            if text:
                await self._emit(LLMStreamEvent(type="content", content=text), None)
        elif event_type == "message_delta" and self.response is not None:
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.response.usage = cast("dict[str, object]", usage)
        elif event_type == "message_stop":
            await self._flush()

    async def _on_assistant(self, item: SdkAssistantMessage) -> None:
        if item.parent_tool_use_id is not None:
            return
        if item.error is not None:
            text = "".join(
                block.text for block in item.content if isinstance(block, TextBlock)
            )
            self._record_call(
                self.response
                or _PendingResponse(message_id=item.message_id, model=item.model),
                None,
                outcome="error",
                error_type=item.error,
            )
            self.response = None
            raise _error_for(item.error, text, self.model.model)
        if self.response is not None and self.response.message_id != item.message_id:
            await self._flush()
        if self.response is None:
            self.response = _PendingResponse(
                message_id=item.message_id, model=item.model
            )
        if item.usage is not None and self.response.usage is None:
            self.response.usage = item.usage
        for block in item.content:
            if isinstance(block, TextBlock):
                self.response.text.append(block.text)
            elif isinstance(block, ToolUseBlock) and block.name.startswith(
                _MCP_TOOL_PREFIX
            ):
                tool_call = ToolCallItem(
                    id=block.id,
                    type="function",
                    function=ToolCallFunction(
                        name=block.name.removeprefix(_MCP_TOOL_PREFIX),
                        arguments=block.input,
                    ),
                )
                self.response.tool_calls.append(tool_call)
                await self._emit(
                    LLMStreamEvent(type="tool_call", tool_call=tool_call), None
                )

    # -- the session --------------------------------------------------------

    async def _pump(self, client: ClaudeSDKClient) -> None:
        try:
            async for sdk_message in client.receive_response():
                await self.inbox.put(sdk_message)
        except Exception as exc:
            await self.inbox.put(_Failed(exc))
        else:
            await self.inbox.put(_Done())

    async def _next_item(self, client: ClaudeSDKClient) -> _QueueItem:
        """The session's next item, watching for a stop while waiting for it."""
        provider = self.turn.mid_turn_input_provider
        if provider is None:
            return await self.inbox.get()
        while True:
            if provider.should_interrupt():
                if not provider.suspend_requested:
                    raise asyncio.CancelledError("Turn interrupted by user")
                await self._suspend(client)
            try:
                return await asyncio.wait_for(self.inbox.get(), _STOP_POLL_SECONDS)
            except TimeoutError:
                continue

    async def _suspend(self, client: ClaudeSDKClient) -> None:
        """Stop the session for a graceful shutdown, as Ctrl-C would.

        The session is interrupted between steps, once Claude Code holds the
        results of every call it made (see ``_on_tool_results``), so nothing
        it worked out is lost; until then the model may also finish the turn
        by itself. Near the end of the grace it is interrupted wherever it is,
        and Claude Code records a running call as interrupted, so the session
        can still be recorded and resumed.
        """
        if self.suspended:
            return
        now = time.monotonic()
        if self.suspend_started_at is None:
            self.suspend_started_at = now
            logger.info(
                "Suspending Claude agent session %s of turn %s for shutdown "
                "(%d tool call(s) running)",
                self.session_id,
                self.turn.turn_id,
                len(self.outstanding),
            )
        if now - self.suspend_started_at >= SUSPEND_INTERRUPT_AFTER_SECONDS:
            await self._interrupt_for_suspend(client)

    async def _on_tool_results(self, client: ClaudeSDKClient) -> None:
        """Claude Code has taken tool results: a step boundary."""
        if self.suspend_started_at is not None and not self.outstanding:
            await self._interrupt_for_suspend(client)

    async def _interrupt_for_suspend(self, client: ClaudeSDKClient) -> None:
        if not self.suspended:
            self.suspended = True
            await client.interrupt()

    async def _drive(self, client: ClaudeSDKClient) -> None:
        """Consume the session until it ends, or until this turn has."""
        while True:
            item = await self._next_item(client)
            if isinstance(item, _Failed):
                raise item.error
            if isinstance(item, _Done):
                return
            if isinstance(item, _ToolOutcome):
                if item.attachment_updates is not None:
                    item.attachment_updates(self.pending_attachment_ids)
                self.outstanding.discard(item.message.tool_call_id)
                await self._emit(item.event, item.message)
                self.turn.messages.append(item.message)
                item.handed_over.set()
                if self.turn_ending and not self.outstanding:
                    await client.interrupt()
                    return
            elif isinstance(item, MirrorErrorMessage):
                self.mirror_failed = True
                logger.warning(
                    "Claude agent session transcript batch dropped: %s", item.error
                )
            elif isinstance(item, SdkSystemMessage):
                if item.subtype == "init":
                    self.session_id = item.data.get("session_id")
                    logger.info(
                        "Claude agent session %s on Claude Code %s, model %s, "
                        "credential %s",
                        item.data.get("session_id"),
                        item.data.get("claude_code_version"),
                        item.data.get("model"),
                        item.data.get("apiKeySource"),
                    )
                    _require_subscription(item.data, self.model.model)
            elif isinstance(item, RateLimitEvent):
                _record_rate_limit(item)
            elif isinstance(item, StreamEvent):
                await self._on_stream_event(item)
            elif isinstance(item, SdkAssistantMessage):
                await self._on_assistant(item)
            elif isinstance(item, SdkUserMessage):
                if _carries_tool_results(item):
                    await self._on_tool_results(client)
            elif isinstance(item, ResultMessage):
                self.result = item

    async def run(
        self, prompt: SessionPrompt, definitions: list[ToolDefinition]
    ) -> None:
        server = _mcp_server(definitions, self.call_tool)
        cwd = session_working_directory()
        options = self.loop.options_for(
            model=self.model,
            system_prompt=prompt.system_prompt,
            server=server,
            cwd=cwd,
            pre_tool_use=self.pre_tool_use,
            session_store=self.session_store,
            resume=self.resume,
        )
        try:
            await self._converse(options, prompt)
        except ClaudeSDKError as exc:
            # The SDK's own failures -- a CLI that will not start, a
            # connection lost, a process that exits -- are provider failures to
            # the rest of the application, which falls back on those.
            msg = f"Claude Code session failed: {exc}"
            raise LLMProviderError(msg, PROVIDER_NAME, self.model.model) from exc
        finally:
            # A resumed session runs in a config directory the SDK removes.
            if self.session_id is not None and self.resume is None:
                remove_local_transcript(self.session_id, cwd)
        if self.suspended:
            # What the interrupted response had begun is not a reply: the
            # session that resumes the turn holds it, and writes the reply.
            self.outcome = "suspended"
            await self._record_session(prompt.system_prompt)
            raise asyncio.CancelledError("Turn suspended for shutdown")
        await self._flush()
        await self._finish()
        await self._record_session(prompt.system_prompt)

    async def _converse(
        self, options: ClaudeAgentOptions, prompt: SessionPrompt
    ) -> None:
        async with self.loop.client_factory(options) as client:
            await client.query(_opening_message(prompt.content))
            pump = asyncio.create_task(self._pump(client))
            try:
                await self._drive(client)
            finally:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)

    async def _record_session(self, system_prompt: str) -> None:
        """Make the session resumable, if the store holds all of it."""
        if self.outcome not in {"completed", "suspended"} or self.session_id is None:
            return
        if self.session_store.write_failed or self.mirror_failed:
            logger.warning(
                "Claude agent session %s of turn %s is not resumable: part of "
                "its transcript did not reach the store.",
                self.session_id,
                self.turn.turn_id,
            )
            return
        # The turn's reply is already saved; failing to record the session
        # only means a later wake rebuilds instead of resuming.
        try:
            await record_session_turn(
                self.turn.db_context,
                self.scope,
                session_id=self.session_id,
                model=self.model,
                system_prompt=system_prompt,
                taint_metadata=self.turn.taint_tracker.snapshot().to_metadata(),
            )
        except Exception:
            logger.exception(
                "Failed to record Claude agent session %s of turn %s",
                self.session_id,
                self.turn.turn_id,
            )

    async def _finish(self) -> None:
        terminal_text = self.review_state.terminal_denial_escalation_message
        if terminal_text is not None:
            self.outcome = "review_terminated"
            await self._emit_closing(terminal_text)
            return
        if self.quiet_reason is not None:
            self.outcome = "ended_quietly"
            logger.info(
                "Turn %s ended quietly: %s", self.turn.turn_id, self.quiet_reason
            )
            await self._emit_closing(
                quiet_end_record(self.quiet_reason), ended_quietly=True
            )
            return
        result = self.result
        if result is None:
            msg = "Claude Agent SDK session ended without a result."
            raise LLMProviderError(msg, PROVIDER_NAME, self.model.model)
        if result.subtype == "error_max_turns":
            self.outcome = "max_turns"
            await self._emit_closing(
                f"I stopped after {self.loop.config.max_iterations} steps, this "
                "profile's limit, before finishing."
            )
            return
        if result.is_error:
            detail = "; ".join(result.errors or ()) or result.result or result.subtype
            status = result.api_error_status or 0
            raise _error_for(
                "server_error" if status >= 500 else "unknown",
                detail,
                self.model.model,
            )
        self.outcome = "completed"

    async def run_recorded(
        self, prompt: SessionPrompt, definitions: list[ToolDefinition]
    ) -> None:
        """Run the session, counting and logging how it ended."""
        profile = self.loop.config.id
        logger.info(
            "Claude agent session starting: profile=%s turn=%s model=%s effort=%s "
            "tools=%d",
            profile,
            self.turn.turn_id,
            self.model.model,
            self.model.effort,
            len(definitions),
        )
        try:
            await self.run(prompt, definitions)
        except BaseException as exc:
            if not self.suspended:
                self.outcome = _failure_outcome(exc)
            raise
        finally:
            record_claude_agent_session(profile, self.outcome)
            result = self.result
            logger.info(
                "Claude agent session ended: profile=%s turn=%s outcome=%s "
                "result=%s model_turns=%s seconds=%.1f api_seconds=%s",
                profile,
                self.turn.turn_id,
                self.outcome,
                result.subtype if result else None,
                result.num_turns if result else None,
                time.monotonic() - self.started_at,
                f"{result.duration_api_ms / 1000:.1f}" if result else None,
            )


def _carries_tool_results(message: SdkUserMessage) -> bool:
    content = message.content
    return not isinstance(content, str) and any(
        isinstance(block, ToolResultBlock) for block in content
    )


def _failure_outcome(exc: BaseException) -> str:
    if isinstance(exc, ClaudeAgentBillingError):
        return "billing_refused"
    if isinstance(exc, RateLimitError):
        return "rate_limited"
    if isinstance(exc, AuthenticationError):
        return "auth_failed"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return "error"


def _record_rate_limit(event: RateLimitEvent) -> None:
    info = event.rate_limit_info
    record_claude_subscription_rate_limit(
        rate_limit_type=info.rate_limit_type,
        status=info.status,
        utilization=info.utilization,
        resets_at=info.resets_at,
    )
    if info.status != "allowed":
        logger.warning(
            "Claude subscription %s window is %s (utilization %s, resets at %s)",
            info.rate_limit_type,
            info.status,
            info.utilization,
            info.resets_at,
        )


class ClaudeAgentLoop(LLMStreamingLoop):
    """Runs a turn in a Claude Agent SDK session."""

    def __init__(
        self,
        config: LLMStreamingLoopConfig,
        app_config: AppConfig,
        tool_executor: ToolExecutor,
        attachment_processor: AttachmentProcessor,
        *,
        agent_config: ClaudeAgentConfig,
        models: Mapping[str, ClaudeAgentModel],
        client_factory: ClientFactory = default_client_factory,
    ) -> None:
        super().__init__(config, app_config, tool_executor, attachment_processor)
        self.agent_config = agent_config
        self.models = dict(models)
        self.client_factory = client_factory

    def options_for(
        self,
        *,
        model: ClaudeAgentModel,
        system_prompt: str,
        server: Server,
        cwd: str,
        pre_tool_use: HookCallback,
        session_store: SessionStore,
        resume: str | None,
    ) -> ClaudeAgentOptions:
        """The session's options: our tools, and nothing of Claude Code's own."""
        return ClaudeAgentOptions(
            tools=[TOOL_SEARCH_TOOL_NAME],
            mcp_servers={
                MCP_SERVER_NAME: McpSdkServerConfig(
                    type="sdk", name=MCP_SERVER_NAME, instance=server
                )
            },
            strict_mcp_config=True,
            allowed_tools=[f"mcp__{MCP_SERVER_NAME}"],
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            # Rendered history is text other people wrote; without this an
            # "@/path" in it would make Claude Code read a local file.
            verbatim_prompts=True,
            cwd=cwd,
            system_prompt=system_prompt,
            model=model.model,
            effort=model.effort,
            max_turns=self.config.max_iterations,
            env=dict(_SESSION_ENV),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            include_partial_messages=True,
            cli_path=self.agent_config.cli_path,
            client_app="family-assistant",
            session_store=session_store,
            resume=resume,
            max_buffer_size=SESSION_MESSAGE_LIMIT_BYTES,
        )

    async def _run_stream(
        self,
        db_context: Database,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        llm_client: LLMInterface,
        model_selection: ResolvedModelSelection,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        processing_service: ProcessingService | None = None,
        plugins: ProfilePlugins | None = None,
        event_sources: EventSourcesById | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
        memory_review: MemoryReviewContext | None = None,
        allow_quiet_end: bool = False,
        completed_iterations: int = 0,
        context_length_compactor: ContextLengthCompactor | None = None,
        trailing_instruction: str | None = None,
    ) -> AsyncGenerator[_Output]:
        model = self.models.get(model_selection.tier or "")
        if model is None:
            msg = (
                f"Profile '{self.config.id}' runs on the Claude Agent SDK and has "
                f"no session model for tier {model_selection.tier!r}."
            )
            raise ValueError(msg)

        initial_taint_state = prompt_window_taint(messages)
        for source in initial_taint_sources or ():
            initial_taint_state = initial_taint_state.add_source(source)
        if taint_tracker is None:
            taint_tracker = InMemoryTurnTaintTracker(initial_taint_state)
        else:
            merge_taint_state_into_tracker(taint_tracker, initial_taint_state)

        can_confirm = request_confirmation_callback is not None
        tools_provider = self.tool_executor.tools_provider
        definitions = [
            definition
            for definition in await get_tool_definitions_for_advertisement(
                tools_provider, can_confirm=can_confirm
            )
            if definition.get("function", {}).get("name")
            not in RESERVED_META_TOOL_NAMES
        ]
        definitions = await without_unavailable_thread_tools(
            definitions,
            db_context=db_context,
            processing_service=processing_service,
            interface_type=interface_type,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            user_id=user_id,
        )
        if allow_quiet_end:
            definitions.append(END_TURN_QUIETLY_TOOL_DEFINITION)
        # A delegation wake's trigger is a system row, so its instruction is
        # what opens the session; the wake and the results it announces reach
        # the session as transcript, as they do on every later turn.
        prompt = build_session_prompt(
            [
                *messages,
                UserMessage(content=trailing_instruction, is_turn_scaffolding=True),
            ]
            if trailing_instruction
            else messages,
            system_prompt_additions=(
                await collect_system_prompt_addition(
                    tools_provider, can_confirm=can_confirm
                ),
                model.custom_instructions,
            ),
        )
        scope = WakeScope(
            interface_type=interface_type,
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            profile_id=self.config.id,
            turn_id=turn_id,
        )
        resumed_session_id: str | None = None
        suspended, suspended_decision = await suspended_session_to_resume(
            db_context, scope, model=model, system_prompt=prompt.system_prompt
        )
        if suspended_decision != "no_session":
            logger.info(
                "Claude agent turn %s on profile %s resumed after a shutdown: %s",
                turn_id,
                self.config.id,
                suspended_decision,
            )
        if suspended is not None:
            # A turn a graceful shutdown interrupted: its session holds the
            # turn so far, its own reasoning included, and carries on with it.
            resumed_session_id = suspended.session_id
            merge_taint_state_into_tracker(
                taint_tracker,
                TurnTaintState.from_metadata(
                    suspended.taint_metadata_json, preserve_origin=True
                ),
            )
            prompt = SessionPrompt(
                system_prompt=prompt.system_prompt, content=suspended_opening()
            )
        elif trailing_instruction:
            stored, decision = await session_to_resume(
                db_context, scope, model=model, system_prompt=prompt.system_prompt
            )
            record_claude_agent_wake(self.config.id, decision)
            logger.info(
                "Claude agent wake %s on profile %s: %s",
                turn_id,
                self.config.id,
                decision,
            )
            if stored is not None:
                resumed_session_id = stored.session_id
                # The session holds everything its earlier turns saw, some of
                # which may have left the history window since.
                merge_taint_state_into_tracker(
                    taint_tracker,
                    TurnTaintState.from_metadata(
                        stored.taint_metadata_json, preserve_origin=True
                    ),
                )
                added = await db_context.message_history.get_turn_rows_with_metadata(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    subconversation_id=subconversation_id,
                    turn_ids=[turn_id],
                )
                prompt = SessionPrompt(
                    system_prompt=prompt.system_prompt,
                    content=resumed_opening(
                        [row.message for row in added], trailing_instruction
                    ),
                )

        output: asyncio.Queue[_Handoff | _Done | _Failed] = asyncio.Queue()
        session = _TurnSession(
            self,
            _TurnContext(
                db_context=db_context,
                messages=messages,
                interface_type=interface_type,
                conversation_id=conversation_id,
                user_name=user_name,
                turn_id=turn_id,
                chat_interface=chat_interface,
                llm_client=llm_client,
                model_selection=model_selection,
                user_id=user_id,
                chat_interfaces=chat_interfaces,
                confirmation_ui_managers=confirmation_ui_managers,
                request_confirmation_callback=request_confirmation_callback,
                subconversation_id=subconversation_id,
                processing_service=processing_service,
                plugins=plugins,
                event_sources=event_sources,
                mid_turn_input_provider=mid_turn_input_provider,
                taint_tracker=taint_tracker,
                tool_call_review_trigger=tool_call_review_trigger,
                memory_review=memory_review,
                allow_quiet_end=allow_quiet_end,
            ),
            model,
            output,
            scope=scope,
            resume=resumed_session_id,
        )

        async def run_session() -> None:
            try:
                await session.run_recorded(prompt, definitions)
            except BaseException as exc:
                await output.put(_Failed(exc))
            else:
                await output.put(_Done())

        task = asyncio.create_task(run_session())
        try:
            while True:
                item = await output.get()
                if isinstance(item, _Done):
                    return
                if isinstance(item, _Failed):
                    raise item.error
                output_item, taken = item
                yield output_item
                taken.set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def build_claude_agent_loop(
    service: ProcessingService,
    *,
    agent_config: ClaudeAgentConfig,
    models: Mapping[str, ClaudeAgentModel],
    client_factory: ClientFactory | None = None,
) -> ClaudeAgentLoop:
    """The loop that runs *service*'s turns on the tiers in *models*.

    It shares the service's tool executor and attachment processor, so a turn
    it runs reaches the same tools under the same policy as one the ordinary
    loop runs.
    """
    return ClaudeAgentLoop(
        service.service_config,
        service.app_config,
        service.tool_executor,
        service.attachment_processor,
        agent_config=agent_config,
        models=models,
        client_factory=client_factory or default_client_factory,
    )


PROBE_TIMEOUT_SECONDS = 30.0
# What `claude auth status` reports for a subscription credential: a
# `claude setup-token` token, or an interactive claude.ai login.
_SUBSCRIPTION_AUTH_METHODS = frozenset({"oauth_token", "claude.ai"})


def claude_code_executable(agent_config: ClaudeAgentConfig) -> str | None:
    """The Claude Code binary a session will run, as the SDK finds it."""
    if agent_config.cli_path:
        return agent_config.cli_path
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("claude")


async def probe_claude_code(profile_id: str, agent_config: ClaudeAgentConfig) -> bool:
    """Check, without a model request, that a session could run on the subscription.

    Runs ``claude auth status``, which reads local state only: it shows the
    binary runs here and that the credential it would use is a subscription
    one. It cannot tell a revoked or mistyped token from a good one; that shows
    as ``auth_failed`` sessions. The result is logged and exported as
    ``family_assistant_claude_agent_ready``; it never stops startup, since a
    broken profile should not take the others down with it.
    """
    executable = claude_code_executable(agent_config)
    problem: str | None = None
    status: dict[str, object] = {}
    if executable is None:
        problem = "no Claude Code executable was found"
    else:
        # The API-key variables are removed rather than blanked: `auth status`
        # reports an empty API key as the credential in use, though a session
        # given the same environment uses the subscription.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in _BLANKED_CREDENTIAL_ENV
        }
        env.update((key, value) for key, value in _SESSION_ENV.items() if value)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "auth",
                "status",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), PROBE_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            problem = f"running {executable} failed: {exc!r}"
        else:
            problem, status = probe_verdict(process.returncode, stdout, stderr)
    ready = problem is None
    record_claude_agent_ready(profile_id, ready=ready)
    if ready:
        logger.info(
            "Profile '%s' can run Claude Code sessions on the subscription "
            "(executable %s, auth method %s).",
            profile_id,
            executable,
            status.get("authMethod"),
        )
    else:
        logger.error(
            "Profile '%s' cannot run Claude Code sessions on the subscription: %s. "
            "Its claude_code turns will fall back to the API until this is "
            "fixed; see CLAUDE_CODE_OAUTH_TOKEN in "
            "docs/operations/CONFIGURATION_REFERENCE.md.",
            profile_id,
            problem,
        )
    return ready


def probe_verdict(
    returncode: int | None, stdout: bytes, stderr: bytes
) -> tuple[str | None, dict[str, object]]:
    """Why `claude auth status` output rules out a subscription session, if it does."""
    if returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:500]
        return f"`claude auth status` exited {returncode}: {detail}", {}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return "`claude auth status` did not print JSON", {}
    if not isinstance(parsed, dict):
        return "`claude auth status` did not print a JSON object", {}
    status = cast("dict[str, object]", parsed)
    method = status.get("authMethod")
    if not status.get("loggedIn") or method not in _SUBSCRIPTION_AUTH_METHODS:
        return (
            f"Claude Code would authenticate with {method!r}, not a subscription token",
            status,
        )
    return None, status


def _require_subscription(init_data: Mapping[str, object], model: str) -> None:
    source = init_data.get("apiKeySource")
    if source != _SUBSCRIPTION_KEY_SOURCE:
        msg = (
            "The Claude Agent SDK session would be billed to an API key "
            f"(apiKeySource={source!r}), not the Claude subscription a "
            "claude_code tier runs on. Set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`; see "
            "docs/operations/CONFIGURATION_REFERENCE.md."
        )
        raise ClaudeAgentBillingError(msg, PROVIDER_NAME, model)


async def _opening_message(
    content: list[SessionContentBlock],
) -> AsyncIterator[dict[str, object]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _tool_outcome(
    call: ToolCallItem,
    text: str,
    tracker: TurnTaintTracker,
    *,
    error: str | None = None,
) -> _ToolOutcome:
    return _ToolOutcome(
        event=LLMStreamEvent(
            type="tool_result", tool_call_id=call.id, tool_result=text, error=error
        ),
        message=ToolMessage(
            tool_call_id=call.id,
            content=text,
            name=call.function.name,
            error_traceback=error,
            taint_metadata=tracker.snapshot().to_metadata(),
        ),
    )


def _mcp_text(text: str, *, is_error: bool = False) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=text)], isError=is_error
    )


def _mcp_result(message: ToolMessage) -> mcp_types.CallToolResult:
    """A tool's result as Claude Code receives it: its text and its images.

    Images that would take the result over ``MAX_TOOL_RESULT_BYTES`` are left
    out, with a note telling the model so. The text needs no cap: the executor
    has already moved a large text result into an attachment.
    """
    size = len(json.dumps(message.content, ensure_ascii=False).encode())
    images: list[mcp_types.ImageContent] = []
    omitted = 0
    for attachment in message.transient_attachments or ():
        if not attachment.mime_type.startswith("image/"):
            continue
        data = attachment.get_content_as_base64()
        if data is None:
            continue
        if size + len(data) > MAX_TOOL_RESULT_BYTES:
            omitted += 1
            continue
        size += len(data)
        images.append(
            mcp_types.ImageContent(
                type="image", data=data, mimeType=attachment.mime_type
            )
        )
    text = message.content
    if omitted:
        text += (
            f"\n\n[{omitted} image(s) not shown: together they exceed the"
            f" {MAX_TOOL_RESULT_BYTES / 2**20:.0f} MB a tool result can carry."
            " Fetch them in separate calls to see them.]"
        )
    content: list[mcp_types.TextContent | mcp_types.ImageContent] = [
        mcp_types.TextContent(type="text", text=text),
        *images,
    ]
    return mcp_types.CallToolResult(
        content=list(content), isError=message.error_traceback is not None
    )


def _mcp_server(
    definitions: Sequence[ToolDefinition],
    call_tool: Callable[[str, dict[str, object]], Awaitable[mcp_types.CallToolResult]],
) -> Server:
    """An in-process MCP server offering *definitions* and running *call_tool*.

    Built on the low-level server rather than the SDK's helper so arguments
    reach the executor as the model sent them: the executor parses and
    validates them itself, as it does for every other provider.
    """
    tools = [
        mcp_types.Tool(
            name=function["name"],
            description=function.get("description", ""),
            inputSchema=cast(
                "dict[str, Any]",
                function.get("parameters") or {"type": "object", "properties": {}},
            ),
        )
        for definition in definitions
        if (function := definition.get("function"))
    ]
    server: Server = Server(MCP_SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:  # pyright: ignore[reportUnusedFunction] - registered by the decorator
        return tools

    @server.call_tool(validate_input=False)
    async def handle_call(  # pyright: ignore[reportUnusedFunction] - registered by the decorator
        name: str, arguments: dict[str, object]
    ) -> mcp_types.CallToolResult:
        return await call_tool(name, arguments)

    return server
