"""Functional tests for message-history querying and indexing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.embeddings import EmbeddingGenerator, MockEmbeddingGenerator
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
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.repositories.message_history import (
    MessageHistoryAccessDeniedError,
    MessageHistoryQuery,
)
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
async def test_message_history_query_searches_tool_call_text(
    db_engine: AsyncEngine,
) -> None:
    """Structured text search includes assistant tool-call arguments."""
    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
async def test_get_latest_user_profile_id_follows_most_recent_user_message(
    db_engine: AsyncEngine,
) -> None:
    """Adoption uses the most recent *user* message's profile, not a delegated row."""
    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
    assert {
        metadata_filter.key for metadata_filter in captured_query.metadata_filters
    } == {"interface_type", "processing_profile_id"}


@pytest.mark.asyncio
async def test_semantic_history_search_returns_rows_from_matched_turn(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turn-level semantic hits surface the turn rows, not only the first row."""
    async with DatabaseContext(engine=db_engine) as db:
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
            db_context: DatabaseContext,
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
    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
async def test_get_message_history_tool_excludes_internal_rows(
    db_engine: AsyncEngine,
) -> None:
    """The model-facing history tool does not expose hidden wake rows."""
    async with DatabaseContext(engine=db_engine) as db:
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
        db_context: DatabaseContext,
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

    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
    async with DatabaseContext(engine=db_engine) as db:
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
            db_context: DatabaseContext,
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
    async with DatabaseContext(engine=db_engine) as db:
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
    """Message writes fail instead of silently losing semantic indexing."""

    async def fail_enqueue(*args: object, **kwargs: object) -> None:
        _ = args, kwargs
        raise RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError, match="queue unavailable"):
        async with DatabaseContext(engine=db_engine) as db:
            monkeypatch.setattr(db.tasks, "enqueue", fail_enqueue)
            await db.message_history.add_message(
                UserMessage(content="Index me"),
                interface_type="test",
                conversation_id="current",
                timestamp=datetime.now(UTC),
                user_id="user-a",
                processing_profile_id="default",
            )


@pytest.mark.asyncio
async def test_message_history_backfill_task_is_seeded_as_system_task(
    db_engine: AsyncEngine,
) -> None:
    """Startup can seed a one-time backfill for preexisting message history."""
    async with DatabaseContext(engine=db_engine) as db:
        await enqueue_message_history_backfill_task(db, limit=17)

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


@pytest.mark.asyncio
async def test_message_history_indexer_excludes_internal_rows(
    db_engine: AsyncEngine,
) -> None:
    """Hidden rows do not contribute text to the message-history search index."""
    async with DatabaseContext(engine=db_engine) as db:
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
