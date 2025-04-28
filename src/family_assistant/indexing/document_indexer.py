"""
Handles the indexing process for documents uploaded via the API.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, List

from sqlalchemy.exc import SQLAlchemyError

# Use absolute imports
from family_assistant import storage # For DB operations (add_embedding)
from family_assistant.storage.context import DatabaseContext
from family_assistant.embeddings import EmbeddingGenerator, EmbeddingResult # Protocol for embedding
# Import the Document protocol from the correct location (though not directly used here, good practice)
from family_assistant.storage.vector import Document

logger = logging.getLogger(__name__)


# --- Document Indexer Class ---

class DocumentIndexer:
    """
    Handles the indexing process for documents, primarily those uploaded via API.
    Takes dependencies via constructor.
    """
    def __init__(self, embedding_generator: EmbeddingGenerator):
        """
        Initializes the DocumentIndexer.

        Args:
            embedding_generator: An instance conforming to the EmbeddingGenerator protocol.
        """
        if not embedding_generator:
            raise ValueError("EmbeddingGenerator instance is required.")
        self.embedding_generator = embedding_generator
        logger.info(f"DocumentIndexer initialized with embedding generator: {type(embedding_generator).__name__}")

    async def process_document(self, db_context: DatabaseContext, payload: Dict[str, Any]):
        """
        Task handler method to process and index content parts provided for a document.
        """
        document_id = payload.get("document_id")
        content_parts: Optional[Dict[str, str]] = payload.get("content_parts") # e.g., {"title": "...", "content_chunk_0": "..."}

        if not document_id:
            raise ValueError("Missing 'document_id' in process_uploaded_document task payload.")
        if not content_parts:
            logger.warning(f"No 'content_parts' found in payload for document ID {document_id}. Nothing to index.")
            return # Nothing to do, task is successful

        # Dependency is now self.embedding_generator
        logger.info(f"Starting indexing for uploaded document ID: {document_id} with {len(content_parts)} content part(s).")

        # --- 1. Prepare Texts for Embedding ---
        # Extract texts and map keys to embedding types and chunk indices
    texts_to_embed: List[str] = []
    embedding_metadata: List[Dict[str, Any]] = [] # Store type and chunk index for each text

    for key, text_content in content_parts.items():
        if not text_content or not isinstance(text_content, str):
            logger.warning(f"Skipping invalid content part for key '{key}' in document {document_id}. Content: {text_content!r}")
            continue

        texts_to_embed.append(text_content)

        # Determine embedding_type and chunk_index from the key
        embedding_type = key
        chunk_index = 0 # Default for non-chunked types like 'title', 'summary'
        if key.startswith("content_chunk_"):
            embedding_type = "content_chunk"
            try:
                # Extract index from key like "content_chunk_0" -> 0
                chunk_index = int(key.split('_')[-1])
            except (IndexError, ValueError):
                 logger.warning(f"Could not parse chunk index from key '{key}', defaulting to 0.")
                 chunk_index = 0 # Fallback

        embedding_metadata.append({
            "original_key": key,
            "embedding_type": embedding_type,
            "chunk_index": chunk_index,
            "content": text_content,
        })

    if not texts_to_embed:
        logger.warning(f"No valid text content found to embed for document {document_id}. Skipping embedding generation.")
            return

        # --- 2. Generate Embeddings ---
        logger.info(f"Generating embeddings for {len(texts_to_embed)} text part(s) for document {document_id} using model {self.embedding_generator.model_name}...")
        try:
            embedding_result: EmbeddingResult = await self.embedding_generator.generate_embeddings(texts_to_embed)
        except Exception as e:
            logger.error(f"Embedding generation failed for document {document_id}: {e}", exc_info=True)
            # Re-raise to mark the task as failed
        raise RuntimeError(f"Embedding generation failed for document {document_id}") from e


    if len(embedding_result.embeddings) != len(texts_to_embed):
        logger.error(f"Mismatch between number of texts ({len(texts_to_embed)}) and generated embeddings ({len(embedding_result.embeddings)}) for document {document_id}.")
        raise RuntimeError("Embedding generation returned unexpected number of results.")

    # --- 3. Store Embeddings ---
    embedding_model_name = embedding_result.model_name
    stored_count = 0
    for i, embedding_vector in enumerate(embedding_result.embeddings):
        meta = embedding_metadata[i]
        logger.debug(f"Adding embedding: doc={document_id}, chunk={meta['chunk_index']}, type={meta['embedding_type']}, model={embedding_model_name}")
        try:
            await storage.add_embedding(
                db_context=db_context,
                document_id=document_id,
                chunk_index=meta['chunk_index'],
                embedding_type=meta['embedding_type'],
                embedding=embedding_vector,
                embedding_model=embedding_model_name,
                content=meta['content'],
                # content_hash=None # Optional: calculate hash if needed
            )
            stored_count += 1
        except SQLAlchemyError as e:
            logger.error(f"Database error storing embedding for doc {document_id}, key {meta['original_key']}: {e}", exc_info=True)
            # Decide whether to continue or fail the whole task.
            # Let's fail the task if any embedding storage fails.
            raise RuntimeError(f"Failed to store embedding for key {meta['original_key']}") from e
        except Exception as e:
             logger.error(f"Unexpected error storing embedding for doc {document_id}, key {meta['original_key']}: {e}", exc_info=True)
             raise RuntimeError(f"Unexpected error storing embedding for key {meta['original_key']}") from e


        logger.info(f"Successfully stored {stored_count} embeddings for document {document_id}.")
        # Task completion is handled by the worker loop


# Remove the global state and setter function

__all__ = ["DocumentIndexer"]
