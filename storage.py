import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import asyncio
import random
from sqlalchemy.exc import DBAPIError
from dateutil import rrule  # Added for recurrence calculation
from dateutil.parser import isoparse  # Added for parsing dates in recurrence

# Import base components
from db_base import metadata, get_engine, engine # Import engine directly too

# Import specific storage modules
from notes_storage import notes_table, add_or_update_note, get_all_notes, get_note_by_title, delete_note
from email_storage import received_emails_table, store_incoming_email
from message_history_storage import message_history_table, add_message_to_history, get_recent_history, get_message_by_id, get_grouped_message_history
from task_storage import tasks_table, enqueue_task, dequeue_task, update_task_status, reschedule_task_for_retry, get_all_tasks

logger = logging.getLogger(__name__)

# --- Vector Storage Imports ---
try:
    from vector_storage import (
        Base as VectorBase,
        init_vector_db,
        add_document,
        get_document_by_source_id,
        add_embedding,
        delete_document,
        query_vectors,
    )  # Explicit imports

    VECTOR_STORAGE_ENABLED = True
    logger.info("Vector storage module imported successfully.")
except ImportError:
    # Define placeholder types if vector_storage is not available
    class Base: pass # type: ignore
    VectorBase = Base # type: ignore # Define here even if module fails to load
    logger.warning("vector_storage.py not found. Vector storage features disabled.")
    VECTOR_STORAGE_ENABLED = False
    # Define placeholders for the functions if the import failed
    def init_vector_db(): pass # type: ignore # noqa: E305
    vector_storage = None # Placeholder, though functions above handle the no-op
    # No need to re-log warning here, already logged in except block

# --- Global Init Function ---
logger = logging.getLogger(__name__)


# Add vector storage models to the same metadata object if enabled
if VECTOR_STORAGE_ENABLED:

__all__ = [
    "init_db",
    "get_all_notes",
    "get_engine",  # Export the engine creation function/getter
    "get_engine",  # Export the engine creation function/getter
    "add_message_to_history",
    "get_recent_history",
    "get_message_by_id",
    "add_or_update_note",
    "delete_note",
    "enqueue_task",
    "dequeue_task",
    "update_task_status",
    "reschedule_task_for_retry",
    "get_all_tasks",  # Added
    "get_grouped_message_history",
    "notes_table",  # Also export tables if needed elsewhere (e.g., tests)
    "message_history_table", # Corrected table name
    "tasks_table", # Export task table
    "received_emails_table", # Export new email table
    "store_incoming_email", # Export email storage function
    "engine", # Export engine instance from db_base
    "metadata", # Export metadata instance from db_base
    # Vector Storage Exports (conditional)
]

if VECTOR_STORAGE_ENABLED and 'vector_storage' in locals() and vector_storage: # Check locals() and existence
    __all__.extend(
        [
            "add_document",
            "VectorBase", # Export the Base class for models
            "init_vector_db",
            "get_document_by_source_id",
            "add_embedding",
            "delete_document",
            "query_vectors",
        ]
    )
