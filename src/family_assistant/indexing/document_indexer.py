"""
Handles the indexing process for documents uploaded via the API.
"""

import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

# Use absolute imports
from family_assistant.indexing.pipeline import (
    IndexableContent,
    IndexingPipeline,
)  # Added

# Import the Document protocol from the correct location (though not directly used here, good practice)
from family_assistant.storage.vector import get_document_by_id  # Added import
from family_assistant.tools import ToolExecutionContext  # Import the context class

logger = logging.getLogger(__name__)


# --- Document Indexer Class ---


class DocumentIndexer:
    """
    Handles the indexing process for documents, primarily those uploaded via API.
    Takes dependencies via constructor.
    """

    def __init__(self, pipeline: IndexingPipeline) -> None:  # Modified
        """
        Initializes the DocumentIndexer.

        Args:
            pipeline: An instance of IndexingPipeline. # Modified
        """
        if not pipeline:
            raise ValueError("IndexingPipeline instance is required.")
        self.pipeline = pipeline
        logger.info(
            f"DocumentIndexer initialized with pipeline: {type(pipeline).__name__}"
        )

    async def process_document(
        self, exec_context: ToolExecutionContext, payload: dict[str, Any]
    ) -> None:
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
        if not document_id:
            logger.error("Missing 'document_id' in process_document task payload.")
            raise ValueError("Missing 'document_id' in task payload.")

        # Fetch the original DocumentRecord
        try:
            # Assuming a function like get_document_by_id exists.
            original_document_record = await get_document_by_id(  # Changed from storage.get_document_by_id
                db_context,
                document_id,  # The returned object should conform to the Document protocol.
            )
            if not original_document_record:
                raise ValueError(f"Document with ID {document_id} not found.")
        except SQLAlchemyError as e:
            logger.error(
                f"Database error fetching document {document_id}: {e}", exc_info=True
            )
            raise RuntimeError(f"Failed to fetch document {document_id}") from e
        except ValueError as e:
            logger.error(str(e))
            raise  # Re-raise to mark task as failed if document not found

        initial_items: list[IndexableContent] = []

        # Process uploaded file reference, if present
        file_ref: str | None = payload.get("file_ref")
        mime_type: str | None = payload.get("mime_type")
        original_filename: str | None = payload.get("original_filename")

        if file_ref and mime_type: # mime_type is now the detected one from the API
            logger.info(
                f"Creating IndexableContent for file for document ID {document_id}: path='{file_ref}', mime_type='{mime_type}', original_filename='{original_filename}'"
            )
            file_item_metadata = {}
            if original_filename: # original_filename comes from the uploaded file's name
                file_item_metadata["original_filename"] = original_filename

            file_item = IndexableContent(
                content=None,  # Binary content is at file_ref
                embedding_type="original_document_file",  # Generic type for the whole file
                mime_type=mime_type, # Use the detected MIME type passed in payload
                source_processor="DocumentIndexer.process_document",
                metadata=file_item_metadata,
                ref=file_ref, # file_ref is the path to the persistently stored file
            )
            initial_items.append(file_item)
        elif (
            file_ref or mime_type or original_filename
        ):  # Log if some file info is present but not all essential parts
            logger.warning(
                f"Incomplete file information in payload for document ID {document_id}. "
                f"File ref: {file_ref}, MIME type: {mime_type}, Original filename: {original_filename}. "
                "Skipping file item creation."
            )

        # Process content_parts, if present
        content_parts: dict[str, str] | None = payload.get("content_parts")
        if content_parts:
            logger.info(
                f"Preparing content parts for indexing pipeline for document ID: {document_id} with {len(content_parts)} part(s)."
            )
            for key, text_content in content_parts.items():
                if not text_content or not isinstance(text_content, str):
                    logger.warning(
                        f"Skipping invalid content part for key '{key}' in document {document_id}. Content: {text_content!r}"
                    )
                    continue

                embedding_type = key
                metadata_for_item = {"original_key": key}

                if key.startswith("content_chunk_"):
                    embedding_type = "content_chunk"
                    try:
                        # Example: "content_chunk_0", "content_chunk_12"
                        parsed_chunk_index = int(key.split("_")[-1])
                        metadata_for_item["chunk_index"] = parsed_chunk_index
                    except (IndexError, ValueError):
                        logger.warning(
                            f"Could not parse chunk index from key '{key}', not storing in metadata."
                        )
                # Add other specific key parsings if needed, e.g., for 'title'
                elif key == "title":
                    embedding_type = "title"

                item = IndexableContent(
                    content=text_content,
                    embedding_type=embedding_type,
                    mime_type="text/plain",  # Assuming content_parts are always text
                    source_processor="DocumentIndexer.process_document",
                    metadata=metadata_for_item,
                    ref=None,  # Text content is inline
                )
                initial_items.append(item)
        else:
            logger.info(
                f"No 'content_parts' found in payload for document ID {document_id}."
            )

        if not initial_items:
            logger.warning(
                f"No IndexableContent items created for document {document_id} from either file or content_parts. Nothing to index."
            )
            # file_ref now points to a persistent location, so it's not removed here.
            # The file remains in /mnt/data/mailbox/documents/ even if no initial items are processed.
            return  # Nothing to do, task is successful

        # Run the pipeline with the prepared items
        try:
            logger.info(
                f"Running indexing pipeline for document {document_id} with {len(initial_items)} initial item(s)."
            )
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
            # Note: If the pipeline fails, the temporary file (if any) is not cleaned up here.
            # Cleanup should ideally happen after the pipeline (or its constituent tasks)
            # are fully done with the file. This is marked as Phase 4 in the plan.
            raise RuntimeError(
                f"Indexing pipeline failed for document {document_id}"
            ) from e

        logger.info(
            f"Indexing pipeline successfully initiated for document {document_id}."
        )
        # Task completion is handled by the worker loop.
        # The temporary file (if file_ref was used) is expected to be handled
        # by the pipeline processors or a subsequent cleanup mechanism (Phase 4).


__all__ = ["DocumentIndexer"]
