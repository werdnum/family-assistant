# Indexing and Document Processing Testing Guide

This file provides guidance for working with indexing tests in this project.

## Overview

Indexing tests verify the complete document and email processing pipeline, including:

- Indexing documents (PDFs, text files, etc.) for vector search
- Processing emails and extracting content
- Content processor chains for extracting structured data
- Vector embedding generation and similarity search
- Storage and retrieval of indexed content

Tests are located in `tests/functional/indexing/` and cover end-to-end scenarios using real
database, indexing pipeline, and embedding services.

## Test Files

### Document Indexing

**`test_document_basic.py`** - Basic document indexing

- Indexing simple text and PDF documents
- Extracting document metadata (title, author, created date)
- Storing document embeddings
- Basic vector search functionality

**`test_document_advanced.py`** - Advanced document processing

- Complex PDF parsing with multiple pages
- OCR and text extraction from images
- Handling various file formats
- Processing large documents efficiently

**`test_document_retrieval.py`** - Document retrieval and search

- Vector similarity search for documents
- Keyword search and filtering
- Pagination and result ranking
- Retrieving document content and metadata

### Email Indexing

**`test_email_basic.py`** - Basic email processing

- Parsing email headers and content
- Extracting sender, recipient, subject, body
- Basic email indexing and storage
- Email search functionality

**`test_email_advanced.py`** - Advanced email processing

- Processing email threads and conversations
- Handling reply chains and quoted text
- Extracting action items from emails
- Email classification and categorization

**`test_email_attachments.py`** - Email attachment handling

- Extracting attachments from emails
- Processing embedded images
- Storing and retrieving attachments
- Handling various attachment types

### Content Processors

**`processors/`** subdirectory contains tests for individual content processors:

- PDF content extraction
- Email header parsing
- Plain text processing
- Structured data extraction

## Key Fixtures

### Embedding Fixtures

**`mock_pipeline_embedding_generator`** (function scope)

- Returns HashingWordEmbeddingGenerator for deterministic embeddings
- Used for consistent, reproducible tests
- Much faster than real embedding models

```python
async def test_indexing(mock_pipeline_embedding_generator):
    embedding_gen = mock_pipeline_embedding_generator
    # Embeddings are deterministic based on content hash
    embed1 = embedding_gen.generate("hello")
    embed2 = embedding_gen.generate("hello")
    assert embed1 == embed2  # Always same for same input
```

### Indexing Pipeline Fixtures

**`indexing_task_worker`** (function scope)

- TaskWorker configured for indexing tasks
- Returns tuple: `(TaskWorker, new_task_event, shutdown_event)`
- Handles document indexing task execution

```python
async def test_index_document(indexing_task_worker):
    worker, new_task_event, shutdown_event = indexing_task_worker

    # Register indexing handler
    worker.register_handler("index_document", index_handler)

    # Submit indexing task
    # Wait for completion
    await new_task_event.wait()
```

### Database Fixtures

**`test_db_engine`** (function scope, autouse)

- Provides SQLite or PostgreSQL database
- Initializes schema with all tables
- Clean slate for each test

**`pg_vector_db_engine`** (function scope)

- PostgreSQL with pgvector extension
- Required for vector search tests
- Full ACID isolation between tests

## Testing Patterns

### Pattern 1: Testing Document Indexing

```python
async def test_index_pdf_document(db_context, mock_pipeline_embedding_generator):
    # Create document
    doc_path = Path("tests/data/sample.pdf")

    # Index document
    async with db_context() as db:
        document = await db.documents.index_document(
            file_path=doc_path,
            title="Sample PDF",
            embedding_generator=mock_pipeline_embedding_generator
        )

    # Verify document is indexed
    async with db_context() as db:
        retrieved = await db.documents.get_document(document.id)
        assert retrieved.title == "Sample PDF"
        assert retrieved.content  # Content extracted
        assert retrieved.embedding  # Vector embedding generated
```

### Pattern 2: Testing Vector Search

```python
async def test_vector_search(db_context, mock_pipeline_embedding_generator):
    # Index multiple documents
    async with db_context() as db:
        doc1 = await db.documents.index_document("weather.pdf", embedding_gen)
        doc2 = await db.documents.index_document("sports.pdf", embedding_gen)
        doc3 = await db.documents.index_document("finance.pdf", embedding_gen)

    # Search for similar documents
    async with db_context() as db:
        results = await db.documents.search(
            query="sunshine and temperatures",
            limit=3,
            embedding_generator=mock_pipeline_embedding_generator
        )

    # Verify weather document ranks highest
    assert results[0].id == doc1.id
```

### Pattern 3: Testing Email Processing

```python
async def test_email_extraction(db_context):
    # Create email from raw MIME content
    email_data = {
        "headers": {
            "From": "sender@example.com",
            "To": "recipient@example.com",
            "Subject": "Important Update",
            "Date": "2025-01-15T10:30:00Z"
        },
        "body": "This is the email body..."
    }

    # Index email
    async with db_context() as db:
        email = await db.emails.index_email(email_data)

    # Verify metadata extraction
    assert email.sender == "sender@example.com"
    assert email.subject == "Important Update"
    assert email.body == "This is the email body..."
```

### Pattern 4: Testing Content Processors

```python
async def test_pdf_processor_chain(db_context):
    # Create processor chain
    processor = PDFProcessorChain(
        processors=[
            PDFExtractor(),
            TextCleaner(),
            StructuredDataExtractor()
    ])

    # Process PDF
    pdf_path = Path("tests/data/sample.pdf")
    result = await processor.process(pdf_path)

    # Verify extraction
    assert result.text  # Text extracted
    assert result.metadata  # Metadata extracted
    assert result.structured_data  # Structured data extracted
```

## Common Issues and Debugging

### Issue: Vector Search Returns No Results

**Error**: Search queries return empty results even though documents are indexed

**Debug Steps**:

1. Verify documents are actually indexed:

```python
async with db_context() as db:
    count = await db.documents.count_indexed()
    print(f"Indexed documents: {count}")
```

2. Check embedding generator is working:

```python
embedding = await embedding_gen.generate("test query")
assert embedding  # Should not be None
assert len(embedding) > 0  # Should have embedding vector
```

3. Verify embeddings are stored:

```python
async with db_context() as db:
    doc = await db.documents.get_document(doc_id)
    assert doc.embedding is not None
    assert len(doc.embedding) == expected_dimension
```

### Issue: PDF Extraction Fails

**Error**: PDF files not being processed or content is empty

**Debug Steps**:

1. Check PDF file integrity:

```bash
file tests/data/sample.pdf  # Should show PDF file type
pdfinfo tests/data/sample.pdf  # Should show valid PDF info
```

2. Test extraction directly:

```python
from family_assistant.indexing.processors import PDFExtractor

processor = PDFExtractor()
result = await processor.process("tests/data/sample.pdf")
assert result.text  # Should have extracted text
```

3. Check for unsupported PDF features:

```bash
# Test with simple text PDF vs complex PDF
pdftotext tests/data/sample.pdf -  # Should output text
```

### Issue: Email Parsing Errors

**Error**: Email headers not parsed correctly or content is malformed

**Debug Steps**:

1. Verify email format:

```python
from email import message_from_string

email_str = "..."
msg = message_from_string(email_str)
assert msg.get("Subject")  # Should parse subject
```

2. Check encoding:

```python
# Ensure email is UTF-8 or properly decoded
assert isinstance(email_data["body"], str)
```

3. Test with sample emails:

```bash
# Check test email samples
head -20 tests/data/sample_email.eml
```

## Running Indexing Tests

```bash
# Run all indexing tests
pytest tests/functional/indexing/ -xq

# Run specific test file
pytest tests/functional/indexing/test_document_basic.py -xq

# Run document tests only
pytest tests/functional/indexing/test_document*.py -xq

# Run email tests only
pytest tests/functional/indexing/test_email*.py -xq

# Run with verbose output for debugging
pytest tests/functional/indexing/test_vector_search.py -xvs

# Run with PostgreSQL backend
pytest tests/functional/indexing/ --postgres -xq

# Run content processor tests
pytest tests/functional/indexing/processors/ -xq
```

## Integration with Other Features

Indexing is used by multiple features:

- **Vector Search**: `tests/functional/vector_search/` - Similarity search in documents
- **Email Processing**: Integration with Telegram and web API for email ingestion
- **Chat Context**: Documents indexed for retrieval in chat conversations
- **Search API**: `tests/functional/web/api/` - REST API for document search

## Performance Considerations

### Embedding Generation

- Unit tests use `HashingWordEmbeddingGenerator` for speed and determinism
- Functional tests can use real embedding models if needed (slower)
- Mock embeddings are sufficient for correctness testing

### Large Documents

- Tests use sample documents in `tests/data/`
- For testing large document handling, create appropriately sized test files
- Consider using generators for memory efficiency in large-scale tests

### Database Indexes

- pgvector indexes may not be created in SQLite backend
- Use `--postgres` flag for testing vector search with proper indexes
- Verify index creation for performance tests

## See Also

- **[tests/CLAUDE.md](../CLAUDE.md)** - General testing patterns and three-tier test organization
- **[tests/functional/vector_search/CLAUDE.md](../vector_search/CLAUDE.md)** - Vector search tests
- **[tests/integration/CLAUDE.md](../../integration/CLAUDE.md)** - Integration testing with external
  services
- **[src/family_assistant/tools/CLAUDE.md](../../../src/family_assistant/tools/CLAUDE.md)** -
  Indexing tool development
