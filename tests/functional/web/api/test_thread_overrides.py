"""The user's own thread actions: New thread, Continue in a thread, Move to thread.

See docs/design/one-thread-chat-ux.md, "Starting a thread" and "Moving content
across the boundary".
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import select, update

from family_assistant.config_models import AppConfig, ConversationThreadsConfig
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing import ProcessingService
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.user_identity import UserIdentityResolver
from family_assistant.storage import delegation_runs_table, message_history_table
from family_assistant.storage.database import Database
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.dependencies import get_current_user
from tests.mocks.mock_llm import LLMOutput, MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.security.taint import TaintMetadata

# Anchored to the real clock: history windows are read against it.
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
CALLER = "test_user"
ALIAS = "123456789"
CONTEXT_PROFILE = "complex_tasks"
SEALED_PROFILE = "browser_profile"
UNTRUSTED: TaintMetadata = cast(
    "TaintMetadata", {"max_tier": "unknown_external", "sources": []}
)


@dataclass
class Profiles:
    """The models behind each profile, and what each one was shown."""

    seen: dict[str, list[list[LLMMessage]]]

    def saw(self, profile_id: str, text: str) -> bool:
        return any(
            text in str(message.content)
            for call in self.seen[profile_id]
            for message in call
        )


def _recording(seen: list[list[LLMMessage]], reply: str) -> RuleBasedMockLLMClient:
    def answer(kwargs: MatcherArgs) -> LLMOutput:
        seen.append(list(kwargs.get("messages", [])))
        return LLMOutput(content=reply)

    return RuleBasedMockLLMClient(rules=[(lambda _kwargs: True, answer)])


@pytest.fixture
def profiles(
    app_fixture: FastAPI, api_test_processing_service: ProcessingService
) -> Profiles:
    """Threads enabled, a Home profile, and one thread profile of each kind.

    ``complex_tasks`` receives household context; ``browser_profile`` does not.
    """
    app_fixture.state.config = AppConfig(
        database_url=app_fixture.state.config.database_url,
        conversation_threads=ConversationThreadsConfig(enabled=True),
    )
    seen: dict[str, list[list[LLMMessage]]] = {}

    def service(profile_id: str, *, household: bool) -> ProcessingService:
        seen[profile_id] = []
        return ProcessingService(
            llm_client=_recording(seen[profile_id], f"{profile_id} answered"),
            tools_provider=api_test_processing_service.tools_provider,
            service_config=replace(
                api_test_processing_service.service_config,
                id=profile_id,
                receives_household_context=household,
            ),
            context_providers=[],
            server_url="http://testserver",
            app_config=api_test_processing_service.app_config,
        )

    home_service = service(
        api_test_processing_service.service_config.id, household=True
    )
    app_fixture.state.processing_service = home_service
    app_fixture.state.processing_services = {
        home_service.service_config.id: home_service,
        CONTEXT_PROFILE: service(CONTEXT_PROFILE, household=True),
        SEALED_PROFILE: service(SEALED_PROFILE, household=False),
    }
    return Profiles(seen=seen)


async def _home(client: AsyncClient) -> str:
    return (await client.get("/api/v1/chat/home")).json()["conversation_id"]


async def _home_turn(
    db: Database,
    home: str,
    *,
    words: str,
    at: datetime,
    reply_taint: TaintMetadata | None = None,
    user_id: str = CALLER,
) -> dict[str, int]:
    """A finished Home turn that called a tool: its rows' ids by role."""
    reply_taint = reply_taint or TurnTaintState.empty().to_metadata()
    turn_id = str(uuid.uuid4())

    async def add(message: LLMMessage, at: datetime, user: str | None = None) -> int:
        return await db.message_history.add_message(
            message,
            interface_type="web",
            conversation_id=home,
            turn_id=turn_id,
            processing_profile_id="chat_api_test_profile",
            timestamp=at,
            user_id=user,
        )

    call_id = f"call_{turn_id[:8]}"
    ids = {
        "user": await add(UserMessage.from_trusted_user(content=words), at, user_id),
        "call": await add(
            AssistantMessage(
                content=None,
                taint_metadata=TurnTaintState.empty().to_metadata(),
                tool_calls=[
                    ToolCallItem(
                        id=call_id,
                        type="function",
                        function=ToolCallFunction(name="look_up", arguments="{}"),
                    )
                ],
            ),
            at + timedelta(seconds=1),
        ),
        "tool": await add(
            ToolMessage(
                tool_call_id=call_id,
                name="look_up",
                content=f"tool output for {words}",
                taint_metadata=reply_taint,
            ),
            at + timedelta(seconds=2),
        ),
        "reply": await add(
            AssistantMessage(content=f"reply to {words}", taint_metadata=reply_taint),
            at + timedelta(seconds=3),
        ),
    }
    return ids


async def _rows(db: Database, conversation_id: str) -> list[dict[str, object]]:
    rows = await db.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == conversation_id)
        .order_by(
            message_history_table.c.timestamp, message_history_table.c.internal_id
        )
    )
    return [dict(row) for row in rows]


async def _start(client: AsyncClient, **body: object) -> Any:  # noqa: ANN401 - JSON body
    response = await client.post("/api/v1/chat/home/threads", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _as(app_fixture: FastAPI, user_id: str) -> None:
    async def user() -> dict[str, object]:
        return {"user_identifier": user_id}

    app_fixture.dependency_overrides[get_current_user] = user


# ---------------------------------------------------------------------------
# New thread / send-as-thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_new_thread_runs_its_first_turn_on_the_chosen_profile(
    api_test_client: AsyncClient, db_engine: AsyncEngine, profiles: Profiles
) -> None:
    turn_id = str(uuid.uuid4())

    body = await _start(
        api_test_client,
        profile_id=SEALED_PROFILE,
        prompt="Find me three plumbers.",
        turn_id=turn_id,
    )

    thread = body["thread"]
    turn = body["turn"]
    conversation_id = thread["conversation_id"]
    await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": turn["first_seq"]},
    )
    rows = await _rows(Database(engine=db_engine), conversation_id)
    assert [row["content"] for row in rows if row["role"] == "assistant"] == [
        f"{SEALED_PROFILE} answered"
    ]
    assert (thread["profile_id"], thread["title"], turn["conversation_id"]) == (
        SEALED_PROFILE,
        "Find me three plumbers.",
        conversation_id,
    )
    assert profiles.saw(SEALED_PROFILE, "Find me three plumbers.")
    # The opening message is the user's own words: a visible user row.
    [opening] = [
        row
        for row in await _rows(Database(engine=db_engine), conversation_id)
        if row["role"] == "user"
    ]
    assert (
        opening["content"],
        opening["is_internal"],
        opening["user_id"],
        opening["turn_id"],
        opening["processing_profile_id"],
    ) == ("Find me three plumbers.", False, CALLER, turn_id, SEALED_PROFILE)


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_new_thread_hangs_off_the_callers_home(
    api_test_client: AsyncClient,
) -> None:
    home = await _home(api_test_client)
    body = await _start(
        api_test_client,
        profile_id=CONTEXT_PROFILE,
        prompt="Plan the garden beds",
        title="Garden",
    )

    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()

    thread = body["thread"]
    assert (thread["home_conversation_id"], thread["title"]) == (home, "Garden")
    assert [entry["conversation_id"] for entry in listed["threads"]] == [
        thread["conversation_id"]
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_retrying_a_start_returns_the_thread_it_started(
    api_test_client: AsyncClient,
) -> None:
    turn_id = str(uuid.uuid4())
    first = await _start(
        api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan", turn_id=turn_id
    )
    first_thread = cast("dict[str, object]", first["thread"])
    await api_test_client.get(
        f"/api/v1/chat/conversations/{first_thread['conversation_id']}/stream",
        params={"from_seq": 0},
    )

    second = await _start(
        api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan", turn_id=turn_id
    )

    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    second_thread = cast("dict[str, object]", second["thread"])
    assert second_thread["conversation_id"] == first_thread["conversation_id"]
    assert len(listed["threads"]) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_start_whose_first_turn_is_refused_leaves_no_thread(
    api_test_client: AsyncClient,
) -> None:
    response = await api_test_client.post(
        "/api/v1/chat/home/threads",
        json={
            "profile_id": CONTEXT_PROFILE,
            "prompt": "See attached",
            "attachments": [{"type": "image", "content": "", "mime_type": "image/png"}],
        },
    )

    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    assert response.status_code == 400
    assert listed["threads"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_starting_a_thread_leaves_a_marker_in_home(
    api_test_client: AsyncClient,
) -> None:
    home = await _home(api_test_client)
    body = await _start(api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan")

    messages = (
        await api_test_client.get(f"/api/v1/chat/conversations/{home}/messages")
    ).json()["messages"]

    [marker] = messages
    assert marker["started_thread"] == body["thread"]["conversation_id"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_retrying_a_start_leaves_one_marker(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    home = await _home(api_test_client)
    turn_id = str(uuid.uuid4())
    for _attempt in range(2):
        body = await _start(
            api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan", turn_id=turn_id
        )
        await api_test_client.get(
            f"/api/v1/chat/conversations/{body['thread']['conversation_id']}/stream",
            params={"from_seq": 0},
        )

    rows = await _rows(Database(engine=db_engine), home)

    assert len(rows) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_refused_start_leaves_no_marker(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    home = await _home(api_test_client)

    await api_test_client.post(
        "/api/v1/chat/home/threads",
        json={
            "profile_id": CONTEXT_PROFILE,
            "prompt": "See attached",
            "attachments": [{"type": "image", "content": "", "mime_type": "image/png"}],
        },
    )

    assert await _rows(Database(engine=db_engine), home) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_marker_is_not_unread_in_home(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    home = await _home(api_test_client)
    await _start(api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan")

    unread = await Database(engine=db_engine).conversation_threads.unread_counts(
        [home], viewer_user_id=CALLER, viewer_keys=[CALLER]
    )

    assert unread == {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_marker_stays_out_of_the_model_history_window(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
    api_test_processing_service: ProcessingService,
) -> None:
    home = await _home(api_test_client)
    await _start(api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan")

    window = await Database(engine=db_engine).message_history.get_recent(
        interface_type="web",
        conversation_id=home,
        processing_profile_id=api_test_processing_service.service_config.id,
    )

    assert window == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_marker_is_not_what_a_conversation_last_said(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    await _home_turn(db, home, words="the weather", at=NOW)
    await _start(api_test_client, profile_id=CONTEXT_PROFILE, prompt="Plan")

    listed = (await api_test_client.get("/api/v1/chat/conversations")).json()

    [entry] = [
        conversation
        for conversation in listed["conversations"]
        if conversation["conversation_id"] == home
    ]
    assert entry["last_message"] == "reply to the weather"


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_new_thread_needs_an_opening_message(
    api_test_client: AsyncClient,
) -> None:
    response = await api_test_client.post(
        "/api/v1/chat/home/threads", json={"profile_id": CONTEXT_PROFILE}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Continue in a thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_continuing_copies_the_anchor_and_the_turns_before_it(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    turns = [
        await _home_turn(
            db,
            home,
            words=f"question {index}",
            at=NOW + timedelta(seconds=10 * index),
            reply_taint=UNTRUSTED if index == 5 else None,
        )
        for index in range(7)
    ]
    anchor = turns[5]["reply"]

    body = await _start(
        api_test_client,
        profile_id=CONTEXT_PROFILE,
        anchor_message_id=anchor,
        title="Questions",
    )

    thread = body["thread"]
    copied = await _rows(db, thread["conversation_id"])
    # The anchor's turn and the four before it; nothing older, nothing after.
    assert [row["content"] for row in copied if row["role"] == "user"] == [
        f"question {index}" for index in range(1, 6)
    ]
    assert {row["processing_profile_id"] for row in copied} == {CONTEXT_PROFILE}
    [tainted_reply] = [row for row in copied if row["content"] == "reply to question 5"]
    assert tainted_reply["taint_metadata_json"] == UNTRUSTED
    assert body["turn"] is None
    assert body["copied_message_ids"] == [row["internal_id"] for row in copied]
    record = await db.conversation_threads.get_thread(thread["conversation_id"])
    assert record is not None
    assert record.anchor_message_id == anchor
    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    assert [entry["anchor_message_id"] for entry in listed["threads"]] == [anchor]


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_retrying_a_continue_with_no_opening_message_copies_once(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="the quote", at=NOW)
    request = {
        "profile_id": CONTEXT_PROFILE,
        "anchor_message_id": anchor["reply"],
        "turn_id": str(uuid.uuid4()),
    }

    first = await _start(api_test_client, **request)
    second = await _start(api_test_client, **request)

    conversation_id = first["thread"]["conversation_id"]
    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    assert second["thread"]["conversation_id"] == conversation_id
    assert len(listed["threads"]) == 1
    assert len(await _rows(db, conversation_id)) == len(first["copied_message_ids"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_continued_thread_answers_from_the_copied_turns(
    api_test_client: AsyncClient, db_engine: AsyncEngine, profiles: Profiles
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="the plumber's quote", at=NOW)

    body = await _start(
        api_test_client,
        profile_id=CONTEXT_PROFILE,
        anchor_message_id=anchor["reply"],
        prompt="Is that a fair price?",
    )

    thread = body["thread"]
    await api_test_client.get(
        f"/api/v1/chat/conversations/{thread['conversation_id']}/stream",
        params={"from_seq": 0},
    )
    assert profiles.saw(CONTEXT_PROFILE, "reply to the plumber's quote")
    assert profiles.saw(CONTEXT_PROFILE, "Is that a fair price?")


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_continuing_from_an_old_exchange_still_shows_it_to_the_thread(
    api_test_client: AsyncClient, db_engine: AsyncEngine, profiles: Profiles
) -> None:
    """Older than the profile's history age, the exchange is still what was chosen."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(
        db, home, words="last week's quote", at=NOW - timedelta(days=2)
    )

    body = await _start(
        api_test_client,
        profile_id=CONTEXT_PROFILE,
        anchor_message_id=anchor["reply"],
        prompt="Is it still valid?",
    )

    await api_test_client.get(
        f"/api/v1/chat/conversations/{body['thread']['conversation_id']}/stream",
        params={"from_seq": 0},
    )
    assert profiles.saw(CONTEXT_PROFILE, "reply to last week's quote")


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_continuing_with_context_is_refused_into_a_sealed_profile(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="private", at=NOW)

    response = await api_test_client.post(
        "/api/v1/chat/home/threads",
        json={
            "profile_id": SEALED_PROFILE,
            "anchor_message_id": anchor["reply"],
            "prompt": "Book it",
        },
    )

    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    assert response.status_code == 409
    assert "household context" in response.json()["detail"]
    assert listed["threads"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_continuing_without_context_carries_only_the_request(
    api_test_client: AsyncClient, db_engine: AsyncEngine, profiles: Profiles
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="private", at=NOW)

    body = await _start(
        api_test_client,
        profile_id=SEALED_PROFILE,
        anchor_message_id=anchor["reply"],
        include_context=False,
        prompt="Book the cheapest plumber",
    )

    thread = body["thread"]
    conversation_id = thread["conversation_id"]
    await api_test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )
    assert body["copied_message_ids"] == []
    assert not profiles.saw(SEALED_PROFILE, "private")
    record = await db.conversation_threads.get_thread(conversation_id)
    assert record is not None
    assert record.anchor_message_id == anchor["reply"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_continuing_from_outside_the_callers_home_is_not_found(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="mine", at=NOW)

    await _as(app_fixture, "someone_else")
    try:
        response = await api_test_client.post(
            "/api/v1/chat/home/threads",
            json={"profile_id": CONTEXT_PROFILE, "anchor_message_id": anchor["reply"]},
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Move to thread
# ---------------------------------------------------------------------------


async def _thread_on(client: AsyncClient, profile_id: str) -> str:
    body = await _start(client, profile_id=profile_id, prompt="Start here")
    thread = body["thread"]
    conversation_id = thread["conversation_id"]
    await client.get(
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
    )
    return conversation_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_a_turn_takes_the_whole_turn_out_of_home(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(
        db, home, words="the quotes", at=NOW, reply_taint=UNTRUSTED
    )
    await _home_turn(db, home, words="the weather", at=NOW + timedelta(minutes=1))

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["tool"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    home_messages = (
        await api_test_client.get(f"/api/v1/chat/conversations/{home}/messages")
    ).json()["messages"]
    assert [
        (message["role"], message["content"], message["moved_to_thread"])
        for message in home_messages
        if message["role"] != "tool"
        and message["content"] is not None
        and message["started_thread"] is None
    ] == [
        ("user", "the weather", None),
        ("assistant", "reply to the weather", None),
        ("assistant", home_messages[-1]["content"], thread),
    ]
    assert home_messages[-1]["internal_id"] == body["marker_message_id"]
    thread_rows = await _rows(db, thread)
    moved_rows = [
        row for row in thread_rows if row["internal_id"] in body["moved_message_ids"]
    ]
    assert [row["role"] for row in moved_rows] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert {row["processing_profile_id"] for row in moved_rows} == {CONTEXT_PROFILE}
    assert moved_rows[-1]["taint_metadata_json"] == UNTRUSTED
    # The newest thing in the thread, after the turn it opened with.
    assert thread_rows[-len(moved_rows) :] == moved_rows


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_moved_turn_leaves_homes_model_and_reaches_the_threads(
    api_test_client: AsyncClient, db_engine: AsyncEngine, profiles: Profiles
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(db, home, words="the quotes", at=NOW)
    await _home_turn(db, home, words="the weather", at=NOW + timedelta(minutes=1))
    await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["user"]},
    )

    for conversation_id in (home, thread):
        await api_test_client.post(
            "/api/v1/chat/send_message",
            json={
                "prompt": "And then?",
                "conversation_id": conversation_id,
                "interface_type": "web",
            },
        )

    home_profile = "chat_api_test_profile"
    assert not profiles.saw(home_profile, "tool output for the quotes")
    assert profiles.saw(home_profile, "tool output for the weather")
    assert profiles.saw(CONTEXT_PROFILE, "tool output for the quotes")


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_into_a_sealed_thread_is_refused(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, SEALED_PROFILE)
    moved = await _home_turn(db, home, words="private", at=NOW)
    before = await _rows(db, home)

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 409
    assert await _rows(db, home) == before


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_a_turn_still_running_is_refused(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(db, home, words="in progress", at=NOW)
    row = await db.message_history.get_row_by_internal_id(moved["user"])
    assert row is not None and row["turn_id"] is not None
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await hub.start_turn(
        home, turn_id=row["turn_id"], user_id=CALLER, started_at=datetime.now(UTC)
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 409
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_while_home_has_another_reply_in_progress_is_refused(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(db, home, words="finished", at=NOW)
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await hub.start_turn(
        home, turn_id=str(uuid.uuid4()), user_id=CALLER, started_at=datetime.now(UTC)
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 409
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_continuing_from_a_turn_still_running_is_refused(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="in progress", at=NOW)
    row = await db.message_history.get_row_by_internal_id(anchor["user"])
    assert row is not None and row["turn_id"] is not None
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await hub.start_turn(
        home, turn_id=row["turn_id"], user_id=CALLER, started_at=datetime.now(UTC)
    )

    response = await api_test_client.post(
        "/api/v1/chat/home/threads",
        json={"profile_id": CONTEXT_PROFILE, "anchor_message_id": anchor["user"]},
    )

    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_into_a_thread_of_another_home_is_not_found(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both ends must be the caller's web Home and a thread of it."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    moved = await _home_turn(db, home, words="web only", at=NOW)
    elsewhere = "web_conv_thread_of_another_home"
    await db.conversation_threads.create_thread(
        conversation_id=elsewhere,
        home_conversation_id="telegram_home",
        interface_type="web",
        owner_user_id=CALLER,
        profile_id=CONTEXT_PROFILE,
        title="Elsewhere",
        title_taint=None,
        anchor_message_id=None,
        delegation_id=None,
        now=NOW,
    )
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="mine too"),
        interface_type="web",
        conversation_id=elsewhere,
        timestamp=NOW,
        processing_profile_id=CONTEXT_PROFILE,
        user_id=CALLER,
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{elsewhere}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 404
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_turn_awaiting_recovery_is_neither_moved_nor_continued(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """Interrupted by a restart: in no hub, its reply not yet written."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    interrupted = await db.message_history.add_message(
        UserMessage.from_trusted_user(content="half done"),
        interface_type="web",
        conversation_id=home,
        turn_id=str(uuid.uuid4()),
        processing_profile_id="chat_api_test_profile",
        timestamp=NOW,
        user_id=CALLER,
    )

    move = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": interrupted},
    )
    resume = await api_test_client.post(
        "/api/v1/chat/home/threads",
        json={"profile_id": CONTEXT_PROFILE, "anchor_message_id": interrupted},
    )

    assert (move.status_code, resume.status_code) == (409, 409)
    assert await db.message_history.get_row_by_internal_id(interrupted) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
@pytest.mark.parametrize("stopped_after", ["user", "tool_call"])
async def test_moving_into_a_thread_awaiting_its_reply_is_refused(
    api_test_client: AsyncClient, db_engine: AsyncEngine, stopped_after: str
) -> None:
    """The thread's own turn was interrupted by a restart and waits to resume."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(db, home, words="finished", at=NOW)
    interrupted_turn = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="and then?"),
        interface_type="web",
        conversation_id=thread,
        turn_id=interrupted_turn,
        processing_profile_id=CONTEXT_PROFILE,
        timestamp=datetime.now(UTC),
        user_id=CALLER,
    )
    if stopped_after == "tool_call":
        await db.message_history.add_message(
            AssistantMessage(
                content="Let me check.",
                taint_metadata=TurnTaintState.empty().to_metadata(),
                tool_calls=[
                    ToolCallItem(
                        id="call_interrupted",
                        type="function",
                        function=ToolCallFunction(name="look_up", arguments="{}"),
                    )
                ],
            ),
            interface_type="web",
            conversation_id=thread,
            turn_id=interrupted_turn,
            processing_profile_id=CONTEXT_PROFILE,
            timestamp=datetime.now(UTC),
        )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 409
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_while_homes_newest_turn_awaits_recovery_is_refused(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """An older, finished turn waits while a newer Home turn is still owed a reply."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(db, home, words="finished", at=NOW)
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="interrupted"),
        interface_type="web",
        conversation_id=home,
        turn_id=str(uuid.uuid4()),
        processing_profile_id="chat_api_test_profile",
        timestamp=NOW + timedelta(seconds=30),
        user_id=CALLER,
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 409
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["turns", "send_message"])
async def test_retrying_a_moved_turn_does_not_run_it_again(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    profiles: Profiles,
    endpoint: str,
) -> None:
    """Its rows are gone from Home, but the turn was handled: a retry reloads."""
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    turn_id = str(uuid.uuid4())
    request = {
        "turn_id": turn_id,
        "conversation_id": home,
        "prompt": "Book the plumber",
        "interface_type": "web",
    }
    if endpoint == "turns":
        await api_test_client.post("/api/v1/chat/turns", json=request)
        await api_test_client.get(
            f"/api/v1/chat/conversations/{home}/stream", params={"from_seq": 0}
        )
    else:
        await api_test_client.post("/api/v1/chat/send_message", json=request)
    home_messages = (
        await api_test_client.get(f"/api/v1/chat/conversations/{home}/messages")
    ).json()["messages"]
    [user_message] = [
        m for m in home_messages if m["turn_id"] == turn_id and m["role"] == "user"
    ]
    moved = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": user_message["internal_id"]},
    )
    assert moved.status_code == 200, moved.text
    # As after a restart, or once the finished turn was pruned from the hub.
    app_fixture.state.conversation_stream_hub = ConversationStreamHub()
    calls_before = len(profiles.seen["chat_api_test_profile"])

    retry = await api_test_client.post(
        "/api/v1/chat/turns" if endpoint == "turns" else "/api/v1/chat/send_message",
        json=request,
    )

    assert retry.status_code == 200, retry.text
    assert retry.json()["already_complete"] is True
    assert retry.json().get("incomplete", False) is False
    assert len(profiles.seen["chat_api_test_profile"]) == calls_before
    rows = (
        await api_test_client.get(f"/api/v1/chat/conversations/{home}/messages")
    ).json()["messages"]
    assert [m["moved_to_thread"] for m in rows if m["turn_id"] == turn_id] == [thread]


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_an_echo_is_not_moved(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """An echo is the thread's outcome record, not an exchange the user started."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    [reply] = [
        cast("int", row["internal_id"])
        for row in await _rows(db, thread)
        if row["role"] == "assistant"
    ]
    echo = await db.message_history.copy_message_as_echo(
        source_internal_id=reply,
        destination_conversation_id=home,
        destination_profile_id="chat_api_test_profile",
        echoed_from_conversation_id=thread,
        turn_id=str(uuid.uuid4()),
        timestamp=NOW,
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": echo},
    )

    assert response.status_code == 409
    assert await db.message_history.get_row_by_internal_id(echo) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_a_failed_turn_can_be_moved(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """A turn that failed has ended: its error row is its outcome."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    turn_id = str(uuid.uuid4())
    failed = await db.message_history.add_message(
        UserMessage.from_trusted_user(content="this broke"),
        interface_type="web",
        conversation_id=home,
        turn_id=turn_id,
        processing_profile_id="chat_api_test_profile",
        timestamp=NOW,
        user_id=CALLER,
    )
    await db.message_history.add_message(
        ErrorMessage(content="Something went wrong.", error_traceback="boom"),
        interface_type="web",
        conversation_id=home,
        turn_id=turn_id,
        processing_profile_id="chat_api_test_profile",
        timestamp=NOW + timedelta(seconds=1),
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": failed},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["moved_message_ids"]) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_overlapping_retries_of_a_start_share_one_thread(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    anchor = await _home_turn(db, home, words="the quote", at=NOW)
    request = {
        "profile_id": CONTEXT_PROFILE,
        "anchor_message_id": anchor["reply"],
        "turn_id": str(uuid.uuid4()),
    }

    responses = await asyncio.gather(
        api_test_client.post("/api/v1/chat/home/threads", json=request),
        api_test_client.post("/api/v1/chat/home/threads", json=request),
    )

    listed = (await api_test_client.get("/api/v1/chat/home/threads")).json()
    assert [response.status_code for response in responses] == [200, 200]
    assert len(listed["threads"]) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
@pytest.mark.parametrize("status", ["queued", "awaiting_children"])
async def test_moving_into_a_thread_whose_first_run_is_unfinished_is_refused(
    api_test_client: AsyncClient, db_engine: AsyncEngine, status: str
) -> None:
    """The run's turn is in no hub: not yet started, or waiting to be requeued."""
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    moved = await _home_turn(db, home, words="hold on", at=NOW)
    thread = "web_conv_assistant_started"
    await db.conversation_threads.create_thread(
        conversation_id=thread,
        home_conversation_id=home,
        interface_type="web",
        owner_user_id=CALLER,
        profile_id=CONTEXT_PROFILE,
        title="Queued",
        title_taint=None,
        anchor_message_id=None,
        delegation_id="delegation_queued",
        now=NOW,
    )
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Plan the trip."),
        interface_type="web",
        conversation_id=thread,
        timestamp=NOW,
        processing_profile_id=CONTEXT_PROFILE,
        user_id=CALLER,
        is_internal=True,
    )
    await db.delegation_runs.create_run({
        "delegation_id": "delegation_queued",
        "task_id": "task_queued",
        "source_profile_id": "chat_api_test_profile",
        "target_service_id": CONTEXT_PROFILE,
        "interface_type": "web",
        "conversation_id": thread,
        "subconversation_id": None,
        "run_kind": "thread",
        "request_text": "Plan the trip.",
        "content_parts_json": [{"type": "text", "text": "Plan the trip."}],
    })
    await db.execute(
        update(delegation_runs_table)
        .where(delegation_runs_table.c.delegation_id == "delegation_queued")
        .values(status=status)
    )

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": moved["reply"]},
    )

    assert response.status_code == 409
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_moving_a_message_that_is_not_in_home_is_not_found(
    api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    other = await _thread_on(api_test_client, CONTEXT_PROFILE)
    [thread_message] = [
        row["internal_id"] for row in await _rows(db, other) if row["role"] == "user"
    ]

    response = await api_test_client.post(
        f"/api/v1/chat/home/threads/{thread}/moved-turns",
        json={"message_id": thread_message},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_only_the_owner_can_move_a_turn(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
    moved = await _home_turn(db, home, words="mine", at=NOW)

    await _as(app_fixture, "someone_else")
    try:
        response = await api_test_client.post(
            f"/api/v1/chat/home/threads/{thread}/moved-turns",
            json={"message_id": moved["reply"]},
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404
    assert await db.message_history.get_row_by_internal_id(moved["reply"]) is not None


# ---------------------------------------------------------------------------
# Identity, gating and compatible destinations
# ---------------------------------------------------------------------------


def _alias_config(app_fixture: FastAPI) -> None:
    """Map a Telegram login id to the caller, as an operator does."""
    config = AppConfig.model_validate({
        "database_url": app_fixture.state.config.database_url,
        "conversation_threads": {"enabled": True},
        "users": [{"id": CALLER, "telegram": {"user_ids": [int(ALIAS)]}}],
    })
    app_fixture.state.config = config
    app_fixture.state.user_identity_resolver = UserIdentityResolver(config)


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_an_alias_login_starts_and_moves_in_its_owners_home(
    app_fixture: FastAPI, api_test_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    _alias_config(app_fixture)
    db = Database(engine=db_engine)
    home = await _home(api_test_client)
    moved = await _home_turn(db, home, words="from my phone", at=NOW, user_id=ALIAS)
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    handle = hub.subscribe_activity(ALIAS)

    await _as(app_fixture, ALIAS)
    try:
        thread = await _thread_on(api_test_client, CONTEXT_PROFILE)
        response = await api_test_client.post(
            f"/api/v1/chat/home/threads/{thread}/moved-turns",
            json={"message_id": moved["reply"]},
        )
    finally:
        app_fixture.dependency_overrides.pop(get_current_user, None)

    pinged = set()
    while not handle.queue.empty():
        pinged.add(handle.queue.get_nowait().conversation_id)
    record = await db.conversation_threads.get_thread(thread)
    assert response.status_code == 200, response.text
    assert record is not None
    assert (record.home_conversation_id, record.owner_user_id) == (home, CALLER)
    assert pinged == {home, thread}


@pytest.mark.asyncio
async def test_thread_actions_are_absent_while_threads_are_disabled(
    api_test_client: AsyncClient,
) -> None:
    start = await api_test_client.post(
        "/api/v1/chat/home/threads",
        json={"profile_id": "chat_api_test_profile", "prompt": "hello"},
    )
    move = await api_test_client.post(
        "/api/v1/chat/home/threads/web_conv_anything/moved-turns",
        json={"message_id": 1},
    )

    assert (start.status_code, move.status_code) == (404, 404)


@pytest.mark.asyncio
@pytest.mark.usefixtures("profiles")
async def test_profiles_say_which_can_take_home_context(
    api_test_client: AsyncClient,
) -> None:
    body = (await api_test_client.get("/api/v1/profiles")).json()

    flags = {
        profile["id"]: profile["receives_household_context"]
        for profile in body["profiles"]
    }
    assert (flags[CONTEXT_PROFILE], flags[SEALED_PROFILE]) == (True, False)
