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

# Note: poe test may timeout when running all tests. If this happens, run tests in smaller batches:
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
```

### Code Generation
```bash
# Generate SYMBOLS.md file
poe symbols
```

## Architecture Overview

## Development Guidelines

## Important Notes

- Always run 'poe test' when you're done to make sure all lint checks are passing and tests are passing too
- When running tests,  only use -s when you don't get the info you need without it
- Save tokens when running tests: use pytest -q to check if a test passes, only use -s or -v if you need it
- Always commit changes after each major step. Prefer many small self contained commits as long as each commit passes lint checks.