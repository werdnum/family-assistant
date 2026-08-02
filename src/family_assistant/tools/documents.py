"""Document search and management tools.

This module contains tools for searching, retrieving, and managing
documents including ingestion from URLs and accessing user documentation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import uuid
from typing import TYPE_CHECKING, Any, TypedDict, cast

import aiofiles
import filetype  # type: ignore[import-untyped]
from sqlalchemy import select, text, update

from family_assistant.indexing.ingestion import process_document_ingestion_request
from family_assistant.security.taint import TaintSourceType
from family_assistant.storage.email import (
    parse_attachment_infos,
    received_emails_table,
)
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.vector_search import (
    VectorSearchQuery,
    query_vector_store,
)
from family_assistant.tools.taint_helpers import (
    merge_artifact_taint_into_context,
    record_sensitive_read,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolDefinition,
    ToolResult,
    get_attachment_limits,
)

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import ToolExecutionContext


class EmailAttachmentSummary(TypedDict):
    """Summary of a registered email attachment surfaced to tool callers."""

    attachment_id: str | None
    filename: str
    mime_type: str
    size: int | None


logger = logging.getLogger(__name__)

_SEARCH_DOCUMENTS_EXCLUDED_SOURCE_TYPES = ["message_history"]


def _coerce_doc_metadata(value: object) -> dict[str, object]:
    """Return document metadata from dialect-specific JSON row values."""
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return cast("dict[str, object]", loaded) if isinstance(loaded, dict) else {}
    return {}


# Tool Definitions
DOCUMENT_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "Search previously stored documents (emails, notes, files) using semantic and keyword matching. Returns titles and snippets of the most relevant documents.\n\n"
                "Returns: A formatted string containing search results. "
                "On success, returns 'Found relevant documents:' followed by numbered results with Title, Source, Document ID, optional original file availability (📎), Metadata, and Snippet. "
                "When original files (PDFs, images) are available, they are indicated with 📎 symbol and file info. Use get_full_document_content to retrieve the actual file. "
                "If no results found, returns 'No relevant documents found matching the query and filters.'. "
                "On error, returns 'Error: Failed to execute document search. [error details]' or 'Error: Query text cannot be empty.' or 'Error: Failed to generate embedding for the query.'. "
                "Returned document provenance is tracked internally; using external-source results may make later writes, browsing, worker execution, or outgoing messages require approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
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
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_full_document_content",
            "description": (
                "Retrieves the full content of a document in the best available format using its unique document ID (obtained from a previous search). Returns original file (PDF, image, etc.) when available, otherwise text content.\n\n"
                "Returns: ToolResult with file attachment when original file is available, otherwise a string containing the text content. "
                "When original file exists, returns both the file as an attachment and extracted text for context. "
                "For PDFs and images, you can analyze the file directly using your multimodal capabilities. "
                "If only text content is available, returns the full text content (raw content if available, or reconstructed from chunks). "
                "For email documents, the result also includes an `attachments` list with `attachment_id` values for each email attachment; "
                "pass those IDs to `read_text_attachment` or `get_attachment_info` to inspect attachment contents. "
                "The document's stored source provenance is tracked internally; using external-source content may make later writes, browsing, worker execution, or outgoing messages require approval. "
                "For legacy emails ingested before registry integration, the first call may return `attachment_id: null` for some entries — the returned text will describe the action needed (e.g. triggering a reindex) without prescribing a specific tool, since the write tool is only enabled in some profiles. "
                "If document exists but no content available, returns 'Error: Document [id] found, but no content is available.'. "
                "If document not found, returns 'Error: Document with ID [id] not found.'. "
                "On error, returns 'Error: Failed to retrieve content for document ID [id]. [error details]'. "
                "Files larger than 20MB will fallback to text-only for performance."
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
                "Submits a document from a given URL for ingestion and indexing by the system. Use this tool if the user asks you to 'save' a web page. The document will be fetched from the URL, its content extracted, processed, and stored to be made searchable. Provide a unique source_id for tracking this ingestion request.\n\n"
                "Returns: A string indicating the ingestion status. "
                "On success, returns 'URL submitted. Service response: [message]. Document ID: [id]. Task Enqueued: [status].'. "
                "If ingestion fails, returns 'Error submitting URL for ingestion: [message]. Details: [error details]'. "
                "If configuration missing, returns 'Error: Server configuration missing (document storage path).'. "
                "On unexpected error, returns 'Error: An unexpected error occurred while submitting the URL. [error details]'."
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
                "Retrieves the content of a specific user documentation file. Use this to answer questions about how the assistant works or what features it has, based on the official documentation. "
                "The documentation is split by topic: 'USER_GUIDE.md' is a short index describing what each other file covers, so read it first if the right file is not obvious from the names, then read the file (or files) that cover the question — a question spanning two topics needs both. Do not read files unrelated to the question.\nAvailable files: {available_doc_files}\n\n"
                "Returns: A string containing the file content or an error message. "
                "On success, returns the full content of the documentation file. "
                "If access denied, returns 'Error: Access denied. Invalid filename or extension [filename].' or 'Error: Access denied. Invalid path for filename [filename].'. "
                "If file not found, returns 'Error: Documentation file [filename] not found.'. "
                "On read error, returns 'Error: Failed to read documentation file [filename]. [error details]'."
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
    {
        "type": "function",
        "function": {
            "name": "reindex_email",
            "description": (
                "Enqueue an indexing task for an email that was ingested before "
                "attachment-registry integration. Call this when "
                "get_full_document_content surfaces email attachments with "
                "attachment_id=null; once the indexer runs, a subsequent "
                "get_full_document_content call returns the registered "
                "attachment_ids usable with read_text_attachment and "
                "get_attachment_info.\n\n"
                "Returns: ToolResult with the enqueued task_id, or a no-op "
                "status if a reindex for this email is already in flight."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "integer",
                        "description": (
                            "The document ID of the email (obtained from "
                            "search_documents or get_full_document_content)."
                        ),
                    },
                },
                "required": ["document_id"],
            },
        },
    },
]


# Helper function
def _scan_user_docs(docs_user_dir: pathlib.Path | None = None) -> list[str]:
    """Scans the 'docs/user/' directory for allowed documentation files."""
    if docs_user_dir is None:
        # Default path for backward compatibility
        docs_user_dir = pathlib.Path("docs") / "user"
        # Try Docker default if the calculated path doesn't exist
        if not docs_user_dir.exists() and pathlib.Path("/app/docs/user").exists():
            docs_user_dir = pathlib.Path("/app/docs/user")

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
            logger.exception(
                f"Error scanning documentation directory '{docs_user_dir}': {e}"
            )
    else:
        logger.warning(f"User documentation directory not found: '{docs_user_dir}'")
    logger.info(f"Found user documentation files: {available_files}")
    return available_files


# Tool Implementations
async def search_documents_tool(
    exec_context: ToolExecutionContext,
    embedding_generator: EmbeddingGenerator,  # Injected by LocalToolsProvider
    query: str,
    source_types: list[str] | None = None,
    embedding_types: list[str] | None = None,
    limit: int = 5,  # Default limit for LLM tool
) -> str:
    """
    Searches stored documents using hybrid vector and keyword search.

    Args:
        exec_context: The execution context containing the database context.
        embedding_generator: The embedding generator instance.
        query: The natural language query to search for.
        source_types: Optional list of source types to filter by (e.g., ['email', 'note']).
        embedding_types: Optional list of embedding types to filter by (e.g., ['content_chunk', 'summary']).
        limit: Maximum number of results to return.

    Returns:
        A formatted string containing the search results or an error message.
    """
    logger.info(f"Executing search_documents_tool with query: '{query}'")
    db_context = exec_context.db_context
    # Use the provided generator's model name
    embedding_model = embedding_generator.model_name

    try:
        # 1. Generate query embedding
        if not query:
            return "Error: Query text cannot be empty."
        embedding_result = await embedding_generator.generate_embeddings([query])
        if not embedding_result.embeddings or len(embedding_result.embeddings) == 0:
            return "Error: Failed to generate embedding for the query."
        query_embedding = embedding_result.embeddings[0]

        # 2. Construct the search query object
        search_query = VectorSearchQuery(
            search_type="hybrid",
            semantic_query=query,
            keywords=query,  # Use same text for keywords in this simplified tool
            embedding_model=embedding_model,
            source_types=source_types or [],  # Use empty list if None
            excluded_source_types=_SEARCH_DOCUMENTS_EXCLUDED_SOURCE_TYPES,
            embedding_types=embedding_types or [],  # Use empty list if None
            limit=limit,
            visibility_grants=exec_context.visibility_grants,
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

        surfaced_ids = [
            str(res["document_id"]) for res in results if res.get("document_id")
        ]
        record_sensitive_read(
            exec_context,
            kind="documents",
            qualifier=f"search:{query[:200]}",
            surfaced_ids=surfaced_ids,
        )
        for res in results:
            metadata = _coerce_doc_metadata(res.get("doc_metadata"))
            if metadata:
                doc_id = res.get("document_id")
                merge_artifact_taint_into_context(
                    exec_context,
                    provenance_metadata=metadata,
                    fallback_source_type=TaintSourceType.DOCUMENT,
                    fallback_source_id=str(doc_id) if doc_id is not None else None,
                    fallback_reason="Indexed document search result provenance.",
                )

        formatted_results = ["Found relevant documents:"]
        for i, res in enumerate(results):
            title = res.get("title") or "Untitled Document"
            source = res.get("source_type", "Unknown Source")
            doc_id = res.get("document_id", "Unknown")
            metadata = res.get("doc_metadata", {})
            file_path = res.get("file_path")

            # Check if original file is available
            has_file = file_path and await asyncio.to_thread(
                pathlib.Path(file_path).exists
            )

            # Truncate snippet for brevity
            snippet = res.get("embedding_source_content", "")
            if snippet:
                snippet = (snippet[:10000] + "...") if len(snippet) > 10000 else snippet
                snippet_text = f"\n  Snippet: {snippet}"
            else:
                snippet_text = ""

            # Format metadata for display
            metadata_text = ""
            if metadata:
                metadata_text = f"\n  Metadata: {metadata}"

            # Indicate file availability
            file_info = ""
            if has_file and file_path:
                try:
                    file_path_obj = pathlib.Path(file_path)
                    file_size = (await asyncio.to_thread(file_path_obj.stat)).st_size
                    original_filename = (
                        metadata.get("original_filename") or file_path_obj.name
                    )
                    file_info = f"\n  📎 Original file available: {original_filename} ({file_size / 1024:.1f}KB)"
                except Exception:
                    file_info = "\n  📎 Original file available"

            formatted_results.append(
                f"{i + 1}. Title: {title} (Source: {source}, Document ID: {doc_id} - for retrieving full content){file_info}{metadata_text}{snippet_text}"
            )

        return "\n".join(formatted_results)

    except Exception as e:
        logger.exception(f"Error executing search_documents_tool: {e}")
        return f"Error: Failed to execute document search. {e}"


async def get_full_document_content_tool(
    exec_context: ToolExecutionContext,
    document_id: int,
) -> str | ToolResult:
    """
    Retrieves the full content of a document in the best available format.
    Returns original file (PDF, image, etc.) when available, otherwise text content.
    This is typically used after finding a relevant document via search_documents.

    Args:
        exec_context: The execution context containing the database context.
        document_id: The unique ID of the document (obtained from search results).

    Returns:
        ToolResult with file attachment when original file is available,
        otherwise a string containing the text content or an error message.
    """
    logger.info(
        f"Executing get_full_document_content_tool for document ID: {document_id}"
    )
    db_context = exec_context.db_context

    try:
        # First, get document metadata including file_path and original filename
        doc_query = text(
            """
            SELECT file_path, doc_metadata, title, source_type, source_id, visibility_labels
            FROM documents
            WHERE id = :doc_id
        """
        )
        doc_result = await db_context.fetch_one(doc_query, {"doc_id": document_id})

        if not doc_result:
            logger.warning(f"Document ID {document_id} not found.")
            return f"Error: Document with ID {document_id} not found."

        if exec_context.visibility_grants is not None:
            labels = json.loads(doc_result["visibility_labels"] or "[]")
            if not set(labels) <= exec_context.visibility_grants:
                return f"Error: Document with ID {document_id} not found."

        file_path = doc_result["file_path"]
        doc_metadata = _coerce_doc_metadata(doc_result.get("doc_metadata"))
        title = doc_result.get("title")
        source_type = doc_result.get("source_type")
        source_id = doc_result.get("source_id")
        if isinstance(doc_metadata, dict):
            record_sensitive_read(
                exec_context,
                kind="documents",
                qualifier=f"full:{document_id}",
                surfaced_ids=[str(document_id)],
            )
            merge_artifact_taint_into_context(
                exec_context,
                provenance_metadata=doc_metadata,
                fallback_source_type=TaintSourceType.DOCUMENT,
                fallback_source_id=str(document_id),
                fallback_reason="Full indexed document read provenance.",
            )

        email_attachments_summary: list[EmailAttachmentSummary] | None = None
        if source_type == "email" and source_id:
            email_attachments_summary = await resolve_email_attachments(
                db_context=db_context,
                message_id_header=source_id,
            )

        # Try to return original file if available
        if file_path and await asyncio.to_thread(pathlib.Path(file_path).exists):
            try:
                file_path_obj = pathlib.Path(file_path)
                file_size = (await asyncio.to_thread(file_path_obj.stat)).st_size

                # Check multimodal size limit from config
                _, max_multimodal_size = get_attachment_limits(exec_context)
                if file_size > max_multimodal_size:
                    max_mb = max_multimodal_size / (1024 * 1024)
                    logger.warning(
                        f"File for document ID {document_id} is {file_size / (1024 * 1024):.1f}MB, "
                        f"exceeding {max_mb:.0f}MB limit for multimodal attachments. Returning text content only."
                    )
                else:
                    # Read file content and detect MIME type
                    async with aiofiles.open(file_path, "rb") as f:
                        file_content = await f.read()

                    kind = filetype.guess(file_path)
                    mime_type = kind.mime if kind else "application/octet-stream"

                    # Get original filename from metadata or use file basename
                    original_filename = (
                        doc_metadata.get("original_filename") or file_path_obj.name
                    )

                    # Get text content for context
                    text_content = await _get_text_content_fallback(
                        db_context, document_id
                    )

                    logger.info(
                        f"Returning original file for document ID {document_id}: "
                        f"{original_filename} ({file_size / 1024:.1f}KB, {mime_type})"
                    )

                    # Create attachment
                    attachment = ToolAttachment(
                        content=file_content,
                        mime_type=mime_type,
                        description=f"{title or original_filename} ({source_type})",
                    )

                    display_text = (
                        text_content or f"Original document: {original_filename}"
                    )
                    if email_attachments_summary:
                        summary_text = format_email_attachments_text(
                            email_attachments_summary
                        )
                        if summary_text:
                            display_text = (
                                f"{display_text}\n\nAttachments:\n{summary_text}"
                            )
                        return ToolResult(
                            text=display_text,
                            attachments=[attachment],
                            data={
                                "content": text_content or "",
                                "attachments": email_attachments_summary,
                            },
                        )

                    return ToolResult(
                        text=display_text,
                        attachments=[attachment],
                    )
            except Exception as file_err:
                logger.exception(
                    f"Error reading file {file_path} for document ID {document_id}: {file_err}"
                )
                # Fall through to text content

        # Fall back to text content
        text_content = await _get_text_content_fallback(db_context, document_id)
        if not text_content and not email_attachments_summary:
            return f"Error: Document {document_id} found, but no content is available."

        if email_attachments_summary:
            body_text = text_content or ""
            summary_text = format_email_attachments_text(email_attachments_summary)
            display_text = (
                f"{body_text}\n\nAttachments:\n{summary_text}"
                if summary_text
                else body_text
            )
            return ToolResult(
                text=display_text,
                data={
                    "content": body_text,
                    "attachments": email_attachments_summary,
                },
            )

        assert text_content is not None
        return text_content

    except Exception as e:
        logger.exception(
            f"Error executing get_full_document_content_tool for ID {document_id}: {e}"
        )
        return f"Error: Failed to retrieve content for document ID {document_id}. {e}"


async def reindex_email_tool(
    exec_context: ToolExecutionContext,
    document_id: int,
) -> ToolResult:
    """Enqueue an indexing task for an email so legacy attachments get
    registered with the AttachmentRegistry.

    This is the write-path companion to ``get_full_document_content``: when
    that tool surfaces email attachments with ``attachment_id=null``, the
    LLM should call ``reindex_email`` to queue a reindex. Registration
    itself happens in ``EmailIndexer.handle_index_email``, which uses an
    atomic INSERT against
    ``uix_attachment_metadata_email_identity`` (plus an ``IntegrityError``
    fallback re-query) so concurrent/repeat runs cannot create duplicate
    registry rows.

    The "already_in_flight" check is best-effort: concurrent calls may
    still both enqueue an ``index_email`` task because there is no
    uniqueness constraint on pending tasks per email. Duplicate tasks are
    safe — the indexer itself is idempotent — but they do redundant work.
    """
    db_context = exec_context.db_context
    logger.info(f"Executing reindex_email_tool for document ID: {document_id}")

    # Fail fast if no attachment registry is configured: the indexer
    # would log-and-skip registration in that case, so the queued task
    # can never populate any missing attachment_id values and the caller
    # would be stuck in a permanent reindex/retry loop.
    if exec_context.attachment_registry is None:
        return ToolResult(
            data={
                "error": (
                    "Attachment registry is not configured; reindex would "
                    "not register any attachments. Configure "
                    "AttachmentRegistry on the application before retrying."
                )
            }
        )

    doc_row = await db_context.fetch_one(
        text(
            "SELECT source_type, source_id, visibility_labels "
            "FROM documents WHERE id = :doc_id"
        ),
        {"doc_id": document_id},
    )
    # Apply the same visibility gate as get_full_document_content so hidden
    # document IDs can't be distinguished from missing ones via error text,
    # and so we don't enqueue indexing work for docs the caller can't see.
    not_found_error = ToolResult(data={"error": f"Document {document_id} not found"})
    if not doc_row:
        return not_found_error
    if exec_context.visibility_grants is not None:
        labels = json.loads(doc_row["visibility_labels"] or "[]")
        if not set(labels) <= exec_context.visibility_grants:
            return not_found_error
    if doc_row["source_type"] != "email":
        return ToolResult(
            data={
                "error": (
                    f"Document {document_id} is not an email "
                    f"(source_type={doc_row['source_type']})"
                )
            }
        )

    message_id_header = doc_row["source_id"]
    email_row = await db_context.fetch_one(
        select(received_emails_table.c.id).where(
            received_emails_table.c.message_id_header == message_id_header
        )
    )
    if not email_row:
        return ToolResult(
            data={"error": f"Email row for document {document_id} not found"}
        )
    email_db_id = email_row["id"]

    task_prefix = f"index_email_{email_db_id}_"
    # ``startswith`` compiles to a SQL ``LIKE`` predicate, and the
    # underscores in ``index_email_{email_db_id}_`` are LIKE wildcards
    # — without ``autoescape`` the prefix for email ``1`` would also
    # match ``index_email_12_...``, ``index_email_100_...``, etc. and
    # cause ``reindex_email`` to report ``already_in_flight`` for the
    # wrong email (and overwrite ``received_emails.indexing_task_id``
    # with another email's task).
    existing_task = await db_context.fetch_one(
        select(tasks_table.c.task_id)
        .where(tasks_table.c.task_type == "index_email")
        .where(tasks_table.c.task_id.startswith(task_prefix, autoescape=True))
        .where(tasks_table.c.status.in_(("pending", "processing")))
        .limit(1)
    )
    if existing_task:
        # The backfill migration enqueues ``index_email_*`` tasks directly
        # without updating ``received_emails.indexing_task_id``, so the
        # email row may still point at a stale/NULL task id even though a
        # fresh indexing job is already pending. Repair the link here so
        # callers always see the task that's actually in flight.
        await db_context.execute(
            update(received_emails_table)
            .where(received_emails_table.c.id == email_db_id)
            .values(indexing_task_id=existing_task["task_id"])
        )
        return ToolResult(
            data={
                "status": "already_in_flight",
                "task_id": existing_task["task_id"],
                "email_db_id": email_db_id,
            }
        )

    task_id = f"{task_prefix}{uuid.uuid4()}"
    try:
        await db_context.tasks.enqueue(
            task_id=task_id,
            task_type="index_email",
            payload={"email_db_id": email_db_id},
        )
    except Exception as err:
        logger.warning(
            f"Failed to enqueue email reindex task for email {email_db_id}: {err}"
        )
        return ToolResult(data={"error": f"Failed to enqueue reindex: {err}"})

    # Keep ``received_emails.indexing_task_id`` in sync with the newly
    # enqueued job so any status/debugging code that reads it sees the
    # currently-active task rather than a stale or NULL reference.
    await db_context.execute(
        update(received_emails_table)
        .where(received_emails_table.c.id == email_db_id)
        .values(indexing_task_id=task_id)
    )

    return ToolResult(
        data={
            "status": "enqueued",
            "task_id": task_id,
            "email_db_id": email_db_id,
        }
    )


def format_email_attachments_text(
    attachments: list[EmailAttachmentSummary],
) -> str:
    """Format an email's attachment summary as a human-readable list.

    Surfaces the ``attachment_id`` when present so the caller can pipe
    it into ``read_text_attachment`` / ``get_attachment_info``. Entries
    without an id are rendered with tool-agnostic guidance (a reindex
    is needed) — we deliberately don't prescribe ``reindex_email`` by
    name because that write tool is only enabled in some profiles and
    flows running without it would otherwise be told to call something
    they can't invoke.

    The caller is expected to have a configured ``AttachmentRegistry``;
    without one the ids here can't be dereferenced, but that's a wiring
    bug at the caller's level, not a mode this formatter needs to model.
    """
    lines: list[str] = []
    for att in attachments:
        size = att["size"]
        size_label = f"{size} bytes" if size is not None else "unknown size"
        if att["attachment_id"]:
            lines.append(
                f"- {att['filename']} ({att['mime_type']}, {size_label}) "
                f"— attachment_id: {att['attachment_id']}"
            )
        else:
            lines.append(
                f"- {att['filename']} ({att['mime_type']}, {size_label}) "
                "— attachment_id not yet assigned; this email needs to be "
                "reindexed to register the attachment (use the "
                "`reindex_email` tool if it is available in this profile, "
                "otherwise ask the operator to reindex the email)."
            )
    return "\n".join(lines)


async def resolve_email_attachments(
    *,
    db_context: Database,
    message_id_header: str,
) -> list[EmailAttachmentSummary] | None:
    """Return a read-only summary of attachments for an email.

    Attachments are registered in the ``AttachmentRegistry`` at ingestion
    time (see ``EmailIndexer.handle_index_email``), so this helper never
    writes — keeping ``get_full_document_content`` truly ``READ_ONLY``.

    Legacy emails received before registry integration will have
    ``attachment_id`` set to ``None`` in the result. Registration must be
    triggered by the write-path ``reindex_email`` tool; after that task
    runs, a subsequent call to this helper returns the populated IDs.

    Returns None if the email row is not found or has no attachments.
    """
    row_query = select(received_emails_table.c.attachment_info).where(
        received_emails_table.c.message_id_header == message_id_header
    )
    row = await db_context.fetch_one(row_query)
    if not row:
        return None

    raw_info = row.get("attachment_info")
    if not raw_info:
        return None

    # Per-entry validation: legacy rows occasionally contain malformed
    # attachment descriptors (missing ``storage_path``, ``content_type``
    # null, etc.). Because this helper runs on the ``READ_ONLY``
    # ``get_full_document_content`` path, one bad record must not abort
    # the whole fetch — skip the bad entry and return what we can.
    attachments = parse_attachment_infos(
        raw_info, context=f"message_id={message_id_header}"
    )
    return [
        {
            "attachment_id": att.attachment_id,
            "filename": att.filename,
            "mime_type": att.content_type,
            "size": att.size,
        }
        for att in attachments
    ]


async def _get_text_content_fallback(
    db_context: Database, document_id: int
) -> str | None:
    """Helper function to get text content from embeddings."""
    # First try to get raw/full content types that contain complete text
    raw_types_query = text(
        """
        SELECT content, embedding_type
        FROM document_embeddings
        WHERE document_id = :doc_id
          AND embedding_type IN (
            'raw_note_text', 
            'raw_body_text', 
            'raw_file_text',
            'extracted_markdown_content',
            'fetched_content_markdown'
          )
          AND content IS NOT NULL
        LIMIT 1;
    """
    )
    raw_result = await db_context.fetch_one(raw_types_query, {"doc_id": document_id})

    if raw_result and raw_result["content"]:
        logger.info(
            f"Retrieved text content for document ID {document_id} from "
            f"'{raw_result['embedding_type']}' (Length: {len(raw_result['content'])})."
        )
        return raw_result["content"]

    # Fall back to chunk reconstruction if no raw content found
    chunk_query = text(
        """
        SELECT content
        FROM document_embeddings
        WHERE document_id = :doc_id
          AND embedding_type = 'content_chunk'
          AND content IS NOT NULL
        ORDER BY chunk_index ASC;
    """
    )
    chunk_results = await db_context.fetch_all(chunk_query, {"doc_id": document_id})

    if chunk_results:
        full_content = "".join([row["content"] for row in chunk_results])
        logger.info(
            f"Reconstructed text content for document ID {document_id} from "
            f"{len(chunk_results)} chunks (Length: {len(full_content)})."
        )
        return full_content

    return None


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

    # ast-grep-ignore: no-dict-any - user-provided JSON metadata with arbitrary keys
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
        app_config: AppConfig = exec_context.processing_service.app_config
        document_storage_path_str = app_config.document_storage_path

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
                f"Ingestion service failed for URL '{url_to_ingest}': {ingestion_result.get('message')} - {ingestion_result.get('error_detail')}"
            )
            return f"Error submitting URL for ingestion: {ingestion_result.get('message')}. Details: {ingestion_result.get('error_detail')}"

        doc_id = ingestion_result.get("document_id")
        task_enqueued = ingestion_result.get("task_enqueued")
        service_message = ingestion_result.get("message", "Submission processed.")

        logger.info(
            f"Successfully submitted URL '{url_to_ingest}' via service. Response: {service_message}, Doc ID: {doc_id}, Task Enqueued: {task_enqueued}"
        )
        return f"URL submitted. Service response: {service_message}. Document ID: {doc_id}. Task Enqueued: {task_enqueued}."

    except Exception as e:
        logger.exception(
            f"Unexpected error calling ingestion service for URL '{url_to_ingest}': {e}"
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
    docs_user_dir_env = os.getenv("DOCS_USER_DIR")
    if docs_user_dir_env:
        docs_user_dir = await asyncio.to_thread(pathlib.Path(docs_user_dir_env).resolve)
    else:
        docs_user_dir = pathlib.Path("docs") / "user"
        # Try Docker default if the calculated path doesn't exist
        docs_user_exists = await asyncio.to_thread(docs_user_dir.exists)
        docker_default_exists = await asyncio.to_thread(
            pathlib.Path("/app/docs/user").exists
        )
        if not docs_user_exists and docker_default_exists:
            docs_user_dir = pathlib.Path("/app/docs/user")

    file_path = await asyncio.to_thread((docs_user_dir / filename).resolve)

    # Security Check: Ensure the resolved path is still within the intended directory
    resolved_docs_dir = await asyncio.to_thread(docs_user_dir.resolve)
    if resolved_docs_dir not in file_path.parents:
        logger.error(
            f"Resolved path '{file_path}' is outside the allowed directory '{resolved_docs_dir}'."
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
        logger.exception(f"Error reading user documentation file '{filename}': {e}")
        return f"Error: Failed to read documentation file '{filename}'. {e}"
