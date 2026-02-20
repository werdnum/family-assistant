# Engineer Processing Profile

## Overview

The engineer processing profile provides read-only diagnostic access to the application for
investigating and debugging issues. It enables an LLM assistant to read source code, query the
database, examine error logs, and file bug reports via GitHub issues.

## Motivation

When debugging application issues (e.g., "Why isn't my daily brief firing?"), the assistant needs
access to application internals that aren't available through the standard tools:

- **Source code** to understand implementation
- **Database state** to examine application data
- **Error logs** to find recent failures
- **Source search** to trace code paths

The engineer profile provides these capabilities in a safe, read-only manner.

## Design Decisions

### Read-Only Enforcement

Database queries are protected by two layers:

1. **sqlparse validation**: Only SELECT statements are permitted. The query is parsed using sqlparse
   and each statement type is checked before execution.
2. **SET TRANSACTION READ ONLY** (PostgreSQL only): As defense-in-depth, the transaction is set to
   read-only mode before executing the query. This provides database-level protection even if
   sqlparse validation has a bug.

### Path Traversal Protection

Source code tools validate all file paths using `PROJECT_ROOT` from `family_assistant.paths`:

- All paths are resolved and checked to be within the project root
- Symlink traversal is prevented by using `.resolve()` before the check
- Both `read_source_file` and `search_source_code` enforce this boundary

### Async I/O

- File reads use `aiofiles` (consistent with `workspace_files.py` patterns)
- Source code search uses `asyncio.create_subprocess_exec` with ripgrep
- GitHub issue creation uses async `httpx`
- File stat checks use `asyncio.to_thread`

### Delegation Security

The profile uses `delegation_security_level: "blocked"` to prevent the engineer from delegating to
other profiles. This is intentional: the engineer diagnoses and reports (via GitHub issue); a human
or the main assistant implements fixes.

### Confirmation for Side Effects

`create_github_issue` is the only tool that has external side effects (creating an issue on GitHub).
It is listed in `confirm_tools` so the user must approve before execution.

## Tools

| Tool                  | Purpose                                             | Side Effects                          |
| --------------------- | --------------------------------------------------- | ------------------------------------- |
| `read_source_file`    | Read project source files with optional line ranges | None                                  |
| `search_source_code`  | Search codebase using ripgrep patterns              | None                                  |
| `query_database`      | Execute read-only SQL SELECT queries                | None                                  |
| `read_error_logs`     | Read application error/warning logs                 | None                                  |
| `create_github_issue` | File bug reports on GitHub                          | Creates issue (requires confirmation) |

The profile also includes existing read-only tools: `list_notes`, `get_note`, `search_documents`,
`get_full_document_content`, `get_user_documentation_content`, `list_pending_callbacks`,
`query_recent_events`, `list_automations`, `get_automation`, `get_automation_stats`.

## Relationship with spawn_worker

The engineer profile and `spawn_worker` serve complementary but distinct purposes:

- **Engineer profile**: Diagnoses issues by reading application state (DB, error logs, source code)
- **spawn_worker**: Implements fixes by executing code in an isolated container

They do not overlap: workers cannot access the database or error logs; the engineer cannot execute
code or modify files.

## Usage

Activate via the `/engineer` slash command or by delegating to the `engineer` profile.

## History

Originally proposed in PR #402 (November 2025) by google-labs-jules[bot]. That PR went through
multiple review cycles but became impractical to rebase (200+ commits behind). This implementation
incorporates the design decisions from those reviews while following current project conventions.
