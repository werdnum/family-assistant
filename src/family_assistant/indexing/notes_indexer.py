"""
Handles the indexing process for notes stored in the database.
"""

import logging
from typing import Any, cast

from family_assistant import storage
from family_assistant.indexing.pipeline import IndexableContent, IndexingPipeline
from family_assistant.storage.notes import NoteDocument, get_note_by_id
from family_assistant.storage.vector import (
    Document,
    delete_document_embeddings,
    get_document_by_id,
)
from family_assistant.tools import ToolExecutionContext

logger = logging.getLogger(__name__)


class NotesIndexer:
    """
    Handles the indexing process for notes stored in the database.
    """

    def __init__(self, pipeline: IndexingPipeline) -> None:
        """
        Initializes the NotesIndexer.

        Args:
            pipeline: The IndexingPipeline instance to use for processing notes.
        """
        self.pipeline = pipeline
        logger.info("NotesIndexer initialized with an IndexingPipeline instance.")

    async def handle_index_note(
        self, exec_context: ToolExecutionContext, payload: dict[str, Any]
    ) -> None:
        """
        Task handler to index a specific note from the notes table.
        Receives ToolExecutionContext from the TaskWorker.
        """
        db_context = exec_context.db_context
        if not db_context:
            logger.error(
                "DatabaseContext not found in ToolExecutionContext for handle_index_note."
            )
            raise ValueError("Missing DatabaseContext dependency in context.")

        note_id = payload.get("note_id")
        if not note_id:
            raise ValueError("Missing 'note_id' in index_note task payload.")

        if not self.pipeline:  # Should always be set by constructor
            raise RuntimeError("IndexingPipeline dependency not set for note indexing.")

        logger.info(f"Starting indexing for note ID: {note_id}")

        # --- 1. Fetch Note Data ---
        note_row = await get_note_by_id(db_context, note_id)
        if not note_row:
            logger.warning(f"Note {note_id} not found in database. Skipping indexing.")
            # Don't raise an error, just exit gracefully. Task will be marked 'done'.
            return

        # --- 2. Create Document Object ---
        try:
            note_doc = NoteDocument(
                _id=note_row["id"],
                _title=note_row["title"],
                _content=note_row["content"],
                _created_at=note_row["created_at"],
                _updated_at=note_row["updated_at"],
            )
        except ValueError as e:
            logger.error(f"Failed to create NoteDocument for ID {note_id}: {e}")
            raise  # Re-raise to mark task as failed

        # --- 3. Add/Update Document Record in Vector DB & Get DB Record ---
        doc_id = await storage.add_document(db_context=db_context, doc=note_doc)
        logger.info(
            f"Added/Updated document record for note {note_id}, vector DB doc ID: {doc_id}"
        )

        # --- 4. Delete existing embeddings if re-indexing ---
        await delete_document_embeddings(db_context, doc_id)

        # --- 5. Get the document record for pipeline ---
        try:
            db_document_record = await get_document_by_id(db_context, doc_id)
            if not db_document_record:
                raise ValueError(
                    f"Failed to retrieve document record for ID {doc_id} after adding/updating."
                )
        except Exception as e:
            logger.error(
                f"Error fetching document record {doc_id}: {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Failed to fetch document record {doc_id}") from e

        # --- 6. Prepare Initial Content for Pipeline ---
        initial_items: list[IndexableContent] = []
        if note_doc._content:
            # The pipeline will handle title extraction, chunking, summarizing, etc.
            # Provide the combined title and content as raw text.
            note_text_item = IndexableContent(
                content=f"{note_doc.title}\n\n{note_doc._content}",
                embedding_type="raw_note_text",  # A type for processors to pick up
                mime_type="text/plain",
                source_processor="NotesIndexer.handle_index_note",
                metadata={"source": "note", "title": note_doc.title},
            )
            initial_items.append(note_text_item)

        if not initial_items:
            logger.warning(
                f"No content found to pass to pipeline for note {note_id}. Skipping pipeline run."
            )
            # Task is considered done as the document record was created/updated.
            return

        # --- 7. Run Indexing Pipeline ---
        try:
            logger.info(
                f"Running indexing pipeline for note {note_id} (Doc ID: {doc_id}) with {len(initial_items)} initial items."
            )
            await self.pipeline.run(
                initial_items=initial_items,
                original_document=cast(
                    "Document", db_document_record
                ),  # Pass the DB record, cast for protocol
                context=exec_context,
            )
        except Exception as e:
            logger.error(
                f"Indexing pipeline run failed for note {note_id} (Doc ID: {doc_id}): {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Indexing pipeline failed for note {note_id}") from e

        logger.info(
            f"Indexing pipeline successfully initiated for note {note_id} (Doc ID: {doc_id})."
        )
        # Task completion is handled by the worker loop


__all__ = ["NotesIndexer"]
