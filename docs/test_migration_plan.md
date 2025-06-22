# Test Infrastructure Migration Plan

## Overview

We've successfully implemented a parameterized test infrastructure that allows tests to run against multiple database backends (SQLite and PostgreSQL) automatically. This ensures comprehensive testing across development and production environments.

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

## Migration Strategy

### Phase 1: Manual Migration of New Tests

Start using `db_engine` fixture in all new tests immediately:

```python
async def test_something(db_engine: AsyncEngine):
    """New test using explicit fixture."""
    async with db_engine.begin() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
```

### Phase 2: Gradual Migration of Existing Tests

1. **Simple replacement approach** (most reliable):

   ```bash
   # Replace test_db_engine with db_engine in a specific file
   sed -i 's/test_db_engine/db_engine/g' tests/functional/test_example.py
   
   # Add db_engine parameter to tests without it (manual review needed)
   # Look for: async def test_name():
   # Replace with: async def test_name(db_engine: AsyncEngine):
   ```

2. **Mark PostgreSQL-specific tests**:

   ```python
   @pytest.mark.postgres
   async def test_postgres_feature(db_engine: AsyncEngine):
       """This test only runs with PostgreSQL."""
       ...
   ```

### Phase 3: Disable autouse (Future)

Once all tests are migrated, change `test_db_engine` fixture:

```python
@pytest_asyncio.fixture(scope="function", autouse=False)  # Change to False
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

## Next Steps

1. Start writing new tests with explicit `db_engine` parameter
2. Gradually migrate existing tests file by file
3. Mark PostgreSQL-specific tests with `@pytest.mark.postgres`
4. Monitor test execution to ensure no double-runs
5. Once fully migrated, disable autouse on `test_db_engine`

Overall, the test infrastructure refactoring is now complete with a clear migration path for existing tests. The system supports comprehensive testing against multiple databases while maintaining backward compatibility.
