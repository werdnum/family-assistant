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
from family_assistant.storage.context import (
    DatabaseContext,
    get_db_context,
)  # Add get_db_context
from family_assistant.embeddings import MockEmbeddingGenerator  # For mocking embeddings
from family_assistant.tools import (  # Import tool components
    search_documents_tool,
    LocalToolsProvider,
    ToolExecutionContext,
    AVAILABLE_FUNCTIONS,  # To get the tool function mapping
    TOOLS_DEFINITION,  # To get tool definitions (though not strictly needed for execution)
)

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
            search_type="semantic",  # We are only testing semantic match here
            semantic_query="dummy query text",  # Text isn't used directly, embedding is
            embedding_model=TEST_EMBEDDING_MODEL,
            limit=5,
            # No filters needed for this basic test
        )

        query_results = await query_vector_store(
            db_context=db,
            query=search_query,
            query_embedding=test_embedding_vector,  # Pass the embedding separately
        )

        assert query_results is not None, "query_vector_store returned None"
        assert (
            len(query_results) > 0
        ), "No results returned from vector store query"  # Updated assertion message
        logger.info(f"Query returned {len(query_results)} result(s).")

        # Find the result corresponding to our document
        # query_vector_store returns list of dicts
        found_result = None
        for result_dict in query_results:  # Renamed loop variable
            if result_dict.get("document_id") == doc_id:
                found_result = result_dict  # Assign the dict directly
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
        assert found_result.get("chunk_index") == 0  # Based on test setup

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


@pytest.mark.asyncio
async def test_search_documents_tool(pg_vector_db_engine):
    """
    Tests the search_documents_tool function via LocalToolsProvider.
    1. Adds a test document and embedding.
    2. Sets up a MockEmbeddingGenerator for the query.
    3. Executes the tool using LocalToolsProvider.
    4. Verifies the tool returns the expected document.
    """
    # --- Arrange: Test Data ---
    test_source_id = f"test_tool_doc_{uuid.uuid4()}"
    test_source_type = "tool_test_source"
    test_title = f"Tool Test Document {uuid.uuid4()}"
    test_content = "This is the specific content for the document search tool test."
    test_query = "Tell me about the document search tool test"
    # Use a distinct model name for the mock to avoid conflicts
    mock_embedding_model = "mock-search-model-001"
    mock_embedding_dimension = 128  # Smaller dimension for mock

    # Generate consistent embeddings for test doc and query
    # Use different vectors to simulate non-exact match if desired,
    # but for basic function test, using the same makes assertion easier.
    mock_doc_embedding = (
        np.random.rand(mock_embedding_dimension).astype(np.float32).tolist()
    )
    mock_query_embedding = mock_doc_embedding  # Use same for direct hit

    test_doc = MockDocumentImpl(
        source_type=test_source_type,
        source_id=test_source_id,
        title=test_title,
        metadata={"purpose": "tool_testing"},
    )

    logger.info(f"\n--- Running Search Documents Tool Test ---")
    logger.info(f"Using Source ID: {test_source_id}")
    logger.info(
        f"Using Mock Embedding Model: {mock_embedding_model} (Dim: {mock_embedding_dimension})"
    )

    # --- Arrange: Add Document and Embedding to DB ---
    doc_id = None
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info("Adding test document for tool test...")
        doc_id = await add_document(db, doc=test_doc)
        logger.info(f"Document added with ID: {doc_id}")

        logger.info("Adding test embedding for tool test...")
        await add_embedding(
            db,
            document_id=doc_id,
            chunk_index=0,
            embedding_type="content_chunk",  # Match default search or specify if needed
            embedding=mock_doc_embedding,
            embedding_model=mock_embedding_model,  # Use the mock model name
            content=test_content,
        )
        logger.info("Embedding added.")

    # --- Arrange: Tool Execution Environment ---
    # Mock Embedding Generator: Maps the query text to the predefined embedding
    embedding_map = {test_query: mock_query_embedding}
    mock_generator = MockEmbeddingGenerator(
        embedding_map=embedding_map,
        model_name=mock_embedding_model,
        default_embedding=np.zeros(mock_embedding_dimension).tolist(),  # Fallback
    )

    # Local Tools Provider with the mock generator
    # We only need the search tool implementation for this test
    tool_implementations = {"search_documents": search_documents_tool}
    local_provider = LocalToolsProvider(
        definitions=[],  # Definitions not needed for execution test
        implementations=tool_implementations,
        embedding_generator=mock_generator,  # Inject the mock
    )

    # Tool Execution Context (needs a DatabaseContext)
    # Create a new context for the execution phase
    async with DatabaseContext(engine=pg_vector_db_engine) as exec_db_context:
        tool_context = ToolExecutionContext(
            interface_type="test", # Dummy interface
            conversation_id="vector_test_123", # Dummy conversation ID
            db_context=exec_db_context,
            calendar_config={},
            application=None,  # Not needed for search_documents tool
        )

        # --- Act: Execute the tool via the provider ---
        logger.info(f"Executing search_documents tool with query: '{test_query}'")
        tool_result = await local_provider.execute_tool(
            name="search_documents",
            arguments={"query_text": test_query},  # Pass arguments as dict
            context=tool_context,
        )

        logger.info(f"Tool execution result: {tool_result}")

        # --- Assert ---
        assert tool_result is not None, "Tool execution returned None"
        assert isinstance(tool_result, str), "Tool result should be a string"
        assert (
            "Error:" not in tool_result
        ), f"Tool execution reported an error: {tool_result}"
        assert (
            "Found relevant documents:" in tool_result
        ), "Tool result preamble missing"
        assert test_title in tool_result, "Document title not found in tool result"
        assert (
            test_source_type in tool_result
        ), "Document source type not found in tool result"
        # Check for part of the snippet (tool truncates)
        assert (
            test_content[:50] in tool_result
        ), "Document snippet not found in tool result"

    # --- Cleanup: Verify document deletion (optional, relies on fixture) ---
    # Add verification if fixture doesn't guarantee cleanup or if explicit check is desired
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        logger.info(f"Verifying test document (ID: {doc_id}) is deleted after test...")
        # Use direct query as get_document_by_source_id might be cached or slow
        stmt = text("SELECT COUNT(*) FROM documents WHERE id = :doc_id")
        result = await db.execute_with_retry(stmt, {"doc_id": doc_id})
        count = result.scalar_one()
        # NOTE: This assertion depends on the fixture cleaning up *after* the test runs.
        # If the fixture cleans *before*, this check is invalid.
        # Assuming cleanup happens after:
        # assert count == 0, f"Test document ID {doc_id} was not cleaned up after the test."
        # If cleanup happens before, we can't reliably check here.
        # For now, assume fixture handles cleanup.

    logger.info("--- Search Documents Tool Test Passed ---")
