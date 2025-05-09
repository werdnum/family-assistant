"""
Task handlers related to the document indexing pipeline.
"""
import logging
from typing import Any, Dict, List

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import add_embedding

logger = logging.getLogger(__name__)


async def handle_embed_and_store_batch(
    exec_context: "ToolExecutionContext",  # Changed from db_context
    payload: Dict[str, Any],
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
        exec_context: The tool execution context, providing db_context and embedding_generator.
        payload: The task payload containing data for embedding.

    Raises:
        ValueError: If the payload is malformed (e.g., lists have different lengths,
                    texts_to_embed is empty, or required keys are missing).
        SQLAlchemyError: If database operations fail.
        Exception: If embedding generation fails.
    """
    db_context = exec_context.db_context
    embedding_generator = exec_context.embedding_generator

    if not db_context:
        logger.error("DatabaseContext not found in ToolExecutionContext for handle_embed_and_store_batch.")
        raise ValueError("Missing DatabaseContext in execution context.")
    if not embedding_generator:
        logger.error("EmbeddingGenerator not found in ToolExecutionContext for handle_embed_and_store_batch.")
        raise ValueError("Missing EmbeddingGenerator in execution context.")


    try:
        document_id: int = payload["document_id"]
        texts_to_embed: List[str] = payload["texts_to_embed"]
        embedding_metadata_list: List[Dict[str, Any]] = payload[
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
        raise ValueError(
            "Texts to embed and metadata list must have the same length."
        )

    logger.info(
        f"Generating {len(texts_to_embed)} embeddings for document_id {document_id}."
    )
    embedding_result = await embedding_generator.generate_embeddings(texts_to_embed)

    for i, text_content in enumerate(texts_to_embed):
        meta = embedding_metadata_list[i]
        vector = embedding_result.embeddings[i]

        await add_embedding(
            db_context=db_context,
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
