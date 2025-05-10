## Plan: PDF Document Indexing via API

This plan outlines the steps to enable the indexing of PDF documents (and other file types generically) submitted through the API. It includes robust file type detection using the `filetype` library and considers various text extraction strategies for PDFs, including local libraries, `markitdown`, and the Mistral OCR API.

**Phase 1: API Endpoint Modification for Generic File Upload with `filetype` Detection**

1.  **Modify `src/family_assistant/web/routers/api.py` - `upload_document` endpoint:**
    *   The file parameter will be generic: `uploaded_file: Annotated[UploadFile | None, File(description="The document file to upload (e.g., PDF, TXT, DOCX).")] = None`.
    *   If `uploaded_file` is provided:
        *   Save the uploaded file to a temporary, secure location.
        *   Use the `filetype` library to determine the MIME type:
            *   Read a portion of the temporary file.
            *   `import filetype`
            *   `kind = filetype.guess(temporary_file_path)`
            *   `detected_mime_type = kind.mime if kind else None`
            *   If `kind` or `detected_mime_type` is `None`, establish a fallback (e.g., `uploaded_file.content_type`, a default, or an error).
        *   Store `uploaded_file.filename` as `original_filename`.
    *   The `content_parts_json` parameter remains optional for directly providing text or supplementary metadata.

2.  **Adapt Task Payload in `upload_document`:**
    *   If a file is uploaded, the `task_payload` for the `process_uploaded_document` task will include:
        *   `file_ref: str`: The path to the temporarily stored file.
        *   `mime_type: str`: The MIME type determined by the `filetype` library.
        *   `original_filename: str`: The `uploaded_file.filename`.
    *   If `content_parts_json` is also provided, it will be passed along as well.

**Phase 2: `PDFTextExtractor` Content Processor (Updated for Text Extraction Options)**

1.  **Create `PDFTextExtractor` Content Processor:**
    *   Implement a new class `PDFTextExtractor` adhering to the `ContentProcessor` protocol.
    *   **Dependencies & Text Extraction Strategy:** The processor will choose one or more methods for extracting text from PDFs, potentially trying strategies in a preferred order.
        *   **Option A: Local PDF Libraries (e.g., `PyMuPDF`, `pypdfium2`, `pdfminer.six`)**
            *   Add the chosen library to project dependencies.
            *   The processor uses the library to open the PDF file (referenced by `ref`) and extract text directly.
        *   **Option B: `markitdown` Library**
            *   Add `markitdown` to project dependencies.
            *   The processor uses `markitdown` to convert the PDF file to Markdown. The resulting Markdown is treated as extracted text.
            *   Consider if further processing is needed to strip Markdown syntax if plain text is desired.
        *   **Option C: Mistral OCR API**
            *   This involves an external network call.
            *   The processor would:
                *   Read the PDF file content from `ref`.
                *   Send the file content to the Mistral OCR API.
                *   Requires configuration for API key and endpoint URL.
                *   An HTTP client (e.g., `httpx`) would be needed.
            *   The API response (extracted text) would be used.
    *   **`process` method logic:**
        *   Looks for an `IndexableContent` item in `current_items` with `mime_type == 'application/pdf'` and a valid `ref`.
        *   Applies the chosen text extraction strategy (or a sequence).
        *   For each page/document, creates a new `IndexableContent` item:
            *   `content`: The extracted text (or Markdown).
            *   `embedding_type`: e.g., `pdf_content_chunk` or `content_chunk`.
            *   `mime_type`: `text/plain` (or `text/markdown`).
            *   `source_processor`: `PDFTextExtractor.name`.
            *   `metadata`: Include details like `{'page_number': N, 'original_filename': 'doc.pdf', 'extraction_method': 'chosen_method_identifier'}`.
        *   Returns a list of these new text-based `IndexableContent` items.
        *   Handles extraction failures gracefully (log error, return empty list or original item).

**Phase 3: Pipeline Integration and `DocumentIndexer` Adaptation**

1.  **Update `src/family_assistant/indexing/document_indexer.py` - `DocumentIndexer.process_document`:**
    *   This method creates the initial `IndexableContent` based on the task payload.
    *   If `file_ref` and `mime_type` are in the payload:
        *   Create an initial `IndexableContent` item using the `mime_type` from the payload (derived from `filetype` detection):
            *   `content`: `None`.
            *   `embedding_type`: A generic type like `original_document_file`.
            *   `mime_type`: The `mime_type` from the payload.
            *   `source_processor`: `DocumentIndexer.process_document`.
            *   `ref`: The `file_ref` from the payload.
            *   `metadata`: Include `{'original_filename': payload.get('original_filename')}`.
        *   This item is passed to `self.pipeline.run()`.
    *   Logic for `content_parts` (when no file is uploaded) remains.

2.  **Configure the Indexing Pipeline:**
    *   The `IndexingPipeline` (instantiated in main application setup) must be configured with `PDFTextExtractor` and any other relevant processors. The pipeline routes `IndexableContent` based on its `mime_type`.

**Phase 4: Temporary File Cleanup and Dependencies**

1.  Implement a mechanism to clean up temporary files after processing.
2.  **Add Dependencies:**
    *   Add dependencies for the chosen PDF processing strategy (e.g., `PyMuPDF`, `markitdown`, `httpx`).
    *   Add the `filetype` library to project dependencies.

**Phase 5: Testing**

1.  **Unit Tests:**
    *   Write unit tests for `PDFTextExtractor` using sample PDF files.
    *   If using external APIs (like Mistral OCR), mock the API calls.
    *   Test with both text-based and image-based PDFs (if OCR is a strategy).
2.  **Functional/Integration Tests for `upload_document` API:**
    *   Test uploading a PDF file: verify `filetype` identification, processing by `PDFTextExtractor`, and subsequent creation of text chunks and embedding tasks.
    *   Test uploading other file types identifiable by `filetype` to ensure correct `mime_type` propagation.
    *   Test uploading files with ambiguous/unknown types to check fallback or error handling.
    *   Test uploading with `content_parts_json` only.
    *   Test uploading with both a file and `content_parts_json`.
