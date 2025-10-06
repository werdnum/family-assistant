"""Integration tests for multimodal document retrieval."""

import pathlib
import tempfile
from collections.abc import AsyncGenerator
from io import BytesIO

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.indexing.ingestion import process_document_ingestion_request
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import add_embedding
from family_assistant.tools.documents import (
    get_full_document_content_tool,
    search_documents_tool,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)

# Test constants
TEST_EMBEDDING_DIMENSION = 1536


@pytest.fixture
async def mock_embedding_generator() -> MockEmbeddingGenerator:
    """Provides a function-scoped mock embedding generator instance."""
    # Create deterministic embeddings for test content
    test_embeddings = {}

    # Create embeddings for common test phrases
    for i, phrase in enumerate([
        "test PDF document",
        "test image",
        "test text document",
    ]):
        test_embeddings[phrase] = (
            np.random.rand(TEST_EMBEDDING_DIMENSION).astype(np.float32)
            * (0.1 + i * 0.1)
        ).tolist()

    return MockEmbeddingGenerator(
        embedding_map=test_embeddings,
        model_name="mock-embedding-model",
        dimensions=TEST_EMBEDDING_DIMENSION,
    )


class TestDocumentRetrieval:
    """Test multimodal document retrieval functionality."""

    @pytest.fixture
    async def temp_storage_path(self) -> AsyncGenerator[pathlib.Path, None]:
        """Create a temporary directory for document storage."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield pathlib.Path(temp_dir)

    @pytest.fixture
    def sample_pdf_content(self) -> bytes:
        """Create minimal PDF content for testing."""
        # Minimal PDF content - just header
        return b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj 3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj xref 0 4 0000000000 65535 f 0000000010 00000 n 0000000053 00000 n 0000000125 00000 n trailer<</Size 4/Root 1 0 R>> startxref 199 %%EOF"

    @pytest.fixture
    def sample_image_content(self) -> bytes:
        """Create minimal image content for testing."""
        img = Image.new("RGB", (100, 100), color="red")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    @pytest.mark.postgres
    async def test_pdf_document_upload_and_retrieval(
        self,
        pg_vector_db_engine: AsyncEngine,
        temp_storage_path: pathlib.Path,
        sample_pdf_content: bytes,
        mock_embedding_generator: MockEmbeddingGenerator,
    ) -> None:
        """Test uploading a PDF and retrieving it with multimodal support."""

        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conv",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
            )

            # Ingest PDF document
            result = await process_document_ingestion_request(
                db_context=db_context,
                document_storage_path=temp_storage_path,
                source_type="test_pdf",
                source_id="test_pdf_001",
                source_uri="file://test.pdf",
                title="Test PDF Document",
                uploaded_file_content=sample_pdf_content,
                uploaded_file_filename="test.pdf",
                uploaded_file_content_type="application/pdf",
            )

            assert result["document_id"] is not None
            assert result["task_enqueued"] is True
            document_id = result["document_id"]

            # Add a test embedding for search to work

            embedding_result = await mock_embedding_generator.generate_embeddings([
                "test PDF document"
            ])
            test_embedding = embedding_result.embeddings[0]
            await add_embedding(
                db_context=db_context,
                document_id=document_id,
                chunk_index=0,
                embedding_type="full_text",
                embedding=test_embedding,
                embedding_model="mock-embedding-model",
                content="Test PDF document content",
            )

            # Search for the document
            search_results = await search_documents_tool(
                exec_context=exec_context,
                embedding_generator=mock_embedding_generator,
                query="test PDF document",
                limit=5,
            )

            # Should indicate original file is available
            assert "📎 Original file available" in search_results
            assert "test.pdf" in search_results
            assert str(document_id) in search_results

            # Retrieve full document content
            content_result = await get_full_document_content_tool(
                exec_context=exec_context, document_id=document_id
            )

            # Should return ToolResult with attachment for PDF
            assert isinstance(content_result, ToolResult)
            assert content_result.attachments and len(content_result.attachments) > 0

            attachment = content_result.attachments[0]
            assert isinstance(attachment, ToolAttachment)
            assert attachment.mime_type == "application/pdf"
            assert attachment.content == sample_pdf_content
            assert "Test PDF Document" in attachment.description

    @pytest.mark.postgres
    async def test_image_document_upload_and_retrieval(
        self,
        pg_vector_db_engine: AsyncEngine,
        temp_storage_path: pathlib.Path,
        sample_image_content: bytes,
        mock_embedding_generator: MockEmbeddingGenerator,
    ) -> None:
        """Test uploading an image and retrieving it with multimodal support."""

        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conv",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
            )

            # Ingest image document
            result = await process_document_ingestion_request(
                db_context=db_context,
                document_storage_path=temp_storage_path,
                source_type="test_image",
                source_id="test_image_001",
                source_uri="file://test.png",
                title="Test Image",
                uploaded_file_content=sample_image_content,
                uploaded_file_filename="test.png",
                uploaded_file_content_type="image/png",
            )

            assert result["document_id"] is not None
            document_id = result["document_id"]

            # Add a test embedding for the image

            embedding_result = await mock_embedding_generator.generate_embeddings([
                "test image"
            ])
            test_embedding = embedding_result.embeddings[0]
            await add_embedding(
                db_context=db_context,
                document_id=document_id,
                chunk_index=0,
                embedding_type="full_text",
                embedding=test_embedding,
                embedding_model="mock-embedding-model",
                content="Test Image content",
            )

            # Retrieve full document content
            content_result = await get_full_document_content_tool(
                exec_context=exec_context, document_id=document_id
            )

            # Should return ToolResult with attachment for image
            assert isinstance(content_result, ToolResult)
            assert content_result.attachments and len(content_result.attachments) > 0

            attachment = content_result.attachments[0]
            assert isinstance(attachment, ToolAttachment)
            assert attachment.mime_type == "image/png"
            assert attachment.content == sample_image_content
            assert "Test Image" in attachment.description

    @pytest.mark.postgres
    async def test_text_only_document_retrieval(
        self,
        pg_vector_db_engine: AsyncEngine,
        temp_storage_path: pathlib.Path,
        mock_embedding_generator: MockEmbeddingGenerator,
    ) -> None:
        """Test retrieving a document that has no original file."""

        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conv",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
            )

            # Ingest text-only document (no file)
            result = await process_document_ingestion_request(
                db_context=db_context,
                document_storage_path=temp_storage_path,
                source_type="test_text",
                source_id="test_text_001",
                source_uri="content://manual",
                title="Test Text Document",
                content_parts={"content": "This is a test text document."},
            )

            assert result["document_id"] is not None
            document_id = result["document_id"]

            # Add a test embedding for the text document

            embedding_result = await mock_embedding_generator.generate_embeddings([
                "test text document"
            ])
            test_embedding = embedding_result.embeddings[0]
            await add_embedding(
                db_context=db_context,
                document_id=document_id,
                chunk_index=0,
                embedding_type="raw_note_text",  # Use a type that _get_text_content_fallback recognizes
                embedding=test_embedding,
                embedding_model="mock-embedding-model",
                content="This is a test text document.",
            )

            # Search should not show file attachment
            search_results = await search_documents_tool(
                exec_context=exec_context,
                embedding_generator=mock_embedding_generator,
                query="test text document",
                limit=5,
            )

            # Should NOT indicate original file is available
            assert "📎 Original file available" not in search_results
            assert str(document_id) in search_results

            # Retrieve full document content - should return string (no multimodal)
            content_result = await get_full_document_content_tool(
                exec_context=exec_context, document_id=document_id
            )

            # Should return string for text-only documents
            assert isinstance(content_result, str)
            assert "test text document" in content_result.lower()

    @pytest.mark.postgres
    async def test_missing_file_fallback(
        self,
        pg_vector_db_engine: AsyncEngine,
        temp_storage_path: pathlib.Path,
        sample_pdf_content: bytes,
        mock_embedding_generator: MockEmbeddingGenerator,
    ) -> None:
        """Test that retrieval falls back to text when file is missing."""

        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conv",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
            )

            # Ingest PDF document
            result = await process_document_ingestion_request(
                db_context=db_context,
                document_storage_path=temp_storage_path,
                source_type="test_pdf_missing",
                source_id="test_pdf_missing_001",
                source_uri="file://missing.pdf",
                title="Missing PDF Document",
                uploaded_file_content=sample_pdf_content,
                uploaded_file_filename="missing.pdf",
                uploaded_file_content_type="application/pdf",
            )

            document_id = result["document_id"]

            # Add a test embedding for the document

            embedding_result = await mock_embedding_generator.generate_embeddings([
                "missing PDF document"
            ])
            test_embedding = embedding_result.embeddings[0]
            await add_embedding(
                db_context=db_context,
                document_id=document_id,
                chunk_index=0,
                embedding_type="full_text",
                embedding=test_embedding,
                embedding_model="mock-embedding-model",
                content="Missing PDF Document content",
            )

            # Remove the file to simulate missing file
            stored_files = list(temp_storage_path.glob("*missing.pdf"))
            for file_path in stored_files:
                file_path.unlink()

            # Retrieve should fall back to text content
            content_result = await get_full_document_content_tool(
                exec_context=exec_context, document_id=document_id
            )

            # Should fall back to string when file is missing
            # (PDF processing would have extracted text during indexing)
            assert isinstance(content_result, str)

    @pytest.mark.postgres
    async def test_file_size_limit_fallback(
        self,
        pg_vector_db_engine: AsyncEngine,
        temp_storage_path: pathlib.Path,
        mock_embedding_generator: MockEmbeddingGenerator,
    ) -> None:
        """Test that large files fall back to text content."""

        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conv",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
            )

            # Create large dummy file content (over 20MB)
            large_content = b"x" * (21 * 1024 * 1024)  # 21MB

            # Ingest large document
            result = await process_document_ingestion_request(
                db_context=db_context,
                document_storage_path=temp_storage_path,
                source_type="test_large",
                source_id="test_large_001",
                source_uri="file://large.txt",
                title="Large Document",
                uploaded_file_content=large_content,
                uploaded_file_filename="large.txt",
                uploaded_file_content_type="text/plain",
            )

            document_id = result["document_id"]

            # Add a test embedding for the large document

            embedding_result = await mock_embedding_generator.generate_embeddings([
                "large document"
            ])
            test_embedding = embedding_result.embeddings[0]
            await add_embedding(
                db_context=db_context,
                document_id=document_id,
                chunk_index=0,
                embedding_type="full_text",
                embedding=test_embedding,
                embedding_model="mock-embedding-model",
                content="Large Document content",
            )

            # Retrieve should fall back to text due to size limit
            content_result = await get_full_document_content_tool(
                exec_context=exec_context, document_id=document_id
            )

            # Should fall back to string for large files
            assert isinstance(content_result, str)

    @pytest.mark.postgres
    async def test_file_path_persistence(
        self,
        pg_vector_db_engine: AsyncEngine,
        temp_storage_path: pathlib.Path,
        sample_pdf_content: bytes,
    ) -> None:
        """Test that file paths are correctly stored in the database."""

        async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
            # Ingest PDF document
            result = await process_document_ingestion_request(
                db_context=db_context,
                document_storage_path=temp_storage_path,
                source_type="test_path_persistence",
                source_id="test_path_001",
                source_uri="file://path_test.pdf",
                title="Path Test Document",
                uploaded_file_content=sample_pdf_content,
                uploaded_file_filename="path_test.pdf",
                uploaded_file_content_type="application/pdf",
            )

            document_id = result["document_id"]

            # Check that file_path is stored in database
            query = text("SELECT file_path FROM documents WHERE id = :doc_id")
            doc_result = await db_context.fetch_one(query, {"doc_id": document_id})

            assert doc_result is not None
            assert doc_result["file_path"] is not None
            assert "path_test.pdf" in doc_result["file_path"]
            assert pathlib.Path(doc_result["file_path"]).exists()
