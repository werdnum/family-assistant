# Alembic Migration Upgrade Guide

This document describes the changes made to the Alembic migration system and how to upgrade existing
deployments.

## Changes Made

### 1. Migration History Rewrite

- Squashed all migrations into a single clean initial migration
  (`2025_08_25-6daf0237b0ba_initial_schema.py`)
- Removed incremental ALTER TABLE statements in favor of proper CREATE TABLE statements
- Fixed empty database upgrade issues where migrations tried to ALTER non-existent tables

### 2. Dialect-Specific Filtering

- Added `include_object` function in `alembic/env.py` to filter database objects by dialect
- PostgreSQL-specific features (vector tables, VECTOR columns, HNSW indexes) are now excluded from
  SQLite migrations
- Ensures SQLite deployments don't get PostgreSQL vector artifacts

### 3. Enhanced Testing

- Added pytest-alembic integration for migration validation
- Automatic testing of migration consistency and schema matching

## Upgrade Instructions

### For New Deployments

No action required. The new migration system will work out of the box.

### For Existing Deployments with Applied Migrations

The migration history rewrite requires different approaches depending on your current migration
state:

#### Case 1: Already on Latest Migration (df4c339d1045)

If your deployment has already applied the most recent migration (`df4c339d1045`), the upgrade is
**backward compatible**:

1. **No database changes required** - Your existing schema is correct
2. **Alembic revision table remains intact** - Migration history is preserved
3. **Only new future migrations need to be applied**

#### Case 2: Not on Latest Migration (Multiple Heads)

If your deployment is on an older migration, `alembic` will detect divergent histories (multiple
heads) and refuse to upgrade. This is because the migration squashing creates a new history that
diverges from the old one.

**CRITICAL: Take a database backup before proceeding**

1. **Check your current revision**:

   ```bash
   alembic current
   ```

2. **If you see an old revision (not df4c339d1045), you must resolve the divergence**:

   ```bash
   # Take a backup first!
   # Then stamp your database to the new base migration
   alembic stamp 6daf0237b0ba

   # Now upgrade to head (applies df4c339d1045 and any newer migrations)
   alembic upgrade head
   ```

3. **Verify the upgrade succeeded**:

   ```bash
   alembic current  # Should show the latest revision
   alembic heads    # Should show only one head
   ```

#### Verification Steps

After deploying the updated code, verify the migration state:

```bash
# Check current migration status
alembic current

# This should show either:
# - df4c339d1045 (if you were already up-to-date) 
# - The latest migration (after resolving divergence)

# Verify single head exists
alembic heads

# Test that future upgrades work
alembic upgrade head
```

#### Troubleshooting Migration Issues

**"Multiple heads detected" error**:

- You have an older migration applied that's not in the new squashed history
- Follow Case 2 instructions above to resolve

**"Can't locate revision" error**:

- Your current revision no longer exists in the new migration files
- Use `alembic stamp 6daf0237b0ba` to reset to the new base

**Schema mismatch after stamping**:

- Your database schema doesn't match what the initial migration expects
- This shouldn't happen if all migrations were previously applied correctly
- Restore from backup and investigate schema differences

### Development Environment Reset

If you want to start fresh in development:

1. Drop your development database
2. Create a new empty database
3. Run `alembic upgrade head` to apply all migrations from scratch

## Technical Details

### Migration Filtering Logic

The new `include_object` function in `alembic/env.py` filters objects based on database dialect:

- **PostgreSQL**: All tables and features are included (vector tables, VECTOR columns, HNSW indexes)
- **SQLite**: Vector-related features are excluded:
  - `document_embeddings` table is skipped
  - Columns with VECTOR types are skipped
  - PostgreSQL-specific indexes (HNSW, GIN, GIST) are skipped

### Schema Consistency

The new migration system ensures:

- Empty database upgrades work correctly (CREATE TABLE instead of ALTER TABLE)
- SQLite doesn't get PostgreSQL vector artifacts
- PostgreSQL gets full vector search capabilities
- Both dialects produce consistent schemas

### Testing

The migration system is now validated by pytest-alembic, which automatically tests:

- Single head revision exists
- Migrations can upgrade from empty to head
- Model definitions match migrated schema
- Up/down consistency

Run tests with: `pytest tests/test_alembic.py`

## Rollback Plan

If you need to rollback to the old migration system:

1. Restore the old `alembic/versions/` files from git history
2. Restore the old `alembic/env.py` from git history
3. Your database schema will remain unchanged and compatible

The old migrations are preserved in git history and can be restored if needed:

```bash
git log --oneline alembic/versions/
```

## Support

If you encounter any migration issues:

1. Check the alembic current status
2. Verify your database schema matches expectations
3. Review the logs for any error messages
4. Restore from backup if necessary
