"""Functional tests for the resumable streaming API surface (M0).

These tests exercise the new endpoints:

* ``POST /api/v1/chat/turns``   — kick off a turn (idempotent on ``turn_id``)
* ``GET  /api/v1/chat/conversations/{id}/stream`` — SSE subscription

and the new behaviours that fall out of them:

* Late subscribers see the full sequence including ``turn_started``
* Same ``turn_id`` POSTed twice does not start two producers
* Buffer eviction surfaces ``active_turns`` in the 410 response
* Disconnect push fires iff no subscriber acked the ``turn_ended`` seq

The fixtures piggyback on ``test_chat_streaming.py`` (same conftest stack).
"""

import asyncio
import contextlib
import json
import logging
import tempfile
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import (
    AppConfig,
    TelegramUserIdentityConfig,
    ToolsConfig,
    UserIdentityConfig,
)
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
)
from family_assistant.llm.messages import MessageReasoningInfo, UserMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage import init_db
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS as local_tool_registrations,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
    ToolsProvider,
)
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.conversation_stream_hub import (
    ConversationStreamHub,
    SubscriptionHandle,
)
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# SSE parsing helpers
# --------------------------------------------------------------------------- #


class SSEEvent(TypedDict):
    type: str
    # ast-grep-ignore: no-dict-any - SSE event payloads are heterogeneous JSON parsed from the wire format; per-event-type typing belongs at the producer/consumer layer, not in test utilities
    data: dict[str, Any]


def parse_sse_events(response_text: str) -> list[SSEEvent]:
    """Parse SSE event frames out of a streaming response body."""
    events: list[SSEEvent] = []
    current_type: str | None = None
    for line in response_text.split("\n"):
        if line.startswith("event:"):
            current_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_type is not None:
            data_str = line.split(":", 1)[1].strip()
            if data_str:
                events.append(SSEEvent(type=current_type, data=json.loads(data_str)))
                current_type = None
    return events


async def collect_sse_stream(
    client: AsyncClient,
    url: str,
    *,
    # ast-grep-ignore: no-dict-any - test helper accepts arbitrary query params (mostly int/str) — same shape as httpx.AsyncClient.stream signature
    params: dict[str, Any] | None = None,
) -> list[SSEEvent]:
    """Stream an SSE response to completion and return parsed events."""
    events: list[SSEEvent] = []
    current_type: str | None = None
    async with client.stream("GET", url, params=params) as response:
        if response.status_code >= 400:
            await response.aread()
            raise AssertionError(
                f"SSE stream {url} returned status {response.status_code}: "
                f"{response.text}"
            )
        async for chunk in response.aiter_text():
            for line in chunk.split("\n"):
                if line.startswith("event:"):
                    current_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and current_type is not None:
                    data_str = line.split(":", 1)[1].strip()
                    if not data_str:
                        continue
                    payload = json.loads(data_str)
                    events.append(SSEEvent(type=current_type, data=payload))
                    current_type = None
                    if payload.get("status") in {"complete", "failed"}:
                        # turn_ended terminates the live tail for the test;
                        # the server may emit follow-on events but the body
                        # for assertion purposes is now complete.
                        return events
    return events


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture(scope="function")
async def db_context(
    db_engine: AsyncEngine,
) -> AsyncGenerator[DatabaseContext]:
    async with get_db_context(engine=db_engine) as ctx:
        yield ctx


@pytest.fixture(scope="function")
def mock_processing_service_config() -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": (
                "You are a test assistant. Time: {current_time}. "
                "Server URL: {server_url}. "
                "Context: {aggregated_other_context}"
            )
        },
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="resumable_streaming_test_profile",
    )


@pytest.fixture(scope="function")
def mock_llm_client() -> RuleBasedMockLLMClient:
    return RuleBasedMockLLMClient(rules=[])


@pytest_asyncio.fixture(scope="function")
async def test_tools_provider(
    mock_processing_service_config: ProcessingServiceConfig,
) -> ToolsProvider:
    local_provider = LocalToolsProvider(
        registrations=local_tool_registrations,
        embedding_generator=None,
        calendar_config=cast(
            # ast-grep-ignore: no-dict-any - CalendarConfig is a project-internal TypedDict but the test only needs the caldav field populated, so we cast a minimal dict to satisfy the type stub
            "Any",
            {"caldav": {"calendar_urls": ["http://test.com"]}},
        ),
    )
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed (mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=composite_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )
    await policy_provider.get_tool_definitions()
    return policy_provider


@pytest.fixture(scope="function")
def test_processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_engine: AsyncEngine,
) -> ProcessingService:
    async def get_entered_db_context_for_provider() -> DatabaseContext:
        async with get_db_context(engine=db_engine) as new_ctx:
            return new_ctx

    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=mock_processing_service_config.prompts,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config=cast(
            # ast-grep-ignore: no-dict-any - CalendarConfig is a project-internal TypedDict but the test only needs the caldav field populated, so we cast a minimal dict to satisfy the type stub
            "Any",
            {"caldav": {"calendar_urls": ["http://test.com"]}},
        ),
        timezone=mock_processing_service_config.timezone,
        prompts=mock_processing_service_config.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={}, prompts=mock_processing_service_config.prompts
    )
    context_providers = [notes_provider, calendar_provider, known_users_provider]

    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture(
    db_engine: AsyncEngine,
    test_processing_service: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_llm_client: LLMInterface,
) -> FastAPI:
    app = FastAPI(
        title=actual_app.title,
        docs_url=actual_app.docs_url,
        redoc_url=actual_app.redoc_url,
        middleware=actual_app.user_middleware,
    )
    app.include_router(actual_app.router)

    app.state.processing_service = test_processing_service
    app.state.tools_provider = test_tools_provider
    app.state.database_engine = db_engine
    app.state.config = AppConfig(database_url=str(db_engine.url))
    app.state.llm_client = mock_llm_client
    app.state.debug_mode = False
    app.state.web_chat_interface = WebChatInterface(db_engine)
    app.state.conversation_stream_hub = ConversationStreamHub()
    app.state.attachment_registry = AttachmentRegistry(
        storage_path=tempfile.mkdtemp(), db_engine=db_engine, config=None
    )

    async with get_db_context(engine=db_engine) as temp_db_ctx:
        await init_db(db_engine)
        await temp_db_ctx.init_vector_db()

    return app


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _SpyNotifier:
    """Captures notification dispatches for assertion."""

    def __init__(self) -> None:
        self.notified = asyncio.Event()
        # ast-grep-ignore: no-dict-any - NotificationMetadata is the only typed field; the rest are primitives or None and we just need to capture them for assertion
        self.calls: list[tuple[str, str, str, NotificationMetadata | None]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.calls.append((user_identifier, title, body, metadata))
        self.notified.set()


def _add_simple_llm_rule(
    mock_llm_client: RuleBasedMockLLMClient,
    *,
    prompt_marker: str,
    reply: str,
) -> None:
    """Configure the mock LLM to respond ``reply`` whenever the user message
    contains ``prompt_marker``."""
    mock_llm_client.rules.append((
        lambda args: any(
            msg.role == "user" and prompt_marker in str(msg.content or "")
            for msg in args.get("messages", [])
        ),
        LLMOutput(
            content=reply,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=10, completion_tokens=10, total_tokens=20
            ),
        ),
    ))


# --------------------------------------------------------------------------- #
# M0 red tests
# --------------------------------------------------------------------------- #


async def test_post_turn_then_subscribe_replays_in_progress_turn(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """The start-of-turn race the plan calls out (Codex finding #2).

    POST /v1/chat/turns returns immediately with {turn_id, conversation_id,
    first_seq}. The producer task starts running in the background. A
    subsequent GET /stream?from_seq=<first_seq> must see *every* event,
    including turn_started — even if the producer has already emitted some
    events by the time the GET arrives. This is closed by seeding turn_started
    into the buffer synchronously inside POST, before returning.
    """
    user_prompt = "Hello, replay test"
    llm_response = "Replayed reply."
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply=llm_response)

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_replay_{uuid.uuid4().hex[:8]}"

    post_response = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post_response.status_code == 200, post_response.text
    body = post_response.json()
    assert body["turn_id"] == turn_id
    assert body["conversation_id"] == conversation_id
    assert body["first_seq"] == 0

    events = await asyncio.wait_for(
        collect_sse_stream(
            test_client,
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0},
        ),
        timeout=10.0,
    )

    types = [e["type"] for e in events]
    assert "turn_started" in types
    assert "text" in types
    assert "turn_ended" in types

    # turn_started is at seq=0 and carries the same turn_id we passed in.
    turn_started = next(e for e in events if e["type"] == "turn_started")
    assert turn_started["data"]["seq"] == 0
    assert turn_started["data"]["turn_id"] == turn_id

    # Text events reconstruct the reply.
    text_chunks = [e["data"]["content"] for e in events if e["type"] == "text"]
    assert "".join(text_chunks) == llm_response

    # turn_ended carries the same turn_id, a complete status, and the LLM's
    # reasoning_info (token/model usage) the mock attached to its response.
    turn_ended = next(e for e in events if e["type"] == "turn_ended")
    assert turn_ended["data"]["turn_id"] == turn_id
    assert turn_ended["data"]["status"] == "complete"
    assert turn_ended["data"]["reasoning_info"]["total_tokens"] == 20


async def test_post_turn_rejects_conversation_owned_by_another_user(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Ownership is enforced before persisting: posting a turn into a
    conversation that already has messages from a different user is rejected
    with 404 (not 403, so we don't leak the conversation's existence), and no
    message is written. Closes the escalation where an attacker could post into
    a victim's conversation and thereby add themselves as an owner."""
    conversation_id = f"conv_owned_{uuid.uuid4().hex[:8]}"

    # Seed a message owned by a *different* user directly in the DB.
    async with get_db_context(engine=db_engine) as ctx:
        await ctx.message_history.add_message(
            UserMessage(content="victim's private message"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            user_id="someone_else",
        )

    response = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "prompt": "let me in",
            "interface_type": "web",
        },
    )
    assert response.status_code == 404, response.text

    # No new message should have been persisted by the rejected request.
    async with get_db_context(engine=db_engine) as ctx:
        rows = await ctx.message_history.get_recent_with_metadata(
            interface_type="web",
            conversation_id=conversation_id,
            limit=20,
        )
    assert len(rows) == 1
    assert rows[0]["user_id"] == "someone_else"


async def test_stream_on_empty_conversation_is_allowed(
    test_client: AsyncClient,
) -> None:
    """Subscribing (GET /stream) to a brand-new empty conversation is allowed:
    the always-on live-update stream attaches to the user's own freshly-created
    conversation before any message is sent, so it must not 404. With no running
    turn, a follow=false stream simply closes immediately (200)."""
    conversation_id = f"conv_empty_{uuid.uuid4().hex[:8]}"
    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )
    assert response.status_code == 200, response.text


async def test_stream_on_multi_owner_conversation_returns_404(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """The hub fans out every event to every subscriber, so a stream is only
    allowed for a conversation the caller *solely* owns. A multi-owner
    conversation (e.g. a Telegram group chat_id, which has several user authors)
    is refused with 404 — it can't be streamed through this hub without leaking
    co-owners' turns. The caller here (``test_user``) is one of two owners."""
    conversation_id = f"conv_multi_{uuid.uuid4().hex[:8]}"
    async with get_db_context(engine=db_engine) as ctx:
        await ctx.message_history.add_message(
            UserMessage(content="from the caller"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            user_id="test_user",
        )
        await ctx.message_history.add_message(
            UserMessage(content="from another group member"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            user_id="someone_else",
        )

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )
    assert response.status_code == 404, response.text


async def test_post_turn_is_idempotent_on_turn_id_retry(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """Codex finding #3: client-supplied turn_id makes POST safe to retry.

    A retried POST with the same turn_id returns the same identity payload
    and does NOT start a second producer or persist a second user message.
    """
    user_prompt = "Idempotency check"
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply="ok")

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_idem_{uuid.uuid4().hex[:8]}"

    first = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert first.status_code == 200, first.text

    second = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert second.status_code == 200, second.text
    assert first.json() == second.json()

    # Let the (single) producer finish so we can inspect persisted state.
    await asyncio.wait_for(
        collect_sse_stream(
            test_client,
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0},
        ),
        timeout=10.0,
    )

    async with get_db_context(engine=db_engine) as ctx:
        rows = await ctx.message_history.get_recent_with_metadata(
            interface_type="web",
            conversation_id=conversation_id,
            limit=20,
        )
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1, (
        f"Expected exactly one user message for the retried turn, got "
        f"{len(user_rows)}: {[r.get('content') for r in user_rows]}"
    )
    assert user_rows[0].get("turn_id") == turn_id


async def test_post_turn_idempotent_across_hub_restart(
    test_client: AsyncClient,
    app_fixture: FastAPI,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """A turn_id retried after a backend restart must not duplicate the turn.

    The hub is in-memory, so a restart wipes its turn registry. The DB is the
    durable record: the endpoint consults ``get_user_row_by_turn_id`` and
    short-circuits to the existing identity instead of persisting a second user
    message and starting a second producer.
    """
    user_prompt = "Restart idempotency check"
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply="ok")

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_restart_{uuid.uuid4().hex[:8]}"
    body = {
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "prompt": user_prompt,
        "interface_type": "web",
    }

    first = await test_client.post("/api/v1/chat/turns", json=body)
    assert first.status_code == 200, first.text

    # Let the first producer finish and persist the user message.
    await asyncio.wait_for(
        collect_sse_stream(
            test_client,
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0},
        ),
        timeout=10.0,
    )

    # Simulate a backend restart: the durable DB row survives, the in-memory
    # hub does not.
    fresh_hub = ConversationStreamHub()
    app_fixture.state.conversation_stream_hub = fresh_hub

    second = await test_client.post("/api/v1/chat/turns", json=body)
    assert second.status_code == 200, second.text
    assert second.json() == {
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "first_seq": 0,
        # The durable fallback signals the turn already finished and is NOT
        # replayable from the (fresh) hub, so clients reload history instead of
        # opening /stream.
        "already_complete": True,
        # The turn produced a reply ("ok"), so it is not incomplete.
        "incomplete": False,
    }

    # No producer was started on the fresh hub, and no duplicate row was
    # persisted.
    assert fresh_hub.get_active_producer_tasks(conversation_id) == []
    assert fresh_hub.get_turn(conversation_id, turn_id) is None

    async with get_db_context(engine=db_engine) as ctx:
        rows = await ctx.message_history.get_recent_with_metadata(
            interface_type="web",
            conversation_id=conversation_id,
            limit=20,
        )
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1, (
        f"Expected exactly one user message across the restart, got "
        f"{len(user_rows)}: {[r.get('content') for r in user_rows]}"
    )


async def test_post_turn_rejects_turn_id_from_another_conversation(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """The durable idempotency short-circuit must be scoped to the conversation.

    A turn_id that already exists in a DIFFERENT conversation must not be echoed
    back as this conversation's identity — the endpoint 404s instead of leaking
    the foreign turn. (turn_ids are UUIDs so this shouldn't happen in practice;
    this guards the defense-in-depth check.)
    """
    turn_id = str(uuid.uuid4())
    other_conversation = f"conv_a_{uuid.uuid4().hex[:8]}"
    target_conversation = f"conv_b_{uuid.uuid4().hex[:8]}"

    # The caller (test_user) already used this turn_id in another conversation.
    async with get_db_context(engine=db_engine) as ctx:
        await ctx.message_history.add_message(
            message=UserMessage(content="original turn"),
            interface_type="web",
            conversation_id=other_conversation,
            interface_message_id=f"temp_{turn_id}",
            turn_id=turn_id,
            thread_root_id=None,
            timestamp=datetime.now(UTC),
            user_id="test_user",
        )

    # Reusing it in a brand-new conversation passes the (allow_new) ownership
    # gate but must be rejected by the conversation-scoped idempotency check.
    response = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": target_conversation,
            "prompt": "reuse across conversations",
            "interface_type": "web",
        },
    )
    assert response.status_code == 404, response.text


async def test_410_during_active_turn_includes_active_turn_metadata(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex finding #6: 410 responses surface ``active_turns`` so the client
    knows the turn is still running and renders "still thinking" instead of
    "interrupted".

    Force the buffer cap to a tiny value so a few text deltas overflow it
    while the turn is still running, then try to resume from seq=0.
    """
    # Replace the hub with one that has a tiny buffer cap. Subscribe-first
    # so we don't race the test's POST.
    tiny_hub = ConversationStreamHub(buffer_max_events=2)
    app_fixture.state.conversation_stream_hub = tiny_hub

    user_prompt = "Eviction test"
    # The mock LLM emits content in chunks; with a tiny buffer of 2 events,
    # the early ones will fall off. Send enough text to overflow.
    llm_response = "Lots of text to fill the tiny buffer past its cap."
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply=llm_response)

    # Gate the LLM so the producer doesn't finish before we attempt to
    # resume — we want the turn to still be running for active_turns
    # to be populated.
    release = asyncio.Event()
    started = asyncio.Event()
    original_generate = mock_llm_client.generate_response

    async def gated_generate(*args: object, **kwargs: object) -> LLMOutput:
        started.set()
        await release.wait()
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mock_llm_client, "generate_response", gated_generate)

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_410_{uuid.uuid4().hex[:8]}"

    post_response = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post_response.status_code == 200, post_response.text

    # Wait until the producer is in flight, then publish a couple of
    # synthetic events to exceed the buffer cap. The simplest way is to
    # directly publish to the hub on behalf of the test (the producer is
    # blocked behind the LLM gate).
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await tiny_hub.publish(
        conversation_id, "text", turn_id=turn_id, payload={"content": "a"}
    )
    await tiny_hub.publish(
        conversation_id, "text", turn_id=turn_id, payload={"content": "b"}
    )
    await tiny_hub.publish(
        conversation_id, "text", turn_id=turn_id, payload={"content": "c"}
    )
    # Buffer now holds 2 events at seq >= 2; turn_started at seq 0 is gone.

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )
    assert response.status_code == 410, response.text
    body = response.json()
    assert body["reason"] == "out_of_buffer"
    active_turns = body.get("active_turns")
    assert active_turns, f"Expected active_turns in 410 body, got: {body}"
    assert any(t["turn_id"] == turn_id for t in active_turns), (
        f"Expected active turn {turn_id} in {active_turns}"
    )

    # Release the gate so the test doesn't leak a hanging task.
    release.set()
    # Drain any background producer left in the hub.
    pending = tiny_hub.get_active_producer_tasks(conversation_id)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_disconnect_without_ack_fires_push(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression-compatible behaviour: a client that disconnects without
    acknowledging turn_ended must still receive the push (preserves the
    existing PR #879 contract under the new endpoint shape)."""
    user_prompt = "Disconnect-no-ack"
    llm_response = "Reply delivered via push."
    conversation_id = f"conv_noack_{uuid.uuid4().hex[:8]}"
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply=llm_response)

    started = asyncio.Event()
    release = asyncio.Event()
    original_generate = mock_llm_client.generate_response

    async def gated_generate(*args: object, **kwargs: object) -> LLMOutput:
        started.set()
        await release.wait()
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mock_llm_client, "generate_response", gated_generate)

    spy = _SpyNotifier()
    app_fixture.state.web_chat_interface = WebChatInterface(db_engine, notifier=spy)

    turn_id = str(uuid.uuid4())
    post_response = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post_response.status_code == 200

    # Open a subscription, but never ack the turn_ended seq. Drop it before
    # turn_ended is delivered.
    subscribe_task = asyncio.create_task(
        test_client.get(
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    subscribe_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await subscribe_task

    release.set()

    # Disconnect push must fire.
    await asyncio.wait_for(spy.notified.wait(), timeout=10.0)
    assert spy.calls, "Expected disconnect push to fire when no ack received"
    _user, _title, body, metadata = spy.calls[0]
    assert llm_response in body
    assert metadata is not None
    assert metadata.category == MESSAGE_CATEGORY

    # Drain any background producer.
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    pending = hub.get_active_producer_tasks(conversation_id)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_subscriber_ack_suppresses_disconnect_push(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """A subscriber that fully consumes the stream through turn_ended (and
    therefore would ack at least that seq) should *not* see the disconnect
    push fire. The push is intended for the offline-recipient case only."""
    user_prompt = "Suppress push when acked"
    llm_response = "Reply observed by live subscriber."
    conversation_id = f"conv_ack_{uuid.uuid4().hex[:8]}"
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply=llm_response)

    spy = _SpyNotifier()
    app_fixture.state.web_chat_interface = WebChatInterface(db_engine, notifier=spy)

    turn_id = str(uuid.uuid4())
    post_response = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post_response.status_code == 200

    # Consume the full stream — this implicitly acks the turn_ended seq.
    events = await asyncio.wait_for(
        collect_sse_stream(
            test_client,
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0},
        ),
        timeout=10.0,
    )
    turn_ended = next(e for e in events if e["type"] == "turn_ended")
    last_seq = turn_ended["data"]["seq"]

    # Ack the highest observed seq so push suppression has a chance to fire.
    # (M0 servers will accept ack_seq on the subscribe URL or via a dedicated
    # endpoint; here we send a fresh subscribe with ack_seq before the
    # background producer finalizes the push decision.)
    ack_response = await test_client.post(
        "/api/v1/chat/ack",
        json={
            "conversation_id": conversation_id,
            "ack_seq": last_seq,
        },
    )
    assert ack_response.status_code in {200, 204}, ack_response.text

    # Let the background producer run to completion so it has made its
    # push-suppression decision (it waits briefly for an ack after turn_ended,
    # which we already delivered by consuming the stream).
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    producer_tasks = hub.get_active_producer_tasks(conversation_id)
    if producer_tasks:
        await asyncio.gather(*producer_tasks, return_exceptions=True)

    # No push should have fired.
    assert not spy.calls, (
        f"Expected no disconnect push when client acked turn_ended; got {spy.calls}"
    )


async def test_web_chat_interface_publishes_live_update_to_hub(
    db_engine: AsyncEngine,
) -> None:
    """WebChatInterface.send_message (scheduled callbacks / task-worker flows)
    publishes a ``message`` event to the hub so an open follow-stream reloads —
    these messages never go through the /turns producer."""
    async with get_db_context(engine=db_engine) as ctx:
        await init_db(db_engine)
        await ctx.init_vector_db()

    hub = ConversationStreamHub()
    interface = WebChatInterface(db_engine, stream_hub=hub)
    conversation_id = f"conv_oob_{uuid.uuid4().hex[:8]}"

    # A follow-style tail subscriber attached before the out-of-band message.
    handle = await hub.subscribe(conversation_id, from_seq=-1)

    saved = await interface.send_message(
        conversation_id=conversation_id,
        text="Reply from a scheduled callback",
    )
    assert saved is not None

    event = await asyncio.wait_for(handle.queue.get(), timeout=2.0)
    assert event.type == "message"
    # The payload must carry conversation_id so the web client can route the
    # reload to the right open conversation.
    assert event.payload["conversation_id"] == conversation_id
    hub.unsubscribe(conversation_id, handle.queue)


# --------------------------------------------------------------------------- #
# Queue-drain race (finding #1)
# --------------------------------------------------------------------------- #


class _EndOnSubscribeHub(ConversationStreamHub):
    """Hub that ends the registered turn the instant a subscriber attaches.

    This deterministically reproduces the snapshot/queue race (finding #1): the
    new subscriber's queue is registered before ``subscribe`` returns, so the
    ``end_turn`` here lands its tail + ``turn_ended`` ONLY in the live queue (the
    replay snapshot was already taken). A correct generator must drain that queue
    before its non-follow early return, or those events are silently dropped.
    """

    def __init__(self, *, end_turn_id: str) -> None:
        super().__init__()
        self._end_turn_id = end_turn_id
        self._ended = False

    async def subscribe(
        self,
        conversation_id: str,
        *,
        from_seq: int,
        ack_seq: int = -1,
    ) -> SubscriptionHandle:
        handle = await super().subscribe(
            conversation_id, from_seq=from_seq, ack_seq=ack_seq
        )
        if not self._ended:
            self._ended = True
            await self.publish(
                conversation_id,
                "text",
                turn_id=self._end_turn_id,
                payload={"content": "tail-after-snapshot"},
            )
            await self.end_turn(
                conversation_id, turn_id=self._end_turn_id, status="complete"
            )
        return handle


async def test_stream_drains_queue_before_non_follow_close(
    app_fixture: FastAPI,
    test_client: AsyncClient,
) -> None:
    """Finding #1: a turn that ends in the snapshot/check window leaves its tail
    and ``turn_ended`` only in the live queue. A non-follow stream must drain the
    queue before closing, or the client sees a truncated reply and never sees
    turn_ended (thinking it completed cleanly)."""
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_drain_{uuid.uuid4().hex[:8]}"

    hub = _EndOnSubscribeHub(end_turn_id=turn_id)
    app_fixture.state.conversation_stream_hub = hub
    await hub.start_turn(
        conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        started_at=datetime.now(UTC),
    )

    events = await asyncio.wait_for(
        collect_sse_stream(
            test_client,
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0},
        ),
        timeout=10.0,
    )
    types = [e["type"] for e in events]
    assert "turn_ended" in types, f"turn_ended dropped by early return: {types}"
    text_chunks = [e["data"]["content"] for e in events if e["type"] == "text"]
    assert "tail-after-snapshot" in text_chunks, (
        f"reply tail dropped by early return: {types}"
    )


# --------------------------------------------------------------------------- #
# Graceful shutdown (finding #6)
# --------------------------------------------------------------------------- #


async def test_follow_stream_closes_on_shutdown(
    app_fixture: FastAPI,
    test_client: AsyncClient,
) -> None:
    """Finding #6: a follow=true stream heartbeats forever unless it observes the
    app shutdown event. When shutdown is signalled it must emit a
    ``stream_dropped`` frame (reason=server_shutdown) and close promptly so a
    graceful SIGTERM is not blocked."""
    # Signal shutdown before subscribing so the follow stream observes it on its
    # first loop and closes immediately. (With ASGITransport the stream's first
    # body chunk is what unblocks the client's stream context, so the
    # stream_dropped frame must be the first thing the generator yields here.)
    shutdown_event = asyncio.Event()
    shutdown_event.set()
    app_fixture.state.shutdown_event = shutdown_event
    conversation_id = f"conv_shutdown_{uuid.uuid4().hex[:8]}"

    events: list[SSEEvent] = []
    async with test_client.stream(
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0, "follow": "true"},
    ) as response:
        assert response.status_code == 200, response.text
        async for chunk in response.aiter_text():
            for line in chunk.split("\n"):
                if line.startswith("event:"):
                    events.append(SSEEvent(type=line.split(":", 1)[1].strip(), data={}))
            if any(e["type"] == "stream_dropped" for e in events):
                break

    assert any(e["type"] == "stream_dropped" for e in events), (
        f"expected stream_dropped on shutdown, got {[e['type'] for e in events]}"
    )


# --------------------------------------------------------------------------- #
# event_types filter (spec §B / finding C3)
# --------------------------------------------------------------------------- #


async def test_event_types_filter_drops_text_keeps_lifecycle(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """spec §B: ``event_types=message`` filters out the token firehose (``text``)
    but ALWAYS keeps lifecycle frames (``turn_started`` is filtered, but
    ``turn_ended`` is always emitted so the client knows when to stop)."""
    user_prompt = "Filter test"
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply="hello")

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_filter_{uuid.uuid4().hex[:8]}"
    post = await test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post.status_code == 200, post.text

    events = await asyncio.wait_for(
        collect_sse_stream(
            test_client,
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": 0, "event_types": "message"},
        ),
        timeout=10.0,
    )
    types = [e["type"] for e in events]
    assert "text" not in types, f"event_types filter let text through: {types}"
    assert "turn_started" not in types, (
        f"event_types filter should drop turn_started: {types}"
    )
    assert "turn_ended" in types, (
        f"turn_ended must always be emitted regardless of filter: {types}"
    )


# --------------------------------------------------------------------------- #
# /send_message turn_id idempotency (finding #8)
# --------------------------------------------------------------------------- #


async def test_send_message_idempotent_on_turn_id(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """Finding #8: /send_message is idempotent on a client-supplied turn_id. A
    retried request returns the already-persisted reply (already_complete=true)
    instead of re-driving the LLM and double-persisting the user message."""
    user_prompt = "Send-message idempotency"
    _add_simple_llm_rule(mock_llm_client, prompt_marker=user_prompt, reply="reply-once")

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_sm_idem_{uuid.uuid4().hex[:8]}"
    body = {
        "prompt": user_prompt,
        "conversation_id": conversation_id,
        "interface_type": "api",
        "turn_id": turn_id,
    }

    first = await test_client.post("/api/v1/chat/send_message", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["already_complete"] is False
    assert first.json()["turn_id"] == turn_id
    assert first.json()["reply"] == "reply-once"

    second = await test_client.post("/api/v1/chat/send_message", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["already_complete"] is True
    assert second.json()["turn_id"] == turn_id
    assert second.json()["reply"] == "reply-once"

    async with get_db_context(engine=db_engine) as ctx:
        rows = await ctx.message_history.get_recent_with_metadata(
            interface_type="api",
            conversation_id=conversation_id,
            limit=20,
        )
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1, (
        f"retried /send_message double-persisted the user message: "
        f"{[r.get('content') for r in user_rows]}"
    )
    assert user_rows[0].get("turn_id") == turn_id


# --------------------------------------------------------------------------- #
# Conversation-list ownership filter + identity mapping (findings #4, #5)
# --------------------------------------------------------------------------- #


async def test_conversation_list_filters_to_owned(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Findings #4/#5: GET /conversations is filtered to conversations the caller
    solely (canonically) owns, so the History UI never lists a conversation it
    then 404s on opening. A conversation owned only by another user is hidden; a
    multi-owner conversation (which /stream refuses) is also hidden."""
    owned = f"conv_owned_{uuid.uuid4().hex[:8]}"
    foreign = f"conv_foreign_{uuid.uuid4().hex[:8]}"
    multi = f"conv_multi_{uuid.uuid4().hex[:8]}"

    async with get_db_context(engine=db_engine) as ctx:
        await init_db(db_engine)
        await ctx.init_vector_db()
        await ctx.message_history.add_message(
            UserMessage(content="mine"),
            interface_type="web",
            conversation_id=owned,
            timestamp=datetime.now(UTC),
            user_id="test_user",
        )
        await ctx.message_history.add_message(
            UserMessage(content="theirs"),
            interface_type="web",
            conversation_id=foreign,
            timestamp=datetime.now(UTC),
            user_id="someone_else",
        )
        await ctx.message_history.add_message(
            UserMessage(content="mine in group"),
            interface_type="web",
            conversation_id=multi,
            timestamp=datetime.now(UTC),
            user_id="test_user",
        )
        await ctx.message_history.add_message(
            UserMessage(content="theirs in group"),
            interface_type="web",
            conversation_id=multi,
            timestamp=datetime.now(UTC),
            user_id="someone_else",
        )

    response = await test_client.get("/api/v1/chat/conversations")
    assert response.status_code == 200, response.text
    listed = {c["conversation_id"] for c in response.json()["conversations"]}
    assert owned in listed
    assert foreign not in listed
    assert multi not in listed


async def test_conversation_list_identity_maps_telegram_owner(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Findings #4/#5: ownership is identity-aware. A conversation stored under a
    user's Telegram id must canonicalize to their web identity so it stays
    visible in the list and openable, not 404'd as someone else's. Here a real
    resolver maps Telegram id 4242 -> the canonical user ``test_user``."""
    resolver_config = AppConfig(
        users=[
            UserIdentityConfig(
                id="test_user",
                telegram=TelegramUserIdentityConfig(user_ids={4242}),
            )
        ]
    )
    app_fixture.state.user_identity_resolver = UserIdentityResolver(resolver_config)

    telegram_conv = f"conv_tg_{uuid.uuid4().hex[:8]}"
    async with get_db_context(engine=db_engine) as ctx:
        await init_db(db_engine)
        await ctx.init_vector_db()
        # Stored under the raw Telegram numeric id, which canonicalizes to
        # test_user via the resolver.
        await ctx.message_history.add_message(
            UserMessage(content="from telegram"),
            interface_type="telegram",
            conversation_id=telegram_conv,
            timestamp=datetime.now(UTC),
            user_id="4242",
        )

    list_response = await test_client.get("/api/v1/chat/conversations")
    assert list_response.status_code == 200, list_response.text
    listed = {c["conversation_id"] for c in list_response.json()["conversations"]}
    assert telegram_conv in listed, (
        "Telegram-owned conversation should canonicalize to the web identity "
        "and stay visible"
    )

    # And it must be openable (not 404) through the identity-aware stream check.
    stream_response = await test_client.get(
        f"/api/v1/chat/conversations/{telegram_conv}/stream",
        params={"from_seq": 0},
    )
    assert stream_response.status_code == 200, stream_response.text


# --------------------------------------------------------------------------- #
# Pagination-ownership correctness (M4 / finding §2.2)
# --------------------------------------------------------------------------- #


async def _seed_owned_conversations(
    db_engine: AsyncEngine,
    *,
    owner_id: str,
    count: int,
    prefix: str,
) -> list[str]:
    """Persist ``count`` single-owner conversations and return their ids in the
    order they were written (oldest first)."""
    conversation_ids: list[str] = []
    base = datetime.now(UTC)
    async with get_db_context(engine=db_engine) as ctx:
        await init_db(db_engine)
        await ctx.init_vector_db()
        for index in range(count):
            conversation_id = f"{prefix}_{index}_{uuid.uuid4().hex[:8]}"
            await ctx.message_history.add_message(
                UserMessage(content=f"message {index}"),
                interface_type="web",
                conversation_id=conversation_id,
                # Distinct increasing timestamps give a deterministic order.
                timestamp=base + timedelta(seconds=index),
                user_id=owner_id,
            )
            conversation_ids.append(conversation_id)
    return conversation_ids


async def _page_all_conversations(
    test_client: AsyncClient, *, page_size: int
) -> tuple[list[str], list[int], int]:
    """Page through GET /conversations to exhaustion.

    A client stops when a page returns fewer than ``page_size`` rows (the last
    page), mirroring the iOS pager. Returns the accumulated conversation ids,
    the per-page row counts, and the reported ``count`` (which must be stable
    and ownership-filtered on every page)."""
    collected: list[str] = []
    page_sizes: list[int] = []
    reported_count = -1
    offset = 0
    while True:
        response = await test_client.get(
            "/api/v1/chat/conversations",
            params={"limit": page_size, "offset": offset},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        reported_count = body["count"]
        page = [c["conversation_id"] for c in body["conversations"]]
        page_sizes.append(len(page))
        collected.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return collected, page_sizes, reported_count


async def test_conversation_list_count_is_ownership_filtered(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """The reported ``count`` reflects only conversations the caller owns, not the
    unfiltered table total. With foreign conversations interleaved among owned
    ones, ``count`` must equal the number of owned conversations so the client's
    pager terminates correctly instead of chasing pages that never materialize."""
    owned = await _seed_owned_conversations(
        db_engine, owner_id="test_user", count=3, prefix="conv_owned"
    )
    # Interleave conversations owned solely by another user.
    async with get_db_context(engine=db_engine) as ctx:
        for index in range(4):
            await ctx.message_history.add_message(
                UserMessage(content="theirs"),
                interface_type="web",
                conversation_id=f"conv_foreign_{index}_{uuid.uuid4().hex[:8]}",
                timestamp=datetime.now(UTC) + timedelta(seconds=100 + index),
                user_id="someone_else",
            )

    response = await test_client.get("/api/v1/chat/conversations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == len(owned)
    listed = {c["conversation_id"] for c in body["conversations"]}
    assert listed == set(owned)


async def test_conversation_list_pagination_has_no_empty_nonfinal_pages(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Paging through an interleaved owned/foreign dataset yields full non-final
    pages and returns every owned conversation exactly once. A Python post-filter
    would let a page come back short (or empty) even though owned conversations
    remain on later pages — a client that stops on a short page would then miss
    them. With DB-level filtering every non-final page is full."""
    owned = await _seed_owned_conversations(
        db_engine, owner_id="test_user", count=5, prefix="conv_mine"
    )
    # Heavily interleave foreign conversations so a naive post-filter would
    # produce short/empty pages.
    async with get_db_context(engine=db_engine) as ctx:
        for index in range(10):
            await ctx.message_history.add_message(
                UserMessage(content="theirs"),
                interface_type="web",
                conversation_id=f"conv_other_{index}_{uuid.uuid4().hex[:8]}",
                timestamp=datetime.now(UTC) + timedelta(seconds=200 + index),
                user_id="someone_else",
            )

    collected, page_sizes, reported_count = await _page_all_conversations(
        test_client, page_size=2
    )

    assert reported_count == len(owned)
    # Every owned conversation appears exactly once, none is lost to a short page.
    assert sorted(collected) == sorted(owned)
    assert len(collected) == len(set(collected))
    # Only the final page may be short; every earlier page is full.
    for size in page_sizes[:-1]:
        assert size == 2, f"non-final page was short: {page_sizes}"


async def test_conversation_list_multi_owner_excluded_from_count(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A conversation with a co-owner (which /stream refuses) is excluded from
    both the page and the ownership-filtered ``count`` — the DB-level NOT EXISTS
    disqualifies any conversation carrying a foreign owner id, keeping the list
    and the count consistent with what the caller can actually open."""
    owned = await _seed_owned_conversations(
        db_engine, owner_id="test_user", count=2, prefix="conv_solo"
    )
    multi = f"conv_shared_{uuid.uuid4().hex[:8]}"
    async with get_db_context(engine=db_engine) as ctx:
        await ctx.message_history.add_message(
            UserMessage(content="mine in group"),
            interface_type="web",
            conversation_id=multi,
            timestamp=datetime.now(UTC) + timedelta(seconds=300),
            user_id="test_user",
        )
        await ctx.message_history.add_message(
            UserMessage(content="theirs in group"),
            interface_type="web",
            conversation_id=multi,
            timestamp=datetime.now(UTC) + timedelta(seconds=301),
            user_id="someone_else",
        )

    response = await test_client.get("/api/v1/chat/conversations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == len(owned)
    listed = {c["conversation_id"] for c in body["conversations"]}
    assert listed == set(owned)
    assert multi not in listed


# --------------------------------------------------------------------------- #
# active_turns wire-shape contract (M4)
# --------------------------------------------------------------------------- #


async def test_active_turns_contract_shape_in_messages(
    app_fixture: FastAPI,
    test_client: AsyncClient,
) -> None:
    """Pin the exact ``active_turns`` wire shape surfaced by GET /messages so the
    iOS ``ChatConversationMessagesResponse`` decoder stays in sync: each entry has
    ``turn_id`` (str), ``started_at`` (ISO-8601 timestamp), ``latest_seq`` (int)
    and ``status`` (str), and nothing else."""
    conversation_id = f"conv_turns_{uuid.uuid4().hex[:8]}"
    turn_id = str(uuid.uuid4())
    started_at = datetime(2026, 7, 21, 12, 34, 56, tzinfo=UTC)

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await hub.start_turn(
        conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        started_at=started_at,
    )
    # Publish an event so latest_seq advances past the turn_started seq.
    await hub.publish(
        conversation_id, "text", turn_id=turn_id, payload={"content": "hi"}
    )

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        params={"limit": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    active_turns = body["active_turns"]
    assert len(active_turns) == 1, active_turns
    turn = active_turns[0]

    assert set(turn.keys()) == {"turn_id", "started_at", "latest_seq", "status"}
    assert turn["turn_id"] == turn_id
    assert isinstance(turn["turn_id"], str)
    assert isinstance(turn["latest_seq"], int)
    assert turn["latest_seq"] >= 1
    assert turn["status"] == "running"
    assert isinstance(turn["status"], str)
    # started_at is an ISO-8601 timestamp that round-trips to the seeded value.
    parsed = datetime.fromisoformat(turn["started_at"])
    assert parsed == started_at


# --------------------------------------------------------------------------- #
# Activity stream over HTTP (M4)
# --------------------------------------------------------------------------- #


async def test_activity_stream_delivers_ping_then_disconnects_cleanly(
    app_fixture: FastAPI,
    test_client: AsyncClient,
) -> None:
    """HTTP-level activity-stream test: connect, receive the advisory
    ``conversation_activity`` ping published for the caller, then disconnect
    cleanly (the generator's finally-block unsubscribes so the hub is left with
    no dangling activity subscriber).

    ``httpx``'s ``ASGITransport`` buffers the whole response, so the stream must
    close before any frame is observable. A background task publishes the ping
    once the subscriber attaches (proving subscribe-then-publish ordering with no
    lost wakeup), then signals shutdown so the generator emits its terminal
    ``stream_dropped`` and returns. The buffered body then carries the ping."""
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    shutdown_event = asyncio.Event()
    app_fixture.state.shutdown_event = shutdown_event
    conversation_id = f"conv_activity_{uuid.uuid4().hex[:8]}"

    async def publish_then_shutdown() -> None:
        await wait_for_condition(
            lambda: hub.has_activity_subscribers("test_user"),
            timeout=5.0,
            description="activity subscriber attaches",
        )
        await hub.publish_activity(
            conversation_id, user_id="test_user", reason="turn_started"
        )
        shutdown_event.set()

    publisher = asyncio.ensure_future(publish_then_shutdown())

    received: list[SSEEvent] = []
    try:
        async with test_client.stream(
            "GET", "/api/v1/chat/activity/stream"
        ) as response:
            assert response.status_code == 200, response.text
            current_type: str | None = None
            async for chunk in response.aiter_text():
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        current_type = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and current_type is not None:
                        data_str = line.split(":", 1)[1].strip()
                        if not data_str:
                            continue
                        received.append(
                            SSEEvent(type=current_type, data=json.loads(data_str))
                        )
                        current_type = None
                if any(e["type"] == "stream_dropped" for e in received):
                    break
    finally:
        await asyncio.wait_for(publisher, timeout=5.0)

    # The advisory ping arrived with the compact payload shape.
    activity = next(e for e in received if e["type"] == "conversation_activity")
    assert activity["data"]["conversation_id"] == conversation_id
    assert activity["data"]["reason"] == "turn_started"
    datetime.fromisoformat(activity["data"]["timestamp"])

    # Clean disconnect: the generator's finally-block unsubscribed the queue.
    await wait_for_condition(
        lambda: not hub.has_activity_subscribers("test_user"),
        timeout=5.0,
        description="activity subscriber unsubscribes on disconnect",
    )


# --------------------------------------------------------------------------- #
# Heartbeat emission with an injectable interval (M4)
# --------------------------------------------------------------------------- #


_HEARTBEAT_INTERVAL_SECONDS = 0.02


async def _assert_heartbeat_then_shutdown(
    test_client: AsyncClient,
    shutdown_event: asyncio.Event,
    subscribed: Callable[[], bool],
    *,
    url: str,
    # ast-grep-ignore: no-dict-any - passthrough of httpx query params (int/str), matching stream() signature
    params: dict[str, Any] | None = None,
) -> None:
    """Assert an always-on SSE stream emits ``heartbeat`` frames on the injected
    interval.

    ``httpx``'s ``ASGITransport`` buffers the whole response and only yields the
    body once the app finishes, so an infinite stream must close itself before
    any frame is observable. A background task waits for the subscriber to
    attach, lets several heartbeat intervals elapse so the generator buffers at
    least one ``heartbeat``, then signals shutdown; the generator then emits a
    terminal ``stream_dropped`` and returns. The buffered body therefore contains
    the heartbeat frames followed by ``stream_dropped``."""

    async def drive_shutdown() -> None:
        await wait_for_condition(
            subscribed,
            timeout=5.0,
            interval=_HEARTBEAT_INTERVAL_SECONDS,
            description="stream subscriber attaches",
        )
        # Let several heartbeat intervals elapse so the SSE generator buffers at
        # least one heartbeat frame before we ask it to close. The heartbeat
        # cadence is a server-side timer with no client-observable signal until
        # the stream closes (httpx ASGITransport buffers the body), so there is
        # no condition to wait on — this is a deterministic multiple of the
        # injected interval, not a flaky "hope it's done" wait.
        remaining_intervals = 5
        while remaining_intervals > 0:
            # ast-grep-ignore: no-asyncio-sleep-in-tests - deterministic wait for N server-side heartbeat ticks; no client-observable condition exists before the stream closes
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            remaining_intervals -= 1
        shutdown_event.set()

    shutdown_driver = asyncio.ensure_future(drive_shutdown())

    seen: list[str] = []
    try:
        async with test_client.stream("GET", url, params=params) as response:
            assert response.status_code == 200, response.text
            async for chunk in response.aiter_text():
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        seen.append(line.split(":", 1)[1].strip())
                if "stream_dropped" in seen:
                    break
    finally:
        await asyncio.wait_for(shutdown_driver, timeout=5.0)

    assert "heartbeat" in seen, f"no heartbeat frame emitted: {seen}"


async def test_conversation_stream_emits_heartbeat_with_injected_interval(
    app_fixture: FastAPI,
    test_client: AsyncClient,
) -> None:
    """The conversation stream emits ``event: heartbeat`` on the injectable
    interval. Driving the interval down to a few milliseconds makes the heartbeat
    path fire deterministically without waiting the production 30s cadence."""
    app_fixture.state.stream_heartbeat_interval_seconds = _HEARTBEAT_INTERVAL_SECONDS
    shutdown_event = asyncio.Event()
    app_fixture.state.shutdown_event = shutdown_event
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    conversation_id = f"conv_hb_{uuid.uuid4().hex[:8]}"

    await _assert_heartbeat_then_shutdown(
        test_client,
        shutdown_event,
        lambda: hub.subscriber_count(conversation_id) > 0,
        url=f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0, "follow": "true"},
    )


async def test_activity_stream_emits_heartbeat_with_injected_interval(
    app_fixture: FastAPI,
    test_client: AsyncClient,
) -> None:
    """The account-global activity stream also emits ``event: heartbeat`` on the
    injectable interval, so an idle activity connection is kept alive rather than
    being torn down by an idle-timeout front door."""
    app_fixture.state.stream_heartbeat_interval_seconds = _HEARTBEAT_INTERVAL_SECONDS
    shutdown_event = asyncio.Event()
    app_fixture.state.shutdown_event = shutdown_event
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub

    await _assert_heartbeat_then_shutdown(
        test_client,
        shutdown_event,
        lambda: hub.has_activity_subscribers("test_user"),
        url="/api/v1/chat/activity/stream",
    )
