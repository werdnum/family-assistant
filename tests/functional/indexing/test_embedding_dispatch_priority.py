"""The lane an embedding batch is dispatched into.

``embed_and_store_batch`` is the one handler that serves both lanes: an upload's
batches are work somebody is waiting on, a bulk re-index's are not. The pipeline
does not know which handler ran it, so the dispatch reads the lane from the
execution context the worker built for that task.
"""

from typing import cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.indexing.ingestion import IngestedDocument
from family_assistant.indexing.pipeline import IndexableContent
from family_assistant.indexing.processors.dispatch_processors import (
    EmbeddingDispatchProcessor,
)
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.storage.vector import Document
from family_assistant.tools import ToolExecutionContext


def _context(db_engine: AsyncEngine, priority: TaskPriority) -> ToolExecutionContext:
    """The context the task worker builds for a task running in ``priority``."""
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="indexing-conv",
        user_name="IndexingUser",
        turn_id=None,
        db_context=Database(db_engine),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        task_priority=priority,
    )


def _document(document_id: int) -> Document:
    return cast(
        "Document",
        IngestedDocument(
            source_type="upload",
            source_id=f"doc-{document_id}",
            source_uri=None,
            title="Fruit Facts",
            created_at=None,
            metadata={},
            id=document_id,
        ),
    )


@pytest.mark.parametrize(
    "priority",
    [TaskPriority.INTERACTIVE, TaskPriority.BACKGROUND],
)
@pytest.mark.asyncio
async def test_dispatched_batch_carries_the_running_tasks_lane(
    db_engine: AsyncEngine, priority: TaskPriority
) -> None:
    db = Database(db_engine)
    processor = EmbeddingDispatchProcessor(embedding_types_to_dispatch=["title"])
    items = [
        IndexableContent(
            embedding_type="title",
            source_processor="TitleExtractor",
            content="Fruit Facts",
        )
    ]

    await processor.process(
        current_items=items,
        original_document=_document(1),
        initial_content_ref=None,
        context=_context(db_engine, priority),
    )

    rows = await db.fetch_all(
        select(tasks_table.c.priority).where(
            tasks_table.c.task_type == "embed_and_store_batch"
        )
    )
    assert [row["priority"] for row in rows] == [priority]
