"""The client sync endpoint: one request converges a client from any cursor.

See docs/design/mobile-state-sync.md, "The sync endpoint".
"""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate
from family_assistant.web.routers.sync_api import SyncResponse

if TYPE_CHECKING:
    from family_assistant.web.conversation_stream_hub import ConversationStreamHub

OWNER = "test_user"
SYNC = "/api/v1/sync"


@pytest_asyncio.fixture(scope="function")
async def client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="function")
async def db(db_engine: AsyncEngine) -> Database:
    return Database(engine=db_engine)


async def _conversation(db: Database, *, user_id: str = OWNER, text: str = "hi") -> str:
    conversation_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content=text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id=user_id,
    )
    return conversation_id


async def _reply(db: Database, conversation_id: str, text: str) -> int:
    return await db.message_history.add_message(
        AssistantMessage(content=text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
    )


async def _sync(client: AsyncClient, **params: str | float) -> SyncResponse:
    response = await client.get(SYNC, params=params)
    assert response.status_code == 200, response.text
    return SyncResponse.model_validate(response.json())


def _conversation_ids(body: SyncResponse) -> set[str]:
    return {row.conversation_id for row in body.conversations}


def _message_texts(body: SyncResponse, conversation_id: str) -> list[object]:
    return [
        message.content
        for change in body.messages
        if change.conversation_id == conversation_id
        for message in change.messages
    ]


async def test_bootstrap_lists_the_callers_conversations_only(
    client: AsyncClient, db: Database
) -> None:
    mine = await _conversation(db)
    await _conversation(db, user_id="someone_else")

    body = await _sync(client)

    assert body.bootstrap is True
    assert body.cursor > 0
    assert _conversation_ids(body) == {mine}


async def test_an_unchanged_client_gets_nothing(
    client: AsyncClient, db: Database
) -> None:
    await _conversation(db)
    first = await _sync(client)

    body = await _sync(client, cursor=first.cursor)

    assert body.bootstrap is False
    assert body.conversations == []
    assert body.messages == []
    assert body.confirmations is None
    assert body.delegations == {}


async def test_a_new_reply_updates_its_conversation_row_and_no_other(
    client: AsyncClient, db: Database
) -> None:
    changed = await _conversation(db)
    await _conversation(db)
    cursor = (await _sync(client)).cursor

    await _reply(db, changed, "the answer")
    body = await _sync(client, cursor=cursor)

    assert _conversation_ids(body) == {changed}
    assert body.conversations[0].last_message == "the answer"
    assert body.cursor > cursor


async def test_messages_are_sent_only_for_focus_conversations(
    client: AsyncClient, db: Database
) -> None:
    focus = await _conversation(db)
    other = await _conversation(db)
    cursor = (await _sync(client)).cursor

    await _reply(db, focus, "in focus")
    await _reply(db, other, "elsewhere")
    body = await _sync(client, cursor=cursor, focus=focus)

    assert _message_texts(body, focus) == ["in focus"]
    assert _message_texts(body, other) == []


async def test_fresh_focus_sends_the_latest_page_to_replace_history(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db, text="first")
    await _reply(db, conversation_id, "second")
    cursor = (await _sync(client)).cursor

    body = await _sync(
        client, cursor=cursor, focus=conversation_id, fresh=conversation_id
    )

    [change] = body.messages
    assert change.replaces_history is True
    assert [m.content for m in change.messages] == ["first", "second"]


async def test_a_deleted_message_is_reported_removed(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db)
    doomed = await _reply(db, conversation_id, "doomed")
    cursor = (await _sync(client)).cursor

    await db.execute(
        message_history_table.delete().where(
            message_history_table.c.internal_id == doomed
        )
    )
    body = await _sync(client, cursor=cursor, focus=conversation_id)

    [change] = body.messages
    assert change.removed_message_ids == [doomed]


async def test_a_new_confirmation_sends_the_full_pending_list(
    client: AsyncClient, db: Database
) -> None:
    cursor = (await _sync(client)).cursor

    await db.confirmation_requests.create(
        request_id="req-1",
        target_user_id=OWNER,
        tool_name="list_notes",
        tool_args={},
        tool_call_id="call_1",
        source_message_internal_id=None,
        confirmation_prompt="Allow?",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    body = await _sync(client, cursor=cursor)

    assert [c.request_id for c in body.confirmations or []] == ["req-1"]


async def test_a_delegation_change_sends_that_conversations_pending_list(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db)
    cursor = (await _sync(client)).cursor

    delegation_id = f"research-{uuid.uuid4().hex[:8]}"
    await db.delegation_runs.create_run(
        DelegationRunCreate(
            delegation_id=delegation_id,
            task_id=f"task-{delegation_id}",
            source_profile_id="default_assistant",
            target_service_id="research_profile",
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=f"sub-{delegation_id}",
            request_text="Please research the thing.",
            content_parts_json=[],
            user_id=OWNER,
        )
    )
    await db.delegation_runs.mark_handed_off(delegation_id, datetime.now(UTC))
    body = await _sync(client, cursor=cursor)

    assert [d.delegation_id for d in body.delegations[conversation_id]] == [
        delegation_id
    ]


async def test_a_running_turn_is_reported_with_its_progress_so_far(
    client: AsyncClient, db: Database, app_fixture: FastAPI
) -> None:
    conversation_id = await _conversation(db)
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    turn_id = str(uuid.uuid4())
    await hub.start_turn(
        conversation_id, turn_id=turn_id, user_id=OWNER, started_at=datetime.now(UTC)
    )
    for delta in ("Hel", "lo"):
        await hub.publish(
            conversation_id, "text", turn_id=turn_id, payload={"content": delta}
        )

    body = await _sync(client, focus=conversation_id)

    assert body.running_conversation_ids == [conversation_id]
    [turn] = body.active_turns
    assert turn.parts == [{"type": "text", "content": "Hello"}]


async def test_a_turn_ending_resends_the_row_of_a_client_that_saw_it_running(
    client: AsyncClient, db: Database, app_fixture: FastAPI
) -> None:
    conversation_id = await _conversation(db)
    cursor = (await _sync(client)).cursor

    body = await _sync(client, cursor=cursor, running=conversation_id)

    assert body.running_conversation_ids == []
    assert _conversation_ids(body) == {conversation_id}


async def test_a_small_limit_splits_at_a_transaction_boundary(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db)
    cursor = (await _sync(client)).cursor
    await _reply(db, conversation_id, "one")
    await _reply(db, conversation_id, "two")

    first = await _sync(client, cursor=cursor, focus=conversation_id, limit=1)
    second = await _sync(client, cursor=first.cursor, focus=conversation_id, limit=1)

    assert first.has_more is True
    assert _message_texts(first, conversation_id) == ["one"]
    assert _message_texts(second, conversation_id) == ["two"]
    assert second.has_more is False


async def test_a_cursor_from_another_database_bootstraps(
    client: AsyncClient, db: Database
) -> None:
    await _conversation(db)

    body = await _sync(client, cursor=10_000_000)

    assert body.bootstrap is True


async def test_a_long_poll_returns_as_soon_as_a_change_commits(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db)
    cursor = (await _sync(client)).cursor

    started = time.monotonic()
    poll = asyncio.create_task(
        _sync(client, cursor=cursor, focus=conversation_id, wait=10)
    )
    await _reply(db, conversation_id, "arrived")
    body = await asyncio.wait_for(poll, timeout=15)

    assert _message_texts(body, conversation_id) == ["arrived"]
    assert time.monotonic() - started < 5


async def test_a_long_poll_with_nothing_to_report_returns_at_its_deadline(
    client: AsyncClient, db: Database
) -> None:
    await _conversation(db)
    cursor = (await _sync(client)).cursor

    started = time.monotonic()
    body = await _sync(client, cursor=cursor, wait=0.5)

    assert body.cursor == cursor
    assert body.conversations == []
    assert time.monotonic() - started >= 0.5


async def test_history_pages_back_from_a_message_id(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db, text="first")
    second = await _reply(db, conversation_id, "second")
    await _reply(db, conversation_id, "third")

    response = await client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages",
        params={"before_id": second + 1, "limit": 1},
    )

    assert response.status_code == 200
    body = response.json()
    assert [m["content"] for m in body["messages"]] == ["second"]
    assert body["has_more_before"] is True


async def test_a_synced_client_waits_even_before_any_tracked_write(
    client: AsyncClient,
) -> None:
    """Cursor zero is a real position, not a request to bootstrap again."""
    first = await _sync(client)

    started = time.monotonic()
    body = await _sync(client, cursor=first.cursor, wait=0.5)

    assert first.cursor == 0
    assert body.bootstrap is False
    assert time.monotonic() - started >= 0.5


async def test_a_rating_change_resends_its_turns_messages(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = await _conversation(db)
    turn_id = str(uuid.uuid4())
    await db.message_history.add_message(
        AssistantMessage(content="rate me"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        turn_id=turn_id,
    )
    cursor = (await _sync(client)).cursor

    await db.turn_feedback.set(
        turn_id=turn_id, user_id=OWNER, rating="up", comment=None
    )
    body = await _sync(client, cursor=cursor, focus=conversation_id)

    [change] = body.messages
    assert [(m.content, m.feedback) for m in change.messages] == [("rate me", "up")]


async def test_paging_back_keeps_messages_whose_timestamps_are_out_of_order(
    client: AsyncClient, db: Database
) -> None:
    conversation_id = str(uuid.uuid4())
    later = datetime.now(UTC)
    for text, timestamp in (("newer", later), ("older", later - timedelta(minutes=5))):
        await db.message_history.add_message(
            AssistantMessage(content=text),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=timestamp,
        )
    url = f"/api/v1/chat/conversations/{conversation_id}/messages"
    latest = (await client.get(url, params={"limit": 1})).json()["messages"]

    earlier = (
        await client.get(
            url, params={"limit": 1, "before_id": latest[0]["internal_id"]}
        )
    ).json()["messages"]

    assert [m["content"] for m in latest] == ["newer"]
    assert [m["content"] for m in earlier] == ["older"]
