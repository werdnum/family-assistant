"""
Content processors focused on dispatching embedding tasks.
"""

import logging
import uuid
from typing import Any

from family_assistant.indexing.pipeline import ContentProcessor, IndexableContent
from family_assistant.storage.tasks import enqueue_task
from family_assistant.storage.vector import Document  # Document protocol
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class EmbeddingDispatchProcessor(ContentProcessor):
    """
    Identifies IndexableContent items of specified types and dispatches them
    for embedding via the 'embed_and_store_batch' task.
    """

    def __init__(self, embedding_types_to_dispatch: list[str]) -> None:
        """
        Args:
            embedding_types_to_dispatch: A list of embedding_type strings
                that this processor instance should handle and dispatch.
        """
        self._embedding_types_to_dispatch: set[str] = set(embedding_types_to_dispatch)

    @property
    def name(self) -> str:
        return "EmbeddingDispatchProcessor"

    async def process(
        self,
        current_items: list[IndexableContent],
        original_document: Document,  # Document protocol
        initial_content_ref: IndexableContent | None,
        context: ToolExecutionContext,
    ) -> list[IndexableContent]:
        """
        Filters items by configured embedding_types, batches them,
        and dispatches an 'embed_and_store_batch' task.
        All original items are passed through to the next stage.
        """
        items_to_embed: list[IndexableContent] = []
        doc_id_for_log = getattr(original_document, "id", "UNKNOWN_DOC_ID")
        logger.info(
            f"[{self.name}/{doc_id_for_log}] Processing {len(current_items)} items. Dispatch types: {self._embedding_types_to_dispatch}"
        )

        for item in current_items:
            item_content_status = "present" if item.content else "MISSING/EMPTY"
            logger.info(
                f"[{self.name}/{doc_id_for_log}] Evaluating item: type='{item.embedding_type}', content is {item_content_status}, source='{item.source_processor}', metadata='{item.metadata}'"
            )
            if (
                item.embedding_type in self._embedding_types_to_dispatch
                and item.content
            ):
                items_to_embed.append(item)
                logger.info(
                    f"[{self.name}/{doc_id_for_log}] Item '{item.embedding_type}' (source: {item.source_processor}) ADDED to embedding batch."
                )
            elif item.embedding_type not in self._embedding_types_to_dispatch:
                logger.info(
                    f"[{self.name}/{doc_id_for_log}] Item '{item.embedding_type}' (source: {item.source_processor}) SKIPPED (type not in dispatch list {self._embedding_types_to_dispatch})."
                )
            elif not item.content:
                logger.info(
                    f"[{self.name}/{doc_id_for_log}] Item '{item.embedding_type}' (source: {item.source_processor}) SKIPPED (no content)."
                )
            else:
                logger.info(
                    f"[{self.name}/{doc_id_for_log}] Item '{item.embedding_type}' (source: {item.source_processor}) SKIPPED (other reasons)."
                )

        if not items_to_embed:
            logger.info(
                f"[{self.name}/{doc_id_for_log}] No items found for dispatch after filtering. Configured types: {self._embedding_types_to_dispatch}"
            )
            return current_items  # Pass all items through

        document_id = getattr(original_document, "id", None)
        logger.info(
            f"[{self.name}/{doc_id_for_log}] Extracted document_id: {document_id} for dispatch (Source ID: {original_document.source_id})."
        )
        if document_id is None:
            # This assumes original_document is a DocumentRecord instance or similar with an 'id'
            # If not, the Document protocol might need an ID or it must be passed differently.
            logger.error(
                f"[{self.name}/{doc_id_for_log}] CRITICAL: Cannot dispatch embeddings. Original document (source_id: {original_document.source_id}) does not have an 'id' attribute or it's None."
            )
            return current_items  # Pass all items through

        texts_to_embed_list: list[str] = []
        embedding_metadata_list: list[dict[str, Any]] = []

        for item_to_dispatch in items_to_embed:
            if item_to_dispatch.content:  # Ensure content is not None
                texts_to_embed_list.append(item_to_dispatch.content)
                meta_for_task = {
                    "embedding_type": item_to_dispatch.embedding_type,
                    "chunk_index": item_to_dispatch.metadata.get(
                        "chunk_index", 0
                    ),  # Default to 0 if not present
                    "original_content_metadata": item_to_dispatch.metadata,
                    "content_hash": item_to_dispatch.metadata.get(
                        "content_hash"
                    ),  # Can be None
                }
                embedding_metadata_list.append(meta_for_task)

        logger.info(
            f"[{self.name}/{doc_id_for_log}] Prepared {len(texts_to_embed_list)} texts for embedding. Payload details: document_id={document_id}, num_texts={len(texts_to_embed_list)}, num_metadata_items={len(embedding_metadata_list)}"
        )
        if texts_to_embed_list:
            task_payload = {
                "document_id": document_id,
                "texts_to_embed": texts_to_embed_list,
                "embedding_metadata_list": embedding_metadata_list,
            }
            # Generate a unique task ID for idempotency
            task_id = f"embed_batch_{document_id}_{uuid.uuid4()}"

            logger.info(
                f"[{self.name}/{doc_id_for_log}] Attempting to enqueue 'embed_and_store_batch' task (ID: {task_id}) for document ID {document_id} with {len(texts_to_embed_list)} items."
            )
            await enqueue_task(
                db_context=context.db_context,
                task_id=task_id,
                task_type="embed_and_store_batch",
                payload=task_payload,
                notify_event=(
                    context.application.new_task_event  # type: ignore[attr-defined]
                    if context.application
                    and hasattr(context.application, "new_task_event")
                    else None
                ),
            )
            logger.info(
                f"[{self.name}/{doc_id_for_log}] Successfully enqueued 'embed_and_store_batch' task (ID: {task_id}) for document ID {document_id} with {len(texts_to_embed_list)} items."
            )
        else:
            logger.info(
                f"[{self.name}/{doc_id_for_log}] No valid (non-empty) content found in items selected for dispatch for document ID {document_id}."
            )

        return current_items  # Pass all original items through to the next processor
