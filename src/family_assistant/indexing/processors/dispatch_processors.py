"""
Content processors focused on dispatching embedding tasks.
"""
import logging
import uuid
from typing import List, Dict, Any, Set

from family_assistant.indexing.pipeline import IndexableContent, ContentProcessor
from family_assistant.storage.vector import Document # Document protocol
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class EmbeddingDispatchProcessor(ContentProcessor):
    """
    Identifies IndexableContent items of specified types and dispatches them
    for embedding via the 'embed_and_store_batch' task.
    """

    def __init__(self, embedding_types_to_dispatch: List[str]):
        """
        Args:
            embedding_types_to_dispatch: A list of embedding_type strings
                that this processor instance should handle and dispatch.
        """
        self._embedding_types_to_dispatch: Set[str] = set(embedding_types_to_dispatch)

    @property
    def name(self) -> str:
        return "EmbeddingDispatchProcessor"

    async def process(
        self,
        current_items: List[IndexableContent],
        original_document: Document, # Document protocol
        initial_content_ref: IndexableContent,
        context: ToolExecutionContext,
    ) -> List[IndexableContent]:
        """
        Filters items by configured embedding_types, batches them,
        and dispatches an 'embed_and_store_batch' task.
        All original items are passed through to the next stage.
        """
        items_to_embed: List[IndexableContent] = []
        for item in current_items:
            if item.embedding_type in self._embedding_types_to_dispatch and item.content:
                items_to_embed.append(item)

        if not items_to_embed:
            logger.debug(f"{self.name}: No items found for dispatch matching types: {self._embedding_types_to_dispatch}")
            return current_items # Pass all items through

        document_id = getattr(original_document, 'id', None)
        if document_id is None:
            # This assumes original_document is a DocumentRecord instance or similar with an 'id'
            # If not, the Document protocol might need an ID or it must be passed differently.
            logger.error(
                f"{self.name}: Cannot dispatch embeddings. Original document does not have an 'id' attribute. Document source_id: {original_document.source_id}"
            )
            return current_items # Pass all items through

        texts_to_embed_list: List[str] = []
        embedding_metadata_list: List[Dict[str, Any]] = []

        for item_to_dispatch in items_to_embed:
            if item_to_dispatch.content: # Ensure content is not None
                texts_to_embed_list.append(item_to_dispatch.content)
                meta_for_task = {
                    "embedding_type": item_to_dispatch.embedding_type,
                    "chunk_index": item_to_dispatch.metadata.get("chunk_index", 0), # Default to 0 if not present
                    "original_content_metadata": item_to_dispatch.metadata,
                    "content_hash": item_to_dispatch.metadata.get("content_hash"), # Can be None
                }
                embedding_metadata_list.append(meta_for_task)

        if texts_to_embed_list:
            task_payload = {
                "document_id": document_id,
                "texts_to_embed": texts_to_embed_list,
                "embedding_metadata_list": embedding_metadata_list,
            }
            # Generate a unique task ID for idempotency
            task_id = f"embed_batch_{document_id}_{uuid.uuid4()}"

            await context.enqueue_task(
                task_id=task_id, task_type="embed_and_store_batch", payload=task_payload
            )
            logger.info(f"{self.name}: Dispatched {len(texts_to_embed_list)} items for embedding (task_id: {task_id}) for document ID {document_id}.")
        else:
            logger.debug(f"{self.name}: No valid content found in items selected for dispatch for document ID {document_id}.")

        return current_items # Pass all original items through to the next processor

