# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

> **Note**: CLAUDE.md sources this file (AGENTS.md) using `@AGENTS.md`. If you need to edit
> CLAUDE.md, you should edit AGENTS.md instead.
>
> **Architecture Documentation**: For a comprehensive visual overview of the system architecture,
> component interactions, and data flows, see
> [docs/architecture-diagram.md](docs/architecture-diagram.md).

## Context-Specific Guidance

Additional guidance is available in subdirectories:

- **[tests/CLAUDE.md](tests/CLAUDE.md)** - Testing patterns, fixtures, CI debugging
- **[tests/integration/CLAUDE.md](tests/integration/CLAUDE.md)** - Integration testing with VCR.py
- **[tests/functional/web/CLAUDE.md](tests/functional/web/CLAUDE.md)** - Playwright web UI testing
- **[tests/functional/telegram/CLAUDE.md](tests/functional/telegram/CLAUDE.md)** - Telegram bot
  testing
- **[src/family_assistant/tools/CLAUDE.md](src/family_assistant/tools/CLAUDE.md)** - Tool
  development
- **[src/family_assistant/web/CLAUDE.md](src/family_assistant/web/CLAUDE.md)** - Web API development
- **[frontend/CLAUDE.md](frontend/CLAUDE.md)** - Frontend development (React, Vite, testing)
- **[scripts/CLAUDE.md](scripts/CLAUDE.md)** - Script development
- **[.github/workflows/CLAUDE.md](.github/workflows/CLAUDE.md)** - CI workflow development
- **[docs/development/ast-grep-recipes.md](docs/development/ast-grep-recipes.md)** - Code
  transformation recipes

## Style

- Place all imports at the top of the file, organized by the isort rules in `pyproject.toml`.
- Use type hints for all method parameters and return values.
- All methods must have type hints for their parameters and return values.
- Comments are used to explain implementation when it's unclear. Do NOT add comments that are
  self-evident from the code, or that explain the code's history (that's what commit history is
  for). No comments like `# Removed db_context`.

## Development Setup

### Quick Setup

The easiest way to set up your development environment is to use the setup script:

```bash
# Run the setup script to install all dependencies
./scripts/setup-workspace.sh

# Activate the virtual environment
source .venv/bin/activate

# Verify setup
poe test
```

The setup script will:

- Create a virtual environment (`.venv`)
- Install all Python dependencies using `uv sync --extra dev` (includes all dev tools)
- Install pre-commit hooks
- Install frontend dependencies (`npm ci --prefix frontend`)
- Install Playwright browsers

### Manual Installation

If you prefer to set up manually:

```bash
# Create virtual environment
uv venv .venv
source .venv/bin/activate

# Install Python dependencies (includes dev dependencies)
uv sync --extra dev

# Install pre-commit hooks
.venv/bin/pre-commit install

# Install frontend dependencies
npm ci --prefix frontend

# Install Playwright browsers
.venv/bin/playwright install chromium

# Optional: Install local embedding model support (adds ~450MB of dependencies)
# Only needed if you want to use local sentence transformer models instead of cloud APIs
uv sync --extra dev --extra local-embeddings

# Optional: Install pgserver for PostgreSQL testing
# Only needed if TEST_DATABASE_URL is unset (e.g., cloud coding environments)
# Not needed if you have access to an external PostgreSQL database
uv sync --extra dev --extra pgserver
```

## Dependency Management

This project uses [uv](https://docs.astral.sh/uv/) for Python dependency management. Dependencies
are declared in `pyproject.toml` and locked in `uv.lock`.

### Adding Dependencies

```bash
# Add a production dependency
uv add <package-name>

# Add a development dependency (to the [dev] extra)
uv add --dev <package-name>

# Add a dependency with version constraints
uv add "package-name>=1.0.0,<2.0.0"

# Add an optional dependency group
uv add --optional local-embeddings sentence-transformers
```

### Removing Dependencies

```bash
# Remove a dependency
uv remove <package-name>

# Remove a development dependency
uv remove --dev <package-name>
```

### Updating Dependencies

```bash
# Update all dependencies to their latest compatible versions
uv lock --upgrade

# Update a specific package
uv lock --upgrade-package <package-name>

# Sync your virtual environment with the lockfile
uv sync --extra dev
```

### Important Notes

- **NEVER use `uv pip install`** - This bypasses uv's dependency resolution and lockfile management
- **Use `uv add`/`uv remove`** - These commands update both `pyproject.toml` and `uv.lock`
- **Run `uv sync --extra dev`** after pulling changes to ensure your environment matches the
  lockfile
- **Commit `uv.lock`** to version control to ensure reproducible builds across all environments

## Frontend Development

The frontend is a modern React application built with Vite. All frontend code is in the `frontend/`
directory.

```bash
# Install dependencies
npm install --prefix frontend

# Start dev server (starts both backend and frontend with HMR)
poe dev

# Build for production
npm run build --prefix frontend
```

See [frontend/CLAUDE.md](frontend/CLAUDE.md) for detailed frontend development guidance, testing
patterns, and MSW setup.

## Development Commands

### Linting and Type Checking

```bash
# Lint entire codebase (src/ and tests/)
scripts/format-and-lint.sh

# Lint specific Python files only
scripts/format-and-lint.sh path/to/file.py path/to/another.py

# Lint only changed Python files (useful before committing)
scripts/format-and-lint.sh $(git diff --name-only --cached | grep '\.py$')
```

This script runs: `ruff check --fix`, `ruff format`, `basedpyright`, `pylint`, and code conformance
checks.

**Code Conformance**: The project uses ast-grep to enforce pattern-based rules (e.g., banning
`asyncio.sleep()` in tests). Rules are defined in `.ast-grep/rules/`. See
[.ast-grep/rules/README.md](.ast-grep/rules/README.md) for active rules and
[.ast-grep/EXEMPTIONS.md](.ast-grep/EXEMPTIONS.md) for exemption guidance.

**IMPORTANT**: `scripts/format-and-lint.sh` MUST pass before committing. NEVER use
`git commit --no-verify` -- all lint failures must be fixed or properly disabled.

### Using the `llm` CLI

- `llm -f myscript.py 'explain this code'` - Analyze a script
- `git diff | llm -s 'Describe these changes'` - Understand code changes
- `llm -f error.log 'debug this error'` - Debug from log files
- `llm -f file1.py -f file2.py 'how do these interact?'` - Analyze multiple files

### Testing

```bash
# Run all tests
poe test  # Note: You will need a long timeout - something like 15 minutes

# Run tests with PostgreSQL (production database)
poe test-postgres  # Quick mode with -xq
poe test-postgres-verbose  # Verbose mode with -xvs

# Run specific test files
pytest tests/functional/test_specific.py -xq
```

See [tests/CLAUDE.md](tests/CLAUDE.md) for comprehensive testing guidance including:

- Testing principles and patterns
- Test fixtures documentation
- Database backend selection (SQLite vs PostgreSQL)
- CI debugging and troubleshooting

### Running the Application

```bash
# Development mode with hot-reloading (recommended)
poe dev
# Access the app at http://localhost:5173 (or http://devcontainer-backend-1:5173 in dev container)

# Main application entry point (production mode)
python -m family_assistant

# Backend API server only (for testing)
poe serve
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

### Environment Variables for Push Notifications

The following environment variables are required for PWA push notification functionality:

- **`VAPID_PRIVATE_KEY`** - VAPID private key for signing push messages

  - Format: Raw key bytes encoded with URL-safe base64, no padding
  - Generate using: `python scripts/generate_vapid_keys.py`
  - Example: `a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2`

- **`VAPID_CONTACT_EMAIL`** - Admin contact email for VAPID 'sub' claim

  - Format: `mailto:admin@example.com` or similar email
  - Used for notifications when subscriptions fail
  - Example: `mailto:admin@example.com`

- **`VAPID_PUBLIC_KEY`** - (Optional) VAPID public key

  - Format: Same URL-safe base64 encoding as private key
  - Auto-derived from private key if not provided
  - Needed if you want to explicitly provide the public key for client distribution

**Key Generation**:

```bash
# Generate a new VAPID key pair
python scripts/generate_vapid_keys.py

# Output format:
# VAPID_PRIVATE_KEY=<url-safe-base64-no-padding>
# VAPID_PUBLIC_KEY=<url-safe-base64-no-padding>
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

# Search with wildcards
symbex 'test_*'
symbex '*Tool.*'

# Search in specific files
symbex MyClass -f src/family_assistant/assistant.py
```

### Making Large-Scale Changes: Prefer `ast-grep`

`ast-grep` is available for making mechanical syntactic changes and is the tool of choice in most
cases.

**Note**: Use `ast-grep scan` for applying complex rule-based transformations (not `ast-grep run`).
The `scan` command supports YAML rule files and inline rules with `--inline-rules`.

**Quick example:**

```bash
# Simple pattern/replace
ast-grep -U -p 'oldFunction($$$ARGS)' -r 'newFunction($$$ARGS)' .

# Complex transformation with conditions
ast-grep -U --inline-rules '
id: my-transformation
language: python
rule:
  pattern: my_pattern($$$ARGS)
  not:
    has:
      pattern: required_arg = $_
  fix: my_pattern($$$ARGS, required_arg=default)
' .
```

See [docs/development/ast-grep-recipes.md](docs/development/ast-grep-recipes.md) for detailed
transformation recipes and patterns.

## Architecture Overview

Family Assistant is an LLM-powered application for family information management and task
automation. It provides multiple interfaces (Telegram, Web UI, Email webhooks) and uses a modular
architecture built with Python, FastAPI, and SQLAlchemy.

**For detailed architecture documentation**, see
[docs/architecture-diagram.md](docs/architecture-diagram.md) which provides a comprehensive visual
overview of:

- System architecture and component interactions
- Data flows and processing pipelines
- Core components and their responsibilities

### Key Design Patterns

- **No Mutable Global State**: Except in the very outer layer of the application.
- **Repository Pattern**: Data access logic encapsulated in repository classes, accessed via
  DatabaseContext
- **Dependency Injection**: Non-trivial objects with external dependencies should be created using
  dependency injection. Core services should accept dependencies as constructor arguments rather
  than creating them internally.
- **Testing with Real/Fake Dependencies**: Prefer using real or fake dependencies over mocks in
  tests, especially functional tests. Mocks should only be used for external services where fakes
  are not practical (e.g., Telegram). This ensures tests are more realistic and less brittle.
- **Protocol-based Interfaces**: Uses Python protocols for loose coupling (ChatInterface,
  LLMInterface, EmbeddingGenerator)
- **Async/Await**: Fully asynchronous architecture using asyncio
- **Context Managers**: Database operations use context managers for proper resource cleanup
- **Retry Logic**: Built-in retry mechanisms for transient failures
- **Event-Driven**: Loosely coupled components communicate via events

## Security Considerations

### Rule of Two for AI Agent Security

Family Assistant follows the **"Rule of Two"** principle from
[Meta's practical AI agent security framework](https://ai.meta.com/blog/practical-ai-agent-security/).
This principle helps mitigate prompt injection vulnerabilities by ensuring agents satisfy no more
than two of the following three properties in any given session:

1. **[A]** Processing untrustworthy inputs
2. **[B]** Accessing sensitive systems or private data
3. **[C]** Changing state or communicating externally

By restricting agents to any two properties simultaneously, we prevent complete exploit chains that
could lead to data exfiltration or unauthorized actions.

#### Trust Boundaries in Family Assistant

When evaluating input trust levels:

- **Trusted inputs**: Content directly created or fully vetted by authorized users

  - Direct messages from authorized users via Telegram or Web UI
  - Notes, tasks, and calendar events created by the user
  - Forwarded messages from known contacts (user has implicitly vetted the content)

- **Untrusted inputs**: Content from external sources that may not have been fully vetted

  - Emails received via webhooks (sender may be forged, content may contain injections)
  - Forwarded emails (original sender unknown, headers may be manipulated)
  - Web-scraped content or external API responses
  - Any content from unauthenticated sources

#### Implementation Strategy

The current architecture primarily operates in **[BC]** mode:

- Input filtering and authentication ensure only authorized users can interact with the system
- The agent can access sensitive data (notes, calendar, contacts) and take actions (creating tasks,
  sending notifications)
- This approach requires maintaining strong authentication and authorization at the interface
  boundaries

**Important Limitations**: The Rule of Two specifically addresses prompt injection risks. It does
not protect against other AI agent vulnerabilities such as:

- Hallucinations or incorrect information
- Excessive privileges or over-permissioned tools
- Agent mistakes or unintended actions
- Traditional security vulnerabilities (SQL injection, XSS, etc.)

The Rule of Two complements—rather than replaces—traditional security practices like least-privilege
access, input validation, and defense-in-depth approaches.

## Development Guidelines

- ALWAYS make a plan before you make any nontrivial changes.
- ALWAYS ask the user to approve the plan before you start work. In particular, you MUST stop and
  ask for approval before doing major rearchitecture or reimplementations, or making technical
  decisions that may require judgement calls.
- Significant changes should have the plan written to docs/design for approval and future
  documentation.
- When completing a user-visible feature, always update docs/user/USER_GUIDE.md and tell the
  assistant how it works in the system prompt in prompts.yaml or in tool descriptions. This is NOT
  optional or low priority.
- When solving a problem, always consider whether there's a better long term fix and ask the user
  whether they prefer the tactical pragmatic fix or the "proper" long term fix. Look out for design
  or code smells. Refactoring is relatively cheap in this project - cheaper than leaving something
  broken.
- IMPORTANT: You NEVER leave tests broken. We do not commit changes that cause tests to break. You
  NEVER make excuses like saying that test failures are 'unrelated' or 'separate issues'. You ALWAYS
  fix ALL test failures, even if you don't think you caused them.
- **Assumption about test failures**: You are responsible for fixing all test failures, even if you
  believe they are pre-existing. Since the project never commits with failing tests, any failure you
  encounter should be treated as a result of your changes. If you suspect a test is flaky, you may
  try re-running `poe test` to confirm, but you must ultimately resolve all failures before
  committing.
- **Hook bypassing**: NEVER attempt to bypass pre-commit hooks, PreToolUse hooks, or any other
  verification hooks (e.g., using `--no-verify`, `--no-gpg-sign`) without explicit permission from
  the user. These hooks exist to enforce quality standards and prevent broken code from being
  committed.

### Debugging and Change Verification

Once you've implemented a change, you ALWAYS go through the following algorithm:

1. Run scripts/format-and-lint.sh to check for linter errors.
2. Make sure that you have tests covering the new functionality, and that they pass.
3. Run a broad subset of tests related to your fixes.
4. Run `poe test` for final verification - this is what runs in CI and it runs all tests and
   linters.

You NEVER push new changes or make a PR if `poe test` does not pass. We do not merge PRs with
failing tests or linter errors.

### Planning Guidelines

- Always break plans down into meaningful milestones that deliver incremental value, or at least
  which can be tested independently. This is key to maintaining momentum.
- Do NOT give timelines in weeks or other units of time. Development on this project does not
  proceed in this manner as a hobby project predominantly developed using LLM assistance tools like
  Claude Code.

### Adding New Tools

Tools must be registered in TWO places:

1. **In the code** (`src/family_assistant/tools/__init__.py`)
2. **In the configuration** (`config.yaml`)

See [src/family_assistant/tools/CLAUDE.md](src/family_assistant/tools/CLAUDE.md) for complete tool
development guidance.

### Adding New UI Endpoints

When adding new web API endpoints:

1. Create your router in `src/family_assistant/web/routers/`
2. **Important**: Add your new endpoint to the appropriate test files in `tests/functional/web/` to
   ensure it's tested for basic functionality

See [src/family_assistant/web/CLAUDE.md](src/family_assistant/web/CLAUDE.md) for web API development
guidance.

Note: UI pages are now handled entirely by the React frontend. If you need to add new UI views,
create React components in the `frontend/` directory rather than server-side endpoints.

## Important Notes

- Always make sure you start with a clean working directory. Commit any uncommitted changes.

- NEVER revert existing changes without the user's explicit permission.

- Always check that linters and tests are happy when you're finished.

- Always commit changes after each major step. Prefer many small self contained commits as long as
  each commit passes lint checks.

- **Important**: When adding new imports, add the code that uses the import first, then add the
  import. Otherwise, a linter running in another tab might remove the import as unused before you
  add the code that uses it.

- Always use symbolic SQLAlchemy queries, avoid literal SQL text as much as possible. Literal SQL
  text may break across engines.

- **Database Access Pattern**: Use the repository pattern via DatabaseContext:

  ```python
  from family_assistant.storage.context import DatabaseContext

  async with DatabaseContext() as db:
      # Access repositories as properties
      await db.notes.add_or_update(title, content)
      tasks = await db.tasks.get_pending_tasks()
      await db.email.store_email(email_data)
  ```

- **SQLAlchemy Count Queries**: When using `func.count()` in SQLAlchemy queries, always use
  `.label("count")` to give the column an alias:

  ```python
  query = select(func.count(table.c.id).label("count"))
  row = await db_context.fetch_one(query)
  return row["count"] if row else 0
  ```

  This avoids KeyError when accessing the result.

- **SQLAlchemy func imports**: To avoid pylint errors about `func.count()` and `func.now()` not
  being callable, import func as:

  ```python
  from sqlalchemy.sql import functions as func
  ```

  instead of:

  ```python
  from sqlalchemy import func
  ```

  This resolves the "E1102: func.X is not callable" errors while maintaining the same functionality.

## File Management Guidance

- Put temporary files in the repo somewhere. scratch/ is available for truly temporary files but
  files of historical interest can go elsewhere

## DevContainer

The development environment runs using Docker Compose with persistent volumes for:

- `/workspace` - The project code
- `/home/claude` - Claude's home directory with settings and cache
- PostgreSQL data

### Building and Deploying

- To build and push the development container, use: `.devcontainer/build-and-push.sh [tag]`
- If no tag is provided, it defaults to timestamp format: `YYYYMMDD_HHMMSS`
- Example: `.devcontainer/build-and-push.sh` (uses timestamp tag)
- Example: `.devcontainer/build-and-push.sh v1.2.3` (uses custom tag)
- This script builds the container with podman and pushes to the registry

### Automatic Git Synchronization

The dev container automatically pulls the latest changes from git when Claude is invoked:

- Runs `git fetch` and `git pull --rebase` on startup
- Safely stashes and restores any local uncommitted changes
- If conflicts occur, reverts to the original state to avoid breaking the workspace
- This ensures the persistent workspace stays synchronized with the remote repository

### Container Architecture

The Docker Compose setup runs three containers:

1. **postgres** - PostgreSQL with pgvector extension for local development
2. **backend** - Runs the backend server and frontend dev server via `poe dev`
3. **claude** - Runs claude-code-webui on port 8080 with MCP servers configured
