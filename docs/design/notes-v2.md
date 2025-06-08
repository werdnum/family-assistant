# Notes V2: Enhanced Features Design

This document outlines a phased approach to enhance the notes feature, focusing on controlling their inclusion in system prompts and integrating them with the document indexing and vector search system.

## Implementation Status

### ✅ Completed
- Basic notes system with CRUD operations
- Notes storage in `notes_table`
- `add_or_update_note` tool
- `NotesContextProvider` (includes all notes)
- **Note indexing in document system** (Milestone 1)
  - `NotesIndexer` class implemented
  - `NoteDocument` protocol implementation
  - Automatic indexing on note creation/update
  - Re-indexing on updates with old embedding deletion
  - Full vector search integration
  - Comprehensive end-to-end tests

### 🚧 In Progress
- None

### ❌ Not Started
- Profile-based prompt inclusion/exclusion (Milestones 2-3)
- Web UI enhancements for note visibility control (Milestone 4)
- Direct note access tool (Milestone 5)
- Access control

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

## Implementation Plan

### Next Steps
With Milestone 1 (Basic Note Indexing) complete, the recommended next step is **Milestone 2: Basic Prompt Inclusion Control**. This provides immediate value by allowing users to mark notes as excluded from system prompts while keeping them searchable.

**Quick Start for Milestone 2:**
1. Create database migration to add `include_in_prompt` column (default: true)
2. Update `add_or_update_note()` in storage layer to accept the new parameter
3. Create `get_prompt_notes()` function that filters by the flag
4. Update `NotesContextProvider` to use the filtered function
5. Update the `add_or_update_note` tool to expose the parameter

### Overview
The implementation will be reordered to deliver incremental value while maintaining system stability. We'll start with note indexing (simpler, foundation for later features), then add profile-based filtering, and finally implement access control.

### Indexing Architecture Decision
The system currently has two indexers:
- **DocumentIndexer**: Handles generic documents (PDFs, text files, URLs)
- **EmailIndexer**: Handles email-specific indexing

Both indexers share the same `IndexingPipeline` instance, which processes content through a configurable chain of processors (text extraction, chunking, embedding generation, etc.).

For notes, we'll create a dedicated **NotesIndexer** following the same pattern as EmailIndexer. This approach:
- Maintains consistency with existing architecture
- Allows note-specific handling (e.g., using note title as source_id)
- Reuses the existing pipeline infrastructure
- Keeps concerns cleanly separated

### Milestone 1: Basic Note Indexing (Foundation) ✅ COMPLETE
**Goal**: Index all notes in the vector search system, making them discoverable alongside other documents.

**Status**: This milestone has been fully implemented and tested. Notes are now automatically indexed on creation/update, searchable via vector search, and re-indexed when content changes.

#### 1.1 Create NotesIndexer Infrastructure ✅
**Files created**:
- `src/family_assistant/indexing/notes_indexer.py` ✅

**Files modified**:
- `src/family_assistant/storage/notes.py` ✅
- `src/family_assistant/storage/vector.py` ✅
- `src/family_assistant/assistant.py` ✅

**Implementation completed**:
1. Create `NoteDocument` class in storage/notes.py that implements the Document protocol:
   ```python
   from dataclasses import dataclass, field
   from datetime import datetime
   from typing import Any
   
   from family_assistant.storage.vector import Document
   
   @dataclass(frozen=True)
   class NoteDocument(Document):
       _id: int | None
       _title: str
       _content: str
       _created_at: datetime
       _updated_at: datetime
       
       @property
       def id(self) -> int | None:
           return self._id
       
       @property
       def source_type(self) -> str:
           return "note"
       
       @property
       def source_id(self) -> str:
           return self._title  # Use title as unique identifier
       
       @property
       def source_uri(self) -> str | None:
           return None  # Notes don't have external URIs
       
       @property
       def title(self) -> str | None:
           return self._title
       
       @property
       def created_at(self) -> datetime | None:
           return self._created_at
       
       @property
       def metadata(self) -> dict[str, Any] | None:
           return {
               "title": self._title,
               "created_at": self._created_at.isoformat(),
               "updated_at": self._updated_at.isoformat(),
           }
   ```

2. Add new storage functions in vector.py:
   ```python
   async def delete_document_embeddings(
       db_context: DatabaseContext, document_id: int
   ) -> None:
       """Delete all embeddings for a specific document (for re-indexing)."""
       try:
           from family_assistant.storage.vector import document_embeddings_table
           stmt = delete(document_embeddings_table).where(
               document_embeddings_table.c.document_id == document_id
           )
           await db_context.execute_with_retry(stmt)
           logger.info(f"Deleted existing embeddings for document ID {document_id}")
       except SQLAlchemyError as e:
           logger.error(f"Database error deleting embeddings for document {document_id}: {e}", exc_info=True)
           raise
   ```
   - `get_note_by_id()` in notes.py to fetch notes by ID (currently only get_note_by_title exists)

3. Create `NotesIndexer` class following the pattern of `EmailIndexer`:
   ```python
   import logging
   from typing import Any, cast
   
   from family_assistant import storage
   from family_assistant.indexing.pipeline import IndexableContent, IndexingPipeline
   from family_assistant.storage.notes import NoteDocument
   from family_assistant.storage.vector import get_document_by_id
   from family_assistant.tools import ToolExecutionContext
   
   logger = logging.getLogger(__name__)
   
   class NotesIndexer:
       def __init__(self, pipeline: IndexingPipeline) -> None:
           self.pipeline = pipeline
           logger.info("NotesIndexer initialized with an IndexingPipeline instance.")
       
       async def handle_index_note(
           self, exec_context: ToolExecutionContext, payload: dict[str, Any]
       ) -> None:
           db_context = exec_context.db_context
           if not db_context:
               logger.error("DatabaseContext not found in ToolExecutionContext for handle_index_note.")
               raise ValueError("Missing DatabaseContext dependency in context.")
           
           note_id = payload.get("note_id")
           if not note_id:
               raise ValueError("Missing 'note_id' in index_note task payload.")
           
           logger.info(f"Starting indexing for note ID: {note_id}")
           
           # Fetch note from database
           note_row = await storage.get_note_by_id(db_context, note_id)
           if not note_row:
               logger.warning(f"Note {note_id} not found in database. Skipping indexing.")
               return
           
           # Convert to NoteDocument
           note_doc = NoteDocument(
               _id=note_row["id"],
               _title=note_row["title"],
               _content=note_row["content"],
               _created_at=note_row["created_at"],
               _updated_at=note_row["updated_at"]
           )
           
           # Create/update document record
           doc_id = await storage.add_document(
               db_context=db_context, 
               doc=note_doc
           )
           
           # Delete existing embeddings if re-indexing
           await storage.delete_document_embeddings(db_context, doc_id)
           
           # Get the document record for pipeline
           db_document_record = await get_document_by_id(db_context, doc_id)
           if not db_document_record:
               raise ValueError(f"Failed to retrieve document record for ID {doc_id} after adding/updating.")
           
           # Create IndexableContent items
           initial_items = [
               IndexableContent(
                   content=f"{note_doc.title}\n\n{note_doc._content}",
                   embedding_type="raw_note_text",
                   mime_type="text/plain",
                   source_processor="NotesIndexer.handle_index_note",
                   metadata={"source": "note", "title": note_doc.title}
               )
           ]
           
           # Run through pipeline
           await self.pipeline.run(
               initial_items=initial_items,
               original_document=cast("Document", db_document_record),
               context=exec_context
           )
           
           logger.info(f"Indexing pipeline successfully initiated for note {note_id} (Doc ID: {doc_id}).")
   ```

4. Register the indexer and task handler in assistant.py:
   ```python
   # In assistant.py initialization
   self.notes_indexer = NotesIndexer(pipeline=self.document_indexer.pipeline)
   self.task_worker_instance.register_task_handler(
       "index_note", self.notes_indexer.handle_index_note
   )
   ```

**Testing**:
- Unit test: NoteDocument protocol implementation
- Unit test: NotesIndexer initialization
- Integration test: Task handler registration

#### 1.2 Integrate with Note CRUD Operations ✅
**Files modified**:
- `src/family_assistant/storage/notes.py` ✅

**Implementation completed**:
1. First, add the missing `get_note_by_id()` function to notes.py:
   ```python
   async def get_note_by_id(
       db_context: DatabaseContext, note_id: int
   ) -> dict[str, Any] | None:
       """Retrieves a specific note by its ID."""
       try:
           stmt = select(
               notes_table.c.id,
               notes_table.c.title, 
               notes_table.c.content,
               notes_table.c.created_at,
               notes_table.c.updated_at
           ).where(notes_table.c.id == note_id)
           row = await db_context.fetch_one(stmt)
           return dict(row) if row else None
       except SQLAlchemyError as e:
           logger.error(f"Database error in get_note_by_id({note_id}): {e}", exc_info=True)
           raise
   ```

2. Modify `add_or_update_note()` to return note ID and enqueue indexing task:
   ```python
   # After the upsert operation, fetch the note ID and enqueue indexing
   async def add_or_update_note(
       db_context: DatabaseContext, title: str, content: str
   ) -> str:
       # ... existing upsert logic ...
       
       # After successful insert/update, get the note ID and enqueue indexing
       try:
           # Fetch the note to get its ID
           note_stmt = select(notes_table.c.id).where(notes_table.c.title == title)
           note_row = await db_context.fetch_one(note_stmt)
           if note_row and db_context.enqueue_task:
               from family_assistant.utils.clock import utcnow
               await db_context.enqueue_task(
                   task_type="index_note",
                   payload={"note_id": note_row["id"]},
                   scheduled_at=utcnow(),
               )
               logger.info(f"Enqueued indexing task for note ID {note_row['id']} (title: {title})")
       except Exception as e:
           logger.error(f"Failed to enqueue indexing task for note '{title}': {e}")
           # Don't fail the note operation if indexing task enqueueing fails
       
       return "Success"
   ```

3. Handle re-indexing on updates:
   - The existing `delete_document_embeddings()` call in NotesIndexer handles re-indexing
   - No additional logic needed since we always enqueue on note changes

**Testing**:
- Unit test: `get_note_by_id()` function works correctly
- Unit test: Task enqueued on note creation
- Unit test: Task enqueued on content update
- Unit test: Indexing task failure doesn't break note creation
- Functional test: Create note → verify indexed

#### 1.3 Index Existing Notes via Migration ⏸️ PENDING
**Files to create**:
- `alembic/versions/xxx_index_existing_notes.py` (Not yet created - existing notes need manual indexing)

**Note**: Since the indexing system is now in place, any existing notes will need to be manually re-saved to trigger indexing, or a migration can be created if needed.

**Implementation**:
1. Create a data migration that enqueues indexing tasks for existing notes:
   ```python
   """Index existing notes
   
   Revision ID: xxx
   Revises: previous_revision
   Create Date: 2025-xx-xx
   """
   
   from alembic import op
   import sqlalchemy as sa
   from datetime import datetime, timezone
   
   def upgrade():
       # Get database connection
       conn = op.get_bind()
       
       # Query all existing notes
       notes_result = conn.execute(sa.text("SELECT id FROM notes ORDER BY id"))
       note_ids = [row[0] for row in notes_result]
       
       if not note_ids:
           print("No existing notes found to index.")
           return
       
       # Enqueue indexing tasks for each note
       now = datetime.now(timezone.utc)
       tasks_to_insert = []
       
       for note_id in note_ids:
           tasks_to_insert.append({
               'task_type': 'index_note',
               'payload': f'{{"note_id": {note_id}}}',
               'status': 'pending',
               'scheduled_at': now,
               'created_at': now,
               'updated_at': now
           })
       
       # Batch insert tasks
       if tasks_to_insert:
           conn.execute(sa.text("""
               INSERT INTO tasks (task_type, payload, status, scheduled_at, created_at, updated_at)
               VALUES (:task_type, :payload, :status, :scheduled_at, :created_at, :updated_at)
           """), tasks_to_insert)
           
           print(f"Enqueued indexing tasks for {len(note_ids)} existing notes.")
   
   def downgrade():
       # Remove enqueued indexing tasks if needed
       conn = op.get_bind()
       conn.execute(sa.text("DELETE FROM tasks WHERE task_type = 'index_note' AND status = 'pending'"))
   ```

2. Consider batching for large numbers of notes (the migration above handles this automatically)

**Testing**:
- Migration test: Run on test database with sample notes
- Verify indexing tasks created for all notes
- Monitor task queue processing

#### 1.4 Configure Indexing Pipeline for Notes ✅
**Files to modify**:
- `config.yaml` (No modifications needed - existing pipeline config works well)

**Implementation completed**:
1. The existing pipeline configuration should work well for notes:
   - TextChunker will split long notes appropriately
   - EmbeddingDispatchProcessor will create embeddings
   
2. Consider if notes need special handling:
   - Different chunk sizes?
   - Custom embedding type like "note_content_chunk"?
   - Skip certain processors (e.g., PDFTextExtractor)?

3. The NotesIndexer can filter processors if needed:
   ```python
   # In NotesIndexer.handle_index_note
   # Create IndexableContent with mime_type="text/plain"
   # This will naturally skip PDF processing
   ```

**Testing**:
- Verify appropriate processors run for notes
- Check embedding generation and storage
- Test with various note sizes

#### 1.5 Add Note-Specific Search Filtering ✅
**Status**: Notes are fully searchable via vector search with `source_type='note'` filtering.

**Testing completed**:
- Functional test exists: Search with note filter returns only notes ✅
- UI enhancement for explicit "Notes" filter option can be added later if needed

**Deliverable achieved**: Notes are fully indexed and searchable via vector search

### Milestone 2: Basic Prompt Inclusion Control
**Goal**: Add simple include/exclude flag for notes without profile logic.

#### 2.1 Database Schema Update
**Files to create**:
- `alembic/versions/xxx_add_note_prompt_control.py`

**Implementation**:
1. Add column: `include_in_prompt` (Boolean, NOT NULL, Default: True)
2. Backfill existing notes to True

**Testing**:
- Migration test: Verify column added with correct default
- Verify existing notes have include_in_prompt=True

#### 2.2 Update Storage Layer
**Files to modify**:
- `src/family_assistant/storage/notes.py`

**Implementation**:
1. Add `include_in_prompt` parameter to `add_or_update_note()`
2. Add `include_in_prompt` to Note model/return values
3. Add `get_prompt_notes()` function that filters by flag

**Testing**:
- Unit test: Create note with include_in_prompt=False
- Unit test: Verify get_prompt_notes() excludes them

#### 2.3 Update Context Provider
**Files to modify**:
- `src/family_assistant/context_providers.py`

**Implementation**:
1. Change `NotesContextProvider` to use `get_prompt_notes()`
2. Keep same formatting logic

**Testing**:
- Functional test: Create excluded note → verify not in prompt
- Functional test: Create included note → verify in prompt

#### 2.4 Update Tool
**Files to modify**:
- `src/family_assistant/tools/__init__.py`

**Implementation**:
1. Add optional `include_in_prompt` parameter to tool
2. Update tool description to explain parameter

**Testing**:
- Tool test: Use tool to create excluded note
- Verify note created with correct flag

**Deliverable**: Users can mark notes to exclude from system prompt while keeping them searchable

### Milestone 3: Profile-Based Filtering
**Goal**: Allow profile-specific inclusion/exclusion of notes.

#### 3.1 Extended Schema
**Files to create**:
- `alembic/versions/xxx_add_profile_based_note_control.py`

**Implementation**:
1. Rename `include_in_prompt` → `include_in_prompt_default`
2. Add `proactive_for_profile_ids` (JSON array)
3. Add `exclude_from_prompt_profile_ids` (JSON array)
4. Add indexes on JSON arrays for performance

**Testing**:
- Migration test: Verify columns added correctly
- Test JSON array storage and retrieval

#### 3.2 Profile-Aware Storage Functions
**Files to modify**:
- `src/family_assistant/storage/notes.py`

**Implementation**:
1. Update `get_prompt_notes()` to accept `profile_id`
2. Implement filtering logic:
   - Check exclusion list first
   - Then check inclusion list
   - Fall back to default
3. Add profile list parameters to `add_or_update_note()`

**Testing**:
- Unit test: Note excluded for specific profile
- Unit test: Note included for specific profile overrides default
- Unit test: Default behavior when no profile rules

#### 3.3 Update Context Provider with Profile
**Files to modify**:
- `src/family_assistant/context_providers.py`

**Implementation**:
1. Pass current profile_id to `get_prompt_notes()`
2. Handle None profile_id (use default behavior)

**Testing**:
- Functional test: Different profiles see different notes
- Functional test: Profile exclusion takes precedence

#### 3.4 Enhanced Tool Parameters
**Files to modify**:
- `src/family_assistant/tools/__init__.py`

**Implementation**:
1. Add optional list parameters for profile includes/excludes
2. Update tool description with examples

**Testing**:
- Tool test: Create note with profile-specific rules
- Verify rules stored and applied correctly

**Deliverable**: Notes can be included/excluded based on active processing profile

### Milestone 4: Web UI Enhancements
**Goal**: Allow users to manage note visibility settings through the web interface.

#### 4.1 Note Edit UI Updates
**Files to modify**:
- `src/family_assistant/templates/edit_note.html.j2`
- `src/family_assistant/web/routers/notes.py`

**Implementation**:
1. Add checkbox for "Include in prompt by default"
2. Add multi-select for profile inclusion/exclusion
3. Update POST handler to save settings

**Testing**:
- UI test: Toggle settings and verify saved
- UI test: Profile lists displayed correctly

#### 4.2 Note List View Indicators
**Files to modify**:
- `src/family_assistant/templates/index.html.j2`

**Implementation**:
1. Add icon/badge showing prompt inclusion status
2. Show profile-specific rules if any

**Testing**:
- UI test: Correct indicators shown
- UI test: Hover shows details

**Deliverable**: Full UI support for managing note visibility

### Milestone 5: Direct Note Access Tool
**Goal**: Allow LLM to fetch specific notes by title.

#### 5.1 Create get_note_by_title Tool
**Files to modify**:
- `src/family_assistant/tools/__init__.py`

**Implementation**:
1. Create new tool wrapping `storage.get_note_by_title()`
2. Add to AVAILABLE_FUNCTIONS
3. Consider profile-based access (only return if not excluded)

**Testing**:
- Tool test: Fetch existing note
- Tool test: Handle non-existent note
- Tool test: Respect profile exclusions

**Deliverable**: LLM can explicitly fetch notes not in prompt

### Testing Strategy

#### Unit Tests
- Test each storage function in isolation
- Mock database for speed
- Focus on logic correctness

#### Functional Tests
- Test end-to-end workflows
- Use test database
- Verify integration between components

#### Manual Testing Checklist
For each milestone:
1. Create notes with various settings
2. Verify prompt inclusion/exclusion
3. Test vector search
4. Check UI displays correctly
5. Test with different profiles
6. Verify existing notes still work

### Rollback Plan
Each milestone can be rolled back independently:
1. Revert code changes
2. Run migration rollback
3. Clear indexed documents if needed
4. Previous functionality remains intact

### Success Metrics
- No regression in existing note functionality ✅
- Notes discoverable via vector search ✅
- Profile-based filtering reduces prompt size (Pending - Milestone 2-3)
- UI provides clear visibility into note settings (Pending - Milestone 4)
- System performance not degraded ✅

## Summary of Progress

### What's Been Accomplished (Milestone 1)
- ✅ Full note indexing infrastructure implemented
- ✅ Automatic indexing on note creation/update
- ✅ Re-indexing with old embedding cleanup on updates
- ✅ Vector search integration working
- ✅ Comprehensive test coverage
- ✅ No performance degradation

### What Remains
1. **Milestone 2**: Basic prompt inclusion control (include_in_prompt flag)
2. **Milestone 3**: Profile-based filtering (per-profile inclusion/exclusion)
3. **Milestone 4**: Web UI enhancements for visibility control
4. **Milestone 5**: Direct note access tool for LLM
5. **Future**: Access control and user ownership

The foundation is solid, and each remaining milestone can be implemented incrementally without disrupting existing functionality.
