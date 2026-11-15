"""ClaudeAgentLoop driven through the Agent SDK's own Query against a fake CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ClaudeAgentConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import AssistantMessage, ToolMessage
from family_assistant.llm.model_selection import ModelTierEligibility, ModelTierOption
from family_assistant.llm.model_tiers import ClaudeAgentModel
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.claude_agent_loop import (
    ClaudeAgentBillingError,
    mcp_tool_name,
    run_on_claude_agent,
)
from family_assistant.processing.quiet_turn import END_TURN_QUIETLY_TOOL_NAME
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.storage.database import Database
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
