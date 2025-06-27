# Test Infrastructure Migration Plan

## Status: ✅ COMPLETED (2025-06-27)

## Overview

We've successfully implemented a parameterized test infrastructure that allows tests to run against
multiple database backends (SQLite and PostgreSQL) automatically. This ensures comprehensive testing
across development and production environments. The migration is now complete - all tests run
against all applicable databases by default.

## What's Been Implemented

### 1. Infrastructure Changes (✅ Complete)

- Added `--db` command-line option with choices: `sqlite`, `postgres`, `all` (default)
- Created `pytest_generate_tests` hook for dynamic fixture parameterization
- Created new `db_engine` fixture (non-autouse) that accepts database type via parameters
- Added marker support for PostgreSQL-specific tests
- Updated poe tasks to use new `--db` syntax

### 2. Key Features

- **Default behavior**: `pytest` runs all tests against all applicable databases
- **Selective testing**: `pytest --db sqlite` or `pytest --db postgres`
- **No redundant runs**: Tests marked with `@pytest.mark.postgres` only run once with PostgreSQL
- **Lazy container startup**: PostgreSQL container only starts when needed

### 3. Migration Tools Created

- `scripts/migrate_tests_to_db_engine.py` - Uses ast-grep for migration
- `scripts/migrate_tests_simple.py` - Regex-based fallback approach
- `scripts/mark_postgres_tests.py` - Marks PostgreSQL-specific tests

## Migration Complete!

The autouse `test_db_engine` fixture now depends on the parameterized `db_engine` fixture, which
means all tests automatically run against all selected databases without any code changes required.

### For New Tests

While not required, it's recommended to use the explicit `db_engine` fixture in new tests for
clarity:

```python
async def test_something(db_engine: AsyncEngine):
    """New test using explicit fixture."""
    async with db_engine.begin() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
```

### PostgreSQL-Specific Tests

Mark tests that should only run with PostgreSQL:

```python
@pytest.mark.postgres
async def test_postgres_feature(db_engine: AsyncEngine):
    """This test only runs with PostgreSQL."""
    ...
```

### Migration Tools (Still Available)

While migration is no longer required, these tools remain available for code cleanup:

1. **Replace autouse fixture references**:

   ```bash
   sed -i 's/test_db_engine/db_engine/g' tests/functional/test_example.py
   ```

2. **Add explicit db_engine parameter** (for clarity):

   ```bash
   # Manual review needed to add db_engine parameter to test functions
   ```

3. **Mark PostgreSQL tests**:

   ```bash
   python scripts/mark_postgres_tests.py
   ```

## Testing the New Infrastructure

The new system is ready to use. Example commands:

```bash
# Run all tests against all databases (default)
pytest

# Run all tests against SQLite only
pytest --db sqlite

# Run all tests against PostgreSQL only  
pytest --db postgres

# Run specific test file with both databases
pytest tests/functional/test_example.py

# Run specific test with SQLite only
pytest tests/functional/test_example.py --db sqlite
```

## Benefits

1. **Comprehensive testing**: All generic tests run against both SQLite and PostgreSQL by default
2. **Production confidence**: PostgreSQL-specific behaviors are tested
3. **Development speed**: Can run SQLite-only tests for quick feedback
4. **Clear separation**: PostgreSQL-specific tests are explicitly marked
5. **Resource efficiency**: PostgreSQL container only starts when needed

## Summary

The test infrastructure migration is complete! The primary goal has been achieved:

- ✅ All tests run against all applicable databases by default
- ✅ Comprehensive testing coverage without code changes
- ✅ Flexible execution options with `--db` flag
- ✅ PostgreSQL-specific tests properly marked
- ✅ Full backwards compatibility maintained

The system now provides automatic multi-database testing while maintaining flexibility for
development workflows.
