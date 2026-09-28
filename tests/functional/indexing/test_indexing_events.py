"""
Functional tests for document indexing events.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import and_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.types import Integer

# storage module functions now accessed via Database
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.events.indexing_source import IndexingEventType, IndexingSource
from family_assistant.events.processor import EventProcessor
from family_assistant.indexing.tasks import (
    EmbedAndStoreBatchPayload,
    check_document_completion,
    handle_embed_and_store_batch,
)
from family_assistant.storage import Database
from family_assistant.storage.events import recent_events_table
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.storage.types import TaskDict
from family_assistant.storage.vector import add_document
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_condition

if TYPE_CHECKING:
    from family_assistant.storage.types import ActionConfig

logger = logging.getLogger(__name__)


async def poll_for_document_ready_event(
    doc_id: int,
    engine: AsyncEngine,
    # ast-grep-ignore: no-dict-any - Event data is unstructured JSON
) -> dict[str, Any]:
    """Wait for the DOCUMENT_READY event for ``doc_id`` to be stored in recent_events."""
    stmt = select(recent_events_table.c.event_data).where(
        and_(
            recent_events_table.c.source_id == "indexing",
            recent_events_table.c.event_data["event_type"].as_string()
            == IndexingEventType.DOCUMENT_READY.value,
            sa_cast(
                recent_events_table.c.event_data["document_id"].as_string(),
                Integer,
            )
            == doc_id,
        )
    )

    # ast-grep-ignore: no-dict-any - Event data is unstructured JSON
    async def fetch_event_data() -> dict[str, Any] | None:
        rows = await Database(engine=engine).fetch_all(stmt)
        if not rows:
            return None
        event_data_raw = rows[0]["event_data"]
        if isinstance(event_data_raw, str):
            return json.loads(event_data_raw)
        return event_data_raw

    event_data = await wait_for_condition(
        fetch_event_data,
        timeout=10.0,
        description=f"DOCUMENT_READY event for document {doc_id}",
    )
    assert event_data is not None
    return event_data


def _indexing_worker(
    db_engine: AsyncEngine,
    indexing_source: IndexingSource,
    embedding_generator: MockEmbeddingGenerator,
) -> TaskWorker:
    """A task worker wired for indexing, used to run claimed tasks to completion."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=embedding_generator,
        indexing_source=indexing_source,
        engine=db_engine,
    )
    worker.register_task_handler("embed_and_store_batch", handle_embed_and_store_batch)
    return worker


async def _complete_through_worker(
    db_engine: AsyncEngine,
    task: TaskDict,
    indexing_source: IndexingSource,
    embedding_generator: MockEmbeddingGenerator,
) -> None:
    """Run a claimed task through the worker: handler, then the done-commit."""
    worker = _indexing_worker(db_engine, indexing_source, embedding_generator)
    await worker._process_task(Database(engine=db_engine), task, asyncio.Event())


@dataclass
class MockDocument:
    """Test implementation of Document protocol."""

    source_type: str
    source_id: str
    title: str
    content: str | None = None
    source_uri: str | None = None
    created_at: datetime | None = None
    # ast-grep-ignore: no-dict-any - Mock document metadata is unstructured
    metadata: dict[str, Any] | None = None
    id: int | None = None  # Add id property for Document protocol
    file_path: str | None = None  # Add file_path for Document protocol
    visibility_labels: list[str] | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now(UTC)
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
        get_db_context_func=lambda: Database(engine=db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    # Create event listener that captures events
    db_ctx = Database(engine=db_engine)
    await db_ctx.events.create_event_listener(
        name="Test Document Ready Listener",
        description="Test listener for document ready events",
        conversation_id="test-conv",
        interface_type="web",
        source_id="indexing",
        match_conditions={"event_type": IndexingEventType.DOCUMENT_READY.value},
        action_config=cast(
            "ActionConfig", {"prompt": "Document ready: {{ event.document_title }}"}
        ),
        enabled=True,
    )

    try:
        # Start processor and wait for it to be fully initialized
        await event_processor.start()

        # Create a document
        db_ctx = Database(engine=db_engine)
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
        db_ctx = Database(engine=db_engine)
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
                        "content_hash": None,
                    }
                ],
            },
            priority=TaskPriority.INTERACTIVE,
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
                            "content_hash": None,
                        }
                    ],
                },
                priority=TaskPriority.INTERACTIVE,
            )

        for _ in range(1 + len(TEST_DOC_CHUNKS)):
            db_ctx = Database(engine=db_engine)
            task = await db_ctx.tasks.dequeue(
                task_types=["embed_and_store_batch"],
                worker_id="test-worker",
                current_time=datetime.now(UTC),
            )
            assert task is not None

            await _complete_through_worker(
                db_engine, task, indexing_source, embedding_generator
            )

        await indexing_source.wait_for_pending_events()

        event_data = await poll_for_document_ready_event(doc_id, engine=db_engine)

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
    db_ctx = Database(engine=db_engine)
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
                    "content_hash": None,
                }
            ],
        },
        priority=TaskPriority.INTERACTIVE,
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
                    "content_hash": None,
                }
            ],
        },
        priority=TaskPriority.INTERACTIVE,
    )

    # Process only the first task
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    db_ctx = Database(engine=db_engine)
    first_task = await db_ctx.tasks.dequeue(
        task_types=["embed_and_store_batch"],
        worker_id="test-worker",
        current_time=datetime.now(UTC),
    )
    assert first_task is not None

    # Track if event was emitted
    event_emitted = False
    original_emit = indexing_source.emit_event

    # ast-grep-ignore: no-dict-any - Mocking event handler signature
    async def track_emit(event_data: dict[str, Any]) -> asyncio.Future[None]:
        nonlocal event_emitted
        if event_data.get("event_type") == IndexingEventType.DOCUMENT_READY.value:
            event_emitted = True
        return await original_emit(event_data)

    indexing_source.emit_event = track_emit

    # Process first task
    await _complete_through_worker(
        db_engine, first_task, indexing_source, embedding_generator
    )

    # Event should NOT have been emitted since second task is pending
    assert not event_emitted, "DOCUMENT_READY was emitted with pending tasks!"


def _embed_batch_payload(
    doc_id: int, chunk_index: int, text: str
) -> EmbedAndStoreBatchPayload:
    return {
        "document_id": doc_id,
        "texts_to_embed": [text],
        "embedding_metadata_list": [
            {
                "embedding_type": "content_chunk",
                "chunk_index": chunk_index,
                "original_content_metadata": {},
                "content_hash": None,
            }
        ],
    }


async def _enqueue_and_claim_two_batches(
    db_ctx: Database, title: str
) -> tuple[int, TaskDict, TaskDict]:
    """Enqueue two embedding batches for a new document and claim both."""
    doc_id = await add_document(
        db_context=db_ctx,
        doc=MockDocument(
            source_type="test_upload",
            source_id=f"test-{uuid.uuid4()}",
            title=title,
            content="Two batches",
            metadata={},
        ),
    )
    for i in range(2):
        await db_ctx.tasks.enqueue(
            task_id=f"embed_concurrent_{i}_{doc_id}",
            task_type="embed_and_store_batch",
            payload=_embed_batch_payload(doc_id, i, f"Part {i}"),
            priority=TaskPriority.INTERACTIVE,
        )
    claimed: list[TaskDict] = []
    for worker_id in ("worker-a", "worker-b"):
        task = await db_ctx.tasks.dequeue(
            task_types=["embed_and_store_batch"],
            worker_id=worker_id,
            current_time=datetime.now(UTC),
        )
        assert task is not None
        claimed.append(task)
    return doc_id, claimed[0], claimed[1]


def _track_document_ready(indexing_source: IndexingSource) -> list[int]:
    """Record the document id of every DOCUMENT_READY the source emits."""
    ready_document_ids: list[int] = []
    original_emit = indexing_source.emit_event

    # ast-grep-ignore: no-dict-any - Mocking event handler signature
    async def track_emit(event_data: dict[str, Any]) -> asyncio.Future[None]:
        if event_data["event_type"] == IndexingEventType.DOCUMENT_READY.value:
            ready_document_ids.append(event_data["document_id"])
        return await original_emit(event_data)

    indexing_source.emit_event = track_emit
    return ready_document_ids


@pytest.mark.asyncio
async def test_document_ready_not_emitted_while_sibling_batch_processing(
    db_engine: AsyncEngine,
) -> None:
    """A sibling batch claimed by another worker keeps the document not ready.

    Both batches are claimed, so both rows are ``processing``; the first to
    finish must not declare the document ready while the second is still
    embedding.
    """
    indexing_source = IndexingSource()
    db_ctx = Database(engine=db_engine)
    doc_id, first_task, sibling_task = await _enqueue_and_claim_two_batches(
        db_ctx, "Concurrent Batches"
    )
    assert await check_document_completion(db_ctx, doc_id) == 2
    ready_document_ids = _track_document_ready(indexing_source)
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    await _complete_through_worker(
        db_engine, first_task, indexing_source, embedding_generator
    )

    sibling_row = await db_ctx.fetch_one(
        select(tasks_table.c.status).where(
            tasks_table.c.task_id == sibling_task["task_id"]
        )
    )
    assert sibling_row is not None
    assert sibling_row["status"] == "processing"
    assert ready_document_ids == []


@pytest.mark.asyncio
async def test_document_ready_emitted_once_when_two_batches_finish_together(
    db_engine: AsyncEngine,
) -> None:
    """Two batches whose handlers both finish before either is marked done.

    Neither handler can see the other finished, so the decision has to wait
    for the done-commits, and exactly one of the two may announce the document.
    """
    indexing_source = IndexingSource()
    db_ctx = Database(engine=db_engine)
    doc_id, first_task, second_task = await _enqueue_and_claim_two_batches(
        db_ctx, "Batches Finishing Together"
    )
    ready_document_ids = _track_document_ready(indexing_source)

    both_handlers_finished = asyncio.Barrier(2)

    async def handler_then_wait_for_sibling(
        exec_context: ToolExecutionContext, payload: EmbedAndStoreBatchPayload
    ) -> None:
        await handle_embed_and_store_batch(exec_context, payload)
        await both_handlers_finished.wait()

    worker = _indexing_worker(
        db_engine,
        indexing_source,
        MockEmbeddingGenerator(model_name="test-model", dimensions=128),
    )
    worker.register_task_handler("embed_and_store_batch", handler_then_wait_for_sibling)

    await asyncio.gather(
        worker._process_task(db_ctx, first_task, asyncio.Event()),
        worker._process_task(db_ctx, second_task, asyncio.Event()),
    )

    statuses = await db_ctx.fetch_all(
        select(tasks_table.c.status).where(
            tasks_table.c.task_id.in_([first_task["task_id"], second_task["task_id"]])
        )
    )
    assert [row["status"] for row in statuses] == ["done", "done"]
    assert ready_document_ids == [doc_id]


@pytest.mark.asyncio
async def test_document_ready_emitted_when_parent_task_finishes_last(
    db_engine: AsyncEngine,
) -> None:
    """The processing task that enqueued a batch can be marked done after it.

    Its batch then finishes while it is still running and must not announce
    the document; the parent's own completion does.
    """
    indexing_source = IndexingSource()
    db_ctx = Database(engine=db_engine)
    doc_id = await add_document(
        db_context=db_ctx,
        doc=MockDocument(
            source_type="test_upload",
            source_id=f"test-{uuid.uuid4()}",
            title="Parent Finishes Last",
            content="One batch",
            metadata={},
        ),
    )
    await db_ctx.tasks.enqueue(
        task_id=f"process_doc_{doc_id}",
        task_type="process_uploaded_document",
        payload={"document_id": doc_id},
        priority=TaskPriority.INTERACTIVE,
    )
    parent_task = await db_ctx.tasks.dequeue(
        task_types=["process_uploaded_document"],
        worker_id="worker-a",
        current_time=datetime.now(UTC),
    )
    assert parent_task is not None
    await db_ctx.tasks.enqueue(
        task_id=f"embed_batch_{doc_id}",
        task_type="embed_and_store_batch",
        payload=_embed_batch_payload(doc_id, 0, "Only part"),
        priority=TaskPriority.INTERACTIVE,
    )
    batch_task = await db_ctx.tasks.dequeue(
        task_types=["embed_and_store_batch"],
        worker_id="worker-b",
        current_time=datetime.now(UTC),
    )
    assert batch_task is not None
    ready_document_ids = _track_document_ready(indexing_source)
    worker = _indexing_worker(
        db_engine,
        indexing_source,
        MockEmbeddingGenerator(model_name="test-model", dimensions=128),
    )

    async def parent_already_enqueued_its_batch(
        exec_context: ToolExecutionContext, payload: object
    ) -> None:
        pass

    worker.register_task_handler(
        "process_uploaded_document", parent_already_enqueued_its_batch
    )

    await worker._process_task(db_ctx, batch_task, asyncio.Event())
    assert ready_document_ids == []

    await worker._process_task(db_ctx, parent_task, asyncio.Event())
    assert ready_document_ids == [doc_id]


@pytest.mark.asyncio
async def test_document_ready_not_emitted_without_embeddings(
    db_engine: AsyncEngine,
) -> None:
    """A document whose processing produced nothing to embed is never ready."""
    indexing_source = IndexingSource()
    db_ctx = Database(engine=db_engine)
    doc_id = await add_document(
        db_context=db_ctx,
        doc=MockDocument(
            source_type="test_upload",
            source_id=f"test-{uuid.uuid4()}",
            title="Nothing To Embed",
            content="",
            metadata={},
        ),
    )
    await db_ctx.tasks.enqueue(
        task_id=f"process_empty_doc_{doc_id}",
        task_type="process_uploaded_document",
        payload={"document_id": doc_id},
        priority=TaskPriority.INTERACTIVE,
    )
    task = await db_ctx.tasks.dequeue(
        task_types=["process_uploaded_document"],
        worker_id="worker-a",
        current_time=datetime.now(UTC),
    )
    assert task is not None
    ready_document_ids = _track_document_ready(indexing_source)
    worker = _indexing_worker(
        db_engine,
        indexing_source,
        MockEmbeddingGenerator(model_name="test-model", dimensions=128),
    )

    async def enqueues_nothing(
        exec_context: ToolExecutionContext, payload: object
    ) -> None:
        pass

    worker.register_task_handler("process_uploaded_document", enqueues_nothing)

    await worker._process_task(db_ctx, task, asyncio.Event())

    assert ready_document_ids == []


@pytest.mark.asyncio
async def test_indexing_event_listener_integration(db_engine: AsyncEngine) -> None:
    """A listener filtering DOCUMENT_READY events wakes the LLM for a matching document."""
    indexing_source = IndexingSource()

    db_ctx = Database(engine=db_engine)
    listener_id = await db_ctx.events.create_event_listener(
        name="Newsletter Ready Listener",
        description="Test listener for newsletter ready events",
        conversation_id="test-conv",
        interface_type="web",
        source_id="indexing",
        match_conditions={"event_type": IndexingEventType.DOCUMENT_READY.value},
        condition_script="'Newsletter' in event['document_title']",
        action_config={"context": "Summarize the newsletter that was just indexed."},
        enabled=True,
    )

    # Create and process a newsletter document
    db_ctx = Database(engine=db_engine)
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
                    "content_hash": None,
                }
            ],
        },
        priority=TaskPriority.INTERACTIVE,
    )

    # Process the task which should trigger the event
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    # Create processor to handle events
    event_processor = EventProcessor(
        sources={"indexing": indexing_source},
        sample_interval_hours=0.1,
        get_db_context_func=lambda: Database(engine=db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    try:
        # Start processor and wait for it to be fully initialized
        await event_processor.start()

        db_ctx = Database(engine=db_engine)
        task = await db_ctx.tasks.dequeue(
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(UTC),
        )

        assert task is not None

        await _complete_through_worker(
            db_engine, task, indexing_source, embedding_generator
        )
        await indexing_source.wait_for_pending_events()

        callback_tasks = await Database(engine=db_engine).tasks.get_all(
            task_type="llm_callback"
        )
        assert len(callback_tasks) == 1
        callback_payload = callback_tasks[0]["payload"]
        assert callback_payload is not None
        assert callback_payload["conversation_id"] == "test-conv"
        callback_context = callback_payload["callback_context"]
        assert callback_context["listener_id"] == listener_id
        assert (
            callback_context["message"]
            == "Summarize the newsletter that was just indexed."
        )
        event_data = callback_context["event_data"]
        assert event_data["event_type"] == IndexingEventType.DOCUMENT_READY.value
        assert event_data["document_id"] == doc_id
        assert event_data["document_title"] == "School Newsletter - December 2024"
        assert event_data["document_metadata"] == {"sender": "newsletter@school.edu"}

    finally:
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
        get_db_context_func=lambda: Database(engine=db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    # Create a document with rich metadata
    db_ctx = Database(engine=db_engine)
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
                    "content_hash": None,
                }
            ],
        },
        priority=TaskPriority.INTERACTIVE,
    )

    # Process the task
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    try:
        # Start event processor and wait for it to be fully initialized
        await event_processor.start()

        db_ctx = Database(engine=db_engine)
        task = await db_ctx.tasks.dequeue(
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(UTC),
        )

        assert task is not None

        await _complete_through_worker(
            db_engine, task, indexing_source, embedding_generator
        )

        # Wait for all events to be processed before polling
        await indexing_source.wait_for_pending_events()

        event_data = await poll_for_document_ready_event(doc_id, engine=db_engine)

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
        get_db_context_func=lambda: Database(engine=db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    # Create a document with None metadata
    db_ctx = Database(engine=db_engine)
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
                    "content_hash": None,
                }
            ],
        },
        priority=TaskPriority.INTERACTIVE,
    )

    # Process the task
    embedding_generator = MockEmbeddingGenerator(
        model_name="test-model", dimensions=128
    )

    try:
        # Start event processor and wait for it to be fully initialized
        await event_processor.start()

        db_ctx = Database(engine=db_engine)
        task = await db_ctx.tasks.dequeue(
            task_types=["embed_and_store_batch"],
            worker_id="test-worker",
            current_time=datetime.now(UTC),
        )

        assert task is not None

        await _complete_through_worker(
            db_engine, task, indexing_source, embedding_generator
        )

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
    db_ctx = Database(engine=db_engine)
    # Clean up any existing test tasks
    await db_ctx.execute(
        tasks_table.delete().where(tasks_table.c.task_id.like("test_json_%"))
    )

    # Create test tasks with different document_ids
    test_doc_id = 999
    for i in range(3):
        await db_ctx.tasks.enqueue(
            task_id=f"test_json_{i}",
            task_type="embed_and_store_batch",
            payload={
                "document_id": test_doc_id if i < 2 else 888,
                "other_field": "test",
            },
            priority=TaskPriority.INTERACTIVE,
        )

    # Test that it correctly counts pending tasks
    pending_count = await check_document_completion(db_ctx, test_doc_id)
    assert pending_count == 2, f"Expected 2 pending tasks, got {pending_count}"

    # Test with non-existent document
    pending_count = await check_document_completion(db_ctx, 777)
    assert pending_count == 0, (
        f"Expected 0 pending tasks for non-existent doc, got {pending_count}"
    )

    # Clean up
    await db_ctx.execute(
        tasks_table.delete().where(tasks_table.c.task_id.like("test_json_%"))
    )
