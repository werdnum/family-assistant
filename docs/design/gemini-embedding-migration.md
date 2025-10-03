# Gemini Embedding Model Migration

## Overview

Migrated from experimental `gemini-embedding-exp-03-07` to stable `gemini-embedding-001` model.

**Migration Date:** 2025-10-03 **Deadline:** October 30, 2025 (experimental model API stops working)
**Reference:**
[Google Blog Post](https://developers.googleblog.com/en/gemini-embedding-available-gemini-api/)

## Why No Re-embedding Required

Per Google's announcement: "If you are using the experimental gemini-embedding-exp-03-07, you won't
need to re-embed your contents."

The models produce compatible embeddings (same vector space), so existing embeddings remain valid.
Only the model identifier needed updating in the database.

## Changes Made

### 1. Alembic Migration

**File:** `alembic/versions/2025_10_03-6d7dd88fefad_rename_gemini_embedding_model_to_stable.py`

- Renames `embedding_model` column values from old to new model
- Includes `downgrade()` function for easy rollback
- PostgreSQL-only (skips on SQLite since `document_embeddings` requires pgvector)

**SQL:**

```sql
UPDATE document_embeddings
SET embedding_model = 'gemini/gemini-embedding-001'
WHERE embedding_model = 'gemini/gemini-embedding-exp-03-07';
```

### 2. Configuration Update

**File:** `src/family_assistant/__main__.py`

Changed default `embedding_model` from:

```python
"embedding_model": "gemini/gemini-embedding-exp-03-07"
```

To:

```python
"embedding_model": "gemini/gemini-embedding-001"
```

## Deployment

Both changes deploy atomically:

1. Alembic migration runs on application startup
2. Application config loads new model name
3. No downtime or service interruption

## Verification

**Pre-Migration:** 21 embeddings with `gemini-embedding-exp-03-07` **Post-Migration:** 21 embeddings
with `gemini-embedding-001`

Tested:

- ✅ Migration upgrade (PostgreSQL)
- ✅ Migration downgrade (rollback)
- ✅ Re-upgrade
- ✅ All test suite (including alembic schema validation)
- ✅ Search functionality maintained

## Rollback Procedure

If issues arise:

```bash
# Revert code
git revert <commit-hash>

# Rollback database (happens automatically on deploy)
alembic downgrade -1
```

This will restore the experimental model name in both code and database.

## Future Cleanup (Optional)

The old experimental model API will stop working after October 30, 2025. After verifying the
migration is successful and search quality is maintained:

1. Monitor for any issues through October 2025
2. No further action needed - old model name is already replaced

## Technical Notes

- **Embedding Dimensions:** Still 1536 (unchanged)
- **Vector Search:** Already filters by `embedding_model`, so search continues working seamlessly
- **SQLite Compatibility:** Migration skips on SQLite (vector search requires PostgreSQL with
  pgvector)
- **Production Database:** PostgreSQL with pgvector extension

## Related Documentation

- [Google AI Embeddings Documentation](https://ai.google.dev/gemini-api/docs/embeddings)
- [Vector Search Design](vector_search.md)
