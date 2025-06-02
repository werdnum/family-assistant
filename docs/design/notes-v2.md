# Notes V2: Enhanced Features Design

This document outlines a phased approach to enhance the notes feature, focusing on controlling their inclusion in system prompts and integrating them with the document indexing and vector search system.

## Phase 1: Proactive/Reactive Switch for Notes

This phase introduces mechanisms to control whether a note's content is proactively included in an LLM's system prompt or if it's reactively available (i.e., fetchable by tools but not in the default prompt context).

### 1.1. Schema Changes

The `notes_table` will be augmented with the following columns:

*   `include_in_prompt_default` (Boolean, NOT NULL, Default: `True`):
    *   If `True`, the note is generally considered for proactive inclusion in system prompts.
    *   If `False`, the note is generally considered reactive (not in prompts by default).
*   `proactive_for_profile_ids` (Text or Array type, Nullable, Indexed):
    *   A list of processing profile IDs (e.g., `["research", "k8s_debug"]`).
    *   If the current profile ID is in this list, this note *will be* included in the system prompt, overriding `include_in_prompt_default`.
*   `exclude_from_prompt_profile_ids` (Text or Array type, Nullable, Indexed):
    *   A list of processing profile IDs.
    *   If the current profile ID is in this list, this note *will not be* included in the system prompt. This exclusion takes precedence over `include_in_prompt_default` and `proactive_for_profile_ids`.

**Migration Considerations:**
*   Existing notes will need `include_in_prompt_default` backfilled (e.g., to `True` to maintain current behavior).
*   `proactive_for_profile_ids` and `exclude_from_prompt_profile_ids` will default to `NULL` or an empty list/array.

### 1.2. Logic for `NotesContextProvider`

The `NotesContextProvider` will be updated to determine if a note should be included in the system prompt for the current `profile_id`. For each note, the logic is as follows:

1.  **Check Exclusion:** If `current_profile_id` is present in the note's `exclude_from_prompt_profile_ids` list, the note is **Reactive** (not included in the prompt).
2.  **Check Profile-Specific Proactive Inclusion:** Else, if `current_profile_id` is present in the note's `proactive_for_profile_ids` list, the note is **Proactive** (included in the prompt).
3.  **Check Default Behavior:** Else (no specific profile rule applies):
    *   If `note.include_in_prompt_default` is `True`, the note is **Proactive**.
    *   Otherwise (`note.include_in_prompt_default` is `False`), the note is **Reactive**.

### 1.3. Tooling Updates

*   **`add_or_update_note` Tool:**
    *   The existing `add_or_update_note` tool (implemented in `src/family_assistant/tools/__init__.py` and using `src/family_assistant/storage/notes.py`) will be modified.
    *   It will need to accept new optional parameters:
        *   `include_in_prompt_default` (boolean)
        *   `proactive_for_profile_ids` (list of strings)
        *   `exclude_from_prompt_profile_ids` (list of strings)
    *   When a note is created or updated, these values will be stored in the `notes_table`.

## Phase 2: Note Discovery and Indexing

Once notes can be "reactive" (not in the system prompt), mechanisms for discovering and accessing them become crucial. This involves integrating notes into the existing document indexing and vector search subsystem.

### 2.1. Indexing Notes

When a note is added or its content (or its prompt-related attributes relevant to search) is updated via `add_or_update_note`:

1.  **Adapt Note to `Document` Protocol:**
    *   The note's data (title, content, new prompt-related attributes) will be adapted to conform to the `Document` protocol (defined in `src/family_assistant/storage/vector.py`).
    *   `source_type` will be `"note"`.
    *   `source_id` could be the note's unique title or its database ID.
    *   The new prompt-related attributes (`include_in_prompt_default`, `proactive_for_profile_ids`, `exclude_from_prompt_profile_ids`) should be stored as key-value pairs within the `metadata` JSONB field of the `documents` record (e.g., `{"note_include_in_prompt_default": true, "note_proactive_profiles": ["research"], ...}`).

2.  **Create/Update `documents` Record:**
    *   Call `storage.add_document()` to create or update the corresponding record in the `documents` table. This function handles upsert logic based on `source_type` and `source_id` and returns the `document_id`.

3.  **Re-indexing Modified Notes (Handling Content Changes):**
    *   If the note's content or any attributes that affect its indexed representation have changed, any existing embeddings for this `document_id` must be removed from the `document_embeddings` table before new ones are generated.
    *   This requires a new storage function: `async def delete_document_embeddings(db_context: DatabaseContext, document_id: int)` in `src/family_assistant/storage/vector.py`. This function will execute a `DELETE` statement on `document_embeddings` for the given `document_id`.
    *   This deletion step ensures that stale embeddings are removed.

4.  **Prepare `IndexableContent`:**
    *   Create an `IndexableContent` object (from `src/family_assistant/indexing/pipeline.py`) using the note's current content.
    *   `embedding_type` could be `"note_content_chunk"` or a generic `"content_chunk"`.
    *   `mime_type` will be `"text/plain"`.
    *   `metadata` within `IndexableContent` can include the note's title.

5.  **Enqueue Indexing Task:**
    *   Enqueue a `process_document` task (handled by `DocumentIndexer.process_document` as outlined in `docs/design/indexing.md`).
    *   The task payload will include the `document_id` and the `IndexableContent` (serialized) as `initial_content_parts`.
    *   The `IndexingPipeline` will then process this content, leading to the generation and storage of new embeddings.

### 2.2. Discovery Tools for Reactive Notes

1.  **Vector Search (`search_documents_tool`):**
    *   Users or the LLM can use the existing `search_documents_tool` to find notes.
    *   The search query can be augmented to filter by `source_type = 'note'`.
    *   The ACLs (once implemented in Phase 3+) for user and profile access will be applied at the `documents` table level during the search, ensuring only accessible notes are returned.
    *   The metadata stored in `documents.metadata` (like `note_include_in_prompt_default`) could potentially be used for finer-grained filtering if required by specific use cases, though the primary access control will be through dedicated ACL fields.

2.  **Direct Retrieval (`get_full_document_content_tool`):**
    *   If a note's `document_id` is known (e.g., from search results), its full content can be retrieved using `get_full_document_content_tool`.

3.  **Direct Note Access (`get_note_by_title`):**
    *   The existing `storage.get_note_by_title` function can still be used for direct lookups.
    *   If exposed as an LLM tool, this tool must be enhanced to respect the access rules: it should only return notes that are at least "Reactive" for the current user and profile context (i.e., not excluded and matching any ownership or profile list criteria once those are implemented).

## Phase 3: Access Control (Future Enhancement)

While not part of the initial implementation, the design should anticipate future access control requirements:

*   **User Ownership:** Add `owner_user_identifier` to `notes_table` and `doc_owner_user_identifier` to `documents` table.
*   **Profile-Based ACLs:** The `proactive_for_profile_ids` and `exclude_from_prompt_profile_ids` (or more general `allowed_profile_ids`) fields will form the basis of profile-level access.
*   These ACLs will be enforced in:
    *   `NotesContextProvider`.
    *   Direct note retrieval tools.
    *   The `query_vector_store` function by filtering `documents` records.

This phased approach allows for incremental delivery of functionality, starting with the ability to manage system prompt content more granularly, followed by robust search and retrieval for all notes.
