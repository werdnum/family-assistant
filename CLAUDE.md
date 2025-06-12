# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

### Testing
```bash
# Run all tests with verbose output
poe test

# Run specific test files
pytest tests/functional/test_specific.py -v

# Run with coverage
pytest --cov=family_assistant tests/

# Note: poe test will timeout when running all tests. Run tests in smaller batches:
# pytest tests/unit/ -v
# pytest tests/functional/indexing/ -v
# pytest tests/functional/telegram/ -v
# pytest tests/functional/web/ -v
```

### Running the Application
```bash
# Main application entry point
python -m family_assistant

# Via setuptools script
family-assistant

# Web server only
uvicorn family_assistant.web_server:app --reload --host 0.0.0.0 --port 8000
```

### Database Migrations
```bash
# Create new migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Use DATABASE_URL="sqlite+aiosqlite:///family_assistant.db" with alembic to make a new revision
# alembic migrations run on startup
```

### Code Generation
```bash
# Generate SYMBOLS.md file
poe symbols
```

### Finding Symbol Definitions and Signatures
```bash
# Use symbex to find symbol definitions and signatures
# Docs: https://github.com/simonw/symbex

# Find a specific function or class
symbex my_function
symbex MyClass

# Show signatures for all symbols
symbex -s

# Show signatures with docstrings
symbex --docstrings

# Search with wildcards
symbex 'test_*'
symbex '*Tool.*'

# Find async functions
symbex --async -s

# Find undocumented public functions
symbex --function --public --undocumented

# Search in specific files
symbex MyClass -f src/family_assistant/assistant.py
symbex 'handle_*' -f src/family_assistant/telegram_bot.py

# Search in specific directories
symbex -d src/family_assistant --function -s
```

## Architecture Overview

## Development Guidelines

### Adding New Tools

See the detailed guide in `src/family_assistant/tools/README.md` for complete instructions on implementing new tools.

Quick summary:
1. Create tool implementation in `src/family_assistant/tools/something.py`
2. Export in `src/family_assistant/tools/__init__.py`
3. Enable in `config.yaml` under the profile's `enable_local_tools`

## Important Notes

- Always make sure you start with a clean working directory. Commit any uncommitted changes.
- Always check that linters and tests are happy when you're finished.
- When running tests,  only use -s when you don't get the info you need without it
- Save tokens when running tests: use pytest -q to check if a test passes, only use -s or -v if you need it
- Always commit changes after each major step. Prefer many small self contained commits as long as each commit passes lint checks.
- **Important**: When adding new imports, add the code that uses the import first, then add the import. Otherwise, a linter running in another tab might remove the import as unused before you add the code that uses it.
- Always use symbolic SQLAlchemy queries, avoid literal SQL text as much as possible. Literal SQL text may break across engines.