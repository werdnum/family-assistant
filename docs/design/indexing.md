# Document Indexing Pipeline Design

## 1. Goals

*   Provide a flexible and extensible system for processing diverse document types (emails, PDFs, images, notes, web pages, etc.) before embedding their content for vector search.
*   Support extraction of various content representations (raw text, OCR text, summaries, titles, potentially image captions).
*   Enable progressive indexing, where different parts of a document become searchable as soon as they are processed and embedded.
*   Allow for configuration and customization of the processing steps for different needs or document types.
*   Integrate seamlessly with the existing vector storage schema (`documents` and `document_embeddings` tables) as defined in `vector_search.md`.

## 2. Core Concept: Asynchronous Indexing Pipeline

The core idea is an **Indexing Pipeline** composed of sequential **Content Processors**. When a document is ingested (e.g., via email, API upload), its initial content representation is fed into the pipeline. Each processor in the pipeline examines the content items passed to it, potentially transforms them, extracts new information, or generates new content representations.

Crucially, processors can identify when a piece of content is "ready for embedding" (e.g., a title, a summary, a final text chunk). When content is ready, the processor **dispatches an asynchronous background task** (`embed_and_store_batch`) to handle the embedding generation and storage for that specific piece (or batch) of content. The processor then passes any remaining or newly generated content that requires *further* processing to the next stage in the pipeline.

This allows for:
*   **Parallelism:** Multiple embedding tasks can run concurrently.
*   **Progressive Indexing:** Document parts become searchable incrementally.
*   **Decoupling:** Content processing logic is separated from embedding logic.

## 3. Key Components

### 3.1. `IndexableContent` Dataclass

Represents a unit of data flowing through the pipeline, potentially ready for embedding.

*   `content: Optional[str]`: The textual content (or None for binary references).
*   `embedding_type: str`: Defines *what* the content represents (e.g., 'title', 'summary', 'content_chunk', 'ocr_chunk'). Maps directly to `document_embeddings.embedding_type`. **Standardized types:** `title`, `summary`, `content_chunk` (primary text), `ocr_chunk` (OCR), `fetched_chunk` (web), `image_caption`.
*   `mime_type: Optional[str]`: MIME type of the `content` (e.g., 'text/plain', 'image/jpeg', 'application/pdf'). Used by processors to determine applicability.
*   `source_processor: str`: Name of the processor that generated this item.
*   `metadata: Dict[str, Any]`: Processor-specific details (e.g., `{'page_number': 3}`, `{'source_url': '...'}`). This metadata should be stored alongside the embedding.
*   `ref: Optional[str]`: Reference to original binary data if `content` is None (e.g., temporary file path).

### 3.2. `ContentProcessor` Protocol (Interface)

Defines the contract for pipeline stages.

*   `name: str` (Property): Unique identifier.
*   `async process(self, current_items: List[IndexableContent], original_document: Document, initial_content_ref: IndexableContent, context: ToolExecutionContext) -> List[IndexableContent]`:
    *   Receives:
        *   `current_items`: Content items from the previous stage.
        *   `original_document`: The `Document` object representing the source item (for context like title, source type).
        *   `initial_content_ref`: The very first `IndexableContent` item created for the document (useful for accessing the original `ref` to binary data).
        *   `context`: Execution context providing `db_context` and `enqueue_task`.
    *   Actions:
        *   Iterates through `current_items`.
        *   Processes applicable items based on `embedding_type`, `mime_type`, etc.
        *   Generates new `IndexableContent` items.
        *   Accumulates items ready for embedding.
        *   Uses `context.enqueue_task("embed_and_store_batch", ...)` to dispatch batches of ready items.
    *   Returns: A list of `IndexableContent` items that need further processing by subsequent stages.

### 3.3. `IndexingPipeline` Class

Orchestrates the flow.

*   `__init__(self, processors: List[ContentProcessor], config: Dict[str, Any])`: Takes the ordered list of processors and their configurations.
*   `async run(self, initial_content: IndexableContent, original_document: Document, context: ToolExecutionContext)`:
    *   Passes the `initial_content` through each configured `processor.process` method sequentially, along with context.
    *   The final output of `run` is less important, as the primary outcome is the dispatching of embedding tasks by the processors themselves.

### 3.4. `embed_and_store_batch` Task

A background task responsible for embedding and storing dispatched content.

*   **Handler:** `handle_embed_and_store_batch`
*   **Payload:**
    *   `document_id: int`
    *   `texts_to_embed: List[str]`
    *   `embedding_metadata_list: List[Dict[str, Any]]` (Each dict: `{ 'embedding_type': str, 'chunk_index': int, 'original_content_metadata': dict }`)
*   **Action:**
    1.  Gets the `EmbeddingGenerator`.
    2.  Calls `embedding_generator.generate_embeddings(texts_to_embed)`.
    3.  Iterates through results and `embedding_metadata_list`.
    4.  Calls `storage.add_embedding()` for each, passing the vector, `document_id`, `embedding_type`, `chunk_index`, `content` (the original text), and `original_content_metadata`.

## 4. Example Processors

*   **`TitleExtractor`:** Extracts `original_document.title`. Dispatches immediately.
*   **`AttachmentProcessor`:** Identifies attachments (e.g., from email), creates `IndexableContent` with `mime_type` and `ref` to binary data.
*   **`LinkExtractor`:** Extracts URLs from text content. Creates `extracted_link` items (configurable depth).
*   **`PDFTextExtractor`:** Extracts text directly from PDFs (if not scanned). Creates `extracted_text` items.
*   **`OCRExtractor`:** Performs OCR on images or scanned PDFs (using `ref`). Creates `ocr_text` items, potentially one per page with `page_number` in metadata.
*   **`WebFetcher`:** Fetches content for `extracted_link` or `raw_url` items. Creates `fetched_content` items.
*   **`HTMLCleaner`:** Cleans HTML content from `fetched_content`. Creates plain text items.
*   **`LLMSummarizer`:** Generates summaries for text-based items (`raw_text`, `ocr_text`, `fetched_content`). Creates `summary` items. Dispatches immediately.
*   **`TextChunker`:** Splits long text items (`raw_text`, `ocr_text`, `fetched_content`) into smaller chunks. Creates `content_chunk`, `ocr_chunk`, or `fetched_chunk` items. Dispatches batches of chunks. Handles short content by dispatching it as a single chunk.
*   **`ImageCaptioner` (Future):** Generates descriptive captions for images. Creates `image_caption` items. Dispatches immediately.

## 5. Example Pipelines (Pseudocode Flow)

*(See previous discussion for detailed pseudocode examples for Email+Attachment, Scanned PDF, Web Page, Image Flyer)*

The key flow is:
1.  Initial `IndexableContent` created (e.g., email body text, PDF file reference).
2.  Pipeline runs processors sequentially.
3.  `TitleExtractor` dispatches title embedding task.
4.  Processors for attachments/links create reference items.
5.  Processors like `PDFTextExtractor`/`OCRExtractor`/`WebFetcher` convert references/URLs to text items.
6.  `LLMSummarizer` processes text items, dispatches summary embedding task.
7.  `TextChunker` processes remaining long text items, dispatches chunk embedding tasks.
8.  Pipeline completes. Embedding tasks run concurrently in the background.

## 6. Database Schema Changes

*   **`document_embeddings` Table:** Needs a new column to store processor-generated metadata associated with each embedding.
    ```sql
    ALTER TABLE document_embeddings
    ADD COLUMN metadata JSONB;

    -- Optional: Index for querying metadata if needed later
    -- CREATE INDEX idx_doc_embeddings_metadata ON document_embeddings USING GIN (metadata);
    ```
*   The `storage.add_embedding` function needs to accept and store this `metadata` dictionary.

## 7. Design Considerations & Refinements

*   **Access to Original Data:** Processors needing binary data (OCR, Captioning) should access it via the `ref` in the `initial_content_ref` passed to their `process` method.
*   **Metadata Propagation:** Processors must diligently copy relevant metadata from input items to the `metadata` field of their output `IndexableContent`. This metadata is then passed to the `embed_and_store_batch` task and stored in the new `document_embeddings.metadata` column.
*   **Configuration:** The `IndexingPipeline` needs a mechanism to pass configuration dictionaries to individual processors (e.g., chunk size, LLM model for summarizer, link depth).
*   **Error Handling:** Processors should log errors. Critical failures (e.g., OCR on primary content) might need to raise exceptions to fail the originating task. Non-critical failures (e.g., fetching a dead link) should be logged but allow the pipeline to continue. The `embed_and_store_batch` task needs robust error handling for embedding generation or database storage failures.
*   **Standardized Embedding Types:** Use a consistent set: `title`, `summary`, `content_chunk`, `ocr_chunk`, `fetched_chunk`, `image_caption`. Store specifics (page number, source URL) in the embedding's `metadata`.

## 8. Implementation Plan (Incremental)

1.  **Schema Change:** Add the `metadata` column to `document_embeddings`. Update `storage.add_embedding`.
2.  **Core Components:** Implement `IndexableContent`, `ContentProcessor` protocol, `IndexingPipeline`, `embed_and_store_batch` task type and handler.
3.  **Basic Pipeline:** Implement `TitleExtractor`, `TextChunker`. Modify existing indexers (`DocumentIndexer`, `EmailIndexer`) to use the pipeline, initially collecting all results at the end and dispatching one batch task.
4.  **Asynchronous Dispatch:** Refactor processors to dispatch their own batches using `context.enqueue_task`. Remove final collection step from pipeline runner.
5.  **Add Processors:** Incrementally add more processors (OCR, Summarizer, Web Fetch, etc.), ensuring they follow the dispatch pattern and handle metadata correctly.
6.  **Configuration:** Implement configuration loading and passing for processors.
7.  **Error Handling:** Implement robust logging and error handling strategies.
