"""Functional tests for listing the delegations a conversation is waiting on."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate

OWNER = "test_user"


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _start_conversation(db: Database, *, user_id: str = OWNER) -> str:
    conversation_id = str(uuid.uuid4())
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Research this for me"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id=user_id,
    )
    return conversation_id


async def _run(
    db: Database,
    conversation_id: str,
    name: str,
    *,
    handed_off: bool = True,
    parent_subconversation_id: str | None = None,
) -> str:
    delegation_id = f"{name}-{uuid.uuid4().hex[:8]}"
    await db.delegation_runs.create_run(
        DelegationRunCreate(
            delegation_id=delegation_id,
            task_id=f"task-{delegation_id}",
            source_profile_id="default_assistant",
            target_service_id=f"{name}_profile",
            interface_type="web",
            conversation_id=conversation_id,
            subconversation_id=f"sub-{delegation_id}",
            source_subconversation_id=parent_subconversation_id,
            request_text=f"Please {name} the thing.",
            content_parts_json=[],
            user_id=OWNER,
        )
    )
    if handed_off:
        assert await db.delegation_runs.mark_handed_off(
            delegation_id, datetime.now(UTC)
        )
    return delegation_id


async def _finish(db: Database, delegation_id: str, *, delivered: bool) -> None:
    now = datetime.now(UTC)
    await db.delegation_runs.mark_completed(
        delegation_id=delegation_id,
        result_text="done",
        result_attachment_ids=[],
        completed_at=now,
    )
    if delivered:
        await db.delegation_runs.mark_notified(
            delegation_id=delegation_id,
            result_message_internal_id=None,
            notified_at=now,
        )


@pytest.mark.asyncio
async def test_lists_only_background_work_still_owed_to_the_conversation(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = await _start_conversation(db)
    council = await _run(db, conversation_id, "council")
    member_done = await _run(
        db,
        conversation_id,
        "member",
        parent_subconversation_id=f"sub-{council}",
    )
    await _run(
        db, conversation_id, "member", parent_subconversation_id=f"sub-{council}"
    )
    await _finish(db, member_done, delivered=False)
    finishing = await _run(db, conversation_id, "research")
    await _finish(db, finishing, delivered=False)
    delivered = await _run(db, conversation_id, "browse")
    await _finish(db, delivered, delivered=True)
    await _run(db, conversation_id, "quick", handed_off=False)
    await _run(db, await _start_conversation(db), "elsewhere")

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/pending-delegations"
    )

    assert response.status_code == 200
    body = response.json()
    assert [entry["delegation_id"] for entry in body["delegations"]] == [
        council,
        finishing,
    ]
    council_entry, finishing_entry = body["delegations"]
    assert council_entry["target_profile_id"] == "council_profile"
    assert council_entry["status"] == "queued"
    assert council_entry["request_preview"] == "Please council the thing."
    assert council_entry["children_total"] == 2
    assert council_entry["children_finished"] == 1
    assert finishing_entry["status"] == "completed"
    assert finishing_entry["children_total"] == 0


@pytest.mark.asyncio
async def test_another_users_conversation_is_not_found(
    test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    conversation_id = await _start_conversation(db, user_id="someone_else")
    await _run(db, conversation_id, "research")

    response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/pending-delegations"
    )

    assert response.status_code == 404
