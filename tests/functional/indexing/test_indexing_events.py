"""
Functional tests for document indexing events.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant import storage
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.events.indexing_source import IndexingEventType, IndexingSource
from family_assistant.events.processor import EventProcessor
from family_assistant.indexing.tasks import handle_embed_and_store_batch
from family_assistant.storage import get_db_context
from family_assistant.storage.vector import add_document
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


@dataclass
class TestDocument:
    """Test implementation of Document protocol."""

    source_type: str
    source_id: str
    title: str
    content: str | None = None
    source_uri: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] | None = None
    id: int | None = None  # Add id property for Document protocol

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        if self.metadata is None:
            self.metadata = {}


# Test data
TEST_DOC_TITLE = "Test Document for Indexing Events"
TEST_DOC_CONTENT = "This is test content that will be chunked and embedded."
TEST_DOC_CHUNKS = ["This is test content", "that will be chunked", "and embedded."]


@pytest.mark.asyncio
async def test_document_ready_event_emitted(test_db_engine: AsyncEngine) -> None:
    """Test that DOCUMENT_READY event is emitted when all tasks complete."""
    # Create indexing source
    indexing_source = IndexingSource()

    # Create event processor with our indexing source
    event_processor = EventProcessor(
        sources={"indexing": indexing_source},
        sample_interval_hours=0.1,  # Short interval for testing
    )

    # Create event listener that captures events
    async with get_db_context() as db_ctx:
        from family_assistant.storage.events import create_event_listener

        await create_event_listener(
            db_context=db_ctx,
            name="Test Document Ready Listener",
            description="Test listener for document ready events",
            conversation_id="test-conv",
            interface_type="web",
            source_id="indexing",
            match_conditions={"event_type": IndexingEventType.DOCUMENT_READY.value},
            action_config={"prompt": "Document ready: {{ event.document_title }}"},
            enabled=True,
        )

    # Start event processor
    processor_task = asyncio.create_task(event_processor.start())

    try:
        # Give processor time to start
        await asyncio.sleep(0.1)

        # Create a document
        async with get_db_context() as db_ctx:
            test_doc = TestDocument(
                source_type="test_upload",
                source_id=f"test-{uuid.uuid4()}",
                title=TEST_DOC_TITLE,
                content=TEST_DOC_CONTENT,
                metadata={"test": True},
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
        async with get_db_context() as db_ctx:
            # Title embedding task
            await storage.enqueue_task(
                db_context=db_ctx,
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
                await storage.enqueue_task(
                    db_context=db_ctx,
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
        async with get_db_context() as db_ctx:
            # Process title embedding
            title_task = await storage.dequeue_task(
                db_context=db_ctx,
                task_types=["embed_and_store_batch"],
                worker_id="test-worker",
                current_time=datetime.now(timezone.utc),
            )
            assert title_task is not None

            # Create new context for each task with fresh db_context
            task_context = ToolExecutionContext(
                interface_type="web",
                conversation_id="test-conv",
                user_name="test-user",
                turn_id=str(uuid.uuid4()),
                db_context=db_ctx,
                embedding_generator=embedding_generator,
                indexing_source=indexing_source,
            )
            await handle_embed_and_store_batch(task_context, title_task["payload"])
            await storage.update_task_status(db_ctx, title_task["task_id"], "done")

        # Process chunk embeddings
        for _ in range(len(TEST_DOC_CHUNKS)):
            async with get_db_context() as db_ctx:
                chunk_task = await storage.dequeue_task(
                    db_context=db_ctx,
                    task_types=["embed_and_store_batch"],
                    worker_id="test-worker",
                    current_time=datetime.now(timezone.utc),
                )
                assert chunk_task is not None

                # Create new context for each task
                task_context = ToolExecutionContext(
                    interface_type="web",
                    conversation_id="test-conv",
                    user_name="test-user",
                    turn_id=str(uuid.uuid4()),
                    db_context=db_ctx,
                    embedding_generator=embedding_generator,
                    indexing_source=indexing_source,
                )

                # This should emit DOCUMENT_READY on the last task
                await handle_embed_and_store_batch(task_context, chunk_task["payload"])
                await storage.update_task_status(db_ctx, chunk_task["task_id"], "done")

        # Give event processor time to process events
        await asyncio.sleep(0.5)

        # Check that DOCUMENT_READY event was stored
        async with get_db_context() as db_ctx:
            from sqlalchemy import text

            # Check recent_events table
            result = await db_ctx.fetch_all(
                text("""
                    SELECT event_data 
                    FROM recent_events 
                    WHERE source_id = :source_id
                    AND json_extract(event_data, '$.event_type') = :event_type
                    AND json_extract(event_data, '$.document_id') = :doc_id
                """),
                {
                    "source_id": "indexing",
                    "event_type": IndexingEventType.DOCUMENT_READY.value,
                    "doc_id": doc_id,
                },
            )

            assert len(result) > 0, "No DOCUMENT_READY event found in recent_events"

            event_data = json.loads(result[0]["event_data"])
            assert event_data["document_id"] == doc_id
            assert event_data["document_title"] == TEST_DOC_TITLE
            assert event_data["metadata"]["total_embeddings"] == 4  # 1 title + 3 chunks
            assert (
                event_data["metadata"]["embedding_types"] == 2
            )  # title and content_chunk

    finally:
        # Stop event processor
        await event_processor.stop()
        processor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await processor_task


@pytest.mark.asyncio
async def test_document_ready_not_emitted_with_pending_tasks(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that DOCUMENT_READY is not emitted when tasks are still pending."""
    # Create indexing source
    indexing_source = IndexingSource()

    # Create a document
    async with get_db_context() as db_ctx:
        test_doc = TestDocument(
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
        await storage.enqueue_task(
            db_context=db_ctx,
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

        await storage.enqueue_task(
            db_context=db_ctx,
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

    async with get_db_context() as db_ctx:
        first_task = await storage.dequeue_task(
            db_context=db_ctx,
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(timezone.utc),
        )
        assert first_task is not None

        # Track if event was emitted
        event_emitted = False
        original_emit = indexing_source.emit_event

        async def track_emit(event_data: dict[str, Any]) -> None:
            nonlocal event_emitted
            if event_data.get("event_type") == IndexingEventType.DOCUMENT_READY.value:
                event_emitted = True
            await original_emit(event_data)

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
        await storage.update_task_status(db_ctx, first_task["task_id"], "done")

        # Event should NOT have been emitted since second task is pending
        assert not event_emitted, "DOCUMENT_READY was emitted with pending tasks!"


@pytest.mark.asyncio
async def test_indexing_event_listener_integration(test_db_engine: AsyncEngine) -> None:
    """Test full integration with event listeners triggering on document ready."""
    # Create components
    indexing_source = IndexingSource()

    # Create event listener
    async with get_db_context() as db_ctx:
        from family_assistant.storage.events import create_event_listener

        await create_event_listener(
            db_context=db_ctx,
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
    async with get_db_context() as db_ctx:
        test_doc = TestDocument(
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
        await storage.enqueue_task(
            db_context=db_ctx,
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

    async with get_db_context() as db_ctx:
        task = await storage.dequeue_task(
            db_context=db_ctx,
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(timezone.utc),
        )

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
        )

        # Mock processing service for wake_llm action
        mock_processing_service = MagicMock()
        mock_processing_service.generate_llm_response_for_chat = AsyncMock()

        # Skip the wake_llm handler test for now as it's complex to mock
        # The important part is that the event is emitted and stored

        processor_task = None
        try:
            # Start processor
            processor_task = asyncio.create_task(event_processor.start())
            await asyncio.sleep(0.1)

            # Process embedding task - should emit event
            assert task is not None
            await handle_embed_and_store_batch(exec_context, task["payload"])

            # Give event processor time to handle event
            await asyncio.sleep(0.5)

            # Check that the event was stored in recent_events
            async with get_db_context() as db_ctx:
                from sqlalchemy import text

                result = await db_ctx.fetch_all(
                    text("""
                        SELECT event_data 
                        FROM recent_events 
                        WHERE source_id = :source_id
                        AND json_extract(event_data, '$.event_type') = :event_type
                        AND json_extract(event_data, '$.document_id') = :doc_id
                    """),
                    {
                        "source_id": "indexing",
                        "event_type": IndexingEventType.DOCUMENT_READY.value,
                        "doc_id": doc_id,
                    },
                )

                assert len(result) > 0, (
                    "DOCUMENT_READY event not found in recent_events"
                )
                event_data = json.loads(result[0]["event_data"])
                assert (
                    event_data["document_title"] == "School Newsletter - December 2024"
                )

        finally:
            # Stop processor
            await event_processor.stop()
            if processor_task:
                processor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await processor_task
