# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Style

* Comments are used to explain implementation when it's unclear. Do NOT add comments that are self-evident from the code, or that explain the code's history (that's what commit history is for). No comments like `# Removed db_context`.

## Development Policies

* The policy for this project is that there are zero lint errors or test failures (any unavoidable ones are skipped / disabled / ignored). Accordingly, NEVER assume that a test breakage or lint failure is pre-existing unless you can PROVE otherwise (e.g. by stashing your changes and finding the commit that broke them)

## Development Setup

### Installation

```bash
# Install the project in development mode with all dependencies
uv pip install -e '.[dev]'
```

## Development Commands

### Linting and Type Checking

```bash
# Lint entire codebase (src/ and tests/)
scripts/format-and-lint.sh

# Lint specific Python files only
scripts/format-and-lint.sh path/to/file.py path/to/another.py

# Lint only changed Python files (useful before committing)
scripts/format-and-lint.sh $(git diff --name-only --cached | grep '\.py$')

# Note: This script is for Python files only. It will error if given non-Python files.
```

This script runs:

- `ruff check --fix` (linting with auto-fixes)
- `ruff format` (code formatting)
- `basedpyright` (type checking)  
- `pylint` (additional linting in errors-only mode)

**IMPORTANT**: `scripts/format-and-lint.sh` MUST pass before committing. NEVER use `git commit --no-verify` -- all lint failures must be fixed or properly disabled.

### Testing

* IMPORTANT: Write your tests as "end-to-end" as you can.
  * Use mock objects as little as possible. Use real databases (fixtures available in tests/conftest.py and tests/functional/telegram/conftest.py) and only mock external dependencies with no good fake implementations.
* Each test tests one independent behaviour of the system under test. Arrange, Act, Assert. NEVER Arrange, Act, Assert, Act, Assert, Act, Assert.

* ALWAYS run tests with `-xq` so there is less output to process. NEVER use `-s` or `-v` unless you have already tried with `-q` and you are sure there is information in the output of `-s` or `-v` that you need for debugging.

```bash
# Run all tests with verbose output
poe test # Note: You will need a long timeout for this - something like 15 minutes

# Run tests with PostgreSQL (production database)
poe test-postgres  # Quick mode with -xq
poe test-postgres-verbose  # Verbose mode with -xvs

# Run tests with PostgreSQL using pytest directly
pytest --postgres -xq  # All tests with PostgreSQL
pytest --postgres tests/functional/test_specific.py -xq  # Specific tests with PostgreSQL

# Run specific test files
pytest tests/functional/test_specific.py -xq

```

#### Database Backend Selection

By default, tests run with an in-memory SQLite database for speed. However, production uses PostgreSQL, so it's important to test with PostgreSQL to catch database-specific issues:

- Use `--postgres` flag to run tests with PostgreSQL instead of SQLite
- PostgreSQL container starts automatically when the flag is used (requires Docker/Podman)
- Tests that specifically need PostgreSQL features can use `pg_vector_db_engine` fixture, but will get a warning if run without `--postgres` flag
- The unified `test_db_engine` fixture automatically provides the appropriate database based on the flag

**PostgreSQL Test Isolation**: When using `--postgres`, each test gets its own unique database:

- A new database is created before each test (e.g., `test_my_function_12345678`)
- The database is completely dropped after the test completes
- This ensures complete isolation - tests cannot interfere with each other
- No data persists between tests, eliminating order-dependent failures

**Important**: Running tests with `--postgres` has already revealed PostgreSQL-specific issues like:

- Event loop conflicts in error logging when using PostgreSQL
- Different transaction handling between SQLite and PostgreSQL
- Schema differences that only manifest with PostgreSQL

It's recommended to run tests with `--postgres` before pushing changes that touch database operations.

[Rest of the content remains the same as in the original file]
