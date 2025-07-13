# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## Style

- Comments are used to explain implementation when it's unclear. Do NOT add comments that are
  self-evident from the code, or that explain the code's history (that's what commit history is
  for). No comments like `# Removed db_context`.

## Development Guidelines

- NEVER assume that linter errors are false positives unless you have very clear evidence proving
  it. Linters in this project are generally set up to be correct. If there is a false positive you
  MUST document your evidence for that alongside the ignore comment or wherever you disable it.
- NEVER leave linting errors unfixed. All code must pass linting checks before completion
- When stuck debugging complex issues or needing a second opinion, use the `llm` CLI tool:
  - `cat myscript.py | llm 'explain this code'` - Analyze a script
  - `git diff | llm -s 'Describe these changes'` - Understand code changes
  - `llm -f error.log 'debug this error'` - Debug from log files
  - `cat file1.py file2.py | llm 'how do these interact?'` - Analyze multiple files
  - Use `llm chat` for multi-line inputs (paste errors/tracebacks with `!multi` and `!end`)

## Linting and Formatting

The project uses several linting and formatting tools. Use these poe commands:

- `poe lint` - Run full linting suite (ruff, basedpyright, pylint in parallel) - takes ~4 minutes
- `poe lint-fast` - Run only fast checks (ruff check/format, mdformat) - completes in ~3 seconds
- `poe format` - Format code only (ruff format + mdformat for markdown files)

The linting script (`scripts/format-and-lint.sh`) runs in two phases:

1. **Fast sequential checks** (fail fast): ruff check, ruff format, mdformat
2. **Parallel deep analysis**: basedpyright and pylint run concurrently

If ruff check fails, it will show suggested fixes including unsafe ones with
`--unsafe-fixes --diff`.

## Testing

- Try pytest --json-report for detailed test results - can query with jq ... .report.json
- NEVER leave tests broken or failing. If a test is failing and cannot be fixed immediately, it MUST
  be skipped with a pytest.skip() or @pytest.mark.skip decorator, along with a detailed comment
  explaining why it's being skipped and what needs to be done to fix it
- When running scripts/format-and-lint.sh or poe test, use a timeout of 10 minutes to ensure tests
  have enough time to complete
- `poe test` - Runs linting and tests with smart parallel execution (tests start after type checking
  begins)

## DevContainer

The development environment runs in Kubernetes using a StatefulSet with persistent volumes for:

- `/workspace` - The project code (20Gi)
- `/home/claude` - Claude's home directory with settings and cache (5Gi)
- PostgreSQL data (5Gi)

### Building and Deploying

- To build and push the development container, use: `.devcontainer/build-and-push.sh [tag]`
- If no tag is provided, it defaults to timestamp format: `YYYYMMDD_HHMMSS`
- Example: `.devcontainer/build-and-push.sh` (uses timestamp tag)
- Example: `.devcontainer/build-and-push.sh v1.2.3` (uses custom tag)
- This script builds the container with podman, pushes to the registry, and updates the Kubernetes
  StatefulSet

### Automatic Git Synchronization

The dev container automatically pulls the latest changes from git when Claude is invoked:

- Runs `git fetch` and `git pull --rebase` on startup
- Safely stashes and restores any local uncommitted changes
- If conflicts occur, reverts to the original state to avoid breaking the workspace
- This ensures the persistent workspace stays synchronized with the remote repository

### Container Architecture

The StatefulSet runs three containers:

1. **postgres** - PostgreSQL with pgvector extension for local development
2. **backend** - Runs the backend server and frontend dev server via `poe dev`
3. **claude** - Runs claude-code-webui on port 8080 with MCP servers configured
