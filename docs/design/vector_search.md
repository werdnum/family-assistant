# Vector Search System Design for Personal Assistant

## 1. Requirements Summary

*   **Goal:** Implement a system for storing and retrieving personal documents (emails, PDFs, notes, images with text) using vector search combined with metadata filtering.
*   **Technology:** Use PostgreSQL with the `pgvector` extension for vector storage and similarity search.
*   **Core Functionality:**
    *   Persist documents and their vector embeddings.
    *   Support semantic search based on user queries (translated by an LLM).
    *   Use LLM during ingestion to extract structured metadata based on a defined schema.
    *   Filter search results based on metadata (e.g., document type, date, source).
    *   Support keyword search on textual content alongside vector search (hybrid search).
    *   Handle various document types (text extraction, OCR).
*   **Chunking:** Design the system to support document chunking, although the initial implementation may use a single chunk per document.
*   **Embeddings:**
    *   Use an LLM to generate embeddings.
    *   Consider having the LLM generate a description/summary for embedding alongside or instead of the full content, especially for non-text or large items.
    *   The design should be agnostic to the specific embedding model used.
*   **Query Examples:** Support queries like "email receipt from pharmacy last October" or "scanned letter from IRS in 2024 in Google Drive".

## 2. Database Schema (PostgreSQL with pgvector)

We'll use two main tables: `documents` (the persistent storage table, mapped to the `DocumentRecord` SQLAlchemy model) to store metadata about the original items, and `document_embeddings` (the persistent storage table, mapped to the `DocumentEmbeddingRecord` SQLAlchemy model) to store various vector embeddings related to each document (e.g., for title, summary, content chunks). The API uses a `Document` protocol for the external interface.

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
    metadata JSONB                   -- Flexible field for additional metadata (e.g., email sender/recipient, file tags, detected entities),
    -- summary TEXT                  -- Optional: Consider adding a dedicated summary field if frequently generated
);

-- Indexes for metadata filtering
CREATE INDEX idx_documents_source_type ON documents (source_type);
CREATE INDEX idx_documents_created_at ON documents (created_at);
CREATE INDEX idx_documents_metadata ON documents USING GIN (metadata); -- For querying JSONB fields
-- CREATE INDEX idx_documents_title_gin ON documents USING GIN (to_tsvector('english', title)); -- Optional for keyword search on title

-- Table to store different types of embeddings associated with documents or their chunks
CREATE TABLE document_embeddings (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL DEFAULT 0, -- 0 for document-level embeddings (title, summary), 1+ for content chunks
    embedding_type VARCHAR(50) NOT NULL, -- e.g., 'title', 'summary', 'content_chunk', 'ocr_text', 'image_clip'
    content TEXT,                       -- Source text for text-based embeddings (NULL for non-text like 'image_clip')
    embedding VECTOR NOT NULL,          -- Variable-dimension vector embedding. Requires pgvector >= 0.5.0
                                        -- Dimensions determined by the 'embedding_model'.
    embedding_model VARCHAR(100) NOT NULL, -- Identifier for the model used to generate the embedding
    content_hash TEXT,                  -- Optional: Hash of the content to detect changes
    added_at TIMESTAMPTZ DEFAULT NOW(),

    -- Composite unique key: Ensure only one embedding of a specific type exists for a specific chunk/document part.
    UNIQUE (document_id, chunk_index, embedding_type)
);

-- Partial vector indexes for specific models/dimensions.
-- Choose index type (hnsw/ivfflat), distance metric (vector_cosine_ops, vector_l2_ops, vector_ip_ops),
-- and dimensions based on the embedding model.
-- based on the embedding model and performance characteristics.
-- Example for gemini-exp-03-07 (assuming 1536 dimensions, Cosine distance)
CREATE INDEX idx_doc_embeddings_gemini_1536_hnsw_cos ON document_embeddings
USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
WHERE embedding_model = 'gemini-exp-03-07';
WITH (m = 16, ef_construction = 64); -- Tune M and ef_construction based on data/needs

-- Indexes to aid filtering during search
CREATE INDEX idx_doc_embeddings_type_model ON document_embeddings (embedding_type, embedding_model);
CREATE INDEX idx_doc_embeddings_document_chunk ON document_embeddings (document_id, chunk_index);
-- GIN expression index for full-text search directly on the 'content' column
CREATE INDEX idx_doc_embeddings_content_fts_gin ON document_embeddings USING GIN (to_tsvector('english', content)) WHERE content IS NOT NULL;

```

**Metadata Schema Definition:**

While the `metadata` JSONB field is flexible, defining a consistent schema is crucial for reliable extraction and querying. This schema should outline common fields expected across different document types and potentially define required fields for specific `source_type`s.

*   **Common Fields:** `sender`, `recipients`, `event_name`, `event_date`, `location`, `vendor`, `amount`, `currency`, `due_date`, `category`, `tags` (as an array of strings).
*   **Type-Specific Fields:**
    *   `email`: `cc`, `bcc`, `thread_id`
    *   `receipt`: `total_amount`, `tax_amount`, `payment_method`
    *   `invoice`: `invoice_number`, `customer_id`, `service_period`
    *   `event_ticket`: `seat_number`, `attendee_names`
*   **LLM Interaction:** When using an LLM for extraction (see Ingestion Process), the prompt should guide the LLM to populate fields according to this defined schema, ideally requesting output in JSON format matching the structure.


**Schema Notes:**

*   **`documents` Table:** Holds high-level information. `source_id` ensures we don't ingest the same item twice. `metadata` allows storing arbitrary key-value pairs. The `title` field itself can be embedded. (Mapped to `DocumentRecord` model).
*   **`document_embeddings` Table:** Stores individual embeddings. (Mapped to `DocumentEmbeddingRecord` model).
    *   `document_id`: Links back to the parent document.
    *   `chunk_index`: Supports multiple chunks per document (initially just 0).
    *   `embedding_type`: Crucial field indicating what the `embedding` represents (e.g., 'title', 'summary', 'content_chunk', 'ocr_text', 'image_clip'). Part of the composite key.
    *   `content`: The source text for text-based embeddings (e.g., the actual title text, the summary text, the chunk text). Can be `NULL` for non-text embeddings.
    *   `embedding`: The vector itself. The dimension needs to match the chosen embedding model.
    *   `embedding_model`: Tracks which model generated the vector, crucial for future migrations or checks.
*   **Indexes:**
*   `chunk_index`: Groups embeddings. `0` typically represents document-level aspects (title, summary), while `1+` can represent sequential content chunks.
*   **Indexes:**
    *   Standard B-tree indexes on `documents` columns frequently used for filtering (`source_type`, `created_at`).
    *   GIN index on `documents.metadata` for efficient querying within the JSONB structure.
    *   Partial `pgvector` indexes (e.g., `idx_doc_embeddings_gemini_1536_hnsw_cos`) targeting specific `embedding_model` values and casting the `embedding` column to the correct dimension are critical for fast similarity search.
    *   Standard indexes on `document_embeddings` (`embedding_type`, `embedding_model`, `document_id`, `chunk_index`) aid filtering before or after vector search.
*   **Uniqueness:** The `UNIQUE (document_id, chunk_index, embedding_type)` constraint ensures data integrity, preventing duplicate embeddings for the same source aspect.

## 3. Ingestion and Embedding Process

The system supports multiple ways to ingest documents:

1.  **Source-Specific Integration (e.g., Email):**
    *   **Acquisition:** Receive email via webhook (`web_server.py` -> `email_storage.store_incoming_email`).
    *   **Processing:** A background task (`handle_index_email`) fetches the stored email, extracts content/metadata, potentially uses LLM enrichment, generates embeddings, and stores them. (See steps below for details).

2.  **Direct API Upload (Recommended for external sources/manual additions):**
    *   **Acquisition:** An HTTP `POST` endpoint (e.g., `/documents/upload`) accepts `multipart/form-data`. The request includes:
        *   Metadata fields (`source_type`, `source_id`, `title`, `created_at`, custom `metadata` JSON, etc.).
        *   The document content itself as a file upload part. The `Content-Type` of the uploaded file (e.g., `text/plain`, `text/markdown`, `application/pdf`) is crucial for determining processing steps.
    *   **Initial Handling (API Endpoint):**
        *   Validate metadata and content presence.
        *   Store the base metadata by calling `storage.add_document` to create the `documents` record and get the `document_id`.
        *   **Temporarily store the uploaded content:**
            *   *Initial Implementation (Text):* For simple text formats (plain, markdown), the content might be stored directly in the task payload if size permits, or in a temporary database field/cache.
            *   *Future Implementation (Files):* For binary files (PDF, images) or larger text files, save the content to a designated temporary file storage location (using existing file infra if available).
        *   Enqueue a background task (e.g., `process_uploaded_document`) using `storage.enqueue_task`. The task payload must contain:
            *   The `document_id`.
            *   Either the `content` directly (for initial text implementation) OR a `temp_file_reference` (path or ID) pointing to the temporarily stored content (for future file handling). These payload fields should have "oneof" semantics.
        *   Return a success response (e.g., 202 Accepted) to the caller.
    *   **Asynchronous Processing (Task Worker):**
        *   A dedicated task handler (`handle_process_uploaded_document`) picks up the task.
        *   Retrieves the content (either directly from payload or from temporary storage using the reference).
        *   Processes the content based on its type (initially, just use text directly; later, add OCR for images/PDFs, parsing for specific formats).
        *   Performs embedding generation and storage (Steps 6-9 below).
        *   Cleans up the temporary content storage if applicable.

3.  **Initial Metadata & Content Extraction (Common Step):** Regardless of acquisition method, perform basic parsing to get essential identifiers (`source_id`), the raw content (text, image data), and any easily obtainable metadata (e.g., email headers, file modification time). Extract the main text content for further processing.

4.  **LLM-Powered Metadata Enrichment (Optional/Common Step):**
    *   Feed the extracted text content (and potentially initial metadata like filename or subject) to an LLM.
    *   Prompt the LLM to analyze the content and extract relevant metadata according to the predefined **Metadata Schema Definition** (see above). Explicitly request the output in a structured JSON format.
    *   Validate the LLM's JSON output against the expected schema. Handle potential errors or missing fields.
5.  **Document Record Creation:** Store the initial and LLM-extracted metadata (combined into the `metadata` JSONB field), `source_type`, `source_id`, `title`, `created_at`, etc., in the `documents` table using `storage.add_document`. Retrieve the generated `document.id`.
    *   The `add_document` storage function accepts a `Document` object conforming to a defined protocol, providing access to `source_type`, `source_id`, `title`, `created_at`, and base `metadata`. It also accepts an optional `enriched_metadata` dictionary.
    *   *Note:* For the API upload flow, this step happens *before* content processing, within the initial API request handler.

6.  **Content Processing & Text Extraction (Performed by Task Worker for async flows):**
    *   Retrieve the full content (e.g., email body from DB, file content from temporary storage).
    *   **Emails:** Parse the body (e.g., from `received_emails` table), strip HTML/signatures. Extract subject separately for potential 'title' embedding. Consider processing attachments (requires file handling logic similar to API uploads).
    *   **Uploaded Content (API):**
        *   *Initial:* Directly use the provided text content (plain, markdown).
        *   *Future:* If PDF/Image, use OCR (e.g., Tesseract, cloud services) to extract text. Handle other formats as needed.
    *   **Notes:** Use the note content directly.

7.  **Text Preparation / Description (Optional but Recommended):**
    *   Clean the extracted text.
    *   For non-text or very large documents, consider using an LLM to generate a concise, descriptive **summary** of the item. Store this summary.
    *   Extract or use the document's **title** (e.g., email subject, filename).
7.  **Chunking:**
    *   Divide the main content (extracted text, note body) into chunks if necessary (e.g., by paragraph). Assign `chunk_index` starting from 1.
    *   Document-level aspects like title and summary conceptually belong to `chunk_index = 0`.
8.  **Embedding Generation:** Generate embeddings for relevant aspects using the chosen model(s):
    *   Embed the `title` text.
    *   Embed the generated `summary` text.
    *   Embed the content of each text `chunk` (from step 5).
    *   Embed OCR text if applicable (future enhancement).
    *   Generate and store other embedding types (e.g., image CLIP vectors) if needed, potentially using different models (future enhancement).
10. **Embedding Storage:** For *each* generated embedding, insert a row into `document_embeddings` using the `add_embedding` function, providing:
    *   `document_id` (obtained previously)
    *   `chunk_index` (0 for title/summary, 1+ for content chunks)
    *   `embedding_type` ('title', 'summary', 'content_chunk', 'ocr_text', etc.)
    *   `content` (the source text, if applicable)
    *   `embedding` (the vector)
    *   `embedding_model` (identifier of the model used)
    *   Store the generated `content_tsvector` along with other data. This can be done via a trigger or during the INSERT statement itself: `to_tsvector('english', content_text)`.

## 4. Querying Process

1.  **User Query Translation:** The LLM assistant receives a natural language query (e.g., "scanned letter from IRS in 2024").
2.  **Query Formulation:** The LLM (or intermediary logic) translates this into:
    *   A **search query text** suitable for embedding (e.g., "scanned letter IRS 2024").
    *   A set of **metadata filters** (e.g., `source_type = 'pdf'` or `'image'`, `created_at >= '2024-01-01'`, potentially keywords in `title` or `metadata`).
    *   **Keywords** for full-text search (e.g., "IRS", "letter", "2024").
    *   Optionally, target **embedding types** (e.g., prioritize searching 'title' and 'summary' first, or only search 'content_chunk').
3.  **Query Embedding & FTS Query:** Generate the embedding for the semantic part of the query. Convert keywords into a `tsquery` (e.g., using `plainto_tsquery('english', keywords)`).
4.  **Database Query:** Execute a SQL query combining vector search and metadata filtering:
    *   **Hybrid Approach:** Retrieve candidates using both vector similarity and FTS matching, then combine and re-rank the results. Reciprocal Rank Fusion (RRF) is a common technique.
    

    ```sql
    WITH relevant_docs AS (
        SELECT id
        FROM documents
        WHERE source_type IN ('pdf', 'image') -- Example filter based on query analysis
          AND created_at >= '2024-01-01'      -- Example filter based on query analysis
        WHERE source_type IN ('pdf', 'image') -- Example filter based on query analysis
          AND created_at >= '2024-01-01'      -- Example filter based on query analysis
          -- AND metadata->>'sender' = 'IRS' -- Example JSONB filter
          -- AND title ILIKE '%IRS%'       -- Example title filter
    )
    , vector_results AS (
      SELECT
          de.id AS embedding_id,
          de.document_id,
          de.embedding <=> $<query_embedding>::vector AS distance,
          ROW_NUMBER() OVER (ORDER BY de.embedding <=> $<query_embedding>::vector ASC) as vec_rank
      FROM document_embeddings de
      WHERE de.document_id IN (SELECT id FROM relevant_docs)
        AND de.embedding_model = $<model_identifier> -- *REQUIRED* filter to use the correct partial index
        AND de.embedding_model = $<model_identifier> -- *REQUIRED* filter to use the correct partial index
        -- AND de.embedding_type IN ('title', 'summary', 'content_chunk') -- Optional filter
      ORDER BY distance ASC
      LIMIT 50 -- Retrieve more candidates for potential re-ranking
    )
    , fts_results AS (
      SELECT
          de.id AS embedding_id,
          de.document_id,
          ts_rank(to_tsvector('english', de.content), plainto_tsquery('english', $<keywords>)) AS score,
          ROW_NUMBER() OVER (ORDER BY ts_rank(to_tsvector('english', de.content), plainto_tsquery('english', $<keywords>)) DESC) as fts_rank
      FROM document_embeddings de
      WHERE de.document_id IN (SELECT id FROM relevant_docs) AND de.content IS NOT NULL
        AND to_tsvector('english', de.content) @@ plainto_tsquery('english', $<keywords>) -- Use expression in query
      ORDER BY score DESC
      LIMIT 50 -- Retrieve more candidates for potential re-ranking
    )
    SELECT
        de.id AS embedding_id,
        de.document_id,
        d.title,
        d.source_type,
        d.created_at,
        de.embedding_type,
        de.content AS embedding_source_content, -- The text that was embedded (if applicable)
        vr.distance,
        vr.vec_rank, -- Include rank for RRF
        fr.score AS fts_score,
        -- Calculate RRF score (k=60 is a common default)
        COALESCE(1.0 / (60 + vr.vec_rank), 0.0) + COALESCE(1.0 / (60 + fr.fts_rank), 0.0) AS rrf_score
    FROM document_embeddings de
    JOIN documents d ON de.document_id = d.id
    LEFT JOIN vector_results vr ON de.id = vr.embedding_id
    LEFT JOIN fts_results fr ON de.id = fr.embedding_id
    WHERE vr.embedding_id IS NOT NULL OR fr.embedding_id IS NOT NULL -- Must appear in at least one result set
    ORDER BY rrf_score DESC -- Order by the combined RRF score
    LIMIT 10; -- Limit results

    ```

    * Replace `<query_embedding>` with the generated query vector and `<model_identifier>` with the correct model name.*
    * Replace `<keywords>` with the keywords extracted for FTS.*
    * Use the appropriate distance operator (`<=>` for cosine, `<->` for L2, `<#>` for inner product) matching the index.*
    * Adjust the RRF formula and `LIMIT` in subqueries as needed.*
    * The query **must filter by `embedding_model`** to allow the query planner to select the correct partial index. The query embedding *must* have the dimension specified in that partial index.
    * Ensure the FTS configuration (`'english'`) in the query matches the one used in the GIN expression index.*

5.  **Result Processing:** The application receives the top N relevant embeddings, their source content (if applicable), and associated document metadata. It might need to de-duplicate results if multiple embeddings from the same document are returned.
6.  **Response Generation:** The LLM uses the retrieved content snippets to synthesize an answer for the user.

## 5. Considerations

*   **Embedding Model Choice:** Select a model appropriate for the task (semantic retrieval) and document types. Consider models supporting the desired languages and text lengths. The schema tracks the model used per chunk.
*   **Dimension Size:** Choose the vector dimension based on the model. Larger dimensions capture more detail but require more storage and potentially more computation.
*   **Index Tuning:** `pgvector` partial index parameters (`m`, `ef_construction` for HNSW; `lists` for IVFFlat) need tuning for each specific index based on dataset size, query latency requirements, and recall accuracy trade-offs.
*   **Re-indexing:** If the embedding model is changed, all existing embeddings will need to be regenerated and the `document_chunks` table updated.
*   **Scalability:** For very large datasets, consider PostgreSQL scaling strategies or potentially dedicated vector databases.
*   **Hybrid Search:** Combining vector and keyword search often yields better results than either alone. Requires careful query construction and result merging (e.g., RRF). Choose an appropriate FTS configuration (language, dictionaries).
*   **OCR Quality:** The quality of OCR significantly impacts the searchability of scanned documents/images.
*   **Variable Vector Dimensions:** Using the `VECTOR` type allows storing embeddings of different dimensions in the same column (requires pgvector >= 0.5.0). Use partial indexes that cast the vector to the specific dimension for each model (`embedding::vector(DIM)`) and filter queries by `embedding_model` to utilize these indexes effectively.
*   **Cost:** Embedding generation (especially using external APIs) and potentially OCR services incur costs.

## 6. Implementation Tasks

*   [x] Define database schema (`documents`, `document_embeddings`). (Committed: 8bd8135)
*   [x] Implement SQLAlchemy models (`DocumentRecord`, `DocumentEmbeddingRecord`). (Committed: 8bd8135)
*   [x] Create skeleton API (`vector_storage.py`) with function signatures. (Committed: 19c5154)
*   [x] Integrate API skeleton into `storage.py` (imports, `init_db` call, `__all__`). (Committed: 19c5154, dadf507)
*   [x] Implement `init_vector_db` to create extension and necessary partial indexes (example for gemini). (Committed: 19c5154)
*   [ ] Implement `add_document` logic (accepts `Document` protocol, handles insert/conflict).
*   [ ] Implement `get_document_by_source_id` logic.
*   [ ] Implement `add_embedding` logic (handle insert/conflict on UNIQUE constraint).
*   [ ] Implement `delete_document` logic.
*   [ ] Implement `query_vectors` logic (hybrid search with RRF).
*   [ ] **API Ingestion Endpoint:**
    *   [ ] Define FastAPI endpoint (`POST /documents/upload`) accepting `multipart/form-data`.
    *   [ ] Implement request validation (metadata, content presence).
    *   [ ] Implement temporary content storage mechanism (initially in task payload or temp DB field, later file-based).
    *   [ ] Implement task enqueuing (`storage.enqueue_task` for `process_uploaded_document`).
*   [ ] **Background Task Processing:**
    *   [ ] Define new task type `process_uploaded_document`.
    *   [ ] Implement task handler (`handle_process_uploaded_document`) in `task_worker.py` or a new indexing module.
    *   [ ] Implement logic to retrieve content (from payload or temp storage).
    *   [ ] Implement initial text processing (use directly).
    *   [ ] Integrate embedding generation (`EmbeddingGenerator`).
    *   [ ] Integrate embedding storage (`storage.add_embedding`).
    *   [ ] Implement temporary storage cleanup.
*   [ ] **Future Enhancements:**
    *   [ ] Implement robust temporary file storage.
    *   [ ] Implement OCR integration for PDFs/images within the task handler.
    *   [ ] Implement email attachment indexing (likely reusing file processing logic).
    *   [ ] Implement document chunking strategy (if needed beyond title/summary/single-content).
*   [ ] Implement LLM-based metadata extraction (calling LLM with JSON mode, potentially within task worker).
*   [x] Implement embedding generation using an LLM/EmbeddingGenerator. (Dependency exists)
*   [x] Integrate querying into the main application flow (e.g., as an LLM tool or background process). (Added `search_documents` tool)
*   [x] Add tool to retrieve full document content by ID (`get_full_document_content`).
*   [ ] Add more partial indexes for other embedding models as they are introduced.
