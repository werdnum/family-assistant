# Test Infrastructure Refactoring - Status Report

**Date**: 2025-06-22\
**Updated**: 2025-06-27\
**Status**: ✅ MIGRATION COMPLETE - All tests run against all databases by default

## Executive Summary

We have successfully achieved the primary goal: **all tests now run against all applicable databases
by default**. This was accomplished by making the autouse `test_db_engine` fixture depend on the
parameterized `db_engine` fixture, leveraging pytest's built-in parameterization behavior.

## Requirements Met

✅ **Primary Requirement**: `pytest` with no flags runs all tests against all applicable databases

- Generic tests run against both SQLite and PostgreSQL
- PostgreSQL-specific tests run only against PostgreSQL
- No redundant test runs

✅ **Flexible Execution**: New `--db` option allows selective testing

- `pytest --db sqlite` - SQLite only
- `pytest --db postgres` - PostgreSQL only
- `pytest --db all` - Both databases (default)

✅ **Backwards Compatibility**: Existing tests continue to work unchanged

## Implementation Details

### 1. Final Solution (Simple and Elegant)

The key insight was that pytest's parameterization automatically handles fixture dependencies. When
an autouse fixture depends on a parameterized fixture, ALL tests run multiple times for each
parameter value.

#### Updated Autouse Fixture (`conftest.py`)

```python
@pytest_asyncio.fixture(scope="function", autouse=True)
async def test_db_engine(
    db_engine: AsyncEngine,
) -> AsyncGenerator[AsyncEngine, None]:
    """
    Autouse fixture that ensures all tests have a database.
    Delegates to the parameterized db_engine fixture.
    """
    # Simply yield the parameterized db_engine
    yield db_engine
```

#### Updated Parameterization Hook (`conftest.py`)

```python
def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Dynamically parameterizes fixtures based on the --db option."""
    # Check if db_engine is needed - either directly or through autouse fixture
    if "db_engine" in metafunc.fixturenames:
        # Parameterize based on --db flag and test markers
        ...
```

This simple change means:

- The autouse `test_db_engine` runs for every test
- It depends on the parameterized `db_engine`
- pytest automatically runs each test multiple times (once per database)
- No test code changes required!

### 2. Configuration Updates

#### pytest.ini

```ini
[pytest]
asyncio_default_fixture_loop_scope = function
markers =
    postgres: marks tests as requiring PostgreSQL database
```

#### pyproject.toml

```toml
[tool.poe.tasks.test-postgres]
help = "Run tests with PostgreSQL database backend"
shell = "pytest --db postgres -xq"

[tool.poe.tasks.test-sqlite]
help = "Run tests with SQLite database backend only"
shell = "pytest --db sqlite -xq"
```

### 3. Migration Tools (Now Optional!)

Since the primary goal has been achieved, migration is now optional. Tests can gradually be updated
to use the explicit `db_engine` parameter for clarity, but this is not required for functionality.

Migration scripts are available but not urgently needed:

- `scripts/migrate_tests_to_db_engine.py` - For automated migration
- `scripts/migrate_tests_simple.py` - Regex-based approach
- `scripts/mark_postgres_tests.py` - Mark PostgreSQL-specific tests

## Current State

### What Works Now

1. **ALL tests automatically run against all databases** (Goal achieved!)

   ```bash
   # Default behavior - runs all tests on both SQLite and PostgreSQL
   pytest

   # Control which databases to test
   pytest --db sqlite      # SQLite only
   pytest --db postgres    # PostgreSQL only
   pytest --db all         # Both (default)
   ```

2. **New test pattern** (recommended for clarity, but not required):

   ```python
   async def test_something(db_engine: AsyncEngine):
       """Test using explicit db_engine fixture."""
       async with db_engine.begin() as conn:
           result = await conn.execute(text("SELECT 1"))
           assert result.scalar() == 1
   ```

3. **PostgreSQL-specific tests** (mark to avoid running on SQLite):

   ```python
   @pytest.mark.postgres
   async def test_postgres_feature(db_engine: AsyncEngine):
       """Test that only runs with PostgreSQL."""
       # Test PostgreSQL-specific features
   ```

### Migration Status

- **Primary Goal**: ✅ ACHIEVED - All tests run on all databases by default
- **Test Migration**: Optional - tests work without changes
- **PostgreSQL Marking**: ✅ COMPLETE - 32 PostgreSQL-specific tests marked

## Migration Complete

### Completed Tasks

- [x] Infrastructure implementation
- [x] Primary goal achieved - all tests run on all databases
- [x] Documentation updated
- [x] Mark PostgreSQL-specific tests with `@pytest.mark.postgres` (32 tests marked)
- [x] Test infrastructure migration fully complete

### Optional Code Cleanup

While not required for functionality, these optional improvements can be made over time:

- [ ] Gradually update tests to use explicit `db_engine` parameter for code clarity
- [ ] Consider removing autouse fixture in the distant future once all tests use explicit fixtures

## Benefits Achieved

1. **Comprehensive Testing**: All tests can run against production-like PostgreSQL
2. **Development Speed**: SQLite-only option for quick feedback
3. **Clear Intent**: Explicit fixture dependencies improve code clarity
4. **No Redundancy**: PostgreSQL-specific tests run only once
5. **Resource Efficiency**: PostgreSQL container starts only when needed

## Recommendations

1. **Start using `db_engine` in all new tests immediately**
2. **Run `pytest --db postgres` before all PRs**
3. **Migrate tests opportunistically when modifying files**
4. **Use `@pytest.mark.postgres` for database-specific features**

## Conclusion

**Mission accomplished!** By leveraging pytest's built-in parameterization behavior and making a
simple change to have the autouse fixture depend on the parameterized fixture, we achieved the
primary goal with minimal code changes. All tests now automatically run against all selected
database backends, providing comprehensive testing coverage by default.

The test infrastructure migration is now complete, and no further action is required. The system
provides automatic multi-database testing while maintaining full backwards compatibility and
flexibility for different development workflows.
