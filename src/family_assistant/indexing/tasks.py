"""
Task handlers related to the document indexing pipeline.
"""

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select

from family_assistant.events.indexing_source import IndexingEventType
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector import add_embedding, get_document_by_id

if TYPE_CHECKING:
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


async def check_document_completion(
    db_context: "DatabaseContext",
    document_id: int,
) -> int:
    """
    Check if there are any pending indexing or embedding tasks for a document.

    Returns:
        Number of pending tasks for the document
    """
    # For SQLite compatibility, fetch all pending tasks and filter in Python
    results = await db_context.fetch_all(
        select(tasks_table.c.payload).where(
            and_(
                tasks_table.c.task_type.in_([
                    "index_document",
                    "index_email",
                    "index_note",
                    "embed_and_store_batch",
                    "process_uploaded_document",
                ]),
                tasks_table.c.status.in_(["pending", "locked"]),
            )
        )
    )

    # Count tasks that match our document_id
    pending_count = 0
    for row in results:
        payload = row["payload"]
        if payload and payload.get("document_id") == document_id:
            pending_count += 1

    return pending_count


async def handle_embed_and_store_batch(
    exec_context: "ToolExecutionContext",  # Changed parameter name to match hypothesized caller
    payload: dict[str, Any],
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
                    and this object provides access to the actual DatabaseContext and EmbeddingGenerator.
        payload: The task payload containing data for embedding.

    Raises:
        ValueError: If the payload is malformed (e.g., lists have different lengths,
                    texts_to_embed is empty, or required keys are missing).
        SQLAlchemyError: If database operations fail.
        Exception: If embedding generation fails.
    """

    # Extract the actual DatabaseContext and EmbeddingGenerator from the ToolExecutionContext.
    db_context = exec_context.db_context
    embedding_generator_instance = exec_context.embedding_generator

    if not db_context:
        logger.error(
            "DatabaseContext not found in ToolExecutionContext for handle_embed_and_store_batch."
        )
        raise ValueError("Missing DatabaseContext in execution context.")
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
        embedding_metadata_list: list[dict[str, Any]] = payload[
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

    logger.info(
        f"Generating {len(texts_to_embed)} embeddings for document_id {document_id}."
    )
    embedding_result = await embedding_generator_instance.generate_embeddings(
        texts_to_embed
    )

    for i, text_content in enumerate(texts_to_embed):
        meta = embedding_metadata_list[i]
        vector = embedding_result.embeddings[i]

        await add_embedding(
            db_context=db_context,  # Use the extracted DatabaseContext
            document_id=document_id,
            chunk_index=meta["chunk_index"],
            embedding_type=meta["embedding_type"],
            embedding=vector,
            embedding_model=embedding_result.model_name,
            content=text_content,
            content_hash=meta.get("content_hash"),
            embedding_doc_metadata=meta["original_content_metadata"],
        )
    logger.info(
        f"Successfully stored {len(texts_to_embed)} embeddings for document_id {document_id}."
    )

    # Check if all tasks for this document are complete
    if indexing_source := exec_context.indexing_source:
        try:
            pending_count = await check_document_completion(db_context, document_id)
        except Exception as e:
            logger.error(
                f"Failed to check document completion for document_id {document_id}: {e}",
                exc_info=True,
            )
            return

        if pending_count == 0:
            logger.info(
                f"All tasks complete for document_id {document_id}. Emitting DOCUMENT_READY event."
            )

            # Get document information for the event
            try:
                doc_info = await get_document_by_id(db_context, document_id)

                # Count embeddings for metadata
                from sqlalchemy import func, select

                from family_assistant.storage.vector import DocumentEmbeddingRecord

                embeddings_result = await db_context.fetch_one(
                    select(
                        func.count().label("total_embeddings"),
                        func.count(
                            func.distinct(DocumentEmbeddingRecord.embedding_type)
                        ).label("embedding_types"),
                    ).where(DocumentEmbeddingRecord.document_id == document_id)
                )
                embeddings_data = embeddings_result

                # Emit the document ready event
                if doc_info:
                    await indexing_source.emit_event({
                        "event_type": IndexingEventType.DOCUMENT_READY.value,
                        "document_id": document_id,
                        "document_type": doc_info.source_type,
                        "document_title": doc_info.title,
                        "metadata": {
                            "total_embeddings": embeddings_data["total_embeddings"]
                            if embeddings_data
                            else 0,
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
            except Exception as e:
                logger.error(
                    f"Failed to emit DOCUMENT_READY event for document {document_id}: {e}",
                    exc_info=True,
                )
        else:
            logger.debug(
                f"Document {document_id} still has {pending_count} pending tasks."
            )
