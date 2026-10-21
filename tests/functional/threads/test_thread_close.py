"""Closing a thread, echoing its outcome into Home, and reopening it.

See docs/design/one-thread-chat-ux.md, "State" and "Home sees its threads as
facts, not transcripts".
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.config_models import (
    AppConfig,
    ConversationThreadsConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage import message_history_table
from family_assistant.storage.database import Database
from family_assistant.threads.listing import ThreadSummary, summarize_threads
from family_assistant.tools.catalog import (
    LOCAL_TOOL_REGISTRATIONS as local_tool_registrations,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.mcp import MCPToolsProvider
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage

HOME_PROFILE = "default_assistant"
THREAD_PROFILE = "complex_tasks"
HOME = "web_conv_home"
THREAD = "web_conv_thread"
OTHER = "web_conv_other"
USER = "alice"
TITLE = "Hot water system quotes"
OUTCOME = "Booked Acme Plumbing for Tuesday at 9am."
QUESTION = "Gas or heat pump?"
PREAMBLE = "Wrapping this up."
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)

UNTRUSTED = TaintSource(
    source_type=TaintSourceType.BROWSER_SNAPSHOT,
    source_id="plumber-site",
    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
    labels=frozenset({"source_unknown_external"}),
    reason="A plumber's website.",
)


def _config(profile_id: str) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        id=profile_id,
        receives_household_context=True,
    )


async def _tools() -> PolicyEnforcingToolsProvider:
    provider = PolicyEnforcingToolsProvider(
        wrapped_provider=CompositeToolsProvider(
            providers=[
                LocalToolsProvider(registrations=local_tool_registrations),
                MCPToolsProvider(mcp_server_configs={}),
            ]
        ),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )
    await provider.get_tool_definitions()
    return provider


def _call(
    name: str, arguments: dict[str, object], *, content: str | None = None
) -> LLMOutput:
    return LLMOutput(
        content=content,
        tool_calls=[
            ToolCallItem(
                id=f"call_{uuid.uuid4()}",
                type="function",
                function=ToolCallFunction(name=name, arguments=json.dumps(arguments)),
            )
        ],
    )


def _last_message(kwargs: MatcherArgs) -> LLMMessage:
    return kwargs["messages"][-1]


def _last_user_text(kwargs: MatcherArgs) -> str:
    return str(_last_message(kwargs).content)


def _after_tool(kwargs: MatcherArgs) -> bool:
    return getattr(_last_message(kwargs), "role", None) == "tool"


class RecordingPublisher:
    """Stands in for the stream hub, recording what it is told."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.activity: list[tuple[str, str]] = []

    def has_running_turn(self, conversation_id: str) -> bool:
        del conversation_id
        return False

    async def publish(
        self,
        conversation_id: str,
        event_type: str,
        *,
        turn_id: str | None,
        payload: dict[str, object],
    ) -> object:
        del event_type, turn_id, payload
        self.published.append(conversation_id)
        return None

    async def publish_activity(
        self,
        conversation_id: str,
        *,
        user_id: str,
        reason: str,
        timestamp: datetime | None = None,
    ) -> None:
        del timestamp
        self.activity.append((conversation_id, f"{user_id}:{reason}"))


@dataclass
class World:
    """A Home with one thread, each served by its own profile."""

    db: Database
    home_service: ProcessingService
    thread_service: ProcessingService
    home_llm: RuleBasedMockLLMClient
    thread_llm: RuleBasedMockLLMClient
    publisher: RecordingPublisher
    tool_results: list[str] = field(default_factory=list)
    # What the thread's assistant ends a turn with after finishing the thread.
    final_reply: str = OUTCOME

    async def say(
        self,
        service: ProcessingService,
        conversation_id: str,
        text: str,
        *,
        tainted: bool = False,
    ) -> None:
        result = await service.handle_chat_interaction(
            db_context=self.db,
            interface_type="web",
            conversation_id=conversation_id,
            trigger_content_parts=[{"type": "text", "text": text}],
            trigger_interface_message_id=None,
            user_name="Alice",
            user_id=USER,
            chat_interface=MagicMock(spec=ChatInterface),
            initial_taint_sources=[UNTRUSTED] if tainted else None,
        )
        assert result.error_traceback is None

    async def in_thread(self, text: str, *, tainted: bool = False) -> None:
        await self.say(self.thread_service, THREAD, text, tainted=tainted)

    async def rows(self, conversation_id: str) -> list[Mapping[str, object]]:
        rows = await self.db.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == conversation_id)
            .order_by(message_history_table.c.internal_id)
        )
        return list(rows)

    async def echoes(self) -> list[Mapping[str, object]]:
        return [
            row
            for row in await self.rows(HOME)
            if row["echoed_from_conversation_id"] is not None
        ]

    async def summary(self) -> ThreadSummary:
        thread = await self.db.conversation_threads.get_thread(THREAD)
        assert thread is not None
        [summary] = await summarize_threads(
            self.db, [thread], turn_running=lambda _id: False, viewer=None
        )
        return summary

    def offered(self, llm: RuleBasedMockLLMClient) -> set[str]:
        tools = llm.get_calls()[-1]["kwargs"]["tools"] or []
        return {tool["function"]["name"] for tool in tools}


def _thread_reply(world_ref: list[World]) -> RuleBasedMockLLMClient:
    def after_tool(kwargs: MatcherArgs) -> LLMOutput:
        world_ref[0].tool_results.append(str(_last_message(kwargs).content))
        return LLMOutput(content=world_ref[0].final_reply)

    return RuleBasedMockLLMClient(
        rules=[
            (_after_tool, after_tool),
            (
                lambda kwargs: "give up" in _last_user_text(kwargs),
                lambda _kwargs: _call("finish_thread", {"outcome": "failed"}),
            ),
            (
                lambda kwargs: "finish" in _last_user_text(kwargs),
                lambda _kwargs: _call(
                    "finish_thread", {"outcome": "done"}, content=PREAMBLE
                ),
            ),
            (
                lambda kwargs: "ask me" in _last_user_text(kwargs),
                lambda _kwargs: LLMOutput(content=QUESTION),
            ),
        ],
        default_response=LLMOutput(content="Noted."),
    )


def _home_reply(world_ref: list[World]) -> RuleBasedMockLLMClient:
    def after_tool(kwargs: MatcherArgs) -> LLMOutput:
        result = str(_last_message(kwargs).content)
        world_ref[0].tool_results.append(result)
        return LLMOutput(content=f"Here is where things stand: {result}")

    return RuleBasedMockLLMClient(
        rules=[
            (_after_tool, after_tool),
            (
                lambda kwargs: "how are the quotes" in _last_user_text(kwargs),
                lambda _kwargs: _call("list_threads", {}),
            ),
        ],
        default_response=LLMOutput(content="Noted."),
    )


async def _make_world(db_engine: AsyncEngine, *, enabled: bool = True) -> World:
    db = Database(engine=db_engine)
    app_config = AppConfig(
        conversation_threads=ConversationThreadsConfig(enabled=enabled)
    )
    world_ref: list[World] = []
    home_llm = _home_reply(world_ref)
    thread_llm = _thread_reply(world_ref)
    publisher = RecordingPublisher()
    services = {
        profile: ProcessingService(
            llm_client=llm,
            tools_provider=await _tools(),
            service_config=_config(profile),
            context_providers=[],
            server_url="http://test.server",
            app_config=app_config,
            home_activity_publisher=publisher,
        )
        for profile, llm in ((HOME_PROFILE, home_llm), (THREAD_PROFILE, thread_llm))
    }
    for service in services.values():
        service.processing_services_registry = services
    world = World(
        db=db,
        home_service=services[HOME_PROFILE],
        thread_service=services[THREAD_PROFILE],
        home_llm=home_llm,
        thread_llm=thread_llm,
        publisher=publisher,
    )
    world_ref.append(world)

    await db.conversation_threads.designate_home(
        user_id=USER, interface_type="web", conversation_id=HOME, now=NOW
    )
    title_taint = TurnTaintState.empty().add_source(UNTRUSTED).to_metadata()
    await db.conversation_threads.create_thread(
        conversation_id=THREAD,
        home_conversation_id=HOME,
        interface_type="web",
        owner_user_id=USER,
        profile_id=THREAD_PROFILE,
        title=TITLE,
        title_taint=title_taint,
        anchor_message_id=None,
        delegation_id=None,
        now=NOW,
    )
    seed = await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Find three plumbers."),
        interface_type="web",
        conversation_id=THREAD,
        timestamp=NOW,
        processing_profile_id=THREAD_PROFILE,
        user_id=USER,
        is_internal=True,
    )
    await db.conversation_threads.set_seed_message(THREAD, seed, NOW)
    return world


@pytest.fixture
async def world(db_engine: AsyncEngine) -> World:
    return await _make_world(db_engine)


@pytest.mark.asyncio
async def test_finishing_echoes_the_final_message_into_home(world: World) -> None:
    await world.in_thread("please finish up", tainted=True)

    [echo] = await world.echoes()
    summary = await world.summary()
    assert (
        echo["role"],
        echo["content"],
        echo["echoed_from_conversation_id"],
        echo["processing_profile_id"],
        echo["is_internal"],
    ) == ("assistant", OUTCOME, THREAD, HOME_PROFILE, False)
    assert (summary.state, summary.closed_by, summary.echo_message_id) == (
        "done",
        "assistant",
        echo["internal_id"],
    )
    assert HOME in world.publisher.published


@pytest.mark.asyncio
async def test_an_echo_pings_the_login_the_turn_ran_as(world: World) -> None:
    """The activity feed matches the exact id a client subscribed under."""
    result = await world.thread_service.handle_chat_interaction(
        db_context=world.db,
        interface_type="web",
        conversation_id=THREAD,
        trigger_content_parts=[{"type": "text", "text": "please finish up"}],
        trigger_interface_message_id=None,
        user_name="Alice",
        user_id="alice-phone",
        chat_interface=MagicMock(spec=ChatInterface),
    )

    assert result.error_traceback is None
    assert [entry for entry in world.publisher.activity if entry[0] == HOME] == [
        (HOME, "alice-phone:thread_echo")
    ]


@pytest.mark.asyncio
async def test_an_echo_keeps_its_taint(world: World) -> None:
    await world.in_thread("please finish up", tainted=True)

    [echo] = await world.echoes()
    [source] = [
        row
        for row in await world.rows(THREAD)
        if row["role"] == "assistant" and row["content"] == OUTCOME
    ]
    taint = cast("dict[str, object]", echo["taint_metadata_json"])
    assert echo["taint_metadata_json"] == source["taint_metadata_json"]
    assert (
        SourceTrustTier.from_value(taint["max_tier"])
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.asyncio
async def test_an_echo_does_not_wake_home(world: World) -> None:
    await world.in_thread("please finish up")

    assert len(await world.echoes()) == 1
    assert world.home_llm.get_calls() == []


@pytest.mark.asyncio
async def test_a_closing_turn_with_no_final_reply_echoes_nothing(world: World) -> None:
    """The text alongside the finish_thread call is not the outcome."""
    world.final_reply = ""

    await world.in_thread("please finish up")

    summary = await world.summary()
    assert (summary.state, summary.echo_message_id) == ("done", None)
    assert await world.echoes() == []


@pytest.mark.asyncio
async def test_a_job_the_assistant_gives_up_on_is_failed(world: World) -> None:
    await world.in_thread("this cannot be done, give up")

    summary = await world.summary()
    assert (summary.state, summary.closed_by) == ("failed", "assistant")
    assert len(await world.echoes()) == 1


@pytest.mark.asyncio
async def test_a_follow_up_turn_that_errors_fails_the_thread(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider failure is never presented as the user's move, and is unread."""
    await world.in_thread("ask me which system I want")

    def provider_down(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        async def stream() -> AsyncIterator[object]:
            raise RuntimeError("The provider is down.")
            yield  # pragma: no cover - makes this an async generator

        return stream()

    monkeypatch.setattr(world.thread_llm, "generate_response_stream", provider_down)
    result = await world.thread_service.handle_chat_interaction(
        db_context=world.db,
        interface_type="web",
        conversation_id=THREAD,
        trigger_content_parts=[{"type": "text", "text": "Gas, please."}],
        trigger_interface_message_id=None,
        user_name="Alice",
        user_id=USER,
        chat_interface=MagicMock(spec=ChatInterface),
    )
    latest = (await world.db.message_history.latest_visible_messages([THREAD]))[THREAD]
    await world.db.conversation_threads.mark_read(
        THREAD, viewer_user_id=USER, through_message_id=latest.internal_id - 1, now=NOW
    )

    summary = await world.summary()
    unread = await world.db.conversation_threads.unread_counts(
        [THREAD], viewer_user_id=USER, viewer_keys={USER}
    )
    assert result.error_traceback is not None
    assert (latest.role, summary.state) == ("error", "failed")
    assert unread == {THREAD: 1}


@pytest.mark.asyncio
async def test_a_thread_ending_on_a_question_is_waiting(world: World) -> None:
    await world.in_thread("ask me which system I want")

    summary = await world.summary()
    assert (summary.state, summary.preview) == ("waiting", QUESTION)
    assert await world.echoes() == []


@pytest.mark.asyncio
async def test_a_closed_thread_reopens_when_a_message_arrives(world: World) -> None:
    await world.in_thread("please finish up")
    assert (await world.summary()).state == "done"

    await world.in_thread("actually, can they come Monday instead?")

    summary = await world.summary()
    assert (summary.state, summary.closed_by) == ("waiting", None)


@pytest.mark.asyncio
async def test_finishing_again_after_a_reopen_writes_a_new_echo(world: World) -> None:
    await world.in_thread("please finish up")
    await world.in_thread("actually, can they come Monday instead?")

    await world.in_thread("great, finish it again")

    echoes = await world.echoes()
    summary = await world.summary()
    assert [echo["content"] for echo in echoes] == [OUTCOME, OUTCOME]
    assert (summary.state, summary.echo_message_id) == (
        "done",
        echoes[-1]["internal_id"],
    )


@pytest.mark.asyncio
async def test_home_answers_a_status_question_from_thread_facts(
    world: World,
) -> None:
    await world.in_thread("please finish up")

    await world.say(world.home_service, HOME, "how are the quotes going?")

    [facts] = world.tool_results[-1:]
    assert f'"{TITLE}"' in facts
    assert "done" in facts
    assert OUTCOME in facts
    # The facts carry the taint of the turn that wrote the title.
    [reply] = [
        row
        for row in await world.rows(HOME)
        if row["role"] == "assistant"
        and isinstance(row["content"], str)
        and row["content"].startswith("Here is where things stand")
    ]
    taint = cast("dict[str, object]", reply["taint_metadata_json"])
    assert (
        SourceTrustTier.from_value(taint["max_tier"])
        is SourceTrustTier.UNKNOWN_EXTERNAL
    )


@pytest.mark.asyncio
async def test_home_never_sees_a_threads_transcript(world: World) -> None:
    await world.in_thread("ask me which system I want")

    await world.say(world.home_service, HOME, "how are the quotes going?")

    [facts] = world.tool_results[-1:]
    assert "waiting on the user" in facts
    assert QUESTION not in facts
    assert "Find three plumbers." not in facts


@pytest.mark.asyncio
async def test_thread_tools_are_offered_only_where_they_apply(world: World) -> None:
    await world.in_thread("hello")
    await world.say(world.home_service, HOME, "hello")

    assert "finish_thread" in world.offered(world.thread_llm)
    assert "list_threads" not in world.offered(world.thread_llm)
    assert "list_threads" in world.offered(world.home_llm)
    assert "finish_thread" not in world.offered(world.home_llm)


@pytest.mark.asyncio
async def test_finish_thread_is_refused_outside_a_thread(world: World) -> None:
    await world.say(world.thread_service, OTHER, "please finish up")

    assert "not a thread" in world.tool_results[-1]
    assert await world.echoes() == []


@pytest.mark.asyncio
async def test_finish_thread_is_hidden_and_refused_while_threads_are_disabled(
    db_engine: AsyncEngine,
) -> None:
    world = await _make_world(db_engine, enabled=False)

    await world.in_thread("please finish up")

    assert "finish_thread" not in world.offered(world.thread_llm)
    assert "Threads are not enabled" in world.tool_results[-1]
    thread = await world.db.conversation_threads.get_thread(THREAD)
    assert thread is not None
    assert thread.close is None
