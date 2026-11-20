"""Functional tests for peeking into running delegated subconversations."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from tests.helpers import wait_for_condition

OWNER = "test_user"


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _start_conversation(db: Database, *, user_id: str = OWNER) -> str:
    conversation_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Main conversation prompt"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC) - timedelta(minutes=5),
        user_id=user_id,
    )
    return conversation_id


async def _create_delegation(
    db: Database,
    conversation_id: str,
    name: str,
    *,
    user_id: str = OWNER,
    parent_subconversation_id: str | None = None,
    handed_off: bool = True,
) -> tuple[str, str]:
    delegation_id = f"{name}-{uuid.uuid4().hex[:8]}"
    subconversation_id = str(uuid.uuid4())
    await db.delegation_runs.create_run(
        DelegationRunCreate(
            delegation_id=delegation_id,
            task_id=f"task-{delegation_id}",
            source_profile_id="default_assistant",
            target_service_id=f"{name}_profile",
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=subconversation_id,
            source_subconversation_id=parent_subconversation_id,
            request_text=f"Please investigate {name} in detail.",
            content_parts_json=[],
            user_id=user_id,
        )
    )
    if handed_off:
        assert await db.delegation_runs.mark_handed_off(
            delegation_id, datetime.now(UTC)
        )
    return delegation_id, subconversation_id


def _parse_sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    frames: list[tuple[str, dict[str, object]]] = []
    for block in body.split("\n\n"):
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            frames.append((event_name, json.loads("\n".join(data_lines))))
    return frames


@pytest.mark.asyncio
async def test_pending_delegations_exposes_subconversation_id(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = await _start_conversation(db)
    delegation_id, subconversation_id = await _create_delegation(
        db, conversation_id, "engineer"
    )

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/pending-delegations"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["delegations"]) == 1
    entry = body["delegations"][0]
    assert entry["delegation_id"] == delegation_id
    assert entry["subconversation_id"] == subconversation_id


@pytest.mark.asyncio
async def test_get_subconversation_messages_isolated_and_includes_metadata(
    test_client: AsyncClient,
    app_fixture: FastAPI,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = await _start_conversation(db)
    delegation_id, sub_id = await _create_delegation(db, conversation_id, "engineer")
    _sibling_id, sibling_sub_id = await _create_delegation(
        db, conversation_id, "research"
    )
    child_delegation_id, child_sub_id = await _create_delegation(
        db,
        conversation_id,
        "member",
        parent_subconversation_id=sub_id,
    )

    base = datetime.now(UTC) - timedelta(minutes=2)
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Delegated user prompt"),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=sub_id,
        turn_id="sub-turn-1",
        timestamp=base,
        user_id=OWNER,
    )
    await db.message_history.add_message(
        AssistantMessage(
            content="Checking logs now.",
            tool_calls=[
                ToolCallItem(
                    id="call-1",
                    type="function",
                    function=ToolCallFunction(
                        name="read_error_logs", arguments='{"limit": 5}'
                    ),
                )
            ],
        ),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=sub_id,
        turn_id="sub-turn-1",
        processing_profile_id="engineer_profile",
        timestamp=base + timedelta(seconds=1),
        user_id=OWNER,
    )
    await db.message_history.add_message(
        ToolMessage(
            tool_call_id="call-1", name="read_error_logs", content="0 errors found"
        ),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=sub_id,
        turn_id="sub-turn-1",
        timestamp=base + timedelta(seconds=2),
        user_id=OWNER,
    )
    # Internal row in the same subconversation must stay hidden.
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Internal continuation payload"),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=sub_id,
        turn_id="sub-turn-1",
        timestamp=base + timedelta(seconds=3),
        user_id=OWNER,
        is_internal=True,
    )
    # Sibling subconversation row must not leak.
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Sibling prompt"),
        interface_type="web",
        conversation_id=conversation_id,
        subconversation_id=sibling_sub_id,
        turn_id="sibling-turn",
        timestamp=base + timedelta(seconds=4),
        user_id=OWNER,
    )

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await hub.subconversations.start_turn(
        sub_id,
        turn_id="sub-turn-1",
        user_id=OWNER,
        started_at=base,
    )
    # Must NOT pollute main hub's running_conversation_ids.
    assert hub.running_conversation_ids({OWNER}) == []

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/subconversations/{sub_id}/messages"
    )
    assert response.status_code == 200
    body = response.json()

    assert body["conversation_id"] == conversation_id
    assert body["subconversation_id"] == sub_id
    assert body["request_text"] == "Please investigate engineer in detail."
    assert body["delegation"]["delegation_id"] == delegation_id
    assert body["delegation"]["subconversation_id"] == sub_id
    assert body["delegation"]["children_total"] == 1
    assert body["delegation"]["children_finished"] == 0
    assert len(body["child_delegations"]) == 1
    assert body["child_delegations"][0]["delegation_id"] == child_delegation_id
    assert body["child_delegations"][0]["subconversation_id"] == child_sub_id

    roles_and_contents = [(m["role"], m["content"]) for m in body["messages"]]
    assert roles_and_contents == [
        ("user", "Delegated user prompt"),
        ("assistant", "Checking logs now."),
        ("tool", "0 errors found"),
    ]
    assert body["total_messages"] == 3
    assert len(body["active_turns"]) == 1
    assert body["active_turns"][0]["turn_id"] == "sub-turn-1"
    assert body["active_turns"][0]["status"] == "running"
    assert body["active_turns"][0]["first_seq"] == 0

    # Pagination via before_id returns older messages cleanly.
    page1 = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/subconversations/{sub_id}/messages?limit=2"
    )
    assert page1.status_code == 200
    p1_body = page1.json()
    assert len(p1_body["messages"]) == 2
    assert p1_body["has_more_before"] is True
    oldest_id = p1_body["messages"][0]["internal_id"]

    page2 = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/subconversations/{sub_id}/messages"
        f"?limit=2&before_id={oldest_id}"
    )
    assert page2.status_code == 200
    p2_body = page2.json()
    assert len(p2_body["messages"]) == 1
    assert p2_body["messages"][0]["content"] == "Delegated user prompt"
    assert p2_body["has_more_before"] is False


@pytest.mark.asyncio
async def test_subconversation_peek_enforces_ownership_and_conversation_scoping(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conv_a = await _start_conversation(db, user_id=OWNER)
    conv_b = await _start_conversation(db, user_id=OWNER)
    other_conv = await _start_conversation(db, user_id="someone_else")

    _, sub_a = await _create_delegation(db, conv_a, "engineer", user_id=OWNER)
    _, other_sub = await _create_delegation(
        db, other_conv, "engineer", user_id="someone_else"
    )

    # Another user's conversation -> 404.
    assert (
        await test_client.get(
            f"/api/v1/chat/conversations/{other_conv}/subconversations/{other_sub}/messages"
        )
    ).status_code == 404
    assert (
        await test_client.get(
            f"/api/v1/chat/conversations/{other_conv}/subconversations/{other_sub}/stream?follow=false"
        )
    ).status_code == 404

    # Subconversation belonging to conv_a cannot be read through conv_b -> 404.
    assert (
        await test_client.get(
            f"/api/v1/chat/conversations/{conv_b}/subconversations/{sub_a}/messages"
        )
    ).status_code == 404
    assert (
        await test_client.get(
            f"/api/v1/chat/conversations/{conv_b}/subconversations/{sub_a}/stream?follow=false"
        )
    ).status_code == 404

    # Unknown subconversation -> 404.
    assert (
        await test_client.get(
            f"/api/v1/chat/conversations/{conv_a}/subconversations/does-not-exist/messages"
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_subconversation_stream_replays_live_events_and_410_on_eviction(
    test_client: AsyncClient,
    app_fixture: FastAPI,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = await _start_conversation(db)
    _, sub_id = await _create_delegation(db, conversation_id, "complex_tasks")

    sub_hub = app_fixture.state.conversation_stream_hub.subconversations
    turn = await sub_hub.start_turn(
        sub_id,
        turn_id="live-sub-turn",
        user_id=OWNER,
        started_at=datetime.now(UTC),
    )
    await sub_hub.publish(
        sub_id,
        "text",
        turn_id="live-sub-turn",
        payload={"content": "Working on step 1... "},
    )

    async def _finish_turn_soon() -> None:
        await wait_for_condition(
            lambda: sub_hub.subscriber_count(sub_id) > 0,
            timeout=5.0,
            description="subconversation stream subscriber attached",
        )
        await sub_hub.publish(
            sub_id,
            "text",
            turn_id="live-sub-turn",
            payload={"content": "Done!"},
        )
        await sub_hub.end_turn(
            sub_id,
            turn_id="live-sub-turn",
            status="complete",
        )

    finisher = asyncio.create_task(_finish_turn_soon())
    try:
        response = await test_client.get(
            f"/api/v1/chat/conversations/{conversation_id}/subconversations/{sub_id}/stream"
            f"?from_seq={turn.first_seq}&follow=false"
        )
    finally:
        await finisher

    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    event_types = [name for name, _ in frames]
    assert "turn_started" in event_types
    assert "turn_ended" in event_types
    text_chunks = [
        payload.get("content")
        for name, payload in frames
        if name == "text" and "content" in payload
    ]
    assert text_chunks == ["Working on step 1... ", "Done!"]

    # Out-of-buffer seq -> 410 Gone with active_turns.
    gone = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/subconversations/{sub_id}/stream"
        f"?from_seq=99999&follow=false"
    )
    assert gone.status_code == 410
    gone_body = gone.json()
    assert gone_body["reason"] == "out_of_buffer"
    assert len(gone_body["active_turns"]) == 1
    assert gone_body["active_turns"][0]["turn_id"] == "live-sub-turn"
