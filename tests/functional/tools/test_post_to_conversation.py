"""``post_to_conversation``, end to end through the task worker.

A thread's assistant posts a note into the user's Home. The note wakes Home's
assistant on Home's own profile, the woken turn starts with the posting turn's
taint, and any conversation that is not another of the user's is refused.

See docs/design/cross-conversation-notes.md.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.config_models import (
    AppConfig,
    ConversationThreadsConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import (
    SourceTrustTier,
    TurnTaintState,
    prompt_window_taint,
    unknown_external_taint_metadata,
)
from family_assistant.storage import delegation_runs_table, message_history_table
from family_assistant.storage.database import Database
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
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient
from tests.mocks.recording_chat_interface import RecordingChatInterface

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage

HOME_PROFILE = "default_assistant"
THREAD_PROFILE = "complex_tasks"
HOME = "web_conv_home"
THREAD = "web_conv_thread"
USER = "alice"
NOTE = "The quotes are in: Acme is cheapest."
HOME_REPLY = "Thanks, noted the Acme quote."
POSTED = "Posted it to Home."


def _config(
    profile_id: str, *, allowed_delegation_sources: list[str] | None = None
) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone=ZoneInfo("UTC"),
        history_budget_chars=100_000,
        history_max_age_hours=24,
        tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        id=profile_id,
        allowed_delegation_sources=allowed_delegation_sources,
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


def _tool_result(messages: list[LLMMessage]) -> str | None:
    if messages and getattr(messages[-1], "role", None) == "tool":
        return str(messages[-1].content)
    return None


def _post_call(target: str) -> LLMOutput:
    return LLMOutput(
        content="Posting a note.",
        tool_calls=[
            ToolCallItem(
                id=f"call_{uuid.uuid4()}",
                type="function",
                function=ToolCallFunction(
                    name="post_to_conversation",
                    arguments=json.dumps({
                        "target_conversation_id": target,
                        "message_content": NOTE,
                    }),
                ),
            )
        ],
    )


@dataclass
class NoteWorld:
    """A Home and one of its threads, each with its own profile's model."""

    db: Database
    thread_service: ProcessingService
    home_calls: list[list[LLMMessage]] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    target: str = "home"

    async def say_in_thread(
        self,
        text: str,
        *,
        interface_type: str = "web",
        conversation_id: str = THREAD,
    ) -> None:
        result = await self.thread_service.handle_chat_interaction(
            db_context=self.db,
            interface_type=interface_type,
            conversation_id=conversation_id,
            trigger_content_parts=[{"type": "text", "text": text}],
            trigger_interface_message_id=None,
            user_name="Alice",
            user_id=USER,
            chat_interface=RecordingChatInterface(),
        )
        assert result.error_traceback is None

    async def rows(self, conversation_id: str) -> list[Mapping[str, object]]:
        rows = await self.db.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == conversation_id)
            .order_by(message_history_table.c.internal_id)
        )
        return list(rows)

    async def wait_for_the_woken_turn(self) -> None:
        async def finished() -> bool:
            rows = await self.db.fetch_all(
                select(delegation_runs_table).where(
                    delegation_runs_table.c.conversation_id == HOME
                )
            )
            return bool(rows) and all(row["notified_at"] is not None for row in rows)

        await wait_for_condition(finished, description="the woken Home turn")


async def _seed(db: Database, now: datetime) -> None:
    await db.conversation_threads.designate_home(
        user_id=USER, interface_type="web", conversation_id=HOME, now=now
    )
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Hello"),
        interface_type="web",
        conversation_id=HOME,
        timestamp=now,
        user_id=USER,
        processing_profile_id=HOME_PROFILE,
    )
    await db.conversation_threads.create_thread(
        conversation_id=THREAD,
        home_conversation_id=HOME,
        interface_type="web",
        owner_user_id=USER,
        profile_id=THREAD_PROFILE,
        title="Hot water quotes",
        title_taint=TurnTaintState.empty().to_metadata(),
        anchor_message_id=None,
        delegation_id=None,
        now=now,
    )
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Get three plumbing quotes."),
        interface_type="web",
        conversation_id=THREAD,
        timestamp=now,
        user_id=USER,
        processing_profile_id=THREAD_PROFILE,
    )
    # What the thread read from the web: its later turns inherit this taint.
    await db.message_history.add_message(
        AssistantMessage(
            content="Acme quoted $1,200.",
            taint_metadata=unknown_external_taint_metadata("A plumber's website."),
        ),
        interface_type="web",
        conversation_id=THREAD,
        timestamp=now,
        processing_profile_id=THREAD_PROFILE,
    )


@pytest.fixture
async def world(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
) -> NoteWorld:
    db = Database(engine=db_engine)
    app_config = AppConfig(conversation_threads=ConversationThreadsConfig(enabled=True))
    world = NoteWorld(db=db, thread_service=cast("ProcessingService", None))

    def thread_reply(kwargs: MatcherArgs) -> LLMOutput:
        messages = kwargs.get("messages", [])
        result = _tool_result(messages)
        if result is not None:
            world.tool_results.append(result)
            return LLMOutput(content=POSTED)
        return _post_call(world.target)

    def home_reply(kwargs: MatcherArgs) -> LLMOutput:
        world.home_calls.append(list(kwargs.get("messages", [])))
        return LLMOutput(content=HOME_REPLY)

    thread_llm = RuleBasedMockLLMClient(rules=[(lambda _kwargs: True, thread_reply)])
    home_llm = RuleBasedMockLLMClient(rules=[(lambda _kwargs: True, home_reply)])
    services = {
        profile: ProcessingService(
            llm_client=llm,
            tools_provider=await _tools(),
            service_config=_config(profile),
            context_providers=[],
            server_url="http://test.server",
            app_config=app_config,
        )
        for profile, llm in ((HOME_PROFILE, home_llm), (THREAD_PROFILE, thread_llm))
    }
    for service in services.values():
        service.processing_services_registry = services
    world.thread_service = services[THREAD_PROFILE]
    task_worker_manager(
        services[HOME_PROFILE],
        RecordingChatInterface(),
        register_delegation_handler=True,
    )
    await _seed(db, datetime.now(UTC))
    return world


@pytest.mark.asyncio
async def test_a_note_wakes_the_targets_assistant_on_its_own_profile(
    world: NoteWorld,
) -> None:
    await world.say_in_thread("tell Home the quotes are in")
    await world.wait_for_the_woken_turn()

    [prompt] = [message for message in world.home_calls[0] if NOTE in str(message)]
    assert "another of the user's conversations" in str(prompt)
    replies = [
        (row["content"], row["processing_profile_id"])
        for row in await world.rows(HOME)
        if row["role"] == "assistant"
    ]
    assert replies == [(HOME_REPLY, HOME_PROFILE)]


@pytest.mark.asyncio
async def test_the_note_is_machine_authored_not_the_users_words(
    world: NoteWorld,
) -> None:
    await world.say_in_thread("tell Home the quotes are in")
    await world.wait_for_the_woken_turn()

    [note] = [row for row in await world.rows(HOME) if NOTE in str(row["content"])]
    assert note["role"] == "user"
    assert note["is_internal"] is True


@pytest.mark.asyncio
async def test_the_woken_turn_carries_the_posting_turns_taint(
    world: NoteWorld,
) -> None:
    await world.say_in_thread("tell Home the quotes are in")
    await world.wait_for_the_woken_turn()

    [reply] = [
        row
        for row in await world.rows(HOME)
        if row["role"] == "assistant" and row["content"] == HOME_REPLY
    ]
    state = TurnTaintState.from_metadata(reply["taint_metadata_json"])
    assert state.max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (THREAD, "That is this conversation"),
        ("web_conv_nobody", "not one of this user's conversations"),
        ("web_conv_bob", "not one of this user's conversations"),
    ],
    ids=["current", "unknown", "another-users"],
)
async def test_refuses_conversations_that_are_not_another_of_the_users(
    world: NoteWorld, target: str, expected: str
) -> None:
    await world.db.message_history.add_message(
        UserMessage.from_trusted_user(content="Hi"),
        interface_type="web",
        conversation_id="web_conv_bob",
        timestamp=datetime.now(UTC),
        user_id="bob",
        processing_profile_id=HOME_PROFILE,
    )
    world.target = target

    await world.say_in_thread("tell them the quotes are in")

    [result] = world.tool_results
    assert result.startswith("Error:")
    assert expected in result
    runs = await world.db.fetch_all(select(delegation_runs_table))
    assert list(runs) == []


@pytest.mark.asyncio
async def test_the_targets_later_turns_stay_tainted_while_the_note_is_in_view(
    world: NoteWorld,
) -> None:
    """The posting turn only inherited its taint, from its own window.

    Home's window holds none of the thread's rows, so the note has to
    introduce that taint there, or Home's next turn would start clean.
    """
    await world.say_in_thread("tell Home the quotes are in")
    await world.wait_for_the_woken_turn()

    window = await world.db.message_history.get_recent(
        interface_type="web",
        conversation_id=HOME,
        processing_profile_id=HOME_PROFILE,
    )
    assert prompt_window_taint(window).max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_home_is_the_web_home_when_posting_from_telegram(
    world: NoteWorld,
) -> None:
    await world.say_in_thread(
        "tell Home the quotes are in",
        interface_type="telegram",
        conversation_id="12345",
    )
    await world.wait_for_the_woken_turn()

    assert any(NOTE in str(message) for message in world.home_calls[0])
