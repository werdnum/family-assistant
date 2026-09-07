"""Functional tests for message-history querying and indexing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from family_assistant.embeddings import (
    EmbeddingGenerator,
    EmbeddingResult,
    MockEmbeddingGenerator,
)
from family_assistant.indexing.message_history_indexer import (
    MESSAGE_HISTORY_BACKFILL_TASK_ID,
    enqueue_message_history_backfill_task,
    handle_index_message_history_batch,
)
from family_assistant.llm.messages import (
    AssistantMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
)
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.message_history import (
    MESSAGE_HISTORY_INDEX_DELAY,
    MessageHistoryAccessDeniedError,
    MessageHistoryQuery,
)
from family_assistant.storage.repositories.tasks import TasksRepository
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import DocumentEmbeddingRecord, DocumentRecord
from family_assistant.storage.vector_search import VectorSearchQuery, query_vector_store
from family_assistant.tools.communication import (
    COMMUNICATION_TOOLS_DEFINITION,
    get_message_history_tool,
)
from family_assistant.tools.documents import search_documents_tool
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_message_history_query_defaults_to_same_user_scope(
    db_engine: AsyncEngine,
) -> None:
    """same_user finds another conversation for the same user but not other users."""
    db = Database(engine=db_engine)
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
    db = Database(engine=db_engine)
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
async def test_message_history_query_searches_tool_call_text(
    db_engine: AsyncEngine,
) -> None:
    """Structured text search includes assistant tool-call arguments."""
    db = Database(engine=db_engine)
    now = datetime.now(UTC)
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
        timestamp=now,
        turn_id="turn-1",
        user_id="user-a",
        processing_profile_id="default",
    )

    rows = await db.message_history.query_history(
        MessageHistoryQuery(
            query="passport",
            scope="current_conversation",
            current_conversation_id="current",
            interface_type="test",
            processing_profile_id="default",
            limit=10,
        )
    )

    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] is None
    assert rows[0]["tool_calls"]


@pytest.mark.asyncio
async def test_message_history_query_excludes_internal_rows_by_default(
    db_engine: AsyncEngine,
) -> None:
    """Hidden wake rows are not returned or included as neighboring context."""
    db = Database(engine=db_engine)
    now = datetime.now(UTC)
    await db.message_history.add_message(
        UserMessage(content="Hidden delegated result data"),
        interface_type="test",
        conversation_id="current",
        timestamp=now,
        user_id="user-a",
        processing_profile_id="default",
        is_internal=True,
    )
    await db.message_history.add_message(
        UserMessage(content="Visible passport note"),
        interface_type="test",
        conversation_id="current",
        timestamp=now + timedelta(seconds=1),
        user_id="user-a",
        processing_profile_id="default",
    )
    await db.message_history.add_message(
        SystemMessage(content="Hidden delegated wake instruction"),
        interface_type="test",
        conversation_id="current",
        timestamp=now + timedelta(seconds=2),
        user_id="user-a",
        processing_profile_id="default",
        is_internal=True,
    )

    hidden_rows = await db.message_history.query_history(
        MessageHistoryQuery(
            query="delegated",
            scope="current_conversation",
            current_conversation_id="current",
            interface_type="test",
            processing_profile_id="default",
            limit=10,
        )
    )
    opted_in_rows = await db.message_history.query_history(
        MessageHistoryQuery(
            query="delegated",
            scope="current_conversation",
            current_conversation_id="current",
            interface_type="test",
            processing_profile_id="default",
            include_internal=True,
            limit=10,
        )
    )
    visible_rows = await db.message_history.query_history(
        MessageHistoryQuery(
            query="passport",
            scope="current_conversation",
            current_conversation_id="current",
            interface_type="test",
            processing_profile_id="default",
            limit=10,
        )
    )
    hydrated = await db.message_history.hydrate_history_results(
        visible_rows,
        include_context=1,
        access_query=MessageHistoryQuery(
            scope="current_conversation",
            current_conversation_id="current",
            interface_type="test",
            processing_profile_id="default",
        ),
    )

    assert hidden_rows == []
    assert {row["content"] for row in opted_in_rows} == {
        "Hidden delegated result data",
        "Hidden delegated wake instruction",
    }
    assert len(hydrated) == 1
    context = hydrated[0].get("context")
    assert context is not None
    assert [row["content"] for row in context] == ["Visible passport note"]


@pytest.mark.asyncio
async def test_same_user_scope_keeps_user_filter_when_conversation_id_is_supplied(
    db_engine: AsyncEngine,
) -> None:
    """A guessed conversation_id cannot bypass same-user history isolation."""
    db = Database(engine=db_engine)
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
async def test_get_latest_user_profile_id_follows_most_recent_user_message(
    db_engine: AsyncEngine,
) -> None:
    """Adoption uses the most recent *user* message's profile, not a delegated row."""
    db = Database(engine=db_engine)
    now = datetime.now(UTC)
    await db.message_history.add_message(
        UserMessage(content="Research and book it"),
        interface_type="test",
        conversation_id="conv-adopt",
        timestamp=now,
        turn_id="turn-1",
        user_id="user-a",
        processing_profile_id="complex_tasks",
    )
    # A later delegated assistant row tagged with a different profile must not
    # win — adoption follows the profile the user was actually talking to.
    await db.message_history.add_message(
        AssistantMessage(content="Handing off to the engineer."),
        interface_type="test",
        conversation_id="conv-adopt",
        timestamp=now + timedelta(seconds=1),
        turn_id="turn-1",
        user_id="user-a",
        processing_profile_id="engineer",
    )

    latest = await db.message_history.get_latest_user_profile_id("conv-adopt")

    assert latest == "complex_tasks"


@pytest.mark.asyncio
async def test_get_latest_user_profile_id_ignores_subconversations_and_empty(
    db_engine: AsyncEngine,
) -> None:
    """Subconversation rows are excluded by default; no user rows yields None."""
    db = Database(engine=db_engine)
    now = datetime.now(UTC)
    # The newest user row lives in a delegated subconversation under a
    # different profile; the foreground page omits it, so adoption must too.
    await db.message_history.add_message(
        UserMessage(content="Top-level question"),
        interface_type="test",
        conversation_id="conv-sub",
        timestamp=now,
        turn_id="turn-1",
        user_id="user-a",
        processing_profile_id="default_assistant",
    )
    await db.message_history.add_message(
        UserMessage(content="Delegated sub-question"),
        interface_type="test",
        conversation_id="conv-sub",
        timestamp=now + timedelta(seconds=5),
        turn_id="turn-2",
        user_id="user-a",
        processing_profile_id="engineer",
        subconversation_id="sub-1",
    )

    latest = await db.message_history.get_latest_user_profile_id("conv-sub")
    empty = await db.message_history.get_latest_user_profile_id("conv-missing")

    assert latest == "default_assistant"
    assert empty is None


@pytest.mark.asyncio
async def test_message_history_query_denies_all_accessible_scope(
    db_engine: AsyncEngine,
) -> None:
    """Broadened all_accessible scope is denied without policy support."""
    db = Database(engine=db_engine)
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
    db = Database(engine=db_engine)
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
async def test_get_message_history_tool_merges_returned_row_taint(
    db_engine: AsyncEngine,
) -> None:
    """Reading tainted stored history should reintroduce taint into the turn."""
    tracker = InMemoryTurnTaintTracker()
    taint_source = TaintSource(
        source_type=TaintSourceType.EMAIL,
        source_id="email-123",
        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        labels=frozenset({"source_unknown_external"}),
        reason="Stored email reply taint.",
    )
    taint_metadata = InMemoryTurnTaintTracker().add_source(taint_source).to_metadata()
    db = Database(engine=db_engine)
    await db.message_history.add_message(
        AssistantMessage(
            content="External email said the pickup code is 1234.",
            taint_metadata=taint_metadata,
        ),
        interface_type="test",
        conversation_id="current",
        timestamp=datetime.now(UTC),
        turn_id="turn-email",
        user_id="user-a",
        processing_profile_id="default",
    )

    result = await get_message_history_tool(
        exec_context=_build_exec_context(db, taint_tracker=tracker),
        query="pickup code",
        roles=["assistant"],
    )

    data = cast("dict[str, Any]", result.data)
    assert data["result_count"] == 1
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_semantic_history_search_prefilters_access_before_vector_limit(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic search applies accessible source IDs before vector-store limiting."""
    db = Database(engine=db_engine)
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
        db_context: Database,
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
    assert {
        metadata_filter.key for metadata_filter in captured_query.metadata_filters
    } == {"interface_type", "processing_profile_id"}


@pytest.mark.asyncio
async def test_semantic_history_search_returns_rows_from_matched_turn(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn-level semantic hits surface the turn rows, not only the first row."""
    db = Database(engine=db_engine)
    timestamp = datetime.now(UTC)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Where did we put the travel documents?",
        timestamp=timestamp,
        turn_id="turn-travel-docs",
    )
    await db.message_history.add_message(
        AssistantMessage(content="The passports are in the blue folder."),
        interface_type="test",
        conversation_id="current",
        timestamp=timestamp + timedelta(seconds=1),
        turn_id="turn-travel-docs",
        user_id="user-a",
        processing_profile_id="default",
    )

    async def fake_query_vector_store(
        *,
        db_context: Database,
        query: VectorSearchQuery,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, object]]:
        _ = db_context, query, query_embedding
        return [{"source_id": "message_turn:turn-travel-docs"}]

    monkeypatch.setattr(
        "family_assistant.tools.communication.query_vector_store",
        fake_query_vector_store,
    )

    result = await get_message_history_tool(
        exec_context=_build_exec_context(
            db,
            embedding_generator=MockEmbeddingGenerator(dimensions=3),
        ),
        query="passports",
        search_mode="semantic",
        limit=1,
    )

    data = cast("dict[str, Any]", result.data)
    assert "error" not in data, data
    assert [row["content"] for row in data["results"]] == [
        "Where did we put the travel documents?",
        "The passports are in the blue folder.",
    ]


@pytest.mark.asyncio
async def test_message_history_context_preserves_same_user_scope(
    db_engine: AsyncEngine,
) -> None:
    """Context hydration does not leak neighboring rows from other users."""
    db = Database(engine=db_engine)
    timestamp = datetime.now(UTC)
    await _store_user_message(
        db,
        conversation_id="shared",
        user_id="user-b",
        content="Other user's previous message",
        timestamp=timestamp,
    )
    await _store_user_message(
        db,
        conversation_id="shared",
        user_id="user-a",
        content="My passport note",
        timestamp=timestamp + timedelta(seconds=1),
    )
    await _store_user_message(
        db,
        conversation_id="shared",
        user_id="user-b",
        content="Other user's next message",
        timestamp=timestamp + timedelta(seconds=2),
    )

    result = await get_message_history_tool(
        exec_context=_build_exec_context(db),
        query="passport",
        conversation_id="shared",
        include_context=2,
    )

    data = cast("dict[str, Any]", result.data)
    assert "error" not in data, data
    assert data["result_count"] == 1
    assert data["results"][0]["content"] == "My passport note"
    assert [row["content"] for row in data["results"][0]["context"]] == [
        "My passport note"
    ]


@pytest.mark.asyncio
async def test_same_user_scope_includes_current_conversation_null_user_rows(
    db_engine: AsyncEngine,
) -> None:
    """same_user can see system/callback rows without user IDs in the current chat."""
    db = Database(engine=db_engine)
    timestamp = datetime.now(UTC)
    await db.message_history.add_message(
        SystemMessage(content="Current callback reminder"),
        interface_type="test",
        conversation_id="current",
        timestamp=timestamp,
        processing_profile_id="default",
    )
    await db.message_history.add_message(
        SystemMessage(content="Other callback reminder"),
        interface_type="test",
        conversation_id="other",
        timestamp=timestamp + timedelta(seconds=1),
        processing_profile_id="default",
    )

    rows = await db.message_history.query_history(
        MessageHistoryQuery(
            query="callback",
            current_conversation_id="current",
            current_user_id="user-a",
            processing_profile_id="default",
            limit=10,
        )
    )

    assert [row["content"] for row in rows] == ["Current callback reminder"]


@pytest.mark.asyncio
async def test_get_message_history_tool_returns_invalid_request_for_bad_datetime(
    db_engine: AsyncEngine,
) -> None:
    """Malformed time filters are returned as tool errors instead of exceptions."""
    db = Database(engine=db_engine)
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
async def test_get_message_history_tool_excludes_internal_rows(
    db_engine: AsyncEngine,
) -> None:
    """The model-facing history tool does not expose hidden wake rows."""
    db = Database(engine=db_engine)
    await db.message_history.add_message(
        UserMessage(content="Hidden delegated completion payload"),
        interface_type="test",
        conversation_id="current",
        timestamp=datetime.now(UTC),
        user_id="user-a",
        processing_profile_id="default",
        is_internal=True,
    )
    result = await get_message_history_tool(
        exec_context=_build_exec_context(db),
        query="delegated",
    )

    data = cast("dict[str, Any]", result.data)
    assert "error" not in data, data
    assert data["result_count"] == 0
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_documents_excludes_message_history_source(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """General document search never searches message-history index entries."""
    captured_query: VectorSearchQuery | None = None

    async def fake_query_vector_store(
        *,
        db_context: Database,
        query: VectorSearchQuery,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, object]]:
        nonlocal captured_query
        _ = db_context, query_embedding
        captured_query = query
        return []

    monkeypatch.setattr(
        "family_assistant.tools.documents.query_vector_store",
        fake_query_vector_store,
    )

    db = Database(engine=db_engine)
    result = await search_documents_tool(
        exec_context=_build_exec_context(db),
        embedding_generator=MockEmbeddingGenerator(dimensions=3),
        query="passport",
    )

    assert result == "No relevant documents found matching the query and filters."
    assert captured_query is not None
    assert captured_query.excluded_source_types == ["message_history"]


@pytest.mark.asyncio
async def test_vector_search_excludes_message_history_without_source_acl(
    db_engine: AsyncEngine,
) -> None:
    """Raw vector search does not expose message history without source IDs."""
    db = Database(engine=db_engine)
    query = VectorSearchQuery(
        search_type="semantic",
        semantic_query="passport",
        embedding_model="mock-embedding-model",
        source_types=["message_history"],
        embedding_types=["message_turn"],
        limit=5,
    )
    results = await query_vector_store(
        db_context=db,
        query=query,
        query_embedding=[0.0, 0.0, 0.0],
    )

    assert results == []


@pytest.mark.asyncio
async def test_local_tools_provider_injects_optional_embedding_generator(
    db_engine: AsyncEngine,
) -> None:
    """Embedding injection recognizes EmbeddingGenerator | None annotations."""

    async def optional_embedding_tool(
        embedding_generator: EmbeddingGenerator | None = None,
    ) -> str:
        assert embedding_generator is not None
        return embedding_generator.model_name

    provider = LocalToolsProvider(
        definitions=[
            {
                "type": "function",
                "function": {
                    "name": "optional_embedding",
                    "description": "Test optional embedding injection.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        implementations={"optional_embedding": optional_embedding_tool},
        embedding_generator=MockEmbeddingGenerator(dimensions=3),
    )
    db = Database(engine=db_engine)
    result = await provider.execute_tool(
        "optional_embedding",
        {},
        _build_exec_context(db),
    )

    assert result == "mock-embedding-model"


@pytest.mark.asyncio
async def test_local_tools_provider_injects_embedding_generator_for_history_tool(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real message-history tool receives provider-level embeddings."""
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="The passports are in the blue folder",
        timestamp=datetime.now(UTC),
        turn_id="turn-passport",
    )

    captured_query_embedding: list[float] | None = None

    async def fake_query_vector_store(
        *,
        db_context: Database,
        query: VectorSearchQuery,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, object]]:
        nonlocal captured_query_embedding
        _ = db_context, query
        captured_query_embedding = query_embedding
        return [{"source_id": "message_turn:turn-passport"}]

    monkeypatch.setattr(
        "family_assistant.tools.communication.query_vector_store",
        fake_query_vector_store,
    )
    provider = LocalToolsProvider(
        definitions=COMMUNICATION_TOOLS_DEFINITION,
        implementations={"get_message_history": get_message_history_tool},
        embedding_generator=MockEmbeddingGenerator(
            dimensions=3,
            embedding_map={"passport": [0.1, 0.2, 0.3]},
        ),
    )

    result = await provider.execute_tool(
        "get_message_history",
        {"query": "passport", "search_mode": "semantic"},
        _build_exec_context(db),
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.data)
    assert "error" not in data, data
    assert data["result_count"] == 1
    assert data["results"][0]["content"] == "The passports are in the blue folder"
    assert captured_query_embedding == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_local_tools_provider_allows_structured_history_without_embeddings(
    db_engine: AsyncEngine,
) -> None:
    """Optional embedding dependencies do not block structured history queries."""
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="The passports are in the blue folder",
        timestamp=datetime.now(UTC),
    )
    provider = LocalToolsProvider(
        definitions=COMMUNICATION_TOOLS_DEFINITION,
        implementations={"get_message_history": get_message_history_tool},
    )

    result = await provider.execute_tool(
        "get_message_history",
        {"query": "passport", "search_mode": "structured"},
        _build_exec_context(db),
    )

    assert isinstance(result, ToolResult)
    data = cast("dict[str, Any]", result.data)
    assert "error" not in data, data
    assert data["result_count"] == 1
    assert data["results"][0]["content"] == "The passports are in the blue folder"


@pytest.mark.asyncio
async def test_add_message_surfaces_index_enqueue_failures(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message write that cannot queue its indexing fails and keeps no row.

    Patched on the class, not on one repository instance: the insert and the
    enqueue share a transaction, so the enqueue runs against a transaction-bound
    repository rather than the handle's.
    """

    async def fail_enqueue(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(TasksRepository, "enqueue", fail_enqueue)

    db = Database(engine=db_engine)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await db.message_history.add_message(
            UserMessage(content="Index me"),
            interface_type="test",
            conversation_id="current",
            timestamp=datetime.now(UTC),
            user_id="user-a",
            processing_profile_id="default",
        )

    monkeypatch.undo()
    rows = await db.fetch_all(
        select(message_history_table).where(
            message_history_table.c.conversation_id == "current"
        )
    )
    assert rows == [], "the message must roll back with its unqueued indexing"


@pytest.mark.asyncio
async def test_add_message_raises_on_a_database_write_failure(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write must not come back as None.

    Callers that read None as merely "no id" would carry on -- continuing an
    LLM turn whose prompt or assistant checkpoint was never committed.
    """

    async def fail_enqueue(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(TasksRepository, "enqueue", fail_enqueue)

    db = Database(engine=db_engine)
    with pytest.raises(SQLAlchemyError, match="database unavailable"):
        await db.message_history.add_message(
            UserMessage(content="Never stored"),
            interface_type="test",
            conversation_id="write-failure",
            timestamp=datetime.now(UTC),
            user_id="user-a",
            processing_profile_id="default",
        )


@pytest.mark.asyncio
async def test_message_history_backfill_task_is_seeded_as_system_task(
    db_engine: AsyncEngine,
) -> None:
    """Startup can seed a one-time backfill for preexisting message history."""
    db = Database(engine=db_engine)
    await enqueue_message_history_backfill_task(
        db, embedding_model="embedding-v1", limit=17
    )

    task_row = await db.fetch_one(
        select(tasks_table).where(
            tasks_table.c.task_id == MESSAGE_HISTORY_BACKFILL_TASK_ID
        )
    )

    assert task_row is not None
    assert task_row["task_type"] == "index_message_history_batch"
    assert task_row["payload"] == {"limit": 17}
    assert task_row["status"] == "pending"


@pytest.mark.asyncio
async def test_message_history_backfill_seed_leaves_a_walk_in_progress_alone(
    db_engine: AsyncEngine,
) -> None:
    """A restart must not rewind the cursor of a backfill already under way.

    The seed is a system task, and a system-task enqueue upserts: it overwrites
    the payload and revives a finished row. Left at that, every process start
    would restart the walk at the beginning of a corpus that only grows, and
    re-embed all of it -- which is what the embedding bill showed.
    """
    db = Database(engine=db_engine)
    task_id = MESSAGE_HISTORY_BACKFILL_TASK_ID
    await enqueue_message_history_backfill_task(
        db, embedding_model="embedding-v1", limit=17
    )
    await db.execute(
        tasks_table
        .update()
        .where(tasks_table.c.task_id == task_id)
        .values(payload={"limit": 17, "after_internal_id": 4200}, status="done")
    )

    await enqueue_message_history_backfill_task(
        db, embedding_model="embedding-v1", limit=17
    )

    task_row = await db.fetch_one(
        select(tasks_table).where(tasks_table.c.task_id == task_id)
    )
    assert task_row is not None
    assert task_row["payload"] == {"limit": 17, "after_internal_id": 4200}
    assert task_row["status"] == "done"


@pytest.mark.asyncio
async def test_message_history_backfill_reruns_when_the_corpus_uses_another_model(
    db_engine: AsyncEngine,
) -> None:
    """A finished walk is cleared when stored turns answer to a different model.

    Search matches `embedding_model` exactly, so those turns answer nothing.
    Reading the corpus rather than keying the seed on the model is what makes
    a rollback work too: returning to a model the corpus has since been
    re-embedded away from needs the same repair as moving to a new one.
    """
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Indexed under the old model",
        timestamp=datetime.now(UTC),
        turn_id="turn-migrated",
    )
    await _index_turn(db, _CountingEmbeddingGenerator("embedding-v1"), "turn-migrated")

    await enqueue_message_history_backfill_task(db, embedding_model="embedding-v1")
    await db.execute(
        tasks_table
        .update()
        .where(tasks_table.c.task_id == MESSAGE_HISTORY_BACKFILL_TASK_ID)
        .values(status="done", payload={"limit": 50, "after_internal_id": 4200})
    )

    await enqueue_message_history_backfill_task(db, embedding_model="embedding-v2")

    task_row = await db.fetch_one(
        select(tasks_table).where(
            tasks_table.c.task_id == MESSAGE_HISTORY_BACKFILL_TASK_ID
        )
    )
    assert task_row is not None
    assert task_row["status"] == "pending"
    assert task_row["payload"] == {"limit": 50}, "the walk restarts from the top"


@pytest.mark.asyncio
async def test_message_history_backfill_leaves_a_running_migration_alone(
    db_engine: AsyncEngine,
) -> None:
    """A restart mid-migration resumes the walk rather than rewinding it."""
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Indexed under the old model",
        timestamp=datetime.now(UTC),
        turn_id="turn-migrating",
    )
    await _index_turn(db, _CountingEmbeddingGenerator("embedding-v1"), "turn-migrating")

    await enqueue_message_history_backfill_task(db, embedding_model="embedding-v2")
    await db.execute(
        tasks_table
        .update()
        .where(tasks_table.c.task_id == MESSAGE_HISTORY_BACKFILL_TASK_ID)
        .values(payload={"limit": 50, "after_internal_id": 4200})
    )

    await enqueue_message_history_backfill_task(db, embedding_model="embedding-v2")

    task_row = await db.fetch_one(
        select(tasks_table).where(
            tasks_table.c.task_id == MESSAGE_HISTORY_BACKFILL_TASK_ID
        )
    )
    assert task_row is not None
    assert task_row["payload"] == {"limit": 50, "after_internal_id": 4200}


@pytest.mark.asyncio
async def test_background_indexing_never_enqueues_ahead_of_scheduled_work(
    db_engine: AsyncEngine,
) -> None:
    """Backfill tasks carry a scheduled time so they cannot starve the queue.

    Dequeue orders by `scheduled_at` with nulls first, so an unscheduled task
    outranks every scheduled one. A backfill is a long chain of them, and left
    unscheduled it would sit in front of reminders and automations for as long
    as the walk lasts.
    """
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Seed content",
        timestamp=datetime.now(UTC),
        turn_id="turn-scheduled",
    )
    await enqueue_message_history_backfill_task(db, embedding_model="embedding-v1")
    await handle_index_message_history_batch(
        _build_exec_context(db, embedding_generator=_CountingEmbeddingGenerator()),
        {"limit": 50},
    )

    rows = await db.fetch_all(
        select(tasks_table).where(
            tasks_table.c.task_type == "index_message_history_batch"
        )
    )
    assert rows
    assert all(row["scheduled_at"] is not None for row in rows), (
        "every background indexing task must be scheduled, never immediate"
    )


@pytest.mark.asyncio
async def test_forced_backfill_reembeds_content_the_fingerprint_calls_current(
    db_engine: AsyncEngine,
) -> None:
    """`force` is the way to rebuild after a change the fingerprint cannot see."""
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Where are the passports?",
        timestamp=datetime.now(UTC),
        turn_id="turn-forced",
    )
    generator = _CountingEmbeddingGenerator()
    await _index_turn(db, generator, "turn-forced")

    await handle_index_message_history_batch(
        _build_exec_context(db, embedding_generator=generator),
        {"turn_id": "turn-forced", "force": True},
    )

    assert len(generator.embedded_texts) == 2


@pytest.mark.asyncio
async def test_persisted_message_defers_its_turn_indexing_task(
    db_engine: AsyncEngine,
) -> None:
    """Indexing waits for the turn, so the turn is embedded once rather than per row."""
    db = Database(engine=db_engine)
    before = datetime.now(UTC)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Deferred indexing",
        timestamp=before,
        turn_id="turn-deferred",
    )

    task_row = await db.fetch_one(
        select(tasks_table).where(
            tasks_table.c.task_type == "index_message_history_batch"
        )
    )
    assert task_row is not None
    scheduled_at = task_row["scheduled_at"]
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    assert scheduled_at >= before + MESSAGE_HISTORY_INDEX_DELAY


@pytest.mark.asyncio
async def test_message_history_indexer_projects_turn_into_document_index(
    db_engine: AsyncEngine,
) -> None:
    """The background task stores one searchable document and embedding per turn."""
    db = Database(engine=db_engine)
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


@pytest.mark.asyncio
async def test_message_history_indexer_excludes_internal_rows(
    db_engine: AsyncEngine,
) -> None:
    """Hidden rows do not contribute text to the message-history search index."""
    db = Database(engine=db_engine)
    timestamp = datetime.now(UTC)
    await db.message_history.add_message(
        UserMessage(content="Hidden delegated wake payload"),
        interface_type="test",
        conversation_id="current",
        timestamp=timestamp,
        turn_id="turn-visible-only",
        user_id="user-a",
        processing_profile_id="default",
        is_internal=True,
    )
    await db.message_history.add_message(
        AssistantMessage(content="Visible source response"),
        interface_type="test",
        conversation_id="current",
        timestamp=timestamp + timedelta(seconds=1),
        turn_id="turn-visible-only",
        user_id="user-a",
        processing_profile_id="default",
    )

    context = _build_exec_context(
        db,
        embedding_generator=MockEmbeddingGenerator(dimensions=3),
    )
    await handle_index_message_history_batch(
        context,
        {"turn_id": "turn-visible-only"},
    )

    embedding = await db.fetch_one(
        select(DocumentEmbeddingRecord.content).join(
            DocumentRecord,
            DocumentEmbeddingRecord.document_id == DocumentRecord.id,
        )
    )

    assert embedding is not None
    assert "Visible source response" in embedding["content"]
    assert "Hidden delegated wake payload" not in embedding["content"]


async def _store_user_message(
    db: Database,
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


class _CountingEmbeddingGenerator:
    """Wraps a generator and records every text it was asked to embed.

    The cost this guards is the provider call, not the row it writes: the
    vector store upserts, so a redundant pass leaves the database identical and
    only the call count tells you it happened.
    """

    def __init__(self, model_name: str = "mock-embedding-model") -> None:
        self._inner = MockEmbeddingGenerator(model_name=model_name, dimensions=3)
        self.embedded_texts: list[str] = []

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        self.embedded_texts.extend(texts)
        return await self._inner.generate_embeddings(texts)


async def _index_turn(
    db: Database,
    generator: _CountingEmbeddingGenerator,
    turn_id: str,
) -> None:
    await handle_index_message_history_batch(
        _build_exec_context(db, embedding_generator=generator),
        {"turn_id": turn_id},
    )


@pytest.mark.asyncio
async def test_message_history_indexer_skips_content_it_already_embedded(
    db_engine: AsyncEngine,
) -> None:
    """Re-indexing an unchanged turn costs a database read, not a provider call."""
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Where are the passports?",
        timestamp=datetime.now(UTC),
        turn_id="turn-repeat",
    )
    generator = _CountingEmbeddingGenerator()

    await _index_turn(db, generator, "turn-repeat")
    await _index_turn(db, generator, "turn-repeat")
    await _index_turn(db, generator, "turn-repeat")

    assert len(generator.embedded_texts) == 1


@pytest.mark.asyncio
async def test_message_history_indexer_reembeds_a_turn_whose_content_grew(
    db_engine: AsyncEngine,
) -> None:
    """A turn that gained a message is genuinely different, so it is embedded again."""
    db = Database(engine=db_engine)
    timestamp = datetime.now(UTC)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Where are the passports?",
        timestamp=timestamp,
        turn_id="turn-grows",
    )
    generator = _CountingEmbeddingGenerator()
    await _index_turn(db, generator, "turn-grows")

    await db.message_history.add_message(
        AssistantMessage(content="In the blue folder."),
        interface_type="test",
        conversation_id="current",
        timestamp=timestamp + timedelta(seconds=1),
        turn_id="turn-grows",
        user_id="user-a",
        processing_profile_id="default",
    )
    await _index_turn(db, generator, "turn-grows")

    assert len(generator.embedded_texts) == 2
    assert "blue folder" in generator.embedded_texts[1]


@pytest.mark.asyncio
async def test_message_history_indexer_reembeds_when_the_model_changes(
    db_engine: AsyncEngine,
) -> None:
    """Identity includes the model, so a migration re-indexes unchanged content."""
    db = Database(engine=db_engine)
    await _store_user_message(
        db,
        conversation_id="current",
        user_id="user-a",
        content="Where are the passports?",
        timestamp=datetime.now(UTC),
        turn_id="turn-remodel",
    )
    await _index_turn(db, _CountingEmbeddingGenerator("embedding-v1"), "turn-remodel")

    successor = _CountingEmbeddingGenerator("embedding-v2")
    await _index_turn(db, successor, "turn-remodel")

    assert len(successor.embedded_texts) == 1
    stored = await db.fetch_one(
        select(DocumentEmbeddingRecord.embedding_model).join(
            DocumentRecord,
            DocumentEmbeddingRecord.document_id == DocumentRecord.id,
        )
    )
    assert stored is not None
    assert stored["embedding_model"] == "embedding-v2"


def _build_exec_context(
    db: Database,
    *,
    embedding_generator: EmbeddingGenerator | None = None,
    taint_tracker: InMemoryTurnTaintTracker | None = None,
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
        taint_tracker=taint_tracker,
        credential_resolvers=None,
        api_backend=None,
    )


async def _seed_conversation(
    db: Database,
    conversation_id: str,
    *,
    timestamp: datetime,
    owner: str,
    extra_owner: str | None = None,
    message_pairs: int = 1,
) -> None:
    """Write ``message_pairs`` user/assistant pairs, all sharing one timestamp.

    A shared timestamp is the interesting case for the summaries query: it is
    what forces the latest-message choice onto the ``internal_id`` tie-break.
    """
    for pair in range(message_pairs):
        await db.message_history.add_message(
            UserMessage(content=f"user {pair} in {conversation_id}"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=timestamp,
            turn_id=f"{conversation_id}-{pair}",
            processing_profile_id="default",
            user_id=owner,
        )
        await db.message_history.add_message(
            AssistantMessage(content=f"assistant {pair} in {conversation_id}"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=timestamp,
            turn_id=f"{conversation_id}-{pair}",
            processing_profile_id="default",
            user_id=None,
        )
    if extra_owner is not None:
        await db.message_history.add_message(
            UserMessage(content=f"foreign user in {conversation_id}"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=timestamp,
            turn_id=f"{conversation_id}-foreign",
            processing_profile_id="default",
            user_id=extra_owner,
        )


@pytest.mark.asyncio
async def test_conversation_summaries_paginate_without_gaps_or_repeats(
    db_engine: AsyncEngine,
) -> None:
    """Paging the whole list yields every conversation exactly once.

    The iOS resync walks this endpoint page by page, so an order that is not
    total between two pages shows up as a conversation the client saw twice and
    another it never saw at all.
    """
    db = Database(engine=db_engine)
    owner = "pagination_owner"
    base = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)

    expected = []
    for i in range(12):
        conv_id = f"page_conv_{i:02d}"
        expected.append(conv_id)
        # Deliberately coarse: conversations 0-2, 3-5, ... share a latest
        # timestamp, so ordering by timestamp alone leaves ties to break.
        await _seed_conversation(
            db, conv_id, timestamp=base + timedelta(minutes=i // 3), owner=owner
        )

    seen: list[str] = []
    for offset in range(0, 12, 5):
        summaries, total = await db.message_history.get_conversation_summaries(
            limit=5,
            offset=offset,
            include_subconversations=False,
            owner_user_ids={owner},
        )
        assert total == 12
        seen.extend(s["conversation_id"] for s in summaries)

    assert len(seen) == len(set(seen)), f"conversation returned on two pages: {seen}"
    assert sorted(seen) == sorted(expected)


@pytest.mark.asyncio
async def test_conversation_summaries_page_matches_unpaged_slice(
    db_engine: AsyncEngine,
) -> None:
    """A page is the corresponding slice of the full ordered list."""
    db = Database(engine=db_engine)
    owner = "slice_owner"
    base = datetime(2026, 3, 2, 9, 0, 0, tzinfo=UTC)

    for i in range(9):
        await _seed_conversation(
            db,
            f"slice_conv_{i:02d}",
            timestamp=base + timedelta(minutes=i // 3),
            owner=owner,
        )

    full, _ = await db.message_history.get_conversation_summaries(
        limit=100, offset=0, include_subconversations=False, owner_user_ids={owner}
    )
    full_ids = [s["conversation_id"] for s in full]

    page, _ = await db.message_history.get_conversation_summaries(
        limit=4, offset=3, include_subconversations=False, owner_user_ids={owner}
    )

    assert [s["conversation_id"] for s in page] == full_ids[3:7]


@pytest.mark.asyncio
async def test_conversation_summaries_counts_and_ownership_survive_paging(
    db_engine: AsyncEngine,
) -> None:
    """Per-conversation counts and the sole-owner filter hold on a later page.

    The count is joined to the page rather than computed for every
    conversation, so it has to stay a property of the conversation and not of
    the slice it happened to land in.
    """
    db = Database(engine=db_engine)
    owner = "count_owner"
    base = datetime(2026, 3, 3, 9, 0, 0, tzinfo=UTC)

    await _seed_conversation(
        db, "count_conv_a", timestamp=base, owner=owner, message_pairs=1
    )
    await _seed_conversation(
        db,
        "count_conv_b",
        timestamp=base + timedelta(minutes=1),
        owner=owner,
        message_pairs=3,
    )
    await _seed_conversation(
        db,
        "count_conv_c",
        timestamp=base + timedelta(minutes=2),
        owner=owner,
        message_pairs=2,
    )
    # A second person posted here, so the caller does not solely own it.
    await _seed_conversation(
        db,
        "count_conv_shared",
        timestamp=base + timedelta(minutes=3),
        owner=owner,
        extra_owner="somebody_else",
    )

    first_page, total = await db.message_history.get_conversation_summaries(
        limit=2, offset=0, include_subconversations=False, owner_user_ids={owner}
    )
    second_page, second_total = await db.message_history.get_conversation_summaries(
        limit=2, offset=2, include_subconversations=False, owner_user_ids={owner}
    )

    counts = {
        s["conversation_id"]: s["message_count"] for s in (*first_page, *second_page)
    }
    assert "count_conv_shared" not in counts
    assert total == 3
    assert second_total == 3
    assert counts == {"count_conv_a": 2, "count_conv_b": 6, "count_conv_c": 4}


@pytest.mark.asyncio
async def test_conversation_summaries_preview_is_latest_message(
    db_engine: AsyncEngine,
) -> None:
    """The preview is the newest message even when every timestamp ties."""
    db = Database(engine=db_engine)
    owner = "preview_owner"
    stamp = datetime(2026, 3, 4, 9, 0, 0, tzinfo=UTC)

    await _seed_conversation(
        db, "preview_conv", timestamp=stamp, owner=owner, message_pairs=3
    )

    summaries, _ = await db.message_history.get_conversation_summaries(
        limit=10, offset=0, include_subconversations=False, owner_user_ids={owner}
    )

    assert len(summaries) == 1
    assert summaries[0]["last_message"] == "assistant 2 in preview_conv"


async def test_legacy_tool_call_without_an_id_is_skipped_not_raised(
    db_engine: AsyncEngine,
) -> None:
    """A tool call stored by an older shape must not break reading history.

    The writer serializes a ``ToolCallItem``, whose ``id``, ``type`` and
    ``function`` are all required, so a stored call missing one came from an
    earlier version of this code. Reading is shared by the chat API and the
    diagnostics export, and raising there costs the reader the whole
    conversation over one archived call — found by an extraction that read all
    history and hit `KeyError: 'id'`.
    """
    db = Database(engine=db_engine)
    now = datetime.now(UTC)
    await db.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="call-good",
                    type="function",
                    function=ToolCallFunction(name="add_calendar_event", arguments={}),
                )
            ],
        ),
        interface_type="test",
        conversation_id="legacy",
        timestamp=now,
        turn_id="turn-1",
        user_id="user-a",
        processing_profile_id="default",
    )

    # Rewrite the stored JSON into the legacy shape the writer can no longer
    # produce: one call missing "id", beside one that is intact.
    await db.execute(
        message_history_table
        .update()
        .where(message_history_table.c.conversation_id == "legacy")
        .values(
            tool_calls=[
                {
                    "type": "function",
                    "function": {"name": "add_calendar_event", "arguments": {}},
                },
                {
                    "id": "call-good",
                    "type": "function",
                    "function": {"name": "add_calendar_event", "arguments": {}},
                },
            ]
        )
    )

    grouped = await db.message_history.get_all_grouped(interface_type="test")

    messages = grouped[("test", "legacy")]
    assert len(messages) == 1
    tool_calls = messages[0]["tool_calls"]
    assert tool_calls is not None
    assert [call.id for call in tool_calls] == ["call-good"]


@pytest.mark.parametrize(
    "missing_field",
    ["type", "function", "function.name", "function.arguments"],
    ids=["no-type", "no-function", "no-name", "no-arguments"],
)
async def test_other_malformed_tool_call_shapes_are_skipped_too(
    db_engine: AsyncEngine, missing_field: str
) -> None:
    db = Database(engine=db_engine)
    now = datetime.now(UTC)
    await db.message_history.add_message(
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="call-1",
                    type="function",
                    function=ToolCallFunction(name="add_calendar_event", arguments={}),
                )
            ],
        ),
        interface_type="test",
        conversation_id="legacy",
        timestamp=now,
        turn_id="turn-1",
        user_id="user-a",
        processing_profile_id="default",
    )
    stored: dict[str, object] = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "add_calendar_event", "arguments": {}},
    }
    parent, _, leaf = missing_field.rpartition(".")
    target = cast("dict[str, object]", stored[parent]) if parent else stored
    del target[leaf]
    await db.execute(
        message_history_table
        .update()
        .where(message_history_table.c.conversation_id == "legacy")
        .values(tool_calls=[stored])
    )

    grouped = await db.message_history.get_all_grouped(interface_type="test")

    assert grouped[("test", "legacy")][0]["tool_calls"] == []
