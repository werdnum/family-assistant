# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Linting and Type Checking
```bash
scripts/format-and-lint.sh
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

This is a family assistant application built around a conversational LLM interface with multiple service profiles. The core architecture follows a service-oriented design:

### Core Components

**Assistant (`src/family_assistant/assistant.py`)**: Central orchestrator that manages all services and coordinates between different components. Handles service profiles, tool management, and application lifecycle.

**Service Profiles**: Configurable assistant behaviors defined in `config.yaml`. Each profile can have different:
- LLM models and parameters
- Available tools (local tools + MCP servers)
- System prompts and processing configurations
- Slash command triggers (e.g., `/browse`, `/k8s`, `/research`)

**Storage Layer (`src/family_assistant/storage/`)**: Abstracted storage interfaces for:
- Message history with turn-based context
- Notes and user documentation
- Vector embeddings and search
- Email data and attachments
- Background tasks and API tokens

**Indexing Pipeline (`src/family_assistant/indexing/`)**: Document processing system with configurable processors:
- File processors (PDF, text extraction)
- LLM processors (summarization, link extraction)
- Network processors (web fetching)
- Metadata and text chunking processors

**Tools Integration**: 
- Local tools for core functionality (notes, calendar, search, etc.)
- MCP (Model Context Protocol) integration for external tools
- Tool confirmation system for destructive operations

### Key Architectural Patterns

**Configuration-Driven Design**: Extensive use of YAML configuration for service profiles, indexing pipelines, and tool configurations. Configuration hierarchy: Code defaults → `config.yaml` → Environment variables → CLI arguments.

**Async/Await Throughout**: All I/O operations use async patterns for better concurrency, especially important for LLM calls and database operations.

**Context Providers**: Modular system for injecting context into LLM conversations (calendar, location, notes, etc.).

**Processing Profiles**: Allow different processing behaviors per service profile, including different LLM models, context providers, and tool configurations.

## Development Guidelines

### Code Style and Quality
- Follow PEP 8 and the project style guide in `docs/STYLE_GUIDE.md`
- Use type hints comprehensively for all function arguments and return values
- Prefer `TypedDict` or dataclasses over `Dict[str, Any]` for structured data
- Use dependency injection and avoid global variables for testability
- Comments should explain "why" not "what" - avoid redundant comments
- Remove unused imports, variables, and commented-out code
- Use `async`/`await` consistently for I/O operations

### Type Safety Requirements
- Use `snake_case` for variables/functions, `PascalCase` for classes
- Type-only imports go in `if TYPE_CHECKING:` blocks
- Use forward references with string literals when needed
- FastAPI dependencies should use `typing.Annotated`
- Use `zip(strict=True)` when iterables should be same length

### Configuration Management
- Main config in `config.yaml` with service profiles
- Prompts defined in `prompts.yaml`
- MCP server definitions in `mcp_config.json`
- Database migrations in `alembic/versions/`

### Testing Approach
- Prioritize realistic integration and functional tests over unit tests
- Use `testcontainers` for database dependencies when possible
- Functional tests in `tests/functional/` test end-to-end workflows
- Unit tests in `tests/unit/` test isolated complex logic
- Mock LLM client available in `tests/mocks/mock_llm.py`
- Use `pytest-asyncio` for async test support
- Separate core logic from interface code for better testability

### Tool Development
- Local tools implemented in `src/family_assistant/tools/`
- Follow the schema defined in `tools/schema.py`
- Tools requiring confirmation listed in profile's `confirm_tools`

### Storage Integration
- Use storage interfaces from `src/family_assistant/storage/`
- All database operations through SQLAlchemy async sessions
- Vector storage supports both SQLite (for development) and PostgreSQL with pgvector

### Indexing and Document Processing
- Processors are configurable and chainable
- Each processor type defined in `src/family_assistant/indexing/processors/`
- Pipeline configuration supports different embedding types and processing strategies

## Important Files and Locations

- **Entry point**: `src/family_assistant/__main__.py`
- **Main config**: `config.yaml`
- **Database models**: `src/family_assistant/storage/base.py`
- **Web routes**: `src/family_assistant/web/routers/`
- **Telegram bot**: `src/family_assistant/telegram_bot.py`
- **Task worker**: `src/family_assistant/task_worker.py`
- **Service definitions**: Service profiles in `config.yaml`

## Commit Guidelines

- Always commit your changes to git. Use a descriptive commit message. 
- Explain not just what you're doing, but why, how, and why you chose to do it that way. 
- Don't make up a rationale if you don't know why and it isn't obvious, but definitely document any context from the conversation that explains the code you wrote and the technical decisions made.

When working with this codebase, always run linting and type checking before committing changes. The application supports both SQLite (development) and PostgreSQL (production) databases.