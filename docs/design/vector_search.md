# Vector Search System Design for Personal Assistant

## 1. Requirements Summary

*   **Goal:** Implement a system for storing and retrieving personal documents (emails, PDFs, notes, images with text) using vector search combined with metadata filtering.
*   **Technology:** Use PostgreSQL with the `pgvector` extension for vector storage and similarity search.
*   **Core Functionality:**
    *   Persist documents and their vector embeddings.
    *   Support semantic search based on user queries (translated by an LLM).
    *   Filter search results based on metadata (e.g., document type, date, source).
    *   Handle various document types (text extraction, OCR).
*   **Chunking:** Design the system to support document chunking, although the initial implementation may use a single chunk per document.
*   **Embeddings:**
    *   Use an LLM to generate embeddings.
    *   Consider having the LLM generate a description/summary for embedding alongside or instead of the full content, especially for non-text or large items.
    *   The design should be agnostic to the specific embedding model used.
*   **Query Examples:** Support queries like "email receipt from pharmacy last October" or "scanned letter from IRS in 2024 in Google Drive".

## 2. Database Schema (PostgreSQL with pgvector)

We'll use two main tables: `documents` to store metadata about the original items, and `document_chunks` to store the text content and corresponding vector embeddings. This allows for future expansion to multiple chunks per document.

```sql
-- Enable pgvector extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS vector;

-- Table to store metadata about the original documents/items
CREATE TABLE documents (
    id BIGSERIAL PRIMARY KEY,
    source_type VARCHAR(50) NOT NULL, -- e.g., 'email', 'pdf', 'google_drive', 'note', 'image'
    source_id TEXT UNIQUE,          -- Unique identifier from the source system (e.g., email message ID, file path, note title)
    source_uri TEXT,                -- URI or path to the original item, if applicable
    title TEXT,                     -- Title or subject of the document
    created_at TIMESTAMPTZ,          -- Original creation date of the item
    added_at TIMESTAMPTZ DEFAULT NOW(), -- When the item was added to this system
    metadata JSONB                   -- Flexible field for additional metadata (e.g., email sender/recipient, file tags, detected entities)
);

-- Indexes for metadata filtering
CREATE INDEX idx_documents_source_type ON documents (source_type);
CREATE INDEX idx_documents_created_at ON documents (created_at);
CREATE INDEX idx_documents_metadata ON documents USING GIN (metadata); -- For querying JSONB fields

-- Table to store text chunks and their embeddings
CREATE TABLE document_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL DEFAULT 0, -- 0 for single chunk, 0, 1, 2... for multiple chunks
    content TEXT NOT NULL,              -- The actual text content of the chunk (or LLM-generated description)
    embedding VECTOR(768) NOT NULL,     -- Vector embedding. Adjust dimension (768) based on the chosen model.
    embedding_model VARCHAR(100) NOT NULL, -- Identifier for the model used to generate the embedding
    content_hash TEXT,                  -- Optional: Hash of the content to detect changes
    added_at TIMESTAMPTZ DEFAULT NOW(),

    -- Ensure chunk_index is unique per document
    UNIQUE (document_id, chunk_index)
);

-- Vector index for similarity search (example using HNSW with Cosine distance)
-- Choose dimensions, index type (hnsw/ivfflat), and distance metric (vector_cosine_ops, vector_l2_ops, vector_ip_ops)
-- based on the embedding model and performance characteristics.
CREATE INDEX idx_document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64); -- Tune M and ef_construction based on data/needs

-- Optional: Add traditional index if searching chunk content directly is needed
-- CREATE INDEX idx_document_chunks_content_gin ON document_chunks USING GIN (to_tsvector('english', content));

```

**Schema Notes:**

*   **`documents` Table:** Holds high-level information. `source_id` ensures we don't ingest the same item twice. `metadata` allows storing arbitrary key-value pairs relevant to the source (e.g., `{ "sender": "...", "recipients": [...] }` for emails).
*   **`document_chunks` Table:** Stores the text and vector.
    *   `document_id`: Links back to the parent document.
    *   `chunk_index`: Supports multiple chunks per document (initially just 0).
    *   `content`: Stores the text used for embedding. This could be the raw extracted text, a cleaned version, or an LLM-generated summary/description.
    *   `embedding`: The vector itself. The dimension needs to match the chosen embedding model.
    *   `embedding_model`: Tracks which model generated the vector, crucial for future migrations or checks.
*   **Indexes:**
    *   Standard B-tree indexes on `documents` columns frequently used for filtering (`source_type`, `created_at`).
    *   GIN index on `documents.metadata` for efficient querying within the JSONB structure.
    *   `pgvector` index (`hnsw` or `ivfflat`) on `document_chunks.embedding` is critical for fast approximate nearest neighbor search. Cosine distance (`vector_cosine_ops`) is often a good starting point for text embeddings.

## 3. Ingestion and Embedding Process

1.  **Item Acquisition:** Obtain the document/item (e.g., read email via IMAP, download file, receive webhook).
2.  **Metadata Extraction:** Parse/extract key metadata (source, ID, title, dates, sender, etc.). Store this in the `documents` table. Get the `document.id`.
3.  **Content Extraction:**
    *   **Emails:** Parse the body, strip HTML/signatures, potentially focus on the main content.
    *   **PDFs/Images:** Use OCR (e.g., Tesseract, cloud services) to extract text.
    *   **Notes:** Use the note content directly.
4.  **Text Preparation / Description (Optional but Recommended):**
    *   Clean the extracted text.
    *   For non-text or very large documents, consider using an LLM to generate a concise, descriptive summary of the item. This summary can then be embedded. This is particularly useful for images ("Photo of receipt from store X dated Y") or complex documents.
5.  **Chunking:**
    *   **Initial:** Treat the entire prepared text/description as a single chunk (index 0).
    *   **Future:** Implement a chunking strategy (e.g., by paragraph, fixed token size with overlap) if documents are large. Create multiple rows in `document_chunks` for each chunk, incrementing `chunk_index`.
6.  **Embedding Generation:** Pass the text content of each chunk to the chosen embedding model (via an abstraction layer like LiteLLM if needed) to get the vector. Record the model identifier used.
7.  **Storage:** Insert the chunk content, vector embedding, `document_id`, `chunk_index`, and `embedding_model` into the `document_chunks` table.

## 4. Querying Process

1.  **User Query Translation:** The LLM assistant receives a natural language query (e.g., "scanned letter from IRS in 2024").
2.  **Query Formulation:** The LLM (or intermediary logic) translates this into:
    *   A **search query text** suitable for embedding (e.g., "scanned letter IRS 2024").
    *   A set of **metadata filters** (e.g., `source_type = 'pdf'` or `'image'`, `created_at >= '2024-01-01'`, potentially keywords in `title` or `metadata`).
3.  **Query Embedding:** Generate the embedding for the search query text using the *same embedding model* used for the stored documents.
4.  **Database Query:** Execute a SQL query combining vector search and metadata filtering:

    ```sql
    WITH relevant_docs AS (
        SELECT id
        FROM documents
        WHERE source_type IN ('pdf', 'image') -- Example filter
          AND created_at >= '2024-01-01'      -- Example filter
          -- AND metadata->>'sender' = 'IRS' -- Example JSONB filter
          -- AND title ILIKE '%IRS%'       -- Example title filter
    )
    SELECT
        dc.id AS chunk_id,
        dc.document_id,
        d.title,
        d.source_type,
        d.created_at,
        dc.content,
        dc.embedding <=> $<query_embedding>::vector AS distance -- Cosine distance operator
    FROM document_chunks dc
    JOIN documents d ON dc.document_id = d.id
    WHERE dc.document_id IN (SELECT id FROM relevant_docs)
      AND dc.embedding_model = $<model_identifier> -- Ensure consistency
    ORDER BY distance ASC -- Order by similarity (lower cosine distance is more similar)
    LIMIT 10; -- Limit results
    ```

    *Replace `<query_embedding>` with the generated query vector and `<model_identifier>` with the correct model name.*
    *Use the appropriate distance operator (`<=>` for cosine, `<->` for L2, `<#>` for inner product) matching the index.*

5.  **Result Processing:** The application receives the top N relevant chunks and their associated document metadata.
6.  **Response Generation:** The LLM uses the retrieved content snippets to synthesize an answer for the user.

## 5. Considerations

*   **Embedding Model Choice:** Select a model appropriate for the task (semantic retrieval) and document types. Consider models supporting the desired languages and text lengths. The schema tracks the model used per chunk.
*   **Dimension Size:** Choose the vector dimension based on the model. Larger dimensions capture more detail but require more storage and potentially more computation.
*   **Index Tuning:** `pgvector` index parameters (`m`, `ef_construction` for HNSW; `lists` for IVFFlat) need tuning based on dataset size, query latency requirements, and recall accuracy trade-offs.
*   **Re-indexing:** If the embedding model is changed, all existing embeddings will need to be regenerated and the `document_chunks` table updated.
*   **Scalability:** For very large datasets, consider PostgreSQL scaling strategies or potentially dedicated vector databases.
*   **Hybrid Search:** The schema could be extended to support keyword search (e.g., using `tsvector`) alongside vector search for hybrid retrieval strategies if needed.
*   **OCR Quality:** The quality of OCR significantly impacts the searchability of scanned documents/images.
*   **Cost:** Embedding generation (especially using external APIs) and potentially OCR services incur costs.


