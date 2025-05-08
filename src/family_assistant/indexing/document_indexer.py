"""
Handles the indexing process for documents uploaded via the API.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, List

from sqlalchemy.exc import SQLAlchemyError

# Use absolute imports
from family_assistant import storage  # For DB operations (add_embedding)
from family_assistant.storage.context import DatabaseContext
from family_assistant.indexing.pipeline import IndexingPipeline, IndexableContent # Added

# Import the Document protocol from the correct location (though not directly used here, good practice)
from family_assistant.storage.vector import Document
from family_assistant.tools import ToolExecutionContext  # Import the context class

logger = logging.getLogger(__name__)


# --- Document Indexer Class ---


class DocumentIndexer:
    """
    Handles the indexing process for documents, primarily those uploaded via API.
    Takes dependencies via constructor.
    """

    def __init__(self, pipeline: IndexingPipeline): # Modified
        """
        Initializes the DocumentIndexer.

        Args:
            pipeline: An instance of IndexingPipeline. # Modified
        """
        if not pipeline: # Modified
            raise ValueError("IndexingPipeline instance is required.") # Modified
        self.pipeline = pipeline # Modified
        logger.info(
            f"DocumentIndexer initialized with pipeline: {type(pipeline).__name__}"
        )

    async def process_document(
        self, exec_context: ToolExecutionContext, payload: Dict[str, Any]
    ):
        """
        Task handler method to process and index content parts provided for a document
        by running them through an indexing pipeline.
        Receives ToolExecutionContext from the TaskWorker.
        """
        # Extract db_context from the execution context
        db_context = exec_context.db_context
        if not db_context:
            logger.error(
                "DatabaseContext not found in ToolExecutionContext for process_document."
            )
            raise ValueError("Missing DatabaseContext dependency in context.")

        document_id = payload.get("document_id")
        content_parts: Optional[Dict[str, str]] = payload.get("content_parts")

        if not document_id:
            raise ValueError(
                "Missing 'document_id' in process_uploaded_document task payload."
            )

        # Fetch the original DocumentRecord
        try:
            # Assuming a function like get_document_by_id exists.
            # The returned object should conform to the Document protocol.
            original_document_record = await storage.get_document_by_id(
                db_context, document_id
            )
            if not original_document_record:
                raise ValueError(f"Document with ID {document_id} not found.")
        except SQLAlchemyError as e:
            logger.error(f"Database error fetching document {document_id}: {e}", exc_info=True)
            raise RuntimeError(f"Failed to fetch document {document_id}") from e
        except ValueError as e:
            logger.error(str(e))
            raise # Re-raise to mark task as failed if document not found

        if not content_parts:
            logger.warning(
                f"No 'content_parts' found in payload for document ID {document_id}. Nothing to index."
            )
            return  # Nothing to do, task is successful

        logger.info(
            f"Preparing content parts for indexing pipeline for document ID: {document_id} with {len(content_parts)} part(s)."
        )

        initial_items: List[IndexableContent] = []
        for key, text_content in content_parts.items():
            if not text_content or not isinstance(text_content, str):
                logger.warning(
                    f"Skipping invalid content part for key '{key}' in document {document_id}. Content: {text_content!r}"
                )
                continue

            embedding_type = key
            # chunk_index = 0 # Default, not directly used for IndexableContent here unless stored in metadata
            metadata_for_item = {"original_key": key}

            if key.startswith("content_chunk_"):
                embedding_type = "content_chunk"
                try:
                    parsed_chunk_index = int(key.split("_")[-1])
                    metadata_for_item["chunk_index"] = parsed_chunk_index
                except (IndexError, ValueError):
                    logger.warning(
                        f"Could not parse chunk index from key '{key}', not storing in metadata."
                    )

            item = IndexableContent(
                content=text_content,
                embedding_type=embedding_type,
                mime_type="text/plain",
                source_processor="DocumentIndexer.process_document",
                metadata=metadata_for_item,
                ref=None,
            )
            initial_items.append(item)

        if not initial_items:
            logger.warning(
                f"No valid IndexableContent items created from content_parts for document {document_id}. Skipping pipeline run."
            )
            return

        # Run the pipeline with the prepared items
        try:
            logger.info(f"Running indexing pipeline for document {document_id} with {len(initial_items)} initial items.")
            # Pass the list of items directly to the pipeline's run method.
            # The pipeline's run method will determine how to handle initial_content_ref from this list.
            await self.pipeline.run(
                initial_items=initial_items,
                original_document=original_document_record,
                context=exec_context,
            )
        except Exception as e:
            logger.error(
                f"Indexing pipeline run failed for document {document_id}: {e}",
                exc_info=True,
            )
            # Re-raise to mark the task as failed
            raise RuntimeError(
                f"Indexing pipeline failed for document {document_id}"
            ) from e

        logger.info(
            f"Indexing pipeline successfully initiated for document {document_id}."
        )
        # Task completion is handled by the worker loop

__all__ = ["DocumentIndexer"]
