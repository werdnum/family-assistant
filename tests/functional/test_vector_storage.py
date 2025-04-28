"""
Functional tests for the vector storage module using PostgreSQL.
"""

import pytest
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import numpy as np  # Using numpy for easy random vector generation
from sqlalchemy import text  # Add this import

# Import the functions and classes we want to test
from family_assistant.storage.vector import (
    add_document,
    add_embedding,
    get_document_by_source_id,
    delete_document,
    Document,  # Import the protocol
    DocumentRecord,  # Import the ORM model for type hints if needed
)
# Import the new query function and schema
from family_assistant.storage.vector_search import (
    query_vector_store,
    VectorSearchQuery,
)
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)

# --- Test Configuration ---
# Define the embedding model and dimension used for testing.
# This MUST match one of the partial indexes created in init_vector_db.
TEST_EMBEDDING_MODEL = "gemini-exp-03-07"
TEST_EMBEDDING_DIMENSION = 1536


# --- Helper Class for Test Data (Renamed to avoid PytestCollectionWarning) ---
class MockDocumentImpl(Document):
    """Simple implementation of the Document protocol for test data."""

    def __init__(
        self,
        source_type: str,
        source_id: str,
        title: Optional[str] = None,
        created_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
    ):
        self._source_type = source_type
        self._source_id = source_id
        self._title = title
        # Ensure datetime is timezone-aware if provided
        self._created_at = (
            created_at.astimezone(timezone.utc)
            if created_at and created_at.tzinfo is None
            else created_at
        )
        self._metadata = metadata
        self._source_uri = source_uri

    @property
    def source_type(self) -> str:
        return self._source_type

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_uri(self) -> Optional[str]:
        return self._source_uri

    @property
    def title(self) -> Optional[str]:
        return self._title

    @property
    def created_at(self) -> Optional[datetime]:
        return self._created_at

    @property
    def metadata(self) -> Optional[Dict[str, Any]]:
        return self._metadata


@pytest.mark.asyncio
async def test_vector_storage_basic_flow(pg_vector_db_engine):
    """
    Basic test to verify core vector storage functionality using PostgreSQL:
    1. Add a document using the Document protocol.
    2. Add an embedding for that document.
    3. Query using the exact embedding vector.
    4. Verify the document is returned with near-zero distance.
    5. Retrieve the document by source ID and verify its details.
    6. Delete the document.
    7. Verify the document is gone.
    """
    # --- Arrange ---
    test_source_id = f"test_doc_{uuid.uuid4()}"
    test_source_type = "test_functional"
    test_title = f"Functional Test Document {uuid.uuid4()}"
    test_metadata = {"category": "functional_test", "run_id": str(uuid.uuid4())}
    test_created_at = datetime.now(timezone.utc)  # Use timezone-aware datetime

    # Create a sample embedding (ensure dimension matches the index)
    test_embedding_vector = (
        np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32).tolist()
    )
    test_embedding_type = "content_chunk"
    test_content = "This is the text content that was embedded."

    # Create a document instance using the protocol implementation
    test_doc = MockDocumentImpl(
        source_type=test_source_type,
        source_id=test_source_id,
        title=test_title,
        created_at=test_created_at,
        metadata=test_metadata,
    )

    logger.info(f"\n--- Running Vector Storage Basic Flow Test ---")
    logger.info(f"Using Source ID: {test_source_id}")
    logger.info(
        f"Using Embedding Model: {TEST_EMBEDDING_MODEL} (Dim: {TEST_EMBEDDING_DIMENSION})"
    )

    doc_id = None  # To store the document ID

    # --- Act & Assert: Add Document and Embedding ---
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info("Adding document...")
        doc_id = await add_document(db, doc=test_doc)
        assert isinstance(doc_id, int), "add_document should return an integer ID"
        logger.info(f"Document added with ID: {doc_id}")

        logger.info("Adding embedding...")
        await add_embedding(
            db,
            document_id=doc_id,
            chunk_index=0,  # Using 0 for simplicity
            embedding_type=test_embedding_type,
            embedding=test_embedding_vector,
            embedding_model=TEST_EMBEDDING_MODEL,
            content=test_content,
        )
        logger.info("Embedding added.")

    # --- Act & Assert: Query Vectors using new function ---
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info("Querying vectors with exact match using query_vector_store...")

        # Create the query object
        search_query = VectorSearchQuery(
            search_type='semantic', # We are only testing semantic match here
            semantic_query="dummy query text", # Text isn't used directly, embedding is
            embedding_model=TEST_EMBEDDING_MODEL,
            limit=5,
            # No filters needed for this basic test
        )

        query_results = await query_vector_store(
            db_context=db,
            query=search_query,
            query_embedding=test_embedding_vector, # Pass the embedding separately
        )

        assert query_results is not None, "query_vector_store returned None"
        assert len(query_results) > 0, "No results returned from vector store query" # Updated assertion message
        logger.info(f"Query returned {len(query_results)} result(s).")

        # Find the result corresponding to our document
        # query_vector_store returns list of dicts
        found_result = None
        for result_dict in query_results: # Renamed loop variable
            if result_dict.get("document_id") == doc_id:
                found_result = result_dict # Assign the dict directly
                break

        assert (
            found_result is not None
        ), f"Added document (ID: {doc_id}) not found in query results"
        logger.info(f"Found matching result: {found_result}")

        # Check distance (should be very close to 0 for exact match)
        assert "distance" in found_result, "Result missing 'distance' field"
        # Handle potential None distance if query somehow failed internally but returned row
        distance = found_result.get("distance")
        assert distance is not None, "Distance is None in the result"
        assert distance == pytest.approx(
            0.0, abs=1e-6
        ), f"Distance should be near zero for exact match, but was {distance}"

        # Check other fields in the result (which is now a dict)
        assert found_result.get("embedding_type") == test_embedding_type
        assert found_result.get("embedding_source_content") == test_content
        assert found_result.get("title") == test_title
        # Verify other fields returned by the new query function if needed
        assert found_result.get("source_id") == test_source_id
        assert found_result.get("chunk_index") == 0 # Based on test setup

    # --- Act & Assert: Retrieve Document by Source ID ---
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info(f"Retrieving document by source ID: {test_source_id}...")
        # Note: get_document_by_source_id returns the ORM model
        retrieved_doc: Optional[DocumentRecord] = await get_document_by_source_id(
            db, test_source_id
        )

        assert (
            retrieved_doc is not None
        ), f"Document not retrieved by source ID '{test_source_id}'"
        logger.info(f"Retrieved document: {retrieved_doc}")

        # Verify retrieved document matches original data
        assert retrieved_doc.id == doc_id
        assert retrieved_doc.source_id == test_source_id
        assert retrieved_doc.source_type == test_source_type
        assert retrieved_doc.title == test_title
        # Compare timezone-aware datetimes carefully
        assert retrieved_doc.created_at is not None
        assert retrieved_doc.created_at.isoformat() == test_created_at.isoformat()
        assert retrieved_doc.doc_metadata == test_metadata

    # --- Act & Assert: Delete Document ---
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info(f"Deleting document with ID: {doc_id}...")
        deleted = await delete_document(db, doc_id)
        assert (
            deleted is True
        ), f"delete_document did not return True for existing ID {doc_id}"
        logger.info("Document deleted.")

    # --- Assert: Verify Deletion ---
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info(f"Verifying document deletion by source ID: {test_source_id}...")
        retrieved_doc_after_delete = await get_document_by_source_id(db, test_source_id)
        assert (
            retrieved_doc_after_delete is None
        ), "Document was found after it should have been deleted"

        # Optional: Verify embeddings are also gone (due to CASCADE)
        # This requires querying the document_embeddings table directly
        stmt = "SELECT COUNT(*) FROM document_embeddings WHERE document_id = :doc_id"
        result = await db.execute_with_retry(text(stmt), {"doc_id": doc_id})
        count = result.scalar_one()
        assert (
            count == 0
        ), f"Embeddings for document ID {doc_id} were found after deletion"
        logger.info("Verified document and embeddings are deleted.")

    logger.info("--- Vector Storage Basic Flow Test Passed ---")
