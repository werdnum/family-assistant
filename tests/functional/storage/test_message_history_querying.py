"""Functional tests for message-history querying and indexing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.message_history_indexer import (
    handle_index_message_history_batch,
)
from family_assistant.llm.messages import AssistantMessage, ToolMessage, UserMessage
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.repositories.message_history import (
    MessageHistoryAccessDeniedError,
    MessageHistoryQuery,
)
from family_assistant.storage.vector import DocumentEmbeddingRecord, DocumentRecord
from family_assistant.tools.communication import get_message_history_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.storage.vector_search import VectorSearchQuery


@pytest.mark.asyncio
async def test_message_history_query_defaults_to_same_user_scope(
    db_engine: AsyncEngine,
) -> None:
    """same_user finds another conversation for the same user but not other users."""
    async with DatabaseContext(engine=db_engine) as db:
        now = datetime.now(UTC)
        await _store_user_message(
            db,
            conversation_id="current",
            user_id="user-a",
            content="I need to renew passports",
            timestamp=now - timedelta(days=10),
        )
        await _store_user_message(
            db,
            conversation_id="other-same-user",
            user_id="user-a",
            content="Passport appointment is next Friday",
            timestamp=now - timedelta(days=5),
        )
        await _store_user_message(
            db,
            conversation_id="other-user",
            user_id="user-b",
            content="Passport details for somebody else",
            timestamp=now,
        )

        rows = await db.message_history.query_history(
            MessageHistoryQuery(
                query="passport",
                current_conversation_id="current",
                current_user_id="user-a",
                processing_profile_id="default",
                limit=10,
            )
        )

    assert {row["conversation_id"] for row in rows} == {
        "current",
        "other-same-user",
    }


@pytest.mark.asyncio
async def test_message_history_query_filters_tools_and_hydrates_context(
    db_engine: AsyncEngine,
) -> None:
    """Tool filters match tool calls/results and include neighboring context."""
    async with DatabaseContext(engine=db_engine) as db:
        now = datetime.now(UTC)
        await _store_user_message(
            db,
            conversation_id="current",
            user_id="user-a",
            content="Please add this to the calendar",
            timestamp=now,
        )
        await db.message_history.add_message(
            AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id="call-1",
                        type="function",
                        function=ToolCallFunction(
                            name="add_calendar_event",
                            arguments={"summary": "Passport appointment"},
                        ),
                    )
                ],
            ),
            interface_type="test",
            conversation_id="current",
            timestamp=now + timedelta(seconds=1),
            turn_id="turn-1",
            user_id="user-a",
            processing_profile_id="default",
        )
        await db.message_history.add_message(
            ToolMessage(
                tool_call_id="call-1",
                name="add_calendar_event",
                content="Created calendar event",
            ),
            interface_type="test",
            conversation_id="current",
            timestamp=now + timedelta(seconds=2),
            turn_id="turn-1",
            user_id="user-a",
            processing_profile_id="default",
        )

        rows = await db.message_history.query_history(
            MessageHistoryQuery(
                scope="current_conversation",
                current_conversation_id="current",
                interface_type="test",
                tool_names=("add_calendar_event",),
                processing_profile_id="default",
                limit=10,
            )
        )
        hydrated = await db.message_history.hydrate_history_results(
            rows,
            include_context=1,
        )

    assert {row["role"] for row in rows} <= {"assistant", "tool"}
    assert any(row["tool_name"] == "add_calendar_event" for row in rows)
    tool_result = next(item for item in hydrated if item["role"] == "tool")
    context = tool_result.get("context")
    assert context is not None
    assert context[0]["role"] == "assistant"
    assert context[1]["role"] == "tool"


@pytest.mark.asyncio
async def test_same_user_scope_keeps_user_filter_when_conversation_id_is_supplied(
    db_engine: AsyncEngine,
) -> None:
    """A guessed conversation_id cannot bypass same-user history isolation."""
    async with DatabaseContext(engine=db_engine) as db:
        now = datetime.now(UTC)
        await _store_user_message(
            db,
            conversation_id="other-user-conversation",
            user_id="user-b",
            content="Passport details for another user",
            timestamp=now,
        )

        rows = await db.message_history.query_history(
            MessageHistoryQuery(
                query="passport",
                conversation_id="other-user-conversation",
                current_conversation_id="current",
                current_user_id="user-a",
                processing_profile_id="default",
            )
        )

    assert rows == []


@pytest.mark.asyncio
async def test_message_history_query_denies_all_accessible_scope(
    db_engine: AsyncEngine,
) -> None:
    """Broadened all_accessible scope is denied without policy support."""
    async with DatabaseContext(engine=db_engine) as db:
        with pytest.raises(MessageHistoryAccessDeniedError):
            await db.message_history.query_history(
                MessageHistoryQuery(
                    scope="all_accessible",
                    current_conversation_id="current",
                )
            )


@pytest.mark.asyncio
async def test_get_message_history_tool_returns_structured_json(
    db_engine: AsyncEngine,
) -> None:
    """The model-facing tool returns compact structured data."""
    async with DatabaseContext(engine=db_engine) as db:
        await _store_user_message(
            db,
            conversation_id="current",
            user_id="user-a",
            content="The passports are in the blue folder",
            timestamp=datetime.now(UTC),
        )
        context = _build_exec_context(db)

        result = await get_message_history_tool(
            exec_context=context,
            query="passports",
            roles=["user"],
        )

    data = cast("dict[str, Any]", result.data)
    assert data["result_count"] == 1
    assert data["results"][0]["content"] == "The passports are in the blue folder"


@pytest.mark.asyncio
async def test_semantic_history_search_prefilters_access_before_vector_limit(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic search applies accessible source IDs before vector-store limiting."""
    async with DatabaseContext(engine=db_engine) as db:
        timestamp = datetime.now(UTC)
        await _store_user_message(
            db,
            conversation_id="other-user-conversation",
            user_id="user-b",
            content="The inaccessible passport note",
            timestamp=timestamp,
            turn_id="turn-other-user",
        )
        await _store_user_message(
            db,
            conversation_id="current",
            user_id="user-a",
            content="The accessible passport note",
            timestamp=timestamp + timedelta(seconds=1),
            turn_id="turn-current-user",
        )

        captured_query: VectorSearchQuery | None = None

        async def fake_query_vector_store(
            *,
            db_context: DatabaseContext,
            query: VectorSearchQuery,
            query_embedding: list[float] | None = None,
        ) -> list[dict[str, object]]:
            nonlocal captured_query
            _ = db_context, query_embedding
            captured_query = query
            return [
                {"source_id": "message_turn:turn-other-user"},
                {"source_id": "message_turn:turn-current-user"},
            ]

        monkeypatch.setattr(
            "family_assistant.tools.communication.query_vector_store",
            fake_query_vector_store,
        )

        result = await get_message_history_tool(
            exec_context=_build_exec_context(
                db,
                embedding_generator=MockEmbeddingGenerator(dimensions=3),
            ),
            query="passport",
            search_mode="semantic",
            limit=1,
        )

    data = cast("dict[str, Any]", result.data)
    assert "error" not in data, data
    assert data["result_count"] == 1
    assert data["results"][0]["content"] == "The accessible passport note"
    assert captured_query is not None
    assert captured_query.limit == 1
    assert captured_query.source_ids == ["message_turn:turn-current-user"]
    metadata_filters = {
        metadata_filter.key: metadata_filter.value
        for metadata_filter in captured_query.metadata_filters
    }
    assert metadata_filters["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_get_message_history_tool_returns_invalid_request_for_bad_datetime(
    db_engine: AsyncEngine,
) -> None:
    """Malformed time filters are returned as tool errors instead of exceptions."""
    async with DatabaseContext(engine=db_engine) as db:
        result = await get_message_history_tool(
            exec_context=_build_exec_context(db),
            query="passport",
            start_time="not-a-date",
        )

    data = cast("dict[str, Any]", result.data)
    assert data["error"] == "invalid_request"
    assert data["message"] == "start_time must be an ISO datetime."
    assert data["results"] == []


@pytest.mark.asyncio
async def test_message_history_indexer_projects_turn_into_document_index(
    db_engine: AsyncEngine,
) -> None:
    """The background task stores one searchable document and embedding per turn."""
    async with DatabaseContext(engine=db_engine) as db:
        timestamp = datetime.now(UTC)
        await _store_user_message(
            db,
            conversation_id="current",
            user_id="user-a",
            content="Let's discuss school forms",
            timestamp=timestamp,
            turn_id="turn-school",
        )
        await db.message_history.add_message(
            AssistantMessage(content="The school forms are due Monday."),
            interface_type="test",
            conversation_id="current",
            timestamp=timestamp + timedelta(seconds=1),
            turn_id="turn-school",
            user_id="user-a",
            processing_profile_id="default",
        )

        context = _build_exec_context(
            db,
            embedding_generator=MockEmbeddingGenerator(dimensions=3),
        )
        await handle_index_message_history_batch(
            context,
            {"turn_id": "turn-school"},
        )

        document = await db.fetch_one(
            select(DocumentRecord.source_type).where(
                DocumentRecord.source_id == "message_turn:turn-school"
            )
        )
        embedding = await db.fetch_one(
            select(
                DocumentEmbeddingRecord.embedding_type,
                DocumentEmbeddingRecord.content,
            ).join(
                DocumentRecord,
                DocumentEmbeddingRecord.document_id == DocumentRecord.id,
            )
        )

    assert document is not None
    assert document["source_type"] == "message_history"
    assert embedding is not None
    assert embedding["embedding_type"] == "message_turn"
    assert "school forms" in embedding["content"]


async def _store_user_message(
    db: DatabaseContext,
    *,
    conversation_id: str,
    user_id: str,
    content: str,
    timestamp: datetime,
    turn_id: str | None = None,
) -> None:
    await db.message_history.add_message(
        UserMessage(content=content),
        interface_type="test",
        conversation_id=conversation_id,
        timestamp=timestamp,
        turn_id=turn_id,
        user_id=user_id,
        processing_profile_id="default",
    )


def _build_exec_context(
    db: DatabaseContext,
    *,
    embedding_generator: MockEmbeddingGenerator | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="current",
        user_name="Alex",
        user_id="user-a",
        turn_id="turn-current",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        processing_profile_id="default",
        embedding_generator=embedding_generator,
    )
