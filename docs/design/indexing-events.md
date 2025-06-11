# Indexing Events Design

## Overview

This document describes the design for adding document indexing events to the event listener system. The goal is to enable users to create event listeners that trigger when documents complete indexing, allowing automations like "Tell me when the school newsletter is indexed" or "Alert me if any document fails to index."

## Key Challenges

Based on analysis of the existing indexing pipeline:

1. **No Completion Tracking**: The current system uses a fire-and-forget approach with no mechanism to track when all indexing tasks for a document complete
2. **Dynamic Task Creation**: The number of embedding tasks varies based on document content (unknown number of chunks until processing)
3. **Asynchronous Processing**: Multiple embedding tasks run concurrently with no coordination
4. **Separation of Concerns**: The pipeline architecture (Paradigm B) deliberately separates content processing from embedding dispatch

## Design Principles

1. **Preserve Existing Architecture**: Add completion tracking without disrupting the fire-and-forget nature of the pipeline
2. **Progressive Enhancement**: Documents remain searchable as soon as individual embeddings complete
3. **Minimal Performance Impact**: Tracking should not significantly slow down indexing
4. **Reliable Events**: Ensure events are emitted exactly once per document
5. **Graceful Degradation**: System continues to function even if event emission fails

## Proposed Solution

### 1. Leverage Existing Task Queue

Since the document ID is already in the task payload, we can query the tasks table directly to check completion status. This avoids creating a new tracking table.

```sql
-- Add minimal status fields to documents table
ALTER TABLE documents 
ADD COLUMN indexing_started_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN indexing_completed_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN indexing_status VARCHAR(50) DEFAULT 'pending';
-- Status values: pending, processing, completed, failed
```

We can check completion with a query like:
```sql
-- Check if any embed_and_store_batch tasks for this document are incomplete
SELECT COUNT(*) as pending_tasks
FROM tasks 
WHERE task_type = 'embed_and_store_batch' 
  AND payload->>'document_id' = ?
  AND status IN ('pending', 'locked');
```

### 2. Track Document Processing State

When indexing starts, update the document status:

```python
# In document_indexer.py, when processing begins
await context.db_context.execute(
    """UPDATE documents 
       SET indexing_status = 'processing',
           indexing_started_at = CURRENT_TIMESTAMP
       WHERE id = ?""",
    [document_id]
)

# Emit indexing started event
if indexing_source := getattr(context, 'indexing_source', None):
    await indexing_source.emit_event({
        "event_type": "indexing_started",
        "document_id": document_id,
        # ... other event data
    })
```

No changes needed to `EmbeddingDispatchProcessor` - it already includes document_id in the task payload.

### 3. Completion Detection

After each embedding task completes, check if all tasks are done by querying the tasks table:

```python
# In handle_embed_and_store_batch, after successful embedding storage:

# Check if this was the last task
result = await db_context.execute(
    """SELECT COUNT(*) as pending_count
       FROM tasks 
       WHERE task_type = 'embed_and_store_batch' 
         AND payload->>'document_id' = ?
         AND status IN ('pending', 'locked')""",
    [str(document_id)]
)
pending_count = result.scalar()

if pending_count == 0:
    # All tasks complete - update document status
    await db_context.execute(
        """UPDATE documents 
           SET indexing_status = 'completed',
               indexing_completed_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        [document_id]
    )
    
    # Emit document ready event
    if indexing_source := getattr(exec_context, 'indexing_source', None):
        doc_info = await get_document_by_id(db_context, document_id)
        
        # Get indexing metrics
        metrics_result = await db_context.execute(
            """SELECT COUNT(*) as total_embeddings,
                      COUNT(DISTINCT embedding_type) as embedding_types
               FROM document_embeddings
               WHERE document_id = ?""",
            [document_id]
        )
        metrics = dict(metrics_result.fetchone())
        
        await indexing_source.emit_event({
            "event_type": "document_ready",
            "document_id": document_id,
            "document_type": doc_info.source_type,
            "document_title": doc_info.title,
            "metadata": {
                "total_embeddings": metrics['total_embeddings'],
                "embedding_types": metrics['embedding_types'],
                "indexing_duration_seconds": (
                    doc_info.indexing_completed_at - doc_info.indexing_started_at
                ).total_seconds() if doc_info.indexing_started_at else None
            }
        })
```

### 4. Event Source Implementation

Create `IndexingSource` as outlined in the original plan, with these event types:

```python
class IndexingEventType(str, Enum):
    """Types of indexing events."""
    INDEXING_STARTED = "indexing_started"      # Pipeline begins
    INDEXING_FAILED = "indexing_failed"        # Fatal error in pipeline
    EMBEDDING_STARTED = "embedding_started"     # Task dispatched
    EMBEDDING_COMPLETED = "embedding_completed" # Task completed
    DOCUMENT_READY = "document_ready"          # All tasks complete
```

### 5. Pipeline Integration Points

#### 5.1 Document Creation
When a document is created for indexing, emit `INDEXING_STARTED`:

```python
# In document_indexer.py, email_indexer.py, notes_indexer.py
await update_document_indexing_status(
    db_context,
    document_id=document_id,
    status="processing",
    started_at=datetime.now(timezone.utc)
)

if indexing_source:
    await indexing_source.emit_event({
        "event_type": IndexingEventType.INDEXING_STARTED,
        # ... event data
    })
```

#### 5.2 Task Dispatch
No changes needed - `EmbeddingDispatchProcessor` already includes document_id in the task payload.

#### 5.3 Task Completion
When `handle_embed_and_store_batch` completes, check for document completion.

#### 5.4 Error Handling
On pipeline or task failures, emit `INDEXING_FAILED` events.

### 6. Handling Edge Cases

#### 6.1 Pipeline with No Embeddings
Some documents might not generate any embeddings:
- Empty documents
- Unsupported file types
- Processing errors

Solution: Track pipeline completion separately from embedding completion.

#### 6.2 Partial Failures
Some embeddings might succeed while others fail.

Solution: 
- Continue processing remaining tasks
- Mark document as "completed_with_errors"
- Include error details in completion event

#### 6.3 Orphaned Tasks
Tasks might fail to complete or get lost.

Solution:
- Periodic cleanup job to detect stuck documents
- Timeout-based completion (e.g., if no activity for 30 minutes)

### 7. Implementation Phases

#### Phase 1: Basic Infrastructure
1. Add status fields to documents table via migration
2. Implement `IndexingSource` 
3. Wire `IndexingSource` into `ToolExecutionContext`
4. Add helper functions for querying task completion

#### Phase 2: Event Emission
1. Add `INDEXING_STARTED` events to indexers
2. Implement completion detection in `handle_embed_and_store_batch`
3. Emit `DOCUMENT_READY` events
4. Add error handling and `INDEXING_FAILED` events

#### Phase 3: Monitoring & Cleanup
1. Add periodic job to detect stuck documents
2. Implement timeout-based completion
3. Add metrics and logging
4. Create admin tools for debugging

### 8. Example Event Flows

#### Successful PDF Indexing
```
1. PDF uploaded → INDEXING_STARTED
2. TitleExtractor → Task registered
3. PDFTextExtractor → Extracts text
4. TextChunker → Creates 5 chunks
5. EmbeddingDispatch → 6 tasks registered (1 title + 5 chunks)
6. Tasks complete → Check completion after each
7. Last task done → DOCUMENT_READY
```

#### Email with Attachments
```
1. Email received → INDEXING_STARTED
2. Email body → 1 embedding task
3. PDF attachment → Separate document, own INDEXING_STARTED
4. Each completes independently
5. Each emits own DOCUMENT_READY
```

### 9. Event Listener Examples

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

# Large document completion
{
    "source_id": "indexing",
    "match_conditions": {
        "event_type": "document_ready",
        "metadata.embedding_count": {"$gt": 50}  # Would need range support
    }
}
```

### 10. Performance Considerations

1. **Database Impact**: 
   - Uses existing tasks table - no new table needed
   - JSON query on payload->>'document_id' (consider adding index if needed)
   - Existing task cleanup handles old records

2. **Completion Check Optimization**:
   ```sql
   -- Fast check: count pending tasks
   SELECT COUNT(*) FROM tasks 
   WHERE task_type = 'embed_and_store_batch'
     AND payload->>'document_id' = ?
     AND status IN ('pending', 'locked');
   ```
   
   For better performance with many documents:
   ```sql
   -- Add functional index on document_id in payload
   CREATE INDEX idx_tasks_embed_doc_id 
   ON tasks ((payload->>'document_id')) 
   WHERE task_type = 'embed_and_store_batch';
   ```

3. **Event Queue**: 
   - Bounded queue (1000 events) prevents memory issues
   - Non-blocking event emission
   - Events dropped if queue full (logged, not fatal)

### 11. Migration Strategy

For existing documents without tracking:
1. Mark all documents with embeddings as "completed" 
2. Start tracking only for new documents
3. Optional: Backfill tracking for recent documents

### 12. Open Questions

1. **Completion Definition**: Should we wait for ALL tasks or accept partial completion?
   - Recommendation: Emit "document_ready" when all registered tasks complete, regardless of success/failure

2. **Event Granularity**: Should we emit events for each embedding completion?
   - Recommendation: Only emit DOCUMENT_READY for most use cases, add EMBEDDING_COMPLETED for debugging

3. **Retry Handling**: How do task retries affect completion?
   - Recommendation: Track task completion regardless of retry count

4. **Email Attachments**: Should attachments be separate documents or part of parent?
   - Current: Separate documents (each gets own events)
   - Alternative: Group under parent email

### 12. Advantages of This Approach

1. **Simplicity**: No new tables or complex tracking infrastructure
2. **Uses Existing Data**: Leverages document_id already in task payloads
3. **Minimal Changes**: Only adds completion checks, no pipeline modifications
4. **Database Agnostic**: Works with both PostgreSQL and SQLite (JSON queries)
5. **Backwards Compatible**: Existing documents continue to work

### 13. Potential Limitations

1. **JSON Query Performance**: May need functional index for large scale
2. **Task Table Dependency**: Couples indexing events to task queue implementation
3. **Historical Data**: Can't easily reconstruct events for past indexing

## Conclusion

This design adds reliable completion tracking to the indexing pipeline by leveraging the existing task queue infrastructure. By querying the tasks table for pending embedding tasks, we can detect when a document completes indexing without adding new tracking tables or modifying the pipeline architecture. This simpler approach reduces complexity while still providing users with powerful automation capabilities through the event listener system.