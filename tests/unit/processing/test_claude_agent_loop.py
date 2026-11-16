"""ClaudeAgentLoop driven through the Agent SDK's own Query against a fake CLI."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from prometheus_client import REGISTRY
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
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.claude_agent_loop import (
    ClaudeAgentBillingError,
    mcp_tool_name,
    probe_verdict,
    run_on_claude_agent,
)
from family_assistant.processing.quiet_turn import END_TURN_QUIETLY_TOOL_NAME
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database
from family_assistant.storage.delegation_runs import DELEGATION_CONTINUATION_TRIGGER
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.fake_claude_code import (
    FakeClaudeCode,
    FakeResponse,
    FakeText,
    FakeToolUse,
    ReplayClaudeCode,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient
from tests.mocks.mock_tools_provider import MockToolsProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.processing.types import ToolExecutionResult

TIER = "claude_opus_5_5"


async def get_weather(city: str, exec_context: ToolExecutionContext) -> str:
    return f"Weather in {city}: 21C, sunny"


def _service(
    fake: FakeClaudeCode | ReplayClaudeCode, *, max_iterations: int = 10
) -> ProcessingService:
    tools = MockToolsProvider()
    tools.add_tool("get_weather", get_weather)
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
            default_tier=TIER, selectable=(ModelTierOption(id=TIER, label="Opus"),)
        ),
    )
    # The tier's chat client serves only auxiliary calls; a turn never uses it.
    aux_client = RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content="unused")
    )
    service = ProcessingService(
        llm_client=aux_client,
        tools_provider=tools,
        service_config=config,
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
        tier_llm_clients={TIER: aux_client},
    )

    def client_factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        return ClaudeSDKClient(options, transport=fake)

    run_on_claude_agent(
        service,
        agent_config=ClaudeAgentConfig(),
        models={
            TIER: ClaudeAgentModel(
                model="claude-opus-5-5", effort="high", custom_instructions=None
            )
        },
        client_factory=client_factory,
    )
    return service


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


@pytest.mark.asyncio
async def test_delegation_wake_opens_with_its_instruction(
    db_engine: AsyncEngine,
) -> None:
    """A wake's trigger is a system row; its results arrive as history before it."""
    db = Database(db_engine)
    await db.message_history.add_message(
        UserMessage(
            content="Engineer result: the cron job was disabled.",
            taint_metadata=TurnTaintState.empty().to_metadata(),
            authorship_taint_metadata=TurnTaintState.empty().to_metadata(),
        ),
        interface_type="test",
        conversation_id="conv-1",
        turn_id="wake-turn",
        timestamp=datetime.now(UTC),
        processing_profile_id="claude_agent",
        is_internal=True,
    )
    fake = FakeClaudeCode(responses=[FakeResponse([FakeText("It was disabled.")])])

    reply = await _service(fake).handle_chat_interaction(
        db_context=db,
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

    texts = [block["text"] for block in fake.prompts[0]["content"]]
    assert reply.text_reply == "It was disabled."
    assert "the cron job was disabled" in texts[0]
    assert "Their results are provided as lower-priority data" in texts[-1]


@pytest.mark.asyncio
async def test_session_billed_to_an_api_key_is_refused(db_engine: AsyncEngine) -> None:
    fake = FakeClaudeCode(
        responses=[FakeResponse([FakeText("Hi.")])],
        api_key_source="ANTHROPIC_API_KEY",
    )
    result = await _interact(_service(fake), db_engine, "Hello")

    assert ClaudeAgentBillingError.__name__ in (result.error_traceback or "")


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
    stream = service.llm_loop.run_stream(
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
