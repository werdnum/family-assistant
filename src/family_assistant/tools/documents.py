"""Document search and management tools.

This module contains tools for searching, retrieving, and managing
documents including ingestion from URLs and accessing user documentation.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import TYPE_CHECKING, Any

import aiofiles
from sqlalchemy import text

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
DOCUMENT_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Search previously stored documents (emails, notes, files) using semantic and keyword matching. Returns titles and snippets of the most relevant documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": (
                            "The natural language query describing the information to search for."
                        ),
                    },
                    "source_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional. Filter results to only include documents from specific sources. Common sources: 'email', 'note', 'google_drive', 'pdf', 'image'. Use ONLY if you are certain about the source type, otherwise omit this filter."
                        ),
                    },
                    "embedding_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional. Filter results based on the type of content that was embedded. Common types: 'content_chunk', 'summary', 'title', 'ocr_text'. Use ONLY if necessary (e.g., searching only titles), otherwise omit this filter."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Optional. Maximum number of results to return (default: 5)."
                        ),
                        "default": 5,
                    },
                },
                "required": ["query_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_document_content",
            "description": (
                "Retrieves the full text content of a specific document using its unique document ID (obtained from a previous search). Use this when you need the complete text after identifying a relevant document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": (
                            "The unique identifier of the document whose full content is needed."
                        ),
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ingest_document_from_url",
            "description": (
                "Submits a document from a given URL for ingestion and indexing by the system. Use this tool if the user asks you to 'save' a web page. The document will be fetched from the URL, its content extracted, processed, and stored to be made searchable. Provide a unique source_id for tracking this ingestion request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_to_ingest": {
                        "type": "string",
                        "format": "uri",
                        "description": (
                            "The fully qualified URL of the document to ingest."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional. The primary title to assign to this document. If omitted, the title will be extracted automatically during the indexing process from the web page content."
                        ),
                    },
                    "source_type": {
                        "type": "string",
                        "description": (
                            "A category or type for this document source, e.g., 'llm_url_ingestion', 'user_link_submission'."
                        ),
                    },
                    "source_id": {
                        "type": "string",
                        "description": (
                            "A unique identifier for this specific document within its source_type. This should be unique for each ingestion request to avoid conflicts. A UUID is a good choice if one is not readily available."
                        ),
                    },
                    "metadata_json": {
                        "type": "string",
                        "description": (
                            'Optional. A JSON string representing a dictionary of additional key-value metadata to associate with the document (e.g., \'{"category": "research", "tags": ["ai", "llm"]}\').'
                        ),
                    },
                },
                "required": ["url_to_ingest", "source_type", "source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_documentation_content",
            "description": (
                "Retrieves the content of a specific user documentation file. Use this to answer questions about how the assistant works or what features it has, based on the official documentation.\nAvailable files: {available_doc_files}"
            ),  # Placeholder added
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": (
                            "The exact filename of the documentation file to retrieve (e.g., 'USER_GUIDE.md'). Must end in .md or .txt."
                        ),
                    },
                },
                "required": ["filename"],
            },
        },
    },
]


# Helper function
def _scan_user_docs() -> list[str]:
    """Scans the 'docs/user/' directory for allowed documentation files."""
    docs_user_dir = pathlib.Path("docs") / "user"
    allowed_extensions = {".md", ".txt"}
    available_files = []
    if docs_user_dir.is_dir():
        try:
            for item in os.listdir(docs_user_dir):
                item_path = docs_user_dir / item
                if item_path.is_file() and any(
                    item.endswith(ext) for ext in allowed_extensions
                ):
                    available_files.append(item)
        except OSError as e:
            logger.error(
                f"Error scanning documentation directory '{docs_user_dir}': {e}",
                exc_info=True,
            )
    else:
        logger.warning(f"User documentation directory not found: '{docs_user_dir}'")
    logger.info(f"Found user documentation files: {available_files}")
    return available_files


# Tool Implementations
async def search_documents_tool(
    exec_context: ToolExecutionContext,
    embedding_generator: EmbeddingGenerator,  # Injected by LocalToolsProvider
    query_text: str,
    source_types: list[str] | None = None,
    embedding_types: list[str] | None = None,
    limit: int = 5,  # Default limit for LLM tool
) -> str:
    """
    Searches stored documents using hybrid vector and keyword search.

    Args:
        exec_context: The execution context containing the database context.
        embedding_generator: The embedding generator instance.
        query_text: The natural language query to search for.
        source_types: Optional list of source types to filter by (e.g., ['email', 'note']).
        embedding_types: Optional list of embedding types to filter by (e.g., ['content_chunk', 'summary']).
        limit: Maximum number of results to return.

    Returns:
        A formatted string containing the search results or an error message.
    """
    from family_assistant.storage.vector_search import (
        VectorSearchQuery,
        query_vector_store,
    )

    logger.info(f"Executing search_documents_tool with query: '{query_text}'")
    db_context = exec_context.db_context
    # Use the provided generator's model name
    embedding_model = embedding_generator.model_name

    try:
        # 1. Generate query embedding
        if not query_text:
            return "Error: Query text cannot be empty."
        embedding_result = await embedding_generator.generate_embeddings([query_text])
        if not embedding_result.embeddings or len(embedding_result.embeddings) == 0:
            return "Error: Failed to generate embedding for the query."
        query_embedding = embedding_result.embeddings[0]

        # 2. Construct the search query object
        search_query = VectorSearchQuery(
            search_type="hybrid",
            semantic_query=query_text,
            keywords=query_text,  # Use same text for keywords in this simplified tool
            embedding_model=embedding_model,
            source_types=source_types or [],  # Use empty list if None
            embedding_types=embedding_types or [],  # Use empty list if None
            limit=limit,
            # Use default rrf_k, metadata_filters, etc.
        )

        # 3. Execute the search
        results = await query_vector_store(
            db_context=db_context,
            query=search_query,
            query_embedding=query_embedding,
        )

        # 4. Format results for LLM
        if not results:
            return "No relevant documents found matching the query and filters."

        formatted_results = ["Found relevant documents:"]
        for i, res in enumerate(results):
            title = res.get("title") or "Untitled Document"
            source = res.get("source_type", "Unknown Source")
            # Truncate snippet for brevity
            snippet = res.get("embedding_source_content", "")
            if snippet:
                snippet = (snippet[:10000] + "...") if len(snippet) > 10000 else snippet
                snippet_text = f"\n  Snippet: {snippet}"
            else:
                snippet_text = ""

            formatted_results.append(
                f"{i + 1}. Title: {title} (Source: {source}){snippet_text}"
            )

        return "\n".join(formatted_results)

    except Exception as e:
        logger.error(f"Error executing search_documents_tool: {e}", exc_info=True)
        return f"Error: Failed to execute document search. {e}"


async def get_full_document_content_tool(
    exec_context: ToolExecutionContext,
    document_id: int,
) -> str:
    """
    Retrieves the full text content associated with a specific document ID.
    This is typically used after finding a relevant document via search_documents.

    Args:
        exec_context: The execution context containing the database context.
        document_id: The unique ID of the document (obtained from search results).

    Returns:
        A string containing the full concatenated text content of the document,
        or an error message if not found or content is unavailable.
    """
    logger.info(
        f"Executing get_full_document_content_tool for document ID: {document_id}"
    )
    db_context = exec_context.db_context

    try:
        # Query for content embeddings associated with the document ID, ordered by chunk index
        # Prioritize 'content_chunk' type, but could potentially fetch others if needed.
        # Using raw SQL for potential performance and direct access to embedding content.
        # Ensure table/column names match your schema.
        stmt = text(
            """
            SELECT content
            FROM document_embeddings
            WHERE document_id = :doc_id
              AND embedding_type = 'content_chunk' -- Assuming this type holds the main content
              AND content IS NOT NULL
            ORDER BY chunk_index ASC;
        """
        )
        results = await db_context.fetch_all(stmt, {"doc_id": document_id})

        if not results:
            # Check if the document exists at all, maybe it has no content embeddings?
            doc_check_stmt = text("SELECT id FROM documents WHERE id = :doc_id")
            doc_exists = await db_context.fetch_one(
                doc_check_stmt, {"doc_id": document_id}
            )
            if doc_exists:
                logger.warning(
                    f"Document ID {document_id} exists, but no 'content_chunk' embeddings with text content found."
                )
                # TODO: Future enhancement: Check document source_type and potentially fetch content
                # from original source (e.g., received_emails table) if no embedding content exists.
                return f"Error: Document {document_id} found, but no text content is available for retrieval via this tool."
            else:
                logger.warning(f"Document ID {document_id} not found.")
                return f"Error: Document with ID {document_id} not found."

        # Concatenate content from all chunks
        full_content = "".join([row["content"] for row in results])

        if not full_content.strip():
            logger.warning(
                f"Document ID {document_id} content chunks were empty or whitespace."
            )
            return f"Error: Document {document_id} found, but its text content appears to be empty."

        logger.info(
            f"Retrieved full content for document ID {document_id} (Length: {len(full_content)})."
        )
        # Return only the content for now. Future versions could return a dict with content_type.
        return full_content

    except Exception as e:
        logger.error(
            f"Error executing get_full_document_content_tool for ID {document_id}: {e}",
            exc_info=True,
        )
        return f"Error: Failed to retrieve content for document ID {document_id}. {e}"


async def ingest_document_from_url_tool(
    exec_context: ToolExecutionContext,
    url_to_ingest: str,
    source_type: str,
    source_id: str,
    title: str | None = None,  # Title is now optional
    metadata_json: str | None = None,
) -> str:
    """
    Submits a document from a given URL for ingestion and indexing.
    The document will be fetched from the URL by the server, processed, and made searchable.
    If a title is not provided, it will be attempted to be extracted during indexing.

    Args:
        exec_context: The execution context.
        url_to_ingest: The URL of the document to ingest.
        source_type: Type of the source (e.g., 'llm_url_ingestion', 'user_submitted_link').
        source_id: A unique identifier for this document within its source type.
        title: Optional. The primary title for the document. If None, a placeholder will be used and the actual title will be extracted during indexing.
        metadata_json: Optional JSON string representing a dictionary of additional metadata.

    Returns:
        A string message indicating success or failure.
    """
    from family_assistant.indexing.ingestion import process_document_ingestion_request

    logger.info(
        f"Executing ingest_document_from_url_tool for URL: '{url_to_ingest}', Provided Title: '{title}'"
    )
    db_context = exec_context.db_context

    title_to_use = title
    if title_to_use is None:
        # Use a placeholder if no title is provided by the LLM.
        # The actual title will be determined by DocumentTitleUpdaterProcessor.
        title_to_use = f"URL Ingest: {url_to_ingest}"
        logger.info(f"No title provided, using placeholder: '{title_to_use}'")

    doc_metadata: dict[str, Any] | None = None
    if metadata_json:
        try:
            doc_metadata = json.loads(metadata_json)
            if not isinstance(doc_metadata, dict):
                logger.warning("Invalid JSON in metadata_json, proceeding without it.")
                doc_metadata = None
        except json.JSONDecodeError:
            logger.warning("Failed to parse metadata_json, proceeding without it.")
            doc_metadata = None

    # Get document_storage_path from config
    document_storage_path_str = None
    if exec_context.processing_service and exec_context.processing_service.app_config:
        document_storage_path_str = exec_context.processing_service.app_config.get(
            "document_storage_path"
        )

    if not document_storage_path_str:
        document_storage_path_str = os.getenv("DOCUMENT_STORAGE_PATH")

    if not document_storage_path_str:
        logger.error(
            "DOCUMENT_STORAGE_PATH not found in app_config or environment for ingest_document_from_url_tool."
        )
        return "Error: Server configuration missing (document storage path)."

    document_storage_path = pathlib.Path(document_storage_path_str)

    try:
        ingestion_result = await process_document_ingestion_request(
            db_context=db_context,
            document_storage_path=document_storage_path,
            source_type=source_type,
            source_id=source_id,
            source_uri=url_to_ingest,  # For URL ingestion, source_uri is the URL itself
            title=title_to_use,  # Use the resolved title (provided or placeholder)
            url_to_scrape=url_to_ingest,
            doc_metadata=doc_metadata,
            # No file content or content_parts for this tool, only URL
        )

        if ingestion_result.get("error_detail"):
            logger.error(
                f"Ingestion service failed for URL '{url_to_ingest}': {ingestion_result['message']} - {ingestion_result['error_detail']}"
            )
            return f"Error submitting URL for ingestion: {ingestion_result['message']}. Details: {ingestion_result['error_detail']}"

        doc_id = ingestion_result.get("document_id")
        task_enqueued = ingestion_result.get("task_enqueued")
        service_message = ingestion_result.get("message", "Submission processed.")

        logger.info(
            f"Successfully submitted URL '{url_to_ingest}' via service. Response: {service_message}, Doc ID: {doc_id}, Task Enqueued: {task_enqueued}"
        )
        return f"URL submitted. Service response: {service_message}. Document ID: {doc_id}. Task Enqueued: {task_enqueued}."

    except Exception as e:
        logger.error(
            f"Unexpected error calling ingestion service for URL '{url_to_ingest}': {e}",
            exc_info=True,
        )
        return f"Error: An unexpected error occurred while submitting the URL. {e}"


async def get_user_documentation_content_tool(
    exec_context: ToolExecutionContext,
    filename: str,
) -> str:
    """
    Retrieves the content of a specified file from the user documentation directory ('docs/user/').

    Args:
        exec_context: The execution context (not directly used here, but available).
        filename: The name of the file within the 'docs/user/' directory (e.g., 'USER_GUIDE.md').

    Returns:
        The content of the file as a string, or an error message if the file is
        not found, not allowed, or cannot be read.
    """
    logger.info(
        f"Executing get_user_documentation_content_tool for filename: '{filename}'"
    )

    # Basic security: Prevent directory traversal and limit to allowed extensions
    allowed_extensions = {".md", ".txt"}
    if ".." in filename or not any(
        filename.endswith(ext) for ext in allowed_extensions
    ):
        logger.warning(f"Attempted access to disallowed filename: '{filename}'")
        return f"Error: Access denied. Invalid filename or extension '{filename}'."

    # Construct the full path relative to the project root (assuming standard structure)
    # Assumes the script runs from the project root or similar context.
    docs_user_dir = pathlib.Path("docs") / "user"
    file_path = (docs_user_dir / filename).resolve()

    # Security Check: Ensure the resolved path is still within the intended directory
    if docs_user_dir.resolve() not in file_path.parents:
        logger.error(
            f"Resolved path '{file_path}' is outside the allowed directory '{docs_user_dir.resolve()}'."
        )
        return f"Error: Access denied. Invalid path for filename '{filename}'."

    try:
        async with aiofiles.open(file_path, encoding="utf-8") as f:
            content = await f.read()
        logger.info(f"Successfully read content from '{filename}'.")
        return content
    except FileNotFoundError:
        logger.warning(f"User documentation file not found: '{file_path}'")
        return f"Error: Documentation file '{filename}' not found."
    except Exception as e:
        logger.error(
            f"Error reading user documentation file '{filename}': {e}", exc_info=True
        )
        return f"Error: Failed to read documentation file '{filename}'. {e}"
