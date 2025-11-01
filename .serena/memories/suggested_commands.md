# Suggested Commands for Development

## Installation and Setup

```bash
# Install the project in development mode with all dependencies
uv sync --extra dev
```

## Linting and Code Quality

```bash
# Lint entire codebase (MUST pass before committing)
scripts/format-and-lint.sh

# Lint specific Python files only
scripts/format-and-lint.sh path/to/file.py

# Lint only changed Python files (before committing)
scripts/format-and-lint.sh $(git diff --name-only --cached | grep '\.py$')
```

The lint script runs:

- `ruff check --fix` (linting with auto-fixes)
- `ruff format` (code formatting)
- `basedpyright` (type checking)
- `pylint` (additional linting in errors-only mode)

## Testing

```bash
# Run all tests (needs ~15 minute timeout)
poe test

# Run tests with PostgreSQL (production database)
poe test-postgres  # Quick mode with -xq
poe test-postgres-verbose  # Verbose mode with -xvs

# Run specific test files
pytest tests/functional/test_specific.py -xq

# Run tests with PostgreSQL using pytest directly
pytest --postgres -xq  # All tests with PostgreSQL
pytest --postgres tests/functional/test_specific.py -xq  # Specific tests
```

## Running the Application

```bash
# Main application entry point
python -m family_assistant

# Via setuptools script
family-assistant

# Web server only
uvicorn family_assistant.web_server:app --reload --host 0.0.0.0 --port 8000
```

## Database Operations

```bash
# Create new migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Use SQLite URL for local migrations
DATABASE_URL="sqlite+aiosqlite:///family_assistant.db" alembic revision --autogenerate -m "Description"
```

## Code Generation and Analysis

```bash
# Generate SYMBOLS.md file
poe symbols

# Find symbol definitions with symbex
symbex MyClass
symbex 'test_*'
symbex --async -s
symbex MyClass -f src/family_assistant/assistant.py
```

## Large-scale Code Changes

```bash
# Use ast-grep for mechanical changes
ast-grep -U --inline-rules '...' .

# Example: Remove cache keyword argument
ast-grep -U --inline-rules '
id: remove-cache-kwarg
language: python
rule:
  pattern: my_function($$$START, cache=$_, $$$END)
  fix: my_function($$$START, $$$END)
' .
```

## Git Operations

```bash
# Standard git commands
git status
git diff
git log --oneline -10
git add -A
git commit -m "feat: Description"

# Local instance auto-restarts on file changes
# SQLite is used locally, PostgreSQL in production (Kubernetes)
```

## System Utilities

```bash
# Directory navigation
ls -la
cd src/family_assistant

# File search
find . -name "*.py" -type f
grep -r "pattern" src/

# Use ripgrep (rg) instead of grep when available
rg "pattern" src/
```
