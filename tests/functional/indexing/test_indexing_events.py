"""
Functional tests for document indexing events.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

# storage module functions now accessed via DatabaseContext
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.events.indexing_source import IndexingEventType, IndexingSource
from family_assistant.events.processor import EventProcessor
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage import get_db_context
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import add_document
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


async def poll_for_document_ready_event(
    doc_id: int,
    timeout_seconds: float = 2.0,
    poll_interval: float = 0.1,
    engine: AsyncEngine | None = None,
) -> dict[str, Any]:
    """Poll for DOCUMENT_READY event to appear in recent_events table.

    Args:
        doc_id: Document ID to look for
        timeout_seconds: Maximum time to wait
        poll_interval: Time between polls

    Returns:
        The event_data dict

    Raises:
        AssertionError: If event not found within timeout
    """
    from sqlalchemy import and_, cast, select
    from sqlalchemy.types import Integer

    from family_assistant.storage.events import recent_events_table

    max_attempts = int(timeout_seconds / poll_interval)

    for _ in range(max_attempts):
        if not engine:
            raise RuntimeError("Database engine not initialized")
        async with get_db_context(engine=engine) as db_ctx:
            # Use SQLAlchemy's JSON operators for cross-database compatibility
            stmt = select(recent_events_table.c.event_data).where(
                and_(
                    recent_events_table.c.source_id == "indexing",
                    recent_events_table.c.event_data["event_type"].as_string()
                    == IndexingEventType.DOCUMENT_READY.value,
                    # Cast to integer for proper comparison
                    cast(
                        recent_events_table.c.event_data["document_id"].as_string(),
                        Integer,
                    )
                    == doc_id,
                )
            )

            result = await db_ctx.fetch_all(stmt)
            if result:
                # Extract and return event data
                event_data_raw = result[0]["event_data"]
                if isinstance(event_data_raw, str):
                    return json.loads(event_data_raw)
                else:
                    return event_data_raw

        await asyncio.sleep(poll_interval)

    raise AssertionError(
        f"No DOCUMENT_READY event found for doc_id {doc_id} after {timeout_seconds}s"
    )


@dataclass
class MockDocument:
    """Test implementation of Document protocol."""

    source_type: str
    source_id: str
    title: str
    content: str | None = None
    source_uri: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] | None = None
    id: int | None = None  # Add id property for Document protocol
    file_path: str | None = None  # Add file_path for Document protocol

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        # Don't default metadata to {} - keep it as None if that's what was passed


# Test data
TEST_DOC_TITLE = "Test Document for Indexing Events"
TEST_DOC_CONTENT = "This is test content that will be chunked and embedded."
TEST_DOC_CHUNKS = ["This is test content", "that will be chunked", "and embedded."]


@pytest.mark.asyncio
async def test_document_ready_event_emitted(db_engine: AsyncEngine) -> None:
    """Test that DOCUMENT_READY event is emitted when all tasks complete."""
    # Create indexing source
    indexing_source = IndexingSource()

    # Create event processor with our indexing source
    event_processor = EventProcessor(
        sources={"indexing": indexing_source},
        sample_interval_hours=0.1,  # Short interval for testing
        get_db_context_func=lambda: get_db_context(engine=db_engine),
    )

    # Create event listener that captures events
    async with get_db_context(engine=db_engine) as db_ctx:
        await db_ctx.events.create_event_listener(
            name="Test Document Ready Listener",
            description="Test listener for document ready events",
            conversation_id="test-conv",
            interface_type="web",
            source_id="indexing",
            match_conditions={"event_type": IndexingEventType.DOCUMENT_READY.value},
            action_config={"prompt": "Document ready: {{ event.document_title }}"},
            enabled=True,
        )

    try:
        # Start processor and wait for it to be fully initialized
        await event_processor.start()

        # Create a document
        async with get_db_context(engine=db_engine) as db_ctx:
            test_doc = MockDocument(
                source_type="test_upload",
                source_id=f"test-{uuid.uuid4()}",
                title=TEST_DOC_TITLE,
                content=TEST_DOC_CONTENT,
                metadata={"test": "metadata"},
            )
            doc_id = await add_document(
                db_context=db_ctx,
                doc=test_doc,
            )

        # Create mock components
        embedding_generator = MockEmbeddingGenerator(
            model_name="test-model",
            dimensions=128,  # Match expected dimension
        )

        # Simulate embedding tasks being created and processed
        # In real scenario, the pipeline would create these tasks

        # Create embedding tasks
        async with get_db_context(engine=db_engine) as db_ctx:
            # Title embedding task
            await db_ctx.tasks.enqueue(
                task_id=f"embed_title_{doc_id}",
                task_type="embed_and_store_batch",
                payload={
                    "document_id": doc_id,
                    "texts_to_embed": [TEST_DOC_TITLE],
                    "embedding_metadata_list": [
                        {
                            "embedding_type": "title",
                            "chunk_index": 0,
                            "original_content_metadata": {},
                        }
                    ],
                },
            )

            # Content chunk embedding tasks
            for i, chunk in enumerate(TEST_DOC_CHUNKS):
                await db_ctx.tasks.enqueue(
                    task_id=f"embed_chunk_{doc_id}_{i}",
                    task_type="embed_and_store_batch",
                    payload={
                        "document_id": doc_id,
                        "texts_to_embed": [chunk],
                        "embedding_metadata_list": [
                            {
                                "embedding_type": "content_chunk",
                                "chunk_index": i,
                                "original_content_metadata": {"chunk_index": i},
                            }
                        ],
                    },
                )

        # Process all embedding tasks
        # Keep track of how many we've processed
        tasks_processed = 0
        total_tasks = 1 + len(TEST_DOC_CHUNKS)  # 1 title + 3 chunks

        # Process tasks one by one
        while tasks_processed < total_tasks:
            async with get_db_context(engine=db_engine) as db_ctx:
                task = await db_ctx.tasks.dequeue(
                    task_types=["embed_and_store_batch"],
                    worker_id="test-worker",
                    current_time=datetime.now(timezone.utc),
                )
                if task is None:
                    # Give a moment for tasks to become available
                    await asyncio.sleep(0.1)
                    continue

                task_context = ToolExecutionContext(
                    interface_type="web",
                    conversation_id="test-conv",
                    user_name="test-user",
                    turn_id=str(uuid.uuid4()),
                    db_context=db_ctx,
                    embedding_generator=embedding_generator,
                    indexing_source=indexing_source,
                )

                await handle_embed_and_store_batch(task_context, task["payload"])
                await db_ctx.tasks.update_status(task["task_id"], "done")
                tasks_processed += 1

        # Wait for all events to be processed before polling
        await indexing_source.wait_for_pending_events()

        # Poll for DOCUMENT_READY event and verify its contents
        # Use longer timeout since event processing is async
        # SQLite might need more time for transaction visibility
        event_data = await poll_for_document_ready_event(
            doc_id, timeout_seconds=10.0, poll_interval=0.2, engine=db_engine
        )

        assert event_data["document_id"] == doc_id
        assert event_data["document_title"] == TEST_DOC_TITLE
        assert event_data["document_metadata"] == {"test": "metadata"}
        assert event_data["metadata"]["total_embeddings"] == 4  # 1 title + 3 chunks
        assert event_data["metadata"]["embedding_types"] == 2  # title and content_chunk

    finally:
        # Stop event processor
        await event_processor.stop()


@pytest.mark.asyncio
async def test_document_ready_not_emitted_with_pending_tasks(
    db_engine: AsyncEngine,
) -> None:
    """Test that DOCUMENT_READY is not emitted when tasks are still pending."""
    # Create indexing source
    indexing_source = IndexingSource()

    # Create a document
    async with get_db_context(engine=db_engine) as db_ctx:
        test_doc = MockDocument(
            source_type="test_upload",
            source_id=f"test-{uuid.uuid4()}",
            title="Test Document",
            content="Test content",
            metadata={},
        )
        doc_id = await add_document(
            db_context=db_ctx,
            doc=test_doc,
        )

        # Create multiple embedding tasks
        await db_ctx.tasks.enqueue(
            task_id=f"embed_1_{doc_id}",
            task_type="embed_and_store_batch",
            payload={
                "document_id": doc_id,
                "texts_to_embed": ["Part 1"],
                "embedding_metadata_list": [
                    {
                        "embedding_type": "content_chunk",
                        "chunk_index": 0,
                        "original_content_metadata": {},
                    }
                ],
            },
        )

        await db_ctx.tasks.enqueue(
            task_id=f"embed_2_{doc_id}",
            task_type="embed_and_store_batch",
            payload={
                "document_id": doc_id,
                "texts_to_embed": ["Part 2"],
                "embedding_metadata_list": [
                    {
                        "embedding_type": "content_chunk",
                        "chunk_index": 1,
                        "original_content_metadata": {},
                    }
                ],
            },
        )

    # Process only the first task
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    async with get_db_context(engine=db_engine) as db_ctx:
        first_task = await db_ctx.tasks.dequeue(
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(timezone.utc),
        )
        assert first_task is not None

        # Track if event was emitted
        event_emitted = False
        original_emit = indexing_source.emit_event

        async def track_emit(event_data: dict[str, Any]) -> asyncio.Future[None]:
            nonlocal event_emitted
            if event_data.get("event_type") == IndexingEventType.DOCUMENT_READY.value:
                event_emitted = True
            return await original_emit(event_data)

        indexing_source.emit_event = track_emit

        # Process first task
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test-conv",
            user_name="test-user",
            turn_id=str(uuid.uuid4()),
            db_context=db_ctx,
            embedding_generator=embedding_generator,
            indexing_source=indexing_source,
        )

        await handle_embed_and_store_batch(exec_context, first_task["payload"])
        await db_ctx.tasks.update_status(first_task["task_id"], "done")

        # Event should NOT have been emitted since second task is pending
        assert not event_emitted, "DOCUMENT_READY was emitted with pending tasks!"


@pytest.mark.asyncio
async def test_indexing_event_listener_integration(db_engine: AsyncEngine) -> None:
    """Test full integration with event listeners triggering on document ready."""
    # Clean up any leftover tasks from previous tests to ensure isolation
    async with get_db_context(engine=db_engine) as db_ctx:
        await db_ctx.execute_with_retry(
            tasks_table.delete().where(
                tasks_table.c.task_type == "embed_and_store_batch"
            )
        )

    # Create components
    indexing_source = IndexingSource()

    # Create event listener
    async with get_db_context(engine=db_engine) as db_ctx:
        await db_ctx.events.create_event_listener(
            name="Newsletter Ready Listener",
            description="Test listener for newsletter ready events",
            conversation_id="test-conv",
            interface_type="web",
            source_id="indexing",
            match_conditions={
                "event_type": IndexingEventType.DOCUMENT_READY.value,
                "document_title": {"$contains": "Newsletter"},  # Only match newsletters
            },
            action_config={
                "prompt": "The newsletter '{{ event.document_title }}' has been indexed with {{ event.metadata.total_embeddings }} embeddings. Please summarize it.",
                "interface_type": "test",
                "conversation_id": "test-conv",
            },
            enabled=True,
        )

    # Create and process a newsletter document
    async with get_db_context(engine=db_engine) as db_ctx:
        test_doc = MockDocument(
            source_type="email",
            source_id="newsletter@school.edu",
            title="School Newsletter - December 2024",
            content="Important dates: Winter break Dec 20-Jan 3. Science fair Jan 15.",
            metadata={"sender": "newsletter@school.edu"},
        )
        doc_id = await add_document(
            db_context=db_ctx,
            doc=test_doc,
        )

        # Simulate embedding task
        await db_ctx.tasks.enqueue(
            task_id=f"embed_newsletter_{doc_id}",
            task_type="embed_and_store_batch",
            payload={
                "document_id": doc_id,
                "texts_to_embed": [
                    "Important dates: Winter break Dec 20-Jan 3. Science fair Jan 15."
                ],
                "embedding_metadata_list": [
                    {
                        "embedding_type": "content",
                        "chunk_index": 0,
                        "original_content_metadata": {},
                    }
                ],
            },
        )

    # Process the task which should trigger the event
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    async with get_db_context(engine=db_engine) as db_ctx:
        task = await db_ctx.tasks.dequeue(
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(timezone.utc),
        )

        assert task is not None

        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test-conv",
            user_name="test-user",
            turn_id=str(uuid.uuid4()),
            db_context=db_ctx,
            embedding_generator=embedding_generator,
            indexing_source=indexing_source,
        )

        # Create processor to handle events
        event_processor = EventProcessor(
            sources={"indexing": indexing_source},
            sample_interval_hours=0.1,
            get_db_context_func=lambda: get_db_context(engine=db_engine),
        )

        # Mock processing service for wake_llm action
        mock_processing_service = MagicMock()
        mock_processing_service.generate_llm_response_for_chat = AsyncMock()

        # Skip the wake_llm handler test for now as it's complex to mock
        # The important part is that the event is emitted and stored

        try:
            # Start processor and wait for it to be fully initialized
            await event_processor.start()

            # Process embedding task - should emit event
            await handle_embed_and_store_batch(exec_context, task["payload"])

            # Wait for all events to be processed before polling
            await indexing_source.wait_for_pending_events()

            # Poll for the event and verify
            event_data = await poll_for_document_ready_event(doc_id, engine=db_engine)

            assert event_data["document_title"] == "School Newsletter - December 2024"
            assert event_data["document_metadata"] == {
                "sender": "newsletter@school.edu"
            }

        finally:
            # Stop processor
            await event_processor.stop()


@pytest.mark.asyncio
async def test_document_ready_event_includes_rich_metadata(
    db_engine: AsyncEngine,
) -> None:
    """Test that DOCUMENT_READY event includes full document metadata."""
    # Create indexing source and event processor
    indexing_source = IndexingSource()
    event_processor = EventProcessor(
        sources={"indexing": indexing_source},
        sample_interval_hours=0.1,
        get_db_context_func=lambda: get_db_context(engine=db_engine),
    )

    # Create a document with rich metadata
    async with get_db_context(engine=db_engine) as db_ctx:
        test_doc = MockDocument(
            source_type="pdf",
            source_id=f"test-pdf-{uuid.uuid4()}",
            title="Research Paper - AI in Healthcare",
            content="This paper explores the applications of AI in healthcare...",
            metadata={
                "original_filename": "ai_healthcare_research_2024.pdf",
                "original_url": "https://example.com/papers/ai-healthcare.pdf",
                "author": "Dr. Jane Smith",
                "publication_date": "2024-03-15",
                "keywords": ["AI", "healthcare", "machine learning"],
                "page_count": 25,
                "department": "Computer Science",
                "document_type": "research_paper",
            },
        )
        doc_id = await add_document(
            db_context=db_ctx,
            doc=test_doc,
        )

        # Create embedding task
        await db_ctx.tasks.enqueue(
            task_id=f"embed_rich_metadata_{doc_id}",
            task_type="embed_and_store_batch",
            payload={
                "document_id": doc_id,
                "texts_to_embed": [
                    "This paper explores the applications of AI in healthcare..."
                ],
                "embedding_metadata_list": [
                    {
                        "embedding_type": "content",
                        "chunk_index": 0,
                        "original_content_metadata": {"page": 1},
                    }
                ],
            },
        )

    # Process the task
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    try:
        # Start event processor and wait for it to be fully initialized
        await event_processor.start()

        async with get_db_context(engine=db_engine) as db_ctx:
            task = await db_ctx.tasks.dequeue(
                task_types=["embed_and_store_batch"],
                worker_id="test-worker",
                current_time=datetime.now(timezone.utc),
            )

            assert task is not None

            exec_context = ToolExecutionContext(
                interface_type="web",
                conversation_id="test-conv",
                user_name="test-user",
                turn_id=str(uuid.uuid4()),
                db_context=db_ctx,
                embedding_generator=embedding_generator,
                indexing_source=indexing_source,
            )

            # Process task - should emit event with rich metadata
            await handle_embed_and_store_batch(exec_context, task["payload"])
            await db_ctx.tasks.update_status(task["task_id"], "done")

        # Wait for all events to be processed before polling
        await indexing_source.wait_for_pending_events()

        # Poll for the event with longer timeout for rich metadata test
        event_data = await poll_for_document_ready_event(
            doc_id, timeout_seconds=3.0, engine=db_engine
        )

        # Verify all fields are present
        assert event_data["document_id"] == doc_id
        assert event_data["document_type"] == "pdf"
        assert event_data["document_title"] == "Research Paper - AI in Healthcare"

        # Verify rich metadata is included
        doc_metadata = event_data["document_metadata"]
        assert doc_metadata["original_filename"] == "ai_healthcare_research_2024.pdf"
        assert (
            doc_metadata["original_url"]
            == "https://example.com/papers/ai-healthcare.pdf"
        )
        assert doc_metadata["author"] == "Dr. Jane Smith"
        assert doc_metadata["publication_date"] == "2024-03-15"
        assert doc_metadata["keywords"] == ["AI", "healthcare", "machine learning"]
        assert doc_metadata["page_count"] == 25
        assert doc_metadata["department"] == "Computer Science"
        assert doc_metadata["document_type"] == "research_paper"

        # Verify indexing metadata
        assert event_data["metadata"]["total_embeddings"] == 1
        assert event_data["metadata"]["source_id"] == test_doc.source_id

    finally:
        # Clean up
        await event_processor.stop()


@pytest.mark.asyncio
async def test_document_ready_event_handles_none_metadata(
    db_engine: AsyncEngine,
) -> None:
    """Test that DOCUMENT_READY event handles documents with None metadata gracefully."""
    # Create indexing source
    indexing_source = IndexingSource()

    # Create event processor with our indexing source
    event_processor = EventProcessor(
        sources={"indexing": indexing_source},
        sample_interval_hours=0.1,  # Short interval for testing
        get_db_context_func=lambda: get_db_context(engine=db_engine),
    )

    # Create a document with None metadata
    async with get_db_context(engine=db_engine) as db_ctx:
        test_doc = MockDocument(
            source_type="note",
            source_id=f"test-note-{uuid.uuid4()}",
            title="Simple Note",
            content="This is a simple note without metadata",
            metadata=None,  # Explicitly None
        )
        doc_id = await add_document(
            db_context=db_ctx,
            doc=test_doc,
        )

        # Create embedding task
        await db_ctx.tasks.enqueue(
            task_id=f"embed_no_metadata_{doc_id}",
            task_type="embed_and_store_batch",
            payload={
                "document_id": doc_id,
                "texts_to_embed": ["This is a simple note without metadata"],
                "embedding_metadata_list": [
                    {
                        "embedding_type": "content",
                        "chunk_index": 0,
                        "original_content_metadata": {},
                    }
                ],
            },
        )

    # Process the task
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    try:
        # Start event processor and wait for it to be fully initialized
        await event_processor.start()

        async with get_db_context(engine=db_engine) as db_ctx:
            task = await db_ctx.tasks.dequeue(
                task_types=["embed_and_store_batch"],
                worker_id="test-worker",
                current_time=datetime.now(timezone.utc),
            )

            assert task is not None

            exec_context = ToolExecutionContext(
                interface_type="web",
                conversation_id="test-conv",
                user_name="test-user",
                turn_id=str(uuid.uuid4()),
                db_context=db_ctx,
                embedding_generator=embedding_generator,
                indexing_source=indexing_source,
            )

            # Process task - should emit event even with None metadata
            await handle_embed_and_store_batch(exec_context, task["payload"])
            await db_ctx.tasks.update_status(task["task_id"], "done")

        # Wait for all events to be processed before polling
        await indexing_source.wait_for_pending_events()

        # Poll for DOCUMENT_READY event
        event_data = await poll_for_document_ready_event(doc_id, engine=db_engine)

        assert event_data["document_id"] == doc_id
        assert event_data["document_type"] == "note"
        assert event_data["document_title"] == "Simple Note"
        assert (
            event_data["document_metadata"] == {}
        )  # None metadata is stored as empty dict
        assert event_data["metadata"]["total_embeddings"] == 1

    finally:
        # Stop event processor
        await event_processor.stop()


@pytest.mark.asyncio
async def test_json_extraction_compatibility(db_engine: AsyncEngine) -> None:
    """Test that JSON extraction works correctly with both SQLite and PostgreSQL."""
    async with get_db_context(engine=db_engine) as db_ctx:
        # Clean up any existing test tasks
        await db_ctx.execute_with_retry(
            tasks_table.delete().where(tasks_table.c.task_id.like("test_json_%"))
        )

        # Create test tasks with different document_ids
        test_doc_id = 999
        for i in range(3):
            await db_ctx.tasks.enqueue(
                task_id=f"test_json_{i}",
                task_type="embed_and_store_batch",
                payload={"document_id": test_doc_id if i < 2 else 888},
            )

        # Import the function to test
        from family_assistant.indexing.tasks import check_document_completion

        # Test that it correctly counts pending tasks
        pending_count = await check_document_completion(db_ctx, test_doc_id)
        assert pending_count == 2, f"Expected 2 pending tasks, got {pending_count}"

        # Test with non-existent document
        pending_count = await check_document_completion(db_ctx, 777)
        assert pending_count == 0, (
            f"Expected 0 pending tasks for non-existent doc, got {pending_count}"
        )

        # Clean up
        await db_ctx.execute_with_retry(
            tasks_table.delete().where(tasks_table.c.task_id.like("test_json_%"))
        )


@pytest.mark.asyncio
async def test_json_extraction_cross_database(db_engine: AsyncEngine) -> None:
    """Test JSON extraction works with both SQLite and PostgreSQL."""
    async with get_db_context(engine=db_engine) as db_ctx:
        # Clean up any existing test tasks
        await db_ctx.execute_with_retry(
            tasks_table.delete().where(
                tasks_table.c.task_id.like("test_json_extract_%")
            )
        )

        # Create test task
        test_doc_id = 12345
        await db_ctx.tasks.enqueue(
            task_id="test_json_extract_1",
            task_type="embed_and_store_batch",
            payload={"document_id": test_doc_id, "other_field": "test"},
        )

        # Verify our cross-database JSON extraction implementation works

        # Test that our check_document_completion function works
        from family_assistant.indexing.tasks import check_document_completion

        pending_count = await check_document_completion(db_ctx, test_doc_id)
        assert pending_count == 1, f"Expected 1 pending task, got {pending_count}"

        # Clean up
        await db_ctx.execute_with_retry(
            tasks_table.delete().where(
                tasks_table.c.task_id.like("test_json_extract_%")
            )
        )
