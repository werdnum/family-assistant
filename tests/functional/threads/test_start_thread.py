"""Starting a thread from Home, end to end through the task worker.

See docs/design/one-thread-chat-ux.md, "Starting a thread".
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
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
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage import delegation_runs_table, message_history_table
from family_assistant.storage.database import Database
from family_assistant.threads.listing import summarize_threads
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage

HOME_PROFILE = "default_assistant"
TARGET_PROFILE = "complex_tasks"
HOME = "web_conv_home"
USER = "alice"
REQUEST = "Find a plumber and get three quotes for the hot water system."
SECRET = "the garage code is 4417"
THREAD_REPLY = "Here are three quotes."
HOME_REPLY = "I've started a thread for the quotes."


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


def _has_tool_result(messages: list[LLMMessage]) -> bool:
    return any(getattr(message, "role", None) == "tool" for message in messages)


def _start_thread_call(attachment_ids: list[str]) -> LLMOutput:
    arguments: dict[str, object] = {
        "target_service_id": TARGET_PROFILE,
        "title": "Hot water system quotes",
        "request": REQUEST,
    }
    if attachment_ids:
        arguments["attachment_ids"] = attachment_ids
    return LLMOutput(
        content="Starting a thread.",
        tool_calls=[
            ToolCallItem(
                id=f"call_{uuid.uuid4()}",
                type="function",
                function=ToolCallFunction(
                    name="start_thread",
                    arguments=json.dumps(arguments),
                ),
            )
        ],
    )


@dataclass
class ThreadWorld:
    """A Home profile that starts a thread, and the profile that runs it."""

    db: Database
    home_service: ProcessingService
    target_calls: list[list[LLMMessage]] = field(default_factory=list)
    home_tool_results: list[str] = field(default_factory=list)
    home_llm: RuleBasedMockLLMClient | None = None
    attachment_registry: AttachmentRegistry | None = None
    # Passed by the Home model when it starts a thread.
    attachment_ids: list[str] = field(default_factory=list)

    async def upload(self) -> str:
        assert self.attachment_registry is not None
        record = await self.attachment_registry.register_user_attachment(
            db_context=self.db,
            content=b"%PDF-1.4 quote",
            mime_type="application/pdf",
            filename="quote.pdf",
            conversation_id=HOME,
            user_id=USER,
            description="A plumber's quote",
        )
        return record.attachment_id

    def tools_offered_on_first_call(self) -> set[str]:
        assert self.home_llm is not None
        tools = self.home_llm.get_calls()[0]["kwargs"]["tools"] or []
        return {tool["function"]["name"] for tool in tools}

    async def say_in(self, conversation_id: str, text: str) -> None:
        result = await self.home_service.handle_chat_interaction(
            db_context=self.db,
            interface_type="web",
            conversation_id=conversation_id,
            trigger_content_parts=[{"type": "text", "text": text}],
            trigger_interface_message_id=None,
            user_name="Alice",
            user_id=USER,
            chat_interface=MagicMock(spec=ChatInterface),
        )
        assert result.error_traceback is None

    async def thread_ids(self) -> list[str]:
        return [
            thread.conversation_id
            for thread in await self.db.conversation_threads.list_for_home(HOME)
        ]

    async def rows(self, conversation_id: str) -> list[Mapping[str, object]]:
        rows = await self.db.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == conversation_id)
            .order_by(message_history_table.c.internal_id)
        )
        return list(rows)

    async def wait_for_thread_run_to_finish(self) -> None:
        async def finished() -> bool:
            rows = await self.db.fetch_all(
                select(delegation_runs_table).where(
                    delegation_runs_table.c.run_kind == "thread"
                )
            )
            return bool(rows) and all(row["notified_at"] is not None for row in rows)

        await wait_for_condition(finished, description="the thread run to finish")


@pytest.fixture
async def world(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ThreadWorld:
    marker = request.node.get_closest_marker("target_sources")
    allowed_sources = marker.args[0] if marker else None
    db = Database(engine=db_engine)
    app_config = AppConfig(conversation_threads=ConversationThreadsConfig(enabled=True))
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )
    world = ThreadWorld(
        db=db,
        home_service=cast("ProcessingService", None),
        attachment_registry=attachment_registry,
    )

    def home_tool_result(kwargs: MatcherArgs) -> LLMOutput:
        messages = kwargs.get("messages", [])
        world.home_tool_results.append(str(messages[-1].content))
        return LLMOutput(content=HOME_REPLY)

    home_llm = RuleBasedMockLLMClient(
        rules=[
            (
                lambda kwargs: _has_tool_result(kwargs.get("messages", [])),
                home_tool_result,
            ),
            (
                lambda kwargs: "start a thread" in str(kwargs["messages"][-1].content),
                lambda _kwargs: _start_thread_call(world.attachment_ids),
            ),
        ],
        default_response=LLMOutput(content="Noted."),
    )

    def target_reply(kwargs: MatcherArgs) -> LLMOutput:
        world.target_calls.append(list(kwargs.get("messages", [])))
        return LLMOutput(content=THREAD_REPLY)

    target_llm = RuleBasedMockLLMClient(rules=[(lambda _kwargs: True, target_reply)])
    if request.node.get_closest_marker("target_fails") is not None:

        def provider_down(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            async def stream() -> AsyncIterator[object]:
                raise RuntimeError("The provider is down.")
                yield  # pragma: no cover - makes this an async generator

            return stream()

        monkeypatch.setattr(target_llm, "generate_response_stream", provider_down)

    world.home_llm = home_llm
    world.home_service = ProcessingService(
        llm_client=home_llm,
        tools_provider=await _tools(),
        service_config=_config(HOME_PROFILE),
        context_providers=[],
        server_url="http://test.server",
        app_config=app_config,
        attachment_registry=attachment_registry,
    )
    target_service = ProcessingService(
        llm_client=target_llm,
        tools_provider=await _tools(),
        service_config=_config(
            TARGET_PROFILE, allowed_delegation_sources=allowed_sources
        ),
        context_providers=[],
        server_url="http://test.server",
        app_config=app_config,
        attachment_registry=attachment_registry,
    )
    registry = {HOME_PROFILE: world.home_service, TARGET_PROFILE: target_service}
    world.home_service.processing_services_registry = registry
    target_service.processing_services_registry = registry
    task_worker_manager(
        world.home_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )
    await db.conversation_threads.designate_home(
        user_id=USER,
        interface_type="web",
        conversation_id=HOME,
        now=datetime.now(UTC),
    )
    return world


@pytest.mark.asyncio
async def test_a_started_thread_runs_on_its_own_profile(world: ThreadWorld) -> None:
    await world.say_in(HOME, "please start a thread for the quotes")
    await world.wait_for_thread_run_to_finish()

    [thread_id] = await world.thread_ids()
    replies = [
        (row["content"], row["processing_profile_id"])
        for row in await world.rows(thread_id)
        if row["role"] == "assistant"
    ]
    assert replies == [(THREAD_REPLY, TARGET_PROFILE)]


@pytest.mark.asyncio
@pytest.mark.target_fails
async def test_a_run_that_exhausts_its_retries_is_failed_not_waiting(
    world: ThreadWorld,
) -> None:
    await world.say_in(HOME, "please start a thread for the quotes")
    await world.wait_for_thread_run_to_finish()

    [thread] = await world.db.conversation_threads.list_for_home(HOME)
    [summary] = await summarize_threads(
        world.db, [thread], turn_running=lambda _id: False, viewer=None
    )
    [run] = await world.db.fetch_all(
        select(delegation_runs_table).where(
            delegation_runs_table.c.run_kind == "thread"
        )
    )
    assert run["status"] == "failed"
    assert summary.state == "failed"


@pytest.mark.asyncio
async def test_a_finished_thread_does_not_wake_home(world: ThreadWorld) -> None:
    await world.say_in(HOME, "please start a thread for the quotes")
    home_rows_after_turn = await world.rows(HOME)

    await world.wait_for_thread_run_to_finish()

    assert await world.rows(HOME) == home_rows_after_turn


@pytest.mark.asyncio
async def test_the_thread_receives_the_request_and_nothing_else_from_home(
    world: ThreadWorld,
) -> None:
    await world.say_in(HOME, f"remember that {SECRET}")
    await world.say_in(HOME, "please start a thread for the quotes")
    await world.wait_for_thread_run_to_finish()

    [messages] = world.target_calls
    user_texts = [
        str(message.content) for message in messages if isinstance(message, UserMessage)
    ]
    # Each user message carries its send time ahead of its text.
    assert [text.endswith(REQUEST) for text in user_texts] == [True]


@pytest.mark.asyncio
@pytest.mark.target_sources(["someone_else"])
async def test_a_profile_closed_to_home_cannot_be_threaded(world: ThreadWorld) -> None:
    await world.say_in(HOME, "please start a thread for the quotes")

    assert await world.thread_ids() == []
    assert "not permitted" in world.home_tool_results[0]


@pytest.mark.asyncio
async def test_a_thread_cannot_be_started_outside_home(world: ThreadWorld) -> None:
    await world.say_in("web_conv_other", "please start a thread for the quotes")

    assert await world.thread_ids() == []
    assert "only be started from the user's Home" in world.home_tool_results[0]


@pytest.mark.asyncio
async def test_home_turns_are_offered_the_tool(world: ThreadWorld) -> None:
    await world.say_in(HOME, "hello")

    assert "start_thread" in world.tools_offered_on_first_call()


@pytest.mark.asyncio
async def test_turns_outside_home_are_not_offered_the_tool(world: ThreadWorld) -> None:
    await world.say_in("web_conv_other", "hello")

    assert "start_thread" not in world.tools_offered_on_first_call()


@pytest.mark.asyncio
async def test_attachments_passed_to_a_thread_stay_in_its_history(
    world: ThreadWorld,
) -> None:
    attachment_id = await world.upload()
    world.attachment_ids = [attachment_id]

    await world.say_in(HOME, "please start a thread for the quotes")

    [thread_id] = await world.thread_ids()
    thread = await world.db.conversation_threads.get_thread(thread_id)
    assert thread is not None
    seed_rows = [
        row
        for row in await world.rows(thread_id)
        if row["internal_id"] == thread.seed_message_id
    ]
    assert [row["attachments"] for row in seed_rows] == [
        [{"type": "attachment_reference", "attachment_id": attachment_id}]
    ]


@pytest.mark.asyncio
async def test_a_thread_records_the_tool_call_that_started_it(
    world: ThreadWorld,
) -> None:
    await world.say_in(HOME, "please start a thread for the quotes")

    [thread_id] = await world.thread_ids()
    thread = await world.db.conversation_threads.get_thread(thread_id)
    tool_call_ids = [
        call["id"]
        for row in await world.rows(HOME)
        if row["role"] == "assistant" and row["tool_calls"]
        for call in cast("list[Mapping[str, object]]", row["tool_calls"])
    ]
    assert thread is not None
    assert [thread.source_tool_call_id] == tool_call_ids
