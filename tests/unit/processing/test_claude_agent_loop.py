"""ClaudeAgentLoop driven through the Agent SDK's own Query against a fake CLI."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    project_key_for_directory,
)
from prometheus_client import REGISTRY
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ClaudeAgentConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.model_selection import (
    ModelTierEligibility,
    ModelTierOption,
    ResolvedModelSelection,
)
from family_assistant.llm.model_tiers import ClaudeAgentModel
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
    claude_agent_loop,
)
from family_assistant.processing.claude_agent_loop import (
    build_claude_agent_loop,
    mcp_tool_name,
    probe_verdict,
)
from family_assistant.processing.claude_agent_prompt import (
    SUSPENDED_RESUME_INSTRUCTION,
)
from family_assistant.processing.claude_agent_sessions import (
    database_session_store,
    session_working_directory,
)
from family_assistant.processing.quiet_turn import END_TURN_QUIETLY_TOOL_NAME
from family_assistant.processing.types import ChatInteractionResult, MidTurnUserInput
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.claude_agent_sessions import (
    claude_agent_session_turns_table,
)
from family_assistant.storage.database import Database
from family_assistant.storage.delegation_runs import DELEGATION_CONTINUATION_TRIGGER
from family_assistant.storage.repositories.claude_agent_sessions import (
    ClaudeAgentSessionsRepository,
)
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.fake_claude_code import (
    SESSION_ID,
    FakeClaudeCode,
    FakeResponse,
    FakeText,
    FakeToolUse,
    ReplayClaudeCode,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient
from tests.mocks.mock_tools_provider import MockToolsProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.processing.types import ToolExecutionResult

TIER = "claude_opus_5_5"


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions write and resolve their transcripts here, not under ~/.claude."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))


# The taint each get_weather call ran under, most recent last.
SEEN_TAINT: list[TurnTaintState] = []


async def get_weather(city: str, exec_context: ToolExecutionContext) -> str:
    if exec_context.taint_tracker is not None:
        SEEN_TAINT.append(exec_context.taint_tracker.snapshot())
    return f"Weather in {city}: 21C, sunny"


API_TIER = "deep"
API_REPLY = "Answered on the API."


def _service(
    fake: FakeClaudeCode | ReplayClaudeCode,
    *,
    max_iterations: int = 10,
    custom_instructions: str | None = None,
    options_seen: list[ClaudeAgentOptions] | None = None,
    default_tier: str = TIER,
    attachment_registry: AttachmentRegistry | None = None,
    extra_tools: Mapping[str, Callable[..., Awaitable[str]]] | None = None,
) -> ProcessingService:
    tools = MockToolsProvider()
    tools.add_tool("get_weather", get_weather)
    for name, tool in (extra_tools or {}).items():
        tools.add_tool(name, tool)
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a household assistant."},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="claude_agent",
        max_iterations=max_iterations,
        tier_eligibility=ModelTierEligibility(
            default_tier=default_tier,
            selectable=(
                ModelTierOption(id=TIER, label="Opus"),
                ModelTierOption(id=API_TIER, label="Deep"),
            ),
        ),
    )
    # The tier's API client: auxiliary calls, and the turn itself when it does
    # not run in a session.
    client = _api_client()
    service = ProcessingService(
        llm_client=client,
        tools_provider=tools,
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        tier_llm_clients={TIER: client, API_TIER: client},
        attachment_registry=attachment_registry,
    )

    def client_factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        if options_seen is not None:
            options_seen.append(options)
        if isinstance(fake, FakeClaudeCode):
            fake.transcript_path = str(
                Path(os.environ["CLAUDE_CONFIG_DIR"])
                / "projects"
                / project_key_for_directory(options.cwd)
                / f"{SESSION_ID}.jsonl"
            )
        return ClaudeSDKClient(options, transport=fake)

    service.claude_agent_loop = build_claude_agent_loop(
        service,
        agent_config=ClaudeAgentConfig(),
        models={
            TIER: ClaudeAgentModel(
                model="claude-opus-5-5",
                effort="high",
                custom_instructions=custom_instructions,
            )
        },
        client_factory=client_factory,
    )
    return service


def _api_client() -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content=API_REPLY)
    )


async def _turn(service: ProcessingService, db_engine: AsyncEngine, text: str) -> str:
    return (await _interact(service, db_engine, text)).text_reply or ""


async def _interact(
    service: ProcessingService,
    db_engine: AsyncEngine,
    text: str,
    *,
    allow_quiet_end: bool = False,
) -> ChatInteractionResult:
    return await service.handle_chat_interaction(
        allow_quiet_end=allow_quiet_end,
        db_context=Database(db_engine),
        interface_type="test",
        conversation_id="conv-1",
        trigger_content_parts=[{"type": "text", "text": text}],
        trigger_interface_message_id="msg-1",
        user_name="TestUser",
    )


async def _saved(db_engine: AsyncEngine) -> list[object]:
    return list(
        await Database(db_engine).message_history.get_recent(
            interface_type="test", conversation_id="conv-1"
        )
    )


def _weather_call() -> FakeToolUse:
    return FakeToolUse(name=mcp_tool_name("get_weather"), input={"city": "Paris"})


@pytest.mark.asyncio
async def test_tool_call_runs_through_the_executor_and_back(
    db_engine: AsyncEngine,
) -> None:
    call = _weather_call()
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([call]),
            FakeResponse([FakeText("It's 21C and sunny in Paris.")]),
        ]
    )
    reply = await _turn(_service(fake), db_engine, "Weather in Paris?")

    assert reply == "It's 21C and sunny in Paris."
    assert fake.tool_results[call.id]["content"] == [
        {"type": "text", "text": "Weather in Paris: 21C, sunny"}
    ]


@pytest.mark.asyncio
async def test_saved_history_is_valid_tool_call_protocol(
    db_engine: AsyncEngine,
) -> None:
    call = _weather_call()
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([FakeText("Checking."), call]),
            FakeResponse([FakeText("Sunny.")]),
        ]
    )
    await _turn(_service(fake), db_engine, "Weather in Paris?")

    saved = await _saved(db_engine)
    roles = [
        (type(m).__name__, getattr(m, "tool_call_id", None))
        for m in saved
        if isinstance(m, (AssistantMessage, ToolMessage))
    ]
    assert roles == [
        ("AssistantMessage", None),
        ("ToolMessage", call.id),
        ("AssistantMessage", None),
    ]
    first = next(m for m in saved if isinstance(m, AssistantMessage))
    assert first.content == "Checking."
    assert [(c.id, c.function.name) for c in first.tool_calls or []] == [
        (call.id, "get_weather")
    ]


@pytest.mark.asyncio
async def test_session_is_offered_the_profiles_tools(db_engine: AsyncEngine) -> None:
    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("Hi.")])])
    await _turn(_service(fake), db_engine, "Hello")

    assert [tool["name"] for tool in fake.offered_tools] == ["get_weather"]


@pytest.mark.asyncio
async def test_opening_message_carries_the_request(db_engine: AsyncEngine) -> None:
    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("Hi.")])])
    await _turn(_service(fake), db_engine, "Hello there")

    texts = [block["text"] for block in fake.prompts[0]["content"]]
    assert any("Hello there" in text for text in texts)


async def _save_interrupted_run(db: Database) -> None:
    """A turn's request and one completed tool call, as a redeploy leaves them."""
    empty = TurnTaintState.empty().to_metadata()
    saved: list[LLMMessage] = [
        UserMessage(
            content="Weather in Paris?",
            taint_metadata=empty,
            authorship_taint_metadata=empty,
        ),
        AssistantMessage(
            tool_calls=[
                ToolCallItem(
                    id="call-1",
                    type="function",
                    function=ToolCallFunction(
                        name="get_weather", arguments={"city": "Paris"}
                    ),
                )
            ],
            taint_metadata=empty,
        ),
        ToolMessage(
            tool_call_id="call-1",
            name="get_weather",
            content="Weather in Paris: 21C, sunny",
            taint_metadata=empty,
        ),
    ]
    for message in saved:
        await db.message_history.add_message(
            message,
            interface_type="test",
            conversation_id="conv-1",
            turn_id="turn-1",
            timestamp=datetime.now(UTC),
            processing_profile_id="claude_agent",
        )


async def _resume(
    service: ProcessingService,
    db: Database,
    trigger_content_parts: list[ContentPartDict],
) -> ChatInteractionResult:
    return await service.handle_chat_interaction(
        db_context=db,
        interface_type="test",
        conversation_id="conv-1",
        trigger_content_parts=trigger_content_parts,
        trigger_interface_message_id="msg-1",
        user_name="TestUser",
        turn_id="turn-1",
        resume=True,
    )


@pytest.mark.asyncio
async def test_resumed_turn_continues_after_its_saved_tool_calls(
    db_engine: AsyncEngine,
) -> None:
    """A turn interrupted mid-run (a redeploy) resumes from the rows it saved."""
    db = Database(db_engine)
    await _save_interrupted_run(db)
    fake = _reply("It's sunny.")

    reply = await _resume(
        _service(fake), db, [{"type": "text", "text": "Weather in Paris?"}]
    )

    texts = _opening_texts(fake)
    assert reply.text_reply == "It's sunny."
    assert "Weather in Paris?" in texts[0]
    assert "Weather in Paris: 21C, sunny" in texts[-1]
    assert "do not repeat a call" in texts[-1]


@pytest.mark.asyncio
async def test_resumed_turn_keeps_its_attachment_with_the_request(
    db_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A rebuilt attachment injection opens the session with the request."""
    db = Database(db_engine)
    registry = AttachmentRegistry(storage_path=str(tmp_path), db_engine=db_engine)
    attachment = await registry.store_and_register_tool_attachment(
        file_content=b"forecast data",
        filename="forecast.txt",
        content_type="text/plain",
        tool_name="test",
        description="Paris forecast",
        conversation_id="conv-1",
        taint_state=TurnTaintState.empty(),
    )
    await _save_interrupted_run(db)
    fake = _reply("It's sunny.")

    await _resume(
        _service(fake, attachment_registry=registry),
        db,
        [
            {"type": "text", "text": "Weather in Paris?"},
            {"type": "attachment", "attachment_id": attachment.attachment_id},
        ],
    )

    texts = _opening_texts(fake)
    assert any("Paris forecast" in text for text in texts[:-1])
    assert "Weather in Paris: 21C, sunny" in texts[-1]
    assert "do not repeat a call" in texts[-1]


class _Shutdown:
    """A turn's stop handle, which a graceful shutdown asks to suspend."""

    def __init__(self) -> None:
        self.suspending = False

    async def drain_pending_mid_turn_inputs(self) -> list[MidTurnUserInput]:
        return []

    def should_interrupt(self) -> bool:
        return self.suspending

    @property
    def suspend_requested(self) -> bool:
        return self.suspending


type _StatusTool = Callable[..., Awaitable[str]]


def _shutdown_during_call(shutdown: _Shutdown) -> _StatusTool:
    async def check_status(exec_context: ToolExecutionContext) -> str:
        shutdown.suspending = True
        return "All systems normal."

    return check_status


def _still_thinking() -> FakeResponse:
    """The model's next response, which a shutdown interrupts before it ends."""
    return FakeResponse([FakeText("Done.")], ready=asyncio.Event())


async def _run_until_suspended(
    db_engine: AsyncEngine,
    fake: FakeClaudeCode,
    tool: Callable[[_Shutdown], _StatusTool],
) -> _Shutdown:
    shutdown = _Shutdown()
    with pytest.raises(asyncio.CancelledError):
        await _service(
            fake, extra_tools={"check_status": tool(shutdown)}
        ).handle_chat_interaction(
            db_context=Database(db_engine),
            interface_type="test",
            conversation_id="conv-1",
            trigger_content_parts=[{"type": "text", "text": "Check the status."}],
            trigger_interface_message_id="msg-1",
            user_name="TestUser",
            turn_id="turn-1",
            mid_turn_input_provider=shutdown,
        )
    return shutdown


def _status_call() -> FakeResponse:
    return FakeResponse([FakeToolUse(name=mcp_tool_name("check_status"), input={})])


@pytest.mark.asyncio
async def test_shutdown_interrupts_the_session_once_its_tools_have_returned(
    db_engine: AsyncEngine,
) -> None:
    fake = FakeClaudeCode(responses=[_status_call(), _still_thinking()])

    await _run_until_suspended(db_engine, fake, _shutdown_during_call)

    saved = await _saved(db_engine)
    session = await Database(db_engine).claude_agent_sessions.take_turn("turn-1")
    assert fake.interrupted
    assert any(
        isinstance(row, ToolMessage) and row.content == "All systems normal."
        for row in saved
    )
    assert not any(
        isinstance(row, AssistantMessage) and row.content == "Done." for row in saved
    )
    assert session is not None
    assert session.session_id == SESSION_ID


@pytest.mark.asyncio
async def test_a_turn_resumed_after_shutdown_continues_its_session(
    db_engine: AsyncEngine,
) -> None:
    await _run_until_suspended(
        db_engine,
        FakeClaudeCode(responses=[_status_call(), _still_thinking()]),
        _shutdown_during_call,
    )
    options: list[ClaudeAgentOptions] = []
    fake = _reply("Everything is fine.")

    reply = await _resume(
        _service(fake, options_seen=options),
        Database(db_engine),
        [{"type": "text", "text": "Check the status."}],
    )

    assert options[0].resume == SESSION_ID
    assert _opening_texts(fake) == [SUSPENDED_RESUME_INSTRUCTION]
    assert reply.text_reply == "Everything is fine."


@pytest.mark.asyncio
async def test_a_resumed_attempt_uses_up_its_checkpoint(
    db_engine: AsyncEngine,
) -> None:
    """An attempt that is lost leaves the next to rebuild, not to go back."""
    await _run_until_suspended(
        db_engine,
        FakeClaudeCode(responses=[_status_call(), _still_thinking()]),
        _shutdown_during_call,
    )

    await _resume(
        _service(FakeClaudeCode(responses=[], api_key_source="ANTHROPIC_API_KEY")),
        Database(db_engine),
        [{"type": "text", "text": "Check the status."}],
    )

    assert await Database(db_engine).claude_agent_sessions.take_turn("turn-1") is None


@pytest.mark.asyncio
async def test_shutdown_interrupts_a_tool_still_running_near_the_end_of_the_grace(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_agent_loop, "SUSPEND_INTERRUPT_AFTER_SECONDS", 0.0)
    monkeypatch.setattr(claude_agent_loop, "_STOP_POLL_SECONDS", 0.01)

    def hang_during_call(shutdown: _Shutdown) -> _StatusTool:
        async def check_status(exec_context: ToolExecutionContext) -> str:
            shutdown.suspending = True
            await asyncio.Event().wait()
            return "unreachable"

        return check_status

    fake = FakeClaudeCode(responses=[_status_call()])

    await _run_until_suspended(db_engine, fake, hang_during_call)

    session = await Database(db_engine).claude_agent_sessions.take_turn("turn-1")
    assert fake.interrupted
    assert session is not None


@pytest.mark.asyncio
async def _add_row(
    db_engine: AsyncEngine, content: str, *, turn_id: str, is_internal: bool = False
) -> None:
    await Database(db_engine).message_history.add_message(
        UserMessage(
            content=content,
            taint_metadata=TurnTaintState.empty().to_metadata(),
            authorship_taint_metadata=TurnTaintState.empty().to_metadata(),
        ),
        interface_type="test",
        conversation_id="conv-1",
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        processing_profile_id="claude_agent",
        is_internal=is_internal,
    )


async def _wake(
    service: ProcessingService, db_engine: AsyncEngine, child_result: str
) -> ChatInteractionResult:
    """A continuation wake: a child's result, then the system trigger."""
    await _add_row(db_engine, child_result, turn_id="wake-turn", is_internal=True)
    return await service.handle_chat_interaction(
        db_context=Database(db_engine),
        interface_type="test",
        conversation_id="conv-1",
        trigger_content_parts=[
            {
                "type": "text",
                "text": (
                    f"{DELEGATION_CONTINUATION_TRIGGER}\n\n"
                    "Their results are provided as lower-priority data in the "
                    "message history for this turn. Continue the task."
                ),
            }
        ],
        trigger_interface_message_id=None,
        user_name="TestUser",
        trigger_is_internal=True,
        trigger_role="system",
        turn_id="wake-turn",
    )


def _opening_texts(fake: FakeClaudeCode) -> list[str]:
    return [block["text"] for block in fake.prompts[0]["content"]]


def _reply(text: str) -> FakeClaudeCode:
    return FakeClaudeCode(responses=[FakeResponse([FakeText(text)])])


@pytest.mark.asyncio
async def test_delegation_wake_opens_with_its_instruction(
    db_engine: AsyncEngine,
) -> None:
    """A wake's trigger is a system row; its results arrive as history before it."""
    fake = _reply("It was disabled.")

    reply = await _wake(
        _service(fake), db_engine, "Engineer result: the cron job was disabled."
    )

    texts = _opening_texts(fake)
    assert reply.text_reply == "It was disabled."
    assert "the cron job was disabled" in texts[0]
    assert "Their results are provided as lower-priority data" in texts[-1]


@pytest.mark.asyncio
async def test_a_completed_turn_is_stored_for_resuming(db_engine: AsyncEngine) -> None:
    fake = _reply("Looking into it.")
    await _turn(_service(fake), db_engine, "Why is the cron job not running?")

    stored = await database_session_store(Database(db_engine)).load({
        "project_key": project_key_for_directory(session_working_directory()),
        "session_id": SESSION_ID,
    })
    session = await Database(db_engine).claude_agent_sessions.latest_turn(
        interface_type="test",
        conversation_id="conv-1",
        subconversation_id=None,
        processing_profile_id="claude_agent",
    )
    assert stored == fake.transcript
    assert session is not None
    assert session.session_id == SESSION_ID


@pytest.mark.asyncio
async def test_a_turn_whose_transcript_was_not_stored_is_not_resumable(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("database went away")

    monkeypatch.setattr(ClaudeAgentSessionsRepository, "append_entries", fail)

    reply = await _turn(_service(_reply("Looking into it.")), db_engine, "Why?")

    session = await Database(db_engine).claude_agent_sessions.latest_turn(
        interface_type="test",
        conversation_id="conv-1",
        subconversation_id=None,
        processing_profile_id="claude_agent",
    )
    assert reply == "Looking into it."
    assert session is None


@pytest.mark.asyncio
async def test_a_wake_resumes_the_session_that_delegated(
    db_engine: AsyncEngine,
) -> None:
    await _turn(_service(_reply("Delegating.")), db_engine, "Check the cron job.")
    options: list[ClaudeAgentOptions] = []
    fake = _reply("It was disabled.")

    await _wake(
        _service(fake, options_seen=options),
        db_engine,
        "Engineer result: the cron job was disabled.",
    )

    texts = _opening_texts(fake)
    assert options[0].resume == SESSION_ID
    assert not any("<conversation_history>" in text for text in texts)
    assert "the cron job was disabled" in texts[0]
    assert "Their results are provided as lower-priority data" in texts[-1]


@pytest.mark.asyncio
async def test_a_wake_after_the_conversation_moved_on_rebuilds(
    db_engine: AsyncEngine,
) -> None:
    await _turn(_service(_reply("Delegating.")), db_engine, "Check the cron job.")
    await _add_row(db_engine, "Also, what's for dinner?", turn_id="later-turn")
    options: list[ClaudeAgentOptions] = []
    fake = _reply("It was disabled.")

    await _wake(_service(fake, options_seen=options), db_engine, "Engineer result.")

    assert options[0].resume is None
    assert "<conversation_history>" in _opening_texts(fake)[0]


@pytest.mark.asyncio
async def test_a_wake_on_a_changed_system_prompt_rebuilds(
    db_engine: AsyncEngine,
) -> None:
    await _turn(_service(_reply("Delegating.")), db_engine, "Check the cron job.")
    options: list[ClaudeAgentOptions] = []

    await _wake(
        _service(
            _reply("It was disabled."),
            custom_instructions="Answer in one line.",
            options_seen=options,
        ),
        db_engine,
        "Engineer result.",
    )

    assert options[0].resume is None


@pytest.mark.asyncio
async def test_a_resumed_wake_keeps_the_taint_its_session_saw(
    db_engine: AsyncEngine,
) -> None:
    """The session may hold content that has since left the history window."""
    await _turn(_service(_reply("Delegating.")), db_engine, "Check my email.")
    email = TaintSource(
        source_type=TaintSourceType.EMAIL,
        source_id="email-1",
        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        labels=frozenset({"source_unknown_external"}),
        reason="an email the session read",
    )
    await Database(db_engine).execute(
        update(claude_agent_session_turns_table).values(
            taint_metadata_json=TurnTaintState.empty().add_source(email).to_metadata()
        )
    )
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([_weather_call()]),
            FakeResponse([FakeText("Done.")]),
        ]
    )

    await _wake(_service(fake), db_engine, "Engineer result.")

    assert "email-1" in [source.source_id for source in SEEN_TAINT[-1].sources]


@pytest.mark.asyncio
async def test_session_billed_to_an_api_key_runs_the_turn_on_the_api(
    db_engine: AsyncEngine,
) -> None:
    fake = FakeClaudeCode(
        responses=[FakeResponse([FakeText("Hi.")])],
        api_key_source="ANTHROPIC_API_KEY",
    )
    reply = await _turn(_service(fake), db_engine, "Hello")

    assert reply == API_REPLY


class _UnstartableClaudeCode(FakeClaudeCode):
    async def connect(self) -> NoReturn:
        raise CLIConnectionError("Failed to start Claude Code")


@pytest.mark.asyncio
async def test_cli_that_will_not_start_runs_the_turn_on_the_api(
    db_engine: AsyncEngine,
) -> None:
    fake = _UnstartableClaudeCode(responses=[FakeResponse([FakeText("Hi.")])])
    reply = await _turn(_service(fake), db_engine, "Hello")

    assert reply == API_REPLY


@pytest.mark.asyncio
async def test_session_failing_after_a_tool_call_fails_the_turn(
    db_engine: AsyncEngine,
) -> None:
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([_weather_call()]),
            FakeResponse([FakeText("Usage limit reached.")], error="rate_limit"),
        ]
    )
    result = await _interact(_service(fake), db_engine, "Weather?")

    assert "RateLimitError" in (result.error_traceback or "")


@pytest.mark.asyncio
async def test_turn_on_a_tier_without_a_session_model_runs_on_the_api(
    db_engine: AsyncEngine,
) -> None:
    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("From the session.")])])
    reply = await _turn(_service(fake, default_tier=API_TIER), db_engine, "Hello")

    assert reply == API_REPLY


@pytest.mark.asyncio
async def test_step_limit_is_reported_to_the_user(db_engine: AsyncEngine) -> None:
    fake = FakeClaudeCode(
        responses=[FakeResponse([_weather_call()])],
        result_subtype="error_max_turns",
    )
    reply = await _turn(_service(fake, max_iterations=1), db_engine, "Weather?")

    assert "1 steps" in reply


@pytest.mark.asyncio
async def test_quiet_end_closes_the_turn_without_a_reply(
    db_engine: AsyncEngine,
) -> None:
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([
                FakeToolUse(
                    name=mcp_tool_name(END_TURN_QUIETLY_TOOL_NAME),
                    input={"reason": "nothing to report"},
                )
            ]),
            FakeResponse([FakeText("This should never be reached.")]),
        ]
    )
    result = await _interact(
        _service(fake), db_engine, "Anything new?", allow_quiet_end=True
    )

    assert (result.ended_quietly, fake.interrupted) == (True, True)


@pytest.mark.asyncio
async def test_recorded_session_replays_into_history(db_engine: AsyncEngine) -> None:
    """A real Claude Code session's frames, including its ToolSearch step."""
    await _turn(_service(ReplayClaudeCode()), db_engine, "Weather in Paris?")

    calls = [
        (call.function.name, call.function.arguments)
        for message in await _saved(db_engine)
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls or ()
    ]
    assert calls == [("get_weather", {"city": "Paris"})]


@pytest.mark.asyncio
async def test_executor_failure_fails_the_turn(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([_weather_call()]),
            FakeResponse([FakeText("This should never be reached.")]),
        ]
    )
    service = _service(fake)

    async def failing_execute(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("attachment storage is full")

    monkeypatch.setattr(service.tool_executor, "execute", failing_execute)
    result = await _interact(service, db_engine, "Weather in Paris?")

    assert "attachment storage is full" in (result.error_traceback or "")


@pytest.mark.asyncio
async def test_review_of_a_later_call_sees_the_turn_so_far(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = (
        _weather_call(),
        FakeToolUse(name=mcp_tool_name("get_weather"), input={"city": "Rome"}),
    )
    fake = FakeClaudeCode(
        responses=[
            FakeResponse([first]),
            FakeResponse([second]),
            FakeResponse([FakeText("Both sunny.")]),
        ]
    )
    service = _service(fake)
    execute = service.tool_executor.execute
    reviewed: list[Sequence[LLMMessage]] = []

    async def recording_execute(
        call: ToolCallItem,
        **kwargs: Any,  # noqa: ANN401 - forwards the executor's keyword arguments unchanged
    ) -> ToolExecutionResult:
        reviewed.append(kwargs["tool_call_review_messages"])
        return await execute(call, **kwargs)

    monkeypatch.setattr(service.tool_executor, "execute", recording_execute)
    await _interact(service, db_engine, "Weather in Paris and Rome?")

    assert [
        message.tool_call_id
        for message in reviewed[1]
        if isinstance(message, ToolMessage)
    ] == [first.id]


@pytest.mark.asyncio
async def test_no_tool_runs_before_its_call_is_saved(db_engine: AsyncEngine) -> None:
    """The caller saves each message before asking for the next.

    Held on the assistant message that makes a call, the session must not
    run the call: a crash then would leave a side effect with no row for it.
    """
    executed = asyncio.Event()

    async def record(city: str, exec_context: ToolExecutionContext) -> str:
        executed.set()
        return f"Weather in {city}: 21C, sunny"

    fake = FakeClaudeCode(
        responses=[
            FakeResponse([_weather_call()]),
            FakeResponse([FakeText("Sunny.")]),
        ]
    )
    service = _service(fake)
    tools = service.tool_executor.tools_provider
    assert isinstance(tools, MockToolsProvider)
    tools.tools["get_weather"] = record
    stream = service.process_message_stream(
        db_context=Database(db_engine),
        messages=[
            SystemMessage(content="You are a household assistant."),
            UserMessage(content="Weather in Paris?"),
        ],
        interface_type="test",
        conversation_id="conv-1",
        user_name="TestUser",
        turn_id="turn-1",
        chat_interface=None,
        llm_client=service.llm_client,
        model_selection=ResolvedModelSelection.unselected(TIER),
    )
    async for _event, message in stream:
        if isinstance(message, AssistantMessage) and message.tool_calls:
            break
    held_executed = await _set_within(executed, seconds=1.0)
    async for _ in stream:
        pass

    assert (held_executed, executed.is_set()) == (False, True)


async def _set_within(event: asyncio.Event, *, seconds: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


SESSIONS = "family_assistant_claude_agent_sessions_total"


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.asyncio
async def test_completed_session_is_counted(db_engine: AsyncEngine) -> None:
    labels = {"profile": "claude_agent", "outcome": "completed"}
    before = _sample(SESSIONS, **labels)
    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("Hi.")])])
    await _turn(_service(fake), db_engine, "Hello")

    assert _sample(SESSIONS, **labels) == before + 1


@pytest.mark.asyncio
async def test_refused_session_is_counted_as_billing_refused(
    db_engine: AsyncEngine,
) -> None:
    labels = {"profile": "claude_agent", "outcome": "billing_refused"}
    before = _sample(SESSIONS, **labels)
    fake = FakeClaudeCode(
        responses=[FakeResponse([FakeText("Hi.")])],
        api_key_source="ANTHROPIC_API_KEY",
    )
    await _interact(_service(fake), db_engine, "Hello")

    assert _sample(SESSIONS, **labels) == before + 1


@pytest.mark.asyncio
async def test_model_calls_reach_the_llm_call_metrics(db_engine: AsyncEngine) -> None:
    labels = {
        "profile": "claude_agent",
        "tier": TIER,
        "provider": "claude_agent_sdk",
        "model": "claude-opus-5-5",
        "resolved_model": "claude-opus-5-5",
        "operation": "agent_session",
        "service_tier": "standard",
        "outcome": "success",
        "error_type": "",
    }
    before = _sample("family_assistant_llm_calls_total", **labels)
    fake = FakeClaudeCode(
        responses=[FakeResponse([_weather_call()]), FakeResponse([FakeText("Sunny.")])]
    )
    await _turn(_service(fake), db_engine, "Weather in Paris?")

    assert _sample("family_assistant_llm_calls_total", **labels) == before + 2


@pytest.mark.asyncio
async def test_subscription_rate_limit_state_is_exported(
    db_engine: AsyncEngine,
) -> None:
    fake = FakeClaudeCode(
        responses=[FakeResponse([FakeText("Hi.")])],
        rate_limit_info={
            "status": "allowed_warning",
            "rateLimitType": "seven_day",
            "utilization": 0.91,
            "resetsAt": 1794730800,
        },
    )
    await _turn(_service(fake), db_engine, "Hello")

    window = {"rate_limit_type": "seven_day"}
    assert (
        _sample("family_assistant_claude_subscription_rate_limit_status", **window),
        _sample("family_assistant_claude_subscription_utilization", **window),
    ) == (1.0, 0.91)


@pytest.mark.parametrize(
    ("returncode", "stdout", "ready"),
    [
        (0, b'{"loggedIn": true, "authMethod": "oauth_token"}', True),
        (0, b'{"loggedIn": true, "authMethod": "api_key"}', False),
        (0, b'{"loggedIn": false}', False),
        (1, b"", False),
        (0, b"not json", False),
    ],
)
def test_probe_accepts_only_a_subscription_credential(
    returncode: int, stdout: bytes, ready: bool
) -> None:
    problem, _ = probe_verdict(returncode, stdout, b"")

    assert (problem is None) == ready


@pytest.mark.asyncio
async def test_a_call_that_only_searches_tools_is_counted(
    db_engine: AsyncEngine,
) -> None:
    labels = {
        "profile": "claude_agent",
        "tier": TIER,
        "provider": "claude_agent_sdk",
        "model": "claude-opus-5-5",
        "resolved_model": "claude-opus-5-5",
        "operation": "agent_session",
        "service_tier": "standard",
        "outcome": "success",
        "error_type": "",
    }
    before = _sample("family_assistant_llm_calls_total", **labels)
    search = FakeToolUse(name="ToolSearch", input={"query": "weather"})
    fake = FakeClaudeCode(
        responses=[FakeResponse([search]), FakeResponse([FakeText("Sunny.")])]
    )
    await _turn(_service(fake), db_engine, "Weather in Paris?")

    assert _sample("family_assistant_llm_calls_total", **labels) == before + 2


@pytest.mark.asyncio
async def test_session_replies_carry_the_experiment_and_timing(
    db_engine: AsyncEngine,
) -> None:
    """A session run is attributed to its experiment arm like any API run."""
    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("Hi.")])])
    service = _service(fake)
    stream = service.process_message_stream(
        db_context=Database(db_engine),
        messages=[
            SystemMessage(content="You are a household assistant."),
            UserMessage(content="Hello"),
        ],
        interface_type="test",
        conversation_id="conv-1",
        user_name="TestUser",
        turn_id="turn-1",
        chat_interface=None,
        llm_client=service.llm_client,
        model_selection=ResolvedModelSelection(
            tier=TIER,
            requested=None,
            source="experiment",
            experiment_id="deep-experiment",
        ),
    )
    replies = [
        message.reasoning_info
        async for _event, message in stream
        if isinstance(message, AssistantMessage) and message.reasoning_info
    ]

    assert len(replies) == 1
    reasoning = replies[0]
    assert (
        reasoning.get("experiment_id"),
        reasoning.get("experiment_arm"),
        reasoning.get("model_tier"),
        reasoning.get("model_tier_source"),
    ) == ("deep-experiment", TIER, TIER, "experiment")
    assert reasoning.get("duration_ms", -1.0) >= 0.0
