import json
import logging
import os
import pathlib
import re
import uuid
from datetime import datetime
from typing import Any

import filetype

from family_assistant import storage
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


# Define a simple class on the fly that behaves like the Document protocol
# This avoids needing a direct import of a specific Document implementation
class IngestedDocument:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def source_type(self) -> str:
        return self._data["_source_type"]

    @property
    def source_id(self) -> str:
        return self._data["_source_id"]

    @property
    def source_uri(self) -> str | None:
        return self._data["_source_uri"]

    @property
    def title(self) -> str | None:
        return self._data["_title"]

    @property
    def created_at(self) -> datetime | None:
        return self._data["_created_at"]

    @property
    def metadata(self) -> dict[str, Any] | None:
        return self._data["_base_metadata"]


async def process_document_ingestion_request(
    db_context: DatabaseContext,
    document_storage_path: pathlib.Path,
    source_type: str,
    source_id: str,
    source_uri: str,
    title: str,
    content_parts: dict[str, str] | None = None,
    uploaded_file_content: bytes | None = None,
    uploaded_file_filename: str | None = None,
    uploaded_file_content_type: str | None = None,  # Client-provided content type
    url_to_scrape: str | None = None,
    created_at_dt: datetime | None = None,
    doc_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Processes a document ingestion request, saves files, stores metadata, and enqueues for indexing.

    Returns:
        A dictionary containing:
            "message": str,
            "document_id": int | None,
            "task_enqueued": bool,
            "error_detail": str | None
    """
    file_ref: str | None = None
    detected_mime_type: str | None = None
    original_filename_for_task: str | None = uploaded_file_filename

    if doc_metadata is None:
        doc_metadata = {}

    # Ensure original_url and original_filename are in doc_metadata if applicable,
    # without overwriting if they were explicitly provided in metadata_json.
    if url_to_scrape and "original_url" not in doc_metadata:
        doc_metadata["original_url"] = url_to_scrape
    if original_filename_for_task and "original_filename" not in doc_metadata:
        doc_metadata["original_filename"] = original_filename_for_task

    try:
        # Process uploaded file content if present
        if uploaded_file_content and uploaded_file_filename:
            original_filename_for_task = uploaded_file_filename
            safe_basename = re.sub(
                r"[^a-zA-Z0-9_.-]",
                "_",
                os.path.basename(uploaded_file_filename),
            )
            unique_filename = f"{uuid.uuid4()}_{safe_basename}"
            target_file_path = document_storage_path / unique_filename

            document_storage_path.mkdir(parents=True, exist_ok=True)

            with open(target_file_path, "wb") as f:
                f.write(uploaded_file_content)

            file_ref = str(target_file_path)
            logger.info(
                f"Uploaded file content for '{uploaded_file_filename}' saved to '{file_ref}' for document {source_id}."
            )

            try:
                kind = filetype.guess(file_ref)
                if kind is None:
                    logger.warning(
                        f"Could not determine file type for '{uploaded_file_filename}' (path: {file_ref}). "
                        f"Falling back to client-provided content type: {uploaded_file_content_type}."
                    )
                    detected_mime_type = uploaded_file_content_type
                else:
                    detected_mime_type = kind.mime
                    logger.info(
                        f"Detected MIME type for '{uploaded_file_filename}' (path: {file_ref}): {detected_mime_type}"
                    )
            except Exception as fe:
                logger.error(
                    f"Error detecting file type for '{uploaded_file_filename}' (path: {file_ref}): {fe}",
                    exc_info=True,
                )
                detected_mime_type = uploaded_file_content_type
                logger.warning(
                    f"Using client-provided content type '{detected_mime_type}' due to detection error for {uploaded_file_filename}."
                )
        elif url_to_scrape:
            logger.info(f"Document ingestion request for URL: {url_to_scrape}")
            # file_ref and detected_mime_type will remain None, original_filename_for_task also None
        elif content_parts:
            logger.info("Document ingestion request with content_parts.")
            # file_ref and detected_mime_type will remain None, original_filename_for_task also None
        else:
            # This case should ideally be caught by the caller, but as a safeguard:
            return {
                "message": "Ingestion request failed: No content provided.",
                "document_id": None,
                "task_enqueued": False,
                "error_detail": (
                    "No file content, URL, or content_parts provided for ingestion."
                ),
            }

        # Create Document Record in DB
        document_data_for_obj = {
            "_source_type": source_type,
            "_source_id": source_id,
            "_source_uri": source_uri,
            "_title": title,
            "_created_at": created_at_dt,
            "_base_metadata": doc_metadata,
        }
        doc_for_storage = IngestedDocument(document_data_for_obj)

        document_id: int = await storage.add_document(
            db_context=db_context,
            doc=doc_for_storage,
        )
        logger.info(f"Stored document record for {source_id}, got DB ID: {document_id}")

        # Enqueue Background Task for Embedding
        task_payload = {
            "document_id": document_id,
            "content_parts": content_parts,
            "file_ref": file_ref,
            "mime_type": detected_mime_type,
            "original_filename": original_filename_for_task,
            "url_to_scrape": url_to_scrape,
        }
        task_id = f"index-doc-{document_id}-{uuid.uuid4()}"
        task_enqueued = False
        try:
            await storage.enqueue_task(
                db_context=db_context,
                task_id=task_id,
                task_type="process_uploaded_document",
                payload=task_payload,
            )
            task_enqueued = True
            logger.info(
                f"Enqueued task '{task_id}' to process document ID {document_id}"
            )
            return {
                "message": "Document received and accepted for processing.",
                "document_id": document_id,
                "task_enqueued": task_enqueued,  # Use the variable
                "error_detail": None,
            }
        except Exception as task_err:
            logger.error(
                f"Failed to enqueue indexing task for document ID {document_id}: {task_err}",
                exc_info=True,
            )
            return {
                "message": (
                    "Document record stored, but failed to enqueue indexing task."
                ),
                "document_id": document_id,
                "task_enqueued": False,
                "error_detail": str(task_err),
            }

    except (
        ValueError,
        json.JSONDecodeError,
    ) as val_err:  # Should be caught by caller usually
        logger.error(
            f"Validation or JSON error during ingestion processing for {source_id}: {val_err}",
            exc_info=True,
        )
        return {
            "message": "Ingestion request failed due to validation or JSON error.",
            "document_id": None,
            "task_enqueued": False,
            "error_detail": str(val_err),
        }
    except (
        FileNotFoundError
    ) as fnf_err:  # If document_storage_path is invalid during file ops
        logger.error(
            f"File not found error during ingestion processing for {source_id}: {fnf_err}",
            exc_info=True,
        )
        return {
            "message": (
                "Ingestion request failed due to file system error (path not found)."
            ),
            "document_id": None,
            "task_enqueued": False,
            "error_detail": str(fnf_err),
        }
    except Exception as db_err:  # Covers storage.add_document errors primarily
        logger.error(
            f"Database or unexpected error storing document record for {source_id}: {db_err}",
            exc_info=True,
        )
        error_detail = str(db_err)
        if (
            "UNIQUE constraint failed" in error_detail
            or "duplicate key value violates unique constraint" in error_detail
        ):
            return {
                "message": (
                    f"Document with source_type '{source_type}' and source_id '{source_id}' already exists."
                ),
                "document_id": None,  # Or fetch existing ID if needed
                "task_enqueued": False,
                "error_detail": "Conflict: Document already exists.",
                "status_code": 409,  # Hint for API layer
            }
        return {
            "message": "Ingestion request failed due to database or unexpected error.",
            "document_id": None,
            "task_enqueued": False,
            "error_detail": error_detail,
            "status_code": 500,  # Hint for API layer
        }
