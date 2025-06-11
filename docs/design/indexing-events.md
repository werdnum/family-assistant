# Indexing Events Design

## Overview

This document describes the design for adding document indexing events to the event listener system. The goal is to enable users to create event listeners that trigger when documents complete indexing, allowing automations like "Tell me when the school newsletter is indexed" or "Alert me if any document fails to index."

## Key Insights

After careful analysis, we can achieve reliable completion detection without modifying the documents table or the existing pipeline architecture:

1. **Leverage Existing Task Queue**: Document IDs are already in task payloads - we can query the tasks table to check completion
2. **Check Only After Embedding Tasks**: To avoid race conditions, only check for completion when embedding tasks finish
3. **No Database Changes Needed**: No new tables or columns required
4. **Simple and Reliable**: Minimal code changes with maximum reliability

## Design Principles

1. **Preserve Existing Architecture**: Add completion tracking without disrupting the fire-and-forget nature of the pipeline
2. **Avoid Race Conditions**: Never check for completion until we know indexing has occurred
3. **Minimal Performance Impact**: Use existing data structures and indexes
4. **Reliable Events**: Ensure events are emitted exactly once per document
5. **Graceful Degradation**: System continues to function even if event emission fails

## Proposed Solution

### 1. Completion Detection Strategy

Check for document completion ONLY after embedding tasks complete. This avoids race conditions where the pipeline hasn't yet created tasks.

```python
# In handle_embed_and_store_batch, after successful embedding storage:

# Check if this was the last task for this document
from sqlalchemy import select, and_, or_, func

# Query for any remaining tasks (both indexing and embedding) for this document
remaining_tasks = await db_context.execute(
    select(func.count())
    .select_from(tasks_table)
    .where(
        and_(
            tasks_table.c.task_type.in_([
                'index_document', 'index_email', 'index_note', 'embed_and_store_batch'
            ]),
            func.json_extract(tasks_table.c.payload, '$.document_id') == str(document_id),
            tasks_table.c.status.in_(['pending', 'locked'])
        )
    )
)
pending_count = remaining_tasks.scalar()

if pending_count == 0:
    # All tasks complete - emit document ready event
    await emit_document_ready_event(exec_context, document_id)
```

### 2. Event Source Implementation

Create `IndexingSource` with these event types:

```python
class IndexingEventType(str, Enum):
    """Types of indexing events."""
    DOCUMENT_READY = "document_ready"          # All tasks complete
    INDEXING_FAILED = "indexing_failed"        # Fatal error in processing
```

### 3. Integration Points

#### 3.1 Task Completion Handler
Modify `handle_embed_and_store_batch` to check for completion after storing embeddings:

```python
async def handle_embed_and_store_batch(
    exec_context: ToolExecutionContext,
    payload: dict[str, Any],
) -> None:
    # ... existing embedding storage logic ...
    
    # After successful storage, check if all tasks are complete
    if indexing_source := getattr(exec_context, 'indexing_source', None):
        await check_and_emit_completion(
            exec_context, 
            document_id, 
            indexing_source
        )
```

#### 3.2 Error Handling
On task failures, emit `INDEXING_FAILED` events if this was a critical failure:

```python
# In task error handlers
if indexing_source and is_critical_failure:
    await indexing_source.emit_event({
        "event_type": IndexingEventType.INDEXING_FAILED,
        "document_id": document_id,
        "error_message": str(error),
        # ... additional context
    })
```

### 4. Handling Edge Cases

#### 4.1 Documents with No Embeddings
Some documents might not generate any embeddings (empty files, errors). These won't trigger completion checks since no embedding tasks are created.

Solution: This is acceptable - if no embeddings are created, the document isn't truly "indexed" and shouldn't emit DOCUMENT_READY.

#### 4.2 Concurrent Processing
Multiple indexing pipelines could process the same document simultaneously.

Solution: The completion check handles this naturally - it only emits when ALL tasks are done.

#### 4.3 Task Failures
Individual embedding tasks might fail while others succeed.

Solution: Failed tasks move to 'failed' status, so they won't block completion detection. Consider emitting DOCUMENT_READY with metadata about partial success.

### 5. Implementation Steps

1. **Create IndexingSource class** implementing the event source interface
2. **Wire IndexingSource into ToolExecutionContext**
3. **Add completion check** to `handle_embed_and_store_batch`
4. **Add helper function** for the completion check logic
5. **Handle errors** by emitting INDEXING_FAILED events
6. **Write tests** for the event emission logic
7. **Optional: Add performance index** for document_id in task payloads

### 6. Example Event Flows

#### Successful PDF Indexing
```
1. PDF uploaded → index_document task created
2. Pipeline runs: TitleExtractor, PDFTextExtractor, TextChunker
3. EmbeddingDispatch → 6 embed_and_store_batch tasks (1 title + 5 chunks)
4. Each embedding task completes → checks for remaining tasks
5. Last task finds no remaining tasks → DOCUMENT_READY event
```

#### Email with Attachments
```
1. Email received → index_email task created
2. Email body → 1 embedding task
3. PDF attachment → Separate document with own index_document task
4. Each document completes independently
5. Each emits own DOCUMENT_READY when all its tasks finish
```

### 7. Event Listener Examples

```python
# Newsletter ready notification
{
    "source_id": "indexing",
    "match_conditions": {
        "event_type": "document_ready",
        "document_type": "email",
        "metadata.sender": "newsletter@school.edu"
    },
    "action_type": "wake_llm",
    "action_config": {
        "prompt": "School newsletter indexed. Please summarize it."
    }
}

# Failed document alert
{
    "source_id": "indexing",
    "match_conditions": {
        "event_type": "indexing_failed"
    },
    "action_type": "wake_llm",
    "action_config": {
        "prompt": "Document indexing failed. Notify user with error details."
    }
}
```

### 8. Performance Considerations

1. **Database Impact**: 
   - Uses existing tasks table - no new tables needed
   - JSON queries with SQLAlchemy's `func.json_extract()`
   - Consider adding functional index for better performance

2. **Completion Check Optimization**:
   ```python
   # SQLAlchemy query for checking completion
   from sqlalchemy import select, and_, func
   
   result = await db_context.execute(
       select(func.count())
       .select_from(tasks_table)
       .where(
           and_(
               tasks_table.c.task_type.in_(['index_document', 'index_email', 
                                           'index_note', 'embed_and_store_batch']),
               func.json_extract(tasks_table.c.payload, '$.document_id') == str(document_id),
               tasks_table.c.status.in_(['pending', 'locked'])
           )
       )
   )
   ```

3. **Optional Performance Index**:
   ```sql
   -- For PostgreSQL
   CREATE INDEX idx_tasks_doc_id ON tasks ((payload->>'document_id'));
   
   -- For SQLite
   CREATE INDEX idx_tasks_doc_id ON tasks (json_extract(payload, '$.document_id'));
   ```

### 9. Advantages of This Approach

1. **Simplicity**: No database schema changes required
2. **Reliability**: Avoids race conditions by checking only after embedding tasks
3. **Minimal Changes**: Only modifies the embedding task handler
4. **Database Agnostic**: Works with both PostgreSQL and SQLite
5. **Backwards Compatible**: Existing documents continue to work normally

## Conclusion

This design adds reliable completion tracking to the indexing pipeline by leveraging the existing task queue infrastructure. By querying the tasks table for pending embedding tasks, we can detect when a document completes indexing without adding new tracking tables or modifying the pipeline architecture. This simpler approach reduces complexity while still providing users with powerful automation capabilities through the event listener system.