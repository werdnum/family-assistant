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

Delegation to and from the engineer profile is gated by confirmation rather than blocked outright:

- **Into the engineer** (`delegate_to_service` with `target_service_id: "engineer"`): a `confirm`
  rule (priority 99) is present in every profile that can delegate at all. Profiles that inherit
  `default_profile_settings` (e.g. `default_assistant`) get it from there; profiles that replace
  `tools_policy` wholesale and still permit `delegate_to_service` — `browser_profile`, `telephone`,
  and `complex_tasks` — each carry their own copy of the gate. The delegation tool only checks the
  *source* profile's policy (and the target's `allowed_delegation_sources`), so the gate must live
  on every source that can reach the engineer; otherwise a wholesale-replacing profile would allow
  the engineer unconditionally.
- **Out of the engineer**: the engineer's `tools_policy` allows `delegate_to_service` with a
  `confirm` decision, so the engineer can hand a fix or follow-up action to another profile — but
  only after the user confirms. Higher-priority (99) `deny` rules block hand-offs to the internal /
  external-caller profiles that the ordinary delegating policies also deny (`reminder`,
  `event_handler`, `telephone_external`), so confirmation cannot be used to start those reserved
  profiles.

The confirmation requirement preserves the engineer's read-only posture in practice: a human always
approves before the engineer hands work to a *different* profile (the engineer diagnoses and
reports; a human or another profile implements fixes), while still letting investigation and
hand-off flow without a hard block. The `delegate_to_service` confirmation prompt shows the full
target, the **complete** request text, and any attachment ids. So the approver can never
rubber-stamp a silently-cut request, `delegate_to_service` refuses any request longer than
`MAX_DELEGATION_REQUEST_CHARS` (3000 — well above the generic 1200-char field bound, and sized to
keep the whole prompt within Telegram's single-message confirmation budget): the tool returns an
error instead of delegating, and the confirmation prompt for such a request states plainly that the
hand-off will be refused rather than displaying a partial body. Bulk content therefore belongs in an
attachment (referenced via `attachment_ids`), not the request string. Read-only delegation status
tools (`get_delegation_status`, `list_delegations`) are allowed without confirmation so the engineer
can track an async hand-off.

**Self-delegation.** `engineer → engineer` is *not* confirm-gated: the runtime injects a synthetic
self-delegation `ALLOW` rule (in `_build_profile_policy_engine`) that, by design, lets every profile
reach itself without confirmation and outranks the profile's own rules. This is intentional and
consistent with the project-wide invariant that self-delegation is never a privilege escalation — an
`engineer → engineer` hand-off stays entirely within the same read-only sandbox and confers no new
capability. The confirmation gate therefore applies to delegation *between distinct* profiles, which
is where the trust boundary is actually crossed.

### Confirmation for Side Effects

`create_github_issue` is the only tool that has external side effects (creating an issue on GitHub).
The engineer profile's `tools_policy` requires confirmation for that tool so the user must approve
before execution.

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

Activate via the `/engineer` slash command or by delegating to the `engineer` profile (delegation
into the engineer requires user confirmation).

## History

Originally proposed in PR #402 (November 2025) by google-labs-jules[bot]. That PR went through
multiple review cycles but became impractical to rebase (200+ commits behind). This implementation
incorporates the design decisions from those reviews while following current project conventions.
