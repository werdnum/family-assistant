"""
Task handlers related to the document indexing pipeline.
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import and_, func, select

from family_assistant.events.indexing_source import IndexingEventType
from family_assistant.indexing.types import EmbedAndStoreBatchPayload, EmbeddingMetadata
from family_assistant.storage.tasks import ACTIVE_TASK_STATUSES, tasks_table
from family_assistant.storage.vector import (
    DocumentEmbeddingRecord,
    DocumentRecord,
    add_embedding,
    get_document_by_id,
)

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.storage.database import Database, DatabaseTransaction
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Task types that work on one document, named by ``document_id`` in their
# payload. The document is ready once none of them is left unfinished.
DOCUMENT_INDEXING_TASK_TYPES = (
    "process_uploaded_document",
    "embed_and_store_batch",
)


async def check_document_completion(
    db_context: "Database | DatabaseTransaction",
    document_id: int,
) -> int:
    """Count the indexing tasks for a document that the queue has not finished.

    A task is unfinished while it is waiting to be claimed or is running.
    """
    result = await db_context.fetch_one(
        select(func.count().label("count"))  # pylint: disable=not-callable
        .select_from(tasks_table)
        .where(
            and_(
                tasks_table.c.task_type.in_(DOCUMENT_INDEXING_TASK_TYPES),
                tasks_table.c.status.in_(ACTIVE_TASK_STATUSES),
                sa.cast(tasks_table.c.payload["document_id"].as_string(), sa.Integer)
                == document_id,
            )
        )
    )
    return result["count"] if result else 0


async def document_ready_after_task_done(
    txn: "DatabaseTransaction",
    task_type: str,
    # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
    payload: Mapping[str, Any] | None,
) -> int | None:
    """Decide, in the transaction marking a task done, whether its document is ready.

    Returns the document id when this task was the document's last unfinished
    indexing task, else ``None``. The caller emits DOCUMENT_READY for it once
    the transaction has committed.

    Runs after the task's own row is marked done, so the last task to finish
    sees no unfinished siblings. The document row is locked first so that two
    tasks finishing together are ordered: the first to take the lock still sees
    the other unfinished, and the second sees it done. Exactly one of them
    reports the document ready. SQLite needs no lock, since its writers are
    already serialized.
    """
    if task_type not in DOCUMENT_INDEXING_TASK_TYPES or not payload:
        return None
    document_id = payload.get("document_id")
    if not isinstance(document_id, int):
        return None
    document = await txn.fetch_one(
        select(DocumentRecord.id)
        .where(DocumentRecord.id == document_id)
        .with_for_update()
    )
    if document is None:
        return None
    if await check_document_completion(txn, document_id) > 0:
        return None
    return document_id


async def handle_embed_and_store_batch(
    exec_context: "ToolExecutionContext",
    payload: EmbedAndStoreBatchPayload,
) -> None:
    """
    Task handler for embedding a batch of texts and storing them in the vector database.

    The payload is expected to contain:
    - document_id (int): The ID of the parent document.
    - texts_to_embed (List[str]): A list of text strings to embed.
    - embedding_metadata_list (List[Dict[str, Any]]): A list of metadata dictionaries,
      one for each text in texts_to_embed. Each dictionary should contain:
        - embedding_type (str): The type of embedding (e.g., 'title', 'content_chunk').
        - chunk_index (int): The index of this chunk for the given embedding_type.
        - original_content_metadata (Dict[str, Any]): Metadata from the content processor.
        - content_hash (Optional[str]): Hash of the content, if available.

    Args:
        db_context: The ToolExecutionContext object. This parameter name matches
                    the keyword argument likely used by the calling TaskWorker,
                    and this object provides access to the actual Database and EmbeddingGenerator.
        payload: The task payload containing data for embedding.

    Raises:
        ValueError: If the payload is malformed (e.g., lists have different lengths,
                    texts_to_embed is empty, or required keys are missing).
        SQLAlchemyError: If database operations fail.
        Exception: If embedding generation fails.
    """

    # Extract the actual Database and EmbeddingGenerator from the ToolExecutionContext.
    db_context = exec_context.db_context
    embedding_generator_instance = exec_context.embedding_generator

    if not db_context:
        logger.error(
            "Database not found in ToolExecutionContext for handle_embed_and_store_batch."
        )
        raise ValueError("Missing Database in execution context.")
    if not embedding_generator_instance:
        logger.error(
            "Embedding generator not found in ToolExecutionContext for handle_embed_and_store_batch."
        )
        raise ValueError(
            "Missing EmbeddingGenerator instance in execution context (exec_context.embedding_generator was None)."
        )

    try:
        document_id: int = payload["document_id"]
        texts_to_embed: list[str] = payload["texts_to_embed"]
        embedding_metadata_list: list[EmbeddingMetadata] = payload[
            "embedding_metadata_list"
        ]
    except KeyError as e:
        logger.error(f"Missing key in 'embed_and_store_batch' payload: {e}")
        raise ValueError(f"Malformed payload: Missing key {e}") from e

    if not texts_to_embed:
        logger.warning(
            f"Task 'embed_and_store_batch' received empty 'texts_to_embed' for document_id {document_id}. Skipping."
        )
        return

    if len(texts_to_embed) != len(embedding_metadata_list):
        logger.error(
            f"Mismatch in lengths for 'texts_to_embed' ({len(texts_to_embed)}) and "
            f"'embedding_metadata_list' ({len(embedding_metadata_list)}) for document_id {document_id}."
        )
        raise ValueError("Texts to embed and metadata list must have the same length.")

    # Configure max content length for embeddings (roughly 8K tokens)
    MAX_CONTENT_LENGTH = 30000  # Characters, not tokens

    logger.info(
        f"Processing {len(texts_to_embed)} items for document_id {document_id}."
    )

    # Process each text item individually for graceful degradation
    successful_embeds = 0
    storage_only_items = 0

    for i, text_content in enumerate(texts_to_embed):
        meta = embedding_metadata_list[i]
        embedding_vector = None
        embedding_model_used = "unknown"

        # Update worker activity every 10 items to prevent timeout
        if i % 10 == 0 and exec_context.update_activity_callback:
            exec_context.update_activity_callback()
            if i > 0:  # Don't log on first iteration
                logger.debug(
                    f"Processing item {i + 1}/{len(texts_to_embed)} for document_id {document_id}"
                )

        # Check if content is too long for embedding
        if len(text_content) > MAX_CONTENT_LENGTH:
            logger.info(
                f"Content too long ({len(text_content)} chars) for embedding type "
                f"'{meta['embedding_type']}' in document {document_id}. Storing without vector."
            )
            embedding_model_used = "text_only_too_long"
            storage_only_items += 1
        else:
            (
                embedding_vector,
                embedding_model_used,
                generated,
            ) = await _generate_embedding_safely(
                embedding_generator_instance,
                text_content,
                meta["embedding_type"],
                document_id,
            )
            if generated:
                successful_embeds += 1
            else:
                storage_only_items += 1

        # Store with or without embedding
        await add_embedding(
            db_context=db_context,
            document_id=document_id,
            chunk_index=meta["chunk_index"],
            embedding_type=meta["embedding_type"],
            embedding=embedding_vector,  # May be None
            embedding_model=embedding_model_used,
            content=text_content,
            content_hash=meta["content_hash"],
            embedding_doc_metadata=meta["original_content_metadata"],
        )

    logger.info(
        f"Completed processing for document_id {document_id}: "
        f"{successful_embeds} embeddings generated, {storage_only_items} stored without vectors."
    )


async def _generate_embedding_safely(
    embedding_generator: "EmbeddingGenerator",
    text_content: str,
    embedding_type: str,
    document_id: int,
) -> tuple[list[float] | None, str, bool]:
    try:
        return await _generate_embedding(
            embedding_generator, text_content, embedding_type, document_id
        )
    except Exception as e:
        logger.warning(
            f"Embedding generation failed for type '{embedding_type}' "
            f"in document {document_id}: {e}. Storing without vector."
        )
        return None, "text_only_error", False


async def _generate_embedding(
    embedding_generator: "EmbeddingGenerator",
    text_content: str,
    embedding_type: str,
    document_id: int,
) -> tuple[list[float] | None, str, bool]:
    result = await embedding_generator.generate_embeddings([text_content])
    if result.embeddings:
        return result.embeddings[0], result.model_name, True
    logger.warning(
        f"Empty embedding result for type '{embedding_type}' "
        f"in document {document_id}. Storing without vector."
    )
    return None, "text_only_empty_result", False


async def emit_document_ready_event(
    db_context: "Database",
    indexing_source: "IndexingSource",
    document_id: int,
) -> None:
    doc_info = await get_document_by_id(db_context, document_id)
    embeddings_data = await db_context.fetch_one(
        select(
            func.count().label("total_embeddings"),  # pylint: disable=not-callable
            func.count(  # pylint: disable=not-callable
                func.distinct(DocumentEmbeddingRecord.embedding_type)
            ).label("embedding_types"),
        ).where(DocumentEmbeddingRecord.document_id == document_id)
    )
    total_embeddings = embeddings_data["total_embeddings"] if embeddings_data else 0
    if total_embeddings == 0:
        # Nothing was embedded, so the document is not searchable: it is not
        # ready, whichever of its tasks finished last.
        logger.info(
            f"Document {document_id} finished indexing with no embeddings; "
            "not emitting DOCUMENT_READY"
        )
        return
    if doc_info:
        await indexing_source.emit_event({
            "event_type": IndexingEventType.DOCUMENT_READY.value,
            "document_id": document_id,
            "document_type": doc_info.source_type,
            "document_title": doc_info.title,
            "document_metadata": doc_info.doc_metadata,
            "metadata": {
                "total_embeddings": total_embeddings,
                "embedding_types": embeddings_data["embedding_types"]
                if embeddings_data
                else 0,
                "source_id": doc_info.source_id,
            },
        })
    else:
        logger.warning(
            f"Document {document_id} not found when emitting DOCUMENT_READY event"
        )
