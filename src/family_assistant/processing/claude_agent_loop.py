"""A turn run by the Claude Agent SDK, with this application's tools.

``ClaudeAgentLoop`` replaces ``LLMStreamingLoop`` for a profile with a
``claude_agent_config``. One turn is one Claude Code session: Claude Code runs
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
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import mcp.types as mcp_types
from claude_agent_sdk import (
    AssistantMessage as SdkAssistantMessage,
)
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk import (
    SystemMessage as SdkSystemMessage,
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
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageReasoningInfo,
    ToolMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    merge_taint_state_into_tracker,
    prompt_window_taint,
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
_BLANKED_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# What a session reports when no API key is in use: a subscription login.
_SUBSCRIPTION_KEY_SOURCE = "none"

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


def _reasoning_info(
    usage: Mapping[str, object] | None,
    *,
    model: str,
    message_id: str | None,
    model_selection: ResolvedModelSelection,
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
    if model_selection.tier is not None:
        info["model_tier"] = model_selection.tier
        info["model_tier_source"] = model_selection.source
    return info


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
        output: asyncio.Queue[_Output | _Done | _Failed],
    ) -> None:
        self.loop = loop
        self.turn = turn
        self.model = model
        self.output = output
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
        self.quiet_reason: str | None = None
        self.result: ResultMessage | None = None

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
            await self.inbox.put(_tool_outcome(call, text, tracker, error="turn_ended"))
            return _mcp_text(text, is_error=True)

        if self.turn.allow_quiet_end and name == END_TURN_QUIETLY_TOOL_NAME:
            self.quiet_reason = quiet_end_reason(call)
            self.turn_ending = True
            await self.inbox.put(_tool_outcome(call, QUIET_END_TOOL_RESULT, tracker))
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
        await self.inbox.put(
            _ToolOutcome(
                event=result.stream_event,
                message=result.llm_message,
                attachment_updates=result.apply_attachment_updates,
            )
        )
        return _mcp_result(result.llm_message)

    # -- output -------------------------------------------------------------

    async def _emit(self, event: LLMStreamEvent, message: LLMMessage | None) -> None:
        await self.output.put((event, message))

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
        text = "".join(current.text)
        if text.strip() or current.tool_calls:
            reasoning = None
            if current.message_id not in self.reported_usage:
                reasoning = _reasoning_info(
                    current.usage,
                    model=current.model,
                    message_id=current.message_id,
                    model_selection=self.turn.model_selection,
                )
                if current.message_id:
                    self.reported_usage.add(current.message_id)
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

    async def _on_stream_event(self, item: StreamEvent) -> None:
        if item.parent_tool_use_id is not None:
            return
        event = item.event
        event_type = event.get("type")
        if event_type == "content_block_delta":
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

    async def _drive(self, client: ClaudeSDKClient) -> None:
        """Consume the session until it ends, or until this turn has."""
        provider = self.turn.mid_turn_input_provider
        while True:
            if provider is not None and provider.should_interrupt():
                raise asyncio.CancelledError("Turn interrupted by user")
            item = await self.inbox.get()
            if isinstance(item, _Failed):
                raise item.error
            if isinstance(item, _Done):
                return
            if isinstance(item, _ToolOutcome):
                if item.attachment_updates is not None:
                    item.attachment_updates(self.pending_attachment_ids)
                self.outstanding.discard(item.message.tool_call_id)
                await self._emit(item.event, item.message)
                if self.turn_ending and not self.outstanding:
                    await client.interrupt()
                    return
            elif isinstance(item, SdkSystemMessage):
                if item.subtype == "init":
                    _require_subscription(item.data, self.model.model)
            elif isinstance(item, StreamEvent):
                await self._on_stream_event(item)
            elif isinstance(item, SdkAssistantMessage):
                await self._on_assistant(item)
            elif isinstance(item, ResultMessage):
                self.result = item

    async def run(
        self, prompt: SessionPrompt, definitions: list[ToolDefinition]
    ) -> None:
        server = _mcp_server(definitions, self.call_tool)
        with tempfile.TemporaryDirectory(prefix="fa-claude-agent-") as cwd:
            options = self.loop.options_for(
                model=self.model,
                system_prompt=prompt.system_prompt,
                server=server,
                cwd=cwd,
                pre_tool_use=self.pre_tool_use,
            )
            async with self.loop.client_factory(options) as client:
                await client.query(_opening_message(prompt.content))
                pump = asyncio.create_task(self._pump(client))
                try:
                    await self._drive(client)
                finally:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
        await self._flush()
        await self._finish()

    async def _finish(self) -> None:
        terminal_text = self.review_state.terminal_denial_escalation_message
        if terminal_text is not None:
            await self._emit_closing(terminal_text)
            return
        if self.quiet_reason is not None:
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
            env=dict.fromkeys(_BLANKED_CREDENTIAL_ENV, ""),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            include_partial_messages=True,
            cli_path=self.agent_config.cli_path,
            client_app="family-assistant",
            extra_args={"no-session-persistence": None},
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
        prompt = build_session_prompt(
            messages,
            system_prompt_additions=(
                await collect_system_prompt_addition(
                    tools_provider, can_confirm=can_confirm
                ),
                model.custom_instructions,
            ),
        )
        if trailing_instruction:
            prompt.content.append({"type": "text", "text": trailing_instruction})

        output: asyncio.Queue[_Output | _Done | _Failed] = asyncio.Queue()
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
        )

        async def run_session() -> None:
            try:
                await session.run(prompt, definitions)
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
                yield item
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def run_on_claude_agent(
    service: ProcessingService,
    *,
    agent_config: ClaudeAgentConfig,
    models: Mapping[str, ClaudeAgentModel],
    client_factory: ClientFactory | None = None,
) -> None:
    """Make *service* run its turns in Claude Agent SDK sessions.

    Everything else about the service -- context, history, persistence, its
    tools -- is unchanged: only the loop that runs a turn is replaced.
    """
    service.llm_loop = ClaudeAgentLoop(
        service.service_config,
        service.app_config,
        service.tool_executor,
        service.attachment_processor,
        agent_config=agent_config,
        models=models,
        client_factory=client_factory or default_client_factory,
    )


def _require_subscription(init_data: Mapping[str, object], model: str) -> None:
    source = init_data.get("apiKeySource")
    if source != _SUBSCRIPTION_KEY_SOURCE:
        msg = (
            "The Claude Agent SDK session would be billed to an API key "
            f"(apiKeySource={source!r}), not the Claude subscription this profile "
            "is for. Set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`; see "
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
    """A tool's result as Claude Code receives it: its text and its images."""
    content: list[mcp_types.TextContent | mcp_types.ImageContent] = [
        mcp_types.TextContent(type="text", text=message.content)
    ]
    for attachment in message.transient_attachments or ():
        data = attachment.get_content_as_base64()
        if data is not None and attachment.mime_type.startswith("image/"):
            content.append(
                mcp_types.ImageContent(
                    type="image", data=data, mimeType=attachment.mime_type
                )
            )
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
