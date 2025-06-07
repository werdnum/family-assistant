# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Linting and Type Checking
```bash
# Lint entire codebase (src/ and tests/)
scripts/format-and-lint.sh

# Lint specific files only
scripts/format-and-lint.sh path/to/file.py path/to/another.py

# Lint only changed files (useful before committing)
scripts/format-and-lint.sh $(git diff --name-only --cached)
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

## Code Structure Summary

The codebase is organized into several key layers:

**Core Application Layer**:
- `assistant.py` - Main orchestrator managing application lifecycle and service coordination
- `__main__.py` - Entry point handling configuration loading and service startup
- `processing.py` - Core processing service managing LLM interactions and tool execution

**User Interfaces**:
- `telegram_bot.py` - Telegram interface with message handling and slash commands
- `web/routers/` - FastAPI web interface with UI and API endpoints
- `web/auth.py` - Authentication system supporting OIDC and API tokens

**Storage & Data Management**:
- `storage/` - Database abstraction layer with async SQLAlchemy
- `storage/vector.py` - Vector storage for embeddings (PostgreSQL + pgvector)
- `storage/message_history.py` - Conversation history with processing profile filtering

**Document Processing**:
- `indexing/pipeline.py` - Configurable document processing pipeline
- `indexing/processors/` - Modular processors (file, LLM, network, text chunking)
- `indexing/document_indexer.py` - Main indexing orchestrator

**Tools & External Integration**:
- `tools/` - Local Python tools and MCP integration
- `tools/mcp.py` - Model Context Protocol for external tool servers
- `calendar_integration.py` - CalDAV integration for calendar events

**Supporting Infrastructure**:
- `llm.py` - LLM client abstraction (primarily LiteLLM)
- `embeddings.py` - Text embedding generation
- `task_worker.py` - Background task processing with retry logic
- `context_providers.py` - Dynamic context injection for LLM prompts

## Important Files and Locations

- **Entry point**: `src/family_assistant/__main__.py`
- **Main config**: `config.yaml`
- **Database models**: `src/family_assistant/storage/base.py`
- **Web routes**: `src/family_assistant/web/routers/`
- **Web architecture**: See `src/family_assistant/web/README.md` for detailed web layer documentation
- **Telegram bot**: `src/family_assistant/telegram_bot.py`
- **Task worker**: `src/family_assistant/task_worker.py`
- **Service definitions**: Service profiles in `config.yaml`

## Commit Guidelines

Always commit your changes to git following these guidelines:

### Format
- Start with a short one-line summary, followed by two line breaks and then any relevant explanation
- Use imperative mood (e.g., "Add feature" not "Adds feature", "Added feature" or "Adding feature")
- Never start with "This commit..."
- Optional: Use conventional commit prefixes like 'feat:', 'fix:', 'chore:' if they add clarity

### Content
- Describe not only _what_ was changed, but _why_ and to what end
- Document any context from the conversation that explains technical decisions
- Briefly describe changes at a function-by-function level rather than line-by-line
- Don't explain obvious things (e.g., why it's good to make tests pass)
- Don't reiterate precise details of code changes that are visible in the diff

### Example
```
feat: Add user authentication system

Implement JWT-based authentication to secure API endpoints. This allows
users to register, login, and access protected resources. Chose JWT over
sessions for stateless operation and easier scaling.

- Add User model with password hashing
- Create auth endpoints for register/login/refresh
- Add authentication middleware for protected routes
- Include comprehensive test coverage
```

## General Feature Development Process

General feature development process:  
- Clarify requirements (immediate / short term vs long term goals / aspirations / ideas)
- Identify potential designs and their trade offs 
- Consider any refactorings, infrastructure or abstractions that would make the task easier or more maintainable 
- Write up the solution in docs/design 
- Break it into milestones emphasizing incremental delivery 
- Gather feedback on the design and iterate until everyone is happy 
- Implement one stage at a time, starting with interfaces and refactoring 
- Lint and git commit at every step 
- Write tests whenever there is something to test 
- Make sure tests pass 
- Do a final acceptance test with the Dev server

When working with this codebase, always run linting and type checking before committing changes. The application supports both SQLite (development) and PostgreSQL (production) databases.