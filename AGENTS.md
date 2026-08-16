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
- **[ios/CLAUDE.md](ios/CLAUDE.md)** - Native iOS app (SwiftUI, error reporting and telemetry lanes)
- **[scripts/CLAUDE.md](scripts/CLAUDE.md)** - Script development
- **[.github/workflows/CLAUDE.md](.github/workflows/CLAUDE.md)** - CI workflow development
- **[docs/development/ast-grep-recipes.md](docs/development/ast-grep-recipes.md)** - Code
  transformation recipes

## Style

- Place all imports at the top of the file, organized by the isort rules in `pyproject.toml`.
- Use type hints for all method parameters and return values.
- Comments are used to explain implementation when it's unclear. Do NOT add comments that are
  self-evident from the code, or that explain the code's history (that's what commit history is
  for). No comments like `# Removed db_context`.

## Development Setup

```bash
./scripts/setup-workspace.sh   # creates .venv, installs deps, hooks, frontend, browsers
source .venv/bin/activate
poe test                       # verify
```

The script installs Python deps with `uv sync --extra dev --extra pgserver` (dev tools plus the
PostgreSQL test server), installs pre-commit hooks, runs `npm ci --prefix frontend`, installs
Playwright browsers via `rebrowser_playwright`, and checks for GNU Parallel, which the default
adaptive `poe test` runner requires. The devcontainer includes Parallel; local hosts should install
the system package named `parallel`.

## Dependency Management

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`, managed with
[uv](https://docs.astral.sh/uv/).

- **Never use `uv pip install`** - it bypasses uv's resolution and the lockfile. Use
  `uv add`/`uv remove` (including `uv add --dev` and `uv add --optional <extra>`), which update both
  `pyproject.toml` and `uv.lock`.
- Commit `uv.lock`.
- Run `uv sync --extra dev` after pulling changes.
- Optional extras: `pgserver` (PostgreSQL test server), `local-embeddings` (local sentence
  transformer models, ~450MB — only needed instead of cloud embedding APIs).

## Frontend Development

The frontend is a React + Vite app in `frontend/`. Use `poe dev` to run backend and frontend
together with HMR; `npm run build --prefix frontend` for a production build. See
[frontend/CLAUDE.md](frontend/CLAUDE.md) for testing patterns and MSW setup.

## Development Commands

### Linting and Type Checking

```bash
scripts/format-and-lint.sh                       # entire codebase
scripts/format-and-lint.sh path/to/file.py       # specific files
scripts/format-and-lint.sh $(git diff --name-only --cached | grep '\.py$')
```

This runs `ruff check --fix`, `ruff format`, `basedpyright`, `pylint`, code conformance checks
(ast-grep pattern rules, e.g. banning `asyncio.sleep()` in tests) and the lint suppression budget
check. Rules live in `.ast-grep/rules/`; see [.ast-grep/rules/README.md](.ast-grep/rules/README.md)
and [.ast-grep/EXEMPTIONS.md](.ast-grep/EXEMPTIONS.md) for exemption guidance.

#### Lint Suppression Budgets

`.lint-budget.toml` caps how many violations of a given ruff rule may go unreported.
`scripts/check_suppression_budget.py` fails if a count rises above its budget, and lowers the budget
by itself when a count falls, so the number only ever goes down. Lowering exits nonzero on purpose:
the rewritten file has to reach the commit, or the ceiling stays high and the headroom you just
freed is available to the next suppression.

The count comes from ruff, not from reading the config — it is the number of diagnostics that exist
but are not reported. Two things follow. Every way of spelling a suppression is covered at once (a
code, a prefix such as `PLW`, a bare directive, a file-wide `per-file-ignores` entry), and a
file-wide entry is not a blank cheque: **adding another violation to an already-ignored file still
costs budget.**

A budgeted rule describes a habit rather than a single bug, which makes it cheaper to silence than
to fix — `PLW0717` (`too-many-statements-in-try-clause`) is the current entry, at a time when 278 of
its violations were being suppressed, most of them by file-wide entries covering `src/`. **When this
check fails, fix the code, do not raise the budget.** For `PLW0717` that means narrowing the `try`
to the statements that can actually raise and moving the rest out of it; lifting pure computation
into a helper called from inside the `try` keeps the exception handling identical. Raising a budget,
or removing a rule from the file, needs the user to agree first.

`scripts/format-and-lint.sh` must pass before committing, and never use `git commit --no-verify` —
lint failures must be fixed or properly disabled.

### Using the `llm` CLI

The `llm` CLI is available for ad-hoc analysis, e.g.
`llm -f file1.py -f file2.py 'how do these interact?'` or
`git diff | llm -s 'Describe these changes'`.

### Testing

```bash
poe test                    # everything (needs a long timeout — ~15 minutes)
poe test-postgres           # PostgreSQL (production database), quick mode
poe test-postgres-verbose   # PostgreSQL, verbose
pytest tests/functional/test_specific.py -xq
```

`poe test` writes a summary to `.poe-test-summary.txt` (RESULT, duration, which checks passed) —
useful when the full output is truncated in LLM tool contexts.

See [tests/CLAUDE.md](tests/CLAUDE.md) for testing principles, fixtures, database backend selection,
and CI debugging.

### Running the Application

```bash
poe dev                  # hot-reloading dev mode; http://localhost:5173
                         # (http://devcontainer-backend-1:5173 in the dev container)
python -m family_assistant   # production entry point
poe serve                # backend API server only
```

### Database Migrations

```bash
alembic revision --autogenerate -m "Description"
alembic upgrade head
```

Use `DATABASE_URL="sqlite+aiosqlite:///family_assistant.db"` with alembic when making a new
revision. Migrations run on startup.

### Deployment Configuration

Environment variables and operator-facing configuration are documented in
[docs/operations/CONFIGURATION_REFERENCE.md](docs/operations/CONFIGURATION_REFERENCE.md) — including
push notifications (VAPID and iOS APNs), email intake, the Google (Gmail & Drive) integration and
the message-history taint epoch, the read-only diagnostics token, embedding providers, and the
global tool policy. Look there before adding or changing anything that reads configuration, and
update it when you add a new setting.

### Gemini Computer Use (visual browser profile)

The `browser_visual_profile` drives a browser via Gemini's native computer-use capability, enabled
per profile with `enable_computer_use: true` on `processing_config`. It is Google-provider only:
setting the flag with a non-Google provider, or combining it with `retry_config`, is a startup
error. Prompt-injection detection is always on, and the model's action space, backend degradation
rules and safety-confirmation flow are documented in
[docs/design/gemini-computer-use-native.md](docs/design/gemini-computer-use-native.md).

### Code Generation

`poe symbols` regenerates `SYMBOLS.md`.

### Type Inference with MonkeyType

MonkeyType traces test execution to infer types, useful for annotating **untyped** functions. Trace
with `poe trace tests/unit/test_my_module.py`, inspect with
`monkeytype stub family_assistant.my_module`, then apply with
`monkeytype apply family_assistant.my_module` (which only touches untyped code) and run `poe lint`
to fix imports.

Only use `--ignore-existing-annotations` to narrow overly broad `Any` types in code you've verified
is called with specific types. It overwrites existing annotations with whatever the tests happened
to exercise, which wrecks protocol types (`LLMInterface` → `MockLLMClient`), optional parameters
(`Clock | None` → `None`), and pulls test mocks into production imports.

The `monkeytype.sqlite3` trace database is gitignored; `rm monkeytype.sqlite3` to clear it between
runs.

### Finding Symbol Definitions and Signatures

Use [symbex](https://github.com/simonw/symbex) to find definitions and signatures, e.g.
`symbex MyClass`, `symbex -s`, `symbex '*Tool.*'`,
`symbex MyClass -f src/family_assistant/assistant.py`.

### Making Large-Scale Changes: Prefer `ast-grep`

`ast-grep` is the tool of choice for mechanical syntactic changes. Use `ast-grep scan` (not
`ast-grep run`) for rule-based transformations — `scan` supports YAML rule files and
`--inline-rules`. Simple replacements work directly:

```bash
ast-grep -U -p 'oldFunction($$$ARGS)' -r 'newFunction($$$ARGS)' .
```

See [docs/development/ast-grep-recipes.md](docs/development/ast-grep-recipes.md) for detailed
transformation recipes and patterns.

## Architecture Overview

Family Assistant is an LLM-powered application for family information management and task
automation. It provides multiple interfaces (Telegram, Web UI, Email webhooks) and uses a modular
architecture built with Python, FastAPI, and SQLAlchemy. See
[docs/architecture-diagram.md](docs/architecture-diagram.md) for the detailed component and
data-flow documentation.

### Key Design Patterns

- **No Mutable Global State**: Except in the very outer layer of the application.
- **Repository Pattern**: Data access logic encapsulated in repository classes, accessed via
  DatabaseContext.
- **Dependency Injection**: Non-trivial objects with external dependencies should be created using
  dependency injection. Core services should accept dependencies as constructor arguments rather
  than creating them internally.
- **Testing with Real/Fake Dependencies**: Prefer real or fake dependencies over mocks, especially
  in functional tests. Mocks are only for external services where fakes are impractical (e.g.
  Telegram).
- **Protocol-based Interfaces**: Python protocols for loose coupling (ChatInterface, LLMInterface,
  EmbeddingGenerator).

## Security Considerations

### Rule of Two for AI Agent Security

Family Assistant follows the **"Rule of Two"** principle from
[Meta's practical AI agent security framework](https://ai.meta.com/blog/practical-ai-agent-security/):
an agent should satisfy no more than two of the following three properties in any given session, so
that no complete exfiltration or unauthorized-action chain exists.

1. **[A]** Processing untrustworthy inputs
2. **[B]** Accessing sensitive systems or private data
3. **[C]** Changing state or communicating externally

#### Trust Boundaries in Family Assistant

- **Trusted inputs**: direct messages from authorized users via Telegram or Web UI; notes, tasks and
  calendar events created by the user; forwarded messages from known contacts (implicitly vetted).
- **Untrusted inputs**: emails received via webhooks (sender may be forged, content may contain
  injections); forwarded emails (original sender unknown, headers manipulable); web-scraped content
  or external API responses; anything from unauthenticated sources.

The architecture primarily operates in **[BC]** mode: the agent can access sensitive data (notes,
calendar, contacts) and take actions (creating tasks, sending notifications), so it depends on
strong authentication and authorization at the interface boundaries.

#### Processing Profiles

**Processing profiles** implement Rule of Two constraints by adjusting available tools and
supervision requirements based on input trust level:

1. **Trusted Profile [BC]**: full tool access for direct user interactions via Telegram or Web UI;
   relies on authentication at the interface boundary.
2. **Untrusted-Readonly Profile [AB]**: can read sensitive data but cannot change state or
   communicate externally (tools disabled or requiring human approval). Example: "Summarize this
   email" — reads context, cannot send results anywhere.
3. **Untrusted-Sandboxed Profile [AC]**: cannot access sensitive user data, but can take scoped/rate
   limited actions. Example: "What's the weather like?" — calls external APIs, cannot read the
   user's calendar.
4. **Engineer Profile [B]**: read-only diagnostic access (source code, database, error logs, notes)
   for debugging the application. Used via `/engineer` or by delegating to the `engineer` profile.
   It cannot change state or communicate externally on its own; every side effect requires user
   confirmation — creating GitHub issues, reconnecting an MCP server, and delegation in either
   direction, so a human approves before the engineer hands off a fix or another profile hands it an
   investigation. Example: "Why isn't my daily brief firing?"
5. **Complex Tasks Profile [BC]**: full tool access via OpenAI GPT-5.6-sol (`gpt-5.6-sol`) at
   `reasoning_effort: high`, with a higher iteration limit (100) for deep multi-step reasoning. Used
   via `/complex` or delegation from the default assistant, which runs GPT-5.6-terra at medium
   effort with 50 iterations. **Not to be confused with `spawn_worker`**, which launches isolated
   coding agents (Claude Code / Gemini CLI) in sandboxed containers with NO access to Family
   Assistant tools or data. Use `complex_tasks` when the task needs FA context (notes, calendar,
   documents, Home Assistant, etc.); use `spawn_worker` for standalone coding or computing tasks.
6. **Media Analyst Profile [A]**: describes and transcribes audio, video, images and PDFs on Gemini,
   which is the only configured provider whose adapter represents audio and video at all. Delegated
   to with `attachment_ids` when a profile's own model cannot read an attachment. It reads untrusted
   media, so it holds neither [B] nor [C], and denying each takes more than an empty tool policy:
   context providers inject the user's notes, calendar and known users into every profile's system
   prompt by default, so they are turned off via `excluded_context_providers`; and
   `global_tools_policy` rules are injected at the `profile` layer, which outranks the `defaults`
   layer a profile's own `tools_policy` occupies, so a profile cannot refuse a global grant through
   its own policy at any priority — `excluded_global_tools` is the mechanism for that, denying in
   the same layer at a higher priority. All three globally granted tools are withheld here:
   `read_text_attachment` and `jq_query` resolve any attachment the acting user owns rather than
   only the current turn's artifacts, and `report_technical_problem` persists model-supplied text.
   The profile therefore reaches no tools at all. Deliberately has no `retry_config`: falling back
   to a provider that cannot read the media would return a confident description of nothing.
7. **Coder Profile [C]**: a coding agent — writes and runs code, works with files, reads the web —
   on Google's Antigravity managed agent (`antigravity-preview-05-2026` reasoning with
   `gemini-3.7-flash`), in a Google-hosted throwaway sandbox. Used via `/coder` or delegation. It
   acts but reads nothing of the household's: no aggregated context, and the agent runs server-side
   with no FA tool surface, so it works only from the request text. As with `media_analyst`, a
   deny-by-default `tools_policy` does not achieve that alone -- the three globally granted tools
   are withheld via `excluded_global_tools`, so the profile is not advertised as holding tools it
   says it has none of; and it has no `retry_config`, because a fallback chat model would answer
   from its own knowledge instead of running the task. **Three ways to reach a coding agent, and
   they are not interchangeable**: `coder` for self-contained code and computation, with the result
   returned into the conversation; `spawn_worker` when the agent must read or write the *shared
   workspace* in our own sandbox; `complex_tasks` when the task needs FA context (notes, calendar,
   documents). See
   [docs/design/gemini-antigravity-managed-agent.md](docs/design/gemini-antigravity-managed-agent.md).

The Rule of Two addresses prompt injection specifically; it complements rather than replaces
least-privilege access, input validation, and defense in depth.

## Native Codex PR Review Policy

- If you are performing a PR review (including native Codex review), read and follow
  `REVIEW_GUIDELINES.md` before writing feedback.
- Use native Codex GitHub review (`@codex review`).

## Development Guidelines

- Make a plan before any nontrivial change. Ask for plan approval when there are significant
  decisions, tradeoffs, or ambiguity requiring user input (major rearchitecture, reimplementation,
  judgement calls). If the plan just restates a clear user request with no meaningful choices, skip
  approval and proceed.
- Significant changes should have the plan written to docs/design for approval and future
  documentation.
- When completing a user-visible feature, update the user documentation and tell the assistant how
  it works in the system prompt in prompts.yaml or in tool descriptions. This is not optional. See
  **[Writing User Documentation](#writing-user-documentation)** below for where the change belongs.
- When solving a problem, consider whether there's a better long term fix and ask the user whether
  they prefer the tactical pragmatic fix or the "proper" one. Look out for design or code smells.
  Refactoring is relatively cheap in this project - cheaper than leaving something broken.
- **Behaviour-altitude principle:** Common/reasonable scenarios should get ideal behaviour;
  uncommon/unusual scenarios should get reasonable (correct, non-broken) behaviour, not necessarily
  ideal behaviour. Do not build elaborate machinery to give rare scenarios ideal behaviour when
  reasonable behaviour suffices; that trades disproportionate complexity for negligible benefit and
  tends to spawn the machinery-edge-case spiral (see the cost/benefit gate in
  `REVIEW_GUIDELINES.md`).
- **Never leave tests broken.** Fix all test failures rather than dismissing them as 'unrelated' or
  'pre-existing' — you are responsible for failures in or near the code you changed. For flakiness
  in areas completely unrelated to your change, see "Debugging and Change Verification" below.
- **Hook bypassing**: never bypass pre-commit hooks, PreToolUse hooks, or other verification hooks
  (e.g. `--no-verify`, `--no-gpg-sign`) without explicit permission from the user.

### Refactoring and Error Handling

See **[docs/development/error-handling.md](docs/development/error-handling.md)** for comprehensive
error handling guidelines. Key principles:

- **Prevention First**: Detect errors at development time with type checking and tests rather than
  handling them at runtime.
- **No Silent Failures**: Never silently drop errors or user input. A silent failure at one layer
  often causes a confusing error at another layer (e.g., user sends video → video silently stripped
  → empty input error shown to user).
- **Fail Fast**: Do not implement "graceful fallbacks" that mask bugs (e.g., returning `None` or
  empty lists when an unexpected error occurs). Errors should look like errors. Let exceptions
  propagate so they can be caught by tests and debugging tools.
- **Handle at the Right Layer**: Only catch errors at layers that have enough context to handle them
  correctly. When in doubt, let errors propagate.
- **Correct vs. Possible**: Don't ask "can we continue?" but "is it correct to continue?" Prefer
  enforcing invariants over "graceful degradation" that produces confusing results.
- **No Backwards Compatibility for Internal Code**: When migrating from implementation X to Y,
  delete X immediately. Do not keep backwards compatibility layers. Rely on the type checker and
  tests to identify all places that need to be updated.

### Debugging and Change Verification

Once you've implemented a change:

1. Run scripts/format-and-lint.sh to check for linter errors.
2. Make sure that you have tests covering the new functionality, and that they pass.
3. Run all tests plausibly impacted by your change.
4. Run `poe test` after major changes for final verification - this is what runs in CI and it runs
   all tests and linters. It doesn't need to be run every single time you push a small fix; CI runs
   the full suite on every push regardless, so skipping a local `poe test` defers verification to
   CI, it does not skip it.

While long-running verification commands such as `poe test` are active, do not provide play-by-play
progress updates. Report only when action is needed or when it finishes.

Flakiness anywhere near the modified code should be addressed even if it expands the scope of the PR
somewhat. Flakiness in areas completely unrelated to the modified code can be ignored if the test
passes on rerun — if it's persistent, mark the test `@flaky` and/or file a GitHub issue.

Never push changes or open a PR with failing tests or linter errors caused by your change.

### Planning Guidelines

- Break plans into meaningful milestones that deliver incremental value, or that can at least be
  tested independently. This is key to maintaining momentum.
- Do not give timelines in weeks or other units of time. Development on this project does not
  proceed in this manner as a hobby project predominantly developed using LLM assistance tools.

### Writing User Documentation

User documentation lives in `docs/user/`, **split by topic, one file per topic**. This matters
because the assistant reads these files at runtime with `get_user_documentation_content`: it can
only fetch a whole file, so a sprawling file wastes context and a well-named small one doesn't.

- **Put the change in the topic file that already covers it.** `docs/user/USER_GUIDE.md` is a short
  index, not a place to document features. If you add an entry there, it's a one-line row pointing
  at a topic guide.
- **Add a new file when a topic genuinely doesn't exist yet**, and add it to the index. Name it for
  what a reader would search for (`smart-home.md`, not `integrations2.md`) — the assistant sees only
  the filenames when choosing what to read. Start it with an H1 and a one-line **What's here:**
  summary.
- **A file that grows past a few hundred lines is a signal to split it**, not to keep appending.
- **Write for the person using the assistant.** Say what the feature does and how to ask for it.
  Skip implementation detail, internal identifiers, and rationale that only matters to whoever built
  it.
- **Do not document the change history.** No "NEW:", no "this now works", no "previously this was
  broken". Describe the current behaviour as though it had always been that way; the commit history
  is the record of what changed.
- **Operator-facing material goes to `docs/operations/`**, primarily
  [CONFIGURATION_REFERENCE.md](docs/operations/CONFIGURATION_REFERENCE.md). Environment variables,
  keys, OAuth client setup, and policy tuning do not belong in a user guide — link to them instead.
- **Design rationale goes to `docs/design/`.** A user guide may link to a design doc; it should not
  reproduce it.

### Adding New Tools

Tools must be registered in TWO places: in the code (`src/family_assistant/tools/__init__.py`) and
in the configuration (`defaults.yaml` / `config.yaml`). See
[src/family_assistant/tools/CLAUDE.md](src/family_assistant/tools/CLAUDE.md) for complete tool
development guidance.

Note that a top-level `global_tools_policy` is injected into every profile's tool policy, so a newly
added tool may already be reachable without a per-profile rule — see
[docs/operations/CONFIGURATION_REFERENCE.md](docs/operations/CONFIGURATION_REFERENCE.md) for the
semantics.

### Adding New UI Endpoints

UI pages are handled entirely by the React frontend — add new views as React components in
`frontend/`, not as server-side endpoints. For API endpoints, see
[src/family_assistant/web/CLAUDE.md](src/family_assistant/web/CLAUDE.md), which covers router
placement, auth, and the error-vs-telemetry reporting lanes.

## Important Notes

- Always make sure you start with a clean working directory. Commit any uncommitted changes.

- Before starting any new work, switch to `main`, pull the latest changes, and create a fresh branch
  from updated `main`. Do not reuse whatever branch happened to be active from previous work.

- Never revert existing changes without the user's explicit permission.

- Always check that linters and tests are happy when you're finished.

- Commit after each major step. Prefer many small self contained commits as long as each commit
  passes lint checks.

- Making a non-draft pull request is a required part of implementation once changes are complete and
  verified, unless the user explicitly asks not to open a PR.

- When adding new imports, add the code that uses the import first, then add the import. Otherwise a
  linter running in another tab might remove the import as unused before you add the code.

- Always use symbolic SQLAlchemy queries, avoid literal SQL text as much as possible. Literal SQL
  text may break across engines.

- **Database Access Pattern**: use the repository pattern via a `Database` handle. It is **not** a
  context manager: every operation on it opens its own transaction and commits before returning, so
  the write is durable when the call returns and the handle can be passed freely across turns,
  tasks, and tool calls.

  ```python
  from family_assistant.storage.database import Database

  db = Database(engine)
  await db.notes.add_or_update(title, content)   # committed
  tasks = await db.tasks.get_pending_tasks()
  ```

  Reach for `DatabaseTransaction` only where rollback is load-bearing — a sequence that must not be
  half-applied:

  ```python
  async with db.transaction() as txn:
      run_id = await txn.delegation_runs.create_run(...)
      await txn.tasks.enqueue(DELEGATED_PROFILE_RUN_TASK_TYPE, ...)
  ```

  Three rules follow, all enforced at runtime rather than by review:

  - **Nothing inside a transaction may reach the handle.** Use `txn.<repository>`, hoist handle work
    to before the block, or — for genuinely detached work — `spawn_detached()`. A handle operation
    under an open transaction raises `AmbientTransactionError` in production on both backends,
    because the alternatives are a deadlock on SQLite and a silent escape from rollback on
    PostgreSQL.
  - **Converting a site to a transaction includes auditing its call tree.** Everything reached from
    inside must take the `DatabaseTransaction` or touch no database at all — including chat
    interfaces, which resolve targets and fetch attachments from their own handle while sending.
  - **No transaction may span an LLM call, a tool dispatch, or a network round trip.** Split the
    work; do not widen the block.

  For multi-statement repository methods, use the closure form `await db.atomic(body)` rather than
  the block: it can be replayed after a rollback, which is what makes serialization failures and
  deadlocks retryable. See [docs/design/db-commit-as-you-go.md](docs/design/db-commit-as-you-go.md).

- **SQLAlchemy Count Queries**: give `func.count()` an alias with `.label("count")` — this avoids a
  KeyError when accessing the result:

  ```python
  query = select(func.count(table.c.id).label("count"))
  row = await db_context.fetch_one(query)
  return row["count"] if row else 0
  ```

- **SQLAlchemy func imports**: import `from sqlalchemy.sql import functions as func` rather than
  `from sqlalchemy import func`, which avoids pylint "E1102: func.X is not callable" errors for
  `func.count()` and `func.now()`.

## File Management Guidance

- Put temporary files in the repo somewhere. scratch/ is available for truly temporary files but
  files of historical interest can go elsewhere

## DevContainer

The development environment runs using Docker Compose with persistent volumes for `/workspace` (the
project code), `/home/claude` (Claude's settings and cache), and PostgreSQL data. The compose stack
runs **postgres** (with pgvector), **backend** (backend server plus frontend dev server via
`poe dev`), and **claude** (an agent shell with MCP servers configured; it runs `sleep infinity` and
exposes no port — the claude-code-webui setup lives in the separate k8s pod definition).

`.devcontainer/CLAUDE.devcontainer.md` documents that environment from the inside — logs, access
points, volume mounts. `setup-workspace.sh` installs it as `/home/claude/.claude/CLAUDE.md` so it
loads as user-level memory for agents running in the container, and skips the install when that path
is mounted read-only. Edit it there, not in this file.

### Building and Deploying

`.devcontainer/build-and-push.sh [tag]` builds the container with podman and pushes it to the
registry. Without an argument the tag defaults to a `YYYYMMDD_HHMMSS` timestamp.

### Automatic Git Synchronization

The dev container runs `git fetch` and `git pull --rebase` when Claude is invoked, stashing and
restoring local uncommitted changes. If conflicts occur it reverts to the original state rather than
breaking the workspace.
