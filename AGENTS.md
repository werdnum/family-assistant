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

This runs `ruff check --fix`, `ruff format`, `basedpyright`, `pylint`, and code conformance checks
(ast-grep pattern rules, e.g. banning `asyncio.sleep()` in tests). Rules live in `.ast-grep/rules/`;
see [.ast-grep/rules/README.md](.ast-grep/rules/README.md) and
[.ast-grep/EXEMPTIONS.md](.ast-grep/EXEMPTIONS.md) for exemption guidance.

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

### Environment Variables for Push Notifications

Required for PWA push notifications. Generate a key pair with
`python scripts/generate_vapid_keys.py`.

- **`VAPID_PRIVATE_KEY`** - Raw key bytes, URL-safe base64, no padding.
- **`VAPID_CONTACT_EMAIL`** - Admin contact for the VAPID `sub` claim, e.g.
  `mailto:admin@example.com`. Used for notifications when subscriptions fail.
- **`VAPID_PUBLIC_KEY`** - Optional, same encoding. Auto-derived from the private key if unset; set
  it explicitly if you want to control the value distributed to clients.

### Environment Variables for iOS Push Notifications (APNs)

Native iOS push is delivered through APNs using provider-token authentication with a `.p8` auth key.
The sender is enabled only when `APNS_TEAM_ID`, `APNS_KEY_ID`, `APNS_BUNDLE_ID` and a private key
are all configured. All map to the `apns` config section.

- **`APNS_TEAM_ID`** - Apple Developer Team ID, the JWT `iss` claim.
- **`APNS_KEY_ID`** - APNs auth key id, the JWT `kid` header.
- **`APNS_AUTH_KEY`** - Contents of the `.p8` private key (PEM). Secret, redacted from logged
  config.
- **`APNS_AUTH_KEY_PATH`** - Path to a `.p8` file (alternative to `APNS_AUTH_KEY`).
- **`APNS_BUNDLE_ID`** - App bundle id, sent as the `apns-topic` header.
- **`APNS_USE_SANDBOX`** - Default environment (`true` for sandbox) when a registered token does not
  specify one. Each device token carries its own `environment`, and a sandbox/production mismatch
  (`BadDeviceToken`) is auto-corrected on the next send.

Clients register device tokens via `POST /api/ios/push-tokens` and unregister via
`DELETE /api/ios/push-tokens/{device_token}` (both authenticated). See
[docs/design/ios_push_notifications.md](docs/design/ios_push_notifications.md) for the full design.

### Environment Variables for Email Intake

- **`MAILGUN_WEBHOOK_SIGNING_KEY`** - Maps to `email_intake.mailgun_webhook_signing_key`. Verifies
  incoming email webhooks; found in the Mailgun dashboard under Sending → Webhooks.

### Environment Variables for Google Integration (Gmail & Drive)

Per-user Gmail and Drive access needs an OAuth client from Google Cloud Console plus a Fernet key
for encrypting refresh tokens at rest. All three secrets must be present; if any is missing the
integration is disabled at startup with an error naming the unmet condition. See
`docs/design/user-scoped-google-data-access.md` and
[Connect your Google account](docs/user/USER_GUIDE.md#connect-your-google-account).

- **`GOOGLE_OAUTH_CLIENT_ID`** → `google_integration.oauth_client_id`.

- **`GOOGLE_OAUTH_CLIENT_SECRET`** → `google_integration.oauth_client_secret`. Secret; redacted from
  logged config and diagnostics export.

- **`CREDENTIAL_ENCRYPTION_KEY`** → `google_integration.credential_encryption_key`. URL-safe
  base64-encoded Fernet key, secret and redacted. Generate with:

  ```bash
  python -c "from family_assistant.services.credential_encryption import generate_key; print(generate_key())"
  ```

  Keep this key stable across deployments. A decryption failure (wrong or changed key) is treated as
  a configuration error: the stored connection row is left untouched, so restoring the correct key
  restores access without any user re-authorization.

**`google_integration` config section:**

```yaml
google_integration:
  oauth_client_id: ""
  oauth_client_secret: ""
  credential_encryption_key: ""
  scopes:
    - "https://www.googleapis.com/auth/gmail.readonly"
    - "https://www.googleapis.com/auth/gmail.compose"
    - "https://www.googleapis.com/auth/drive.readonly"
    - "https://www.googleapis.com/auth/drive.file"
  require_taint_enforcement: true
```

**Scopes allowlist semantics.** The `scopes` list *narrows* the grant — remove Drive scopes to get
Gmail-only, for example. Only scopes used by shipped deterministic tools are allowed; adding an
unsupported scope (`gmail.send`, `gmail.modify`, full `drive`, etc.) disables the integration with a
startup error. The identity scopes `openid` and `email` are always appended in code and are not
configurable. Tool registration follows the configured scopes: `gmail_search`, `gmail_get_message`
and `gmail_get_attachment` require `gmail.readonly`; `gmail_create_draft` requires `gmail.compose`;
`drive_search` registers with either `drive.readonly` or `drive.metadata.readonly`; `drive_get_file`
requires `drive.readonly`; `drive_write_file` requires `drive.file`. The draft tool never sends
email, although Google's `gmail.compose` scope itself also authorizes sending. Drive writes are
deterministically confined to the app-created Family Assistant folder and app-marked files within
it.

**Enablement conditions** (validated at startup): the three secrets are set; the `scopes` list
passes allowlist validation; and real web authentication is active (OIDC configured, `users` block
resolution in effect) — the development `test_user` mode shares one identity across all callers and
therefore refuses to enable this feature.

**`require_taint_enforcement`.** Defaults to `true`: the tools only register when
`taint_policy.mode` is `enforce` and the effective policy matrix floors the key exfiltration sinks
(`arbitrary_external_message`, `attacker_addressable_egress`, `sandbox_network`,
`sensitive_read_broadening`) at `confirm` for untrusted content. If the check fails the tools are
not registered and the integration status endpoint reports the unmet condition. Setting it to
`false` waives the check (logged at startup, surfaced on the status endpoint) and the tools register
regardless of taint mode.

**`taint_policy.history_taint_epoch`.** Optional timezone-aware ISO-8601 timestamp (quote it in
YAML; naive or unparseable values fail startup) granting a read-time amnesty to legacy
message-history taint metadata. Deployment-level only; profiles cannot set it.

- Rows persisted **before** the epoch contribute taint only from explicitly attributed sources: the
  synthetic `legacy_missing_taint_metadata` fallback and anonymous escalation artifacts are ignored
  and the row's tier is recomputed from what remains.
- Rows **at or after** the epoch are trusted as recorded; a post-epoch row missing metadata logs an
  ERROR as a write-path regression alarm, and anonymous post-epoch artifacts are read
  conservatively.
- One filter is timestamp-independent: sources carrying the `legacy_missing_taint_metadata` label
  *inside* persisted metadata are second-hand echoes of another row's read-time fallback, so they
  are dropped and the tier recomputed even for post-epoch rows. First-hand missing-metadata rows
  still escalate and fire the ERROR alarm.
- **Set the epoch to the instant this feature is DEPLOYED (or later), never earlier** (e.g. not the
  taint-metadata migration date `2026-07-06`): rows written before deploy may contain re-baked
  poison that the echo filter only partially neutralizes, so an earlier epoch would keep re-seeding
  it.
- `GET /api/diagnostics/taint-audit` reports the configured epoch and pre/post-epoch row splits, so
  the poison collapse can be verified before switching `taint_policy.mode` to `enforce`.

See [docs/design/taint-history-epoch-amnesty.md](docs/design/taint-history-epoch-amnesty.md).

### Environment Variable for Read-Only Diagnostics Access

- **`DIAGNOSTICS_READONLY_TOKEN`** - Optional shared secret granting read-only access to
  `GET /api/errors/`, `GET /api/errors/{id}`, `GET /api/errors/telemetry`,
  `GET /api/diagnostics/export`, `GET /api/diagnostics/taint-audit`, and
  `GET /api/debug/profiles/tools` without a user session or API token — intended for an external
  monitor that only pulls diagnostics. Supply it as `Authorization: Bearer <token>` or
  `X-API-Token: <token>`; it is compared with a constant-time check. Every other endpoint still
  requires normal authentication — in particular `GET /api/debug/profiles` (full config dump) is
  **not** covered, while the tool-inventory endpoint it does cover exposes only tool names and
  sizes, no prompts or policy bodies. Leave it unset to disable the read-only path entirely.

### Frontend error reports vs. telemetry (breadcrumbs)

Frontend clients POST to `POST /api/errors/`. The report's optional `severity` selects the lane:

- Absent or `"error"` → **error lane**: logged at `ERROR` and persisted to `error_logs` (the table
  the engineer profile reads via `read_error_logs` and a human reads via `GET /api/errors/`). The
  web frontend never sets `severity`, so its reports — including React error-boundary catches that
  use `error_type: "component_error"` — stay here.
- `"info"` / `"warning"` / `"debug"` → **telemetry lane**: recorded in an in-memory ring buffer and
  logged below the `error_logs` threshold, so high-frequency breadcrumbs never drown genuine errors.
  The iOS app sends its sync breadcrumbs (stream restarts/disconnects, resync phases, transport
  events) here. Read them via `GET /api/errors/telemetry` (same diagnostics-reader gate) or the
  engineer-profile `read_frontend_telemetry` tool. The buffer is dropped on restart. See
  [docs/design/ios-frontend-telemetry-lane.md](docs/design/ios-frontend-telemetry-lane.md).

### Global Tool Policy (`global_tools_policy`)

`global_tools_policy` is a top-level config section whose rules are injected into **every**
profile's tool-policy engine, regardless of the profile's own `tools_policy` (which otherwise
replaces the shipped defaults wholesale). Operator policy still overrides global rules. The shipped
default uses it for `report_technical_problem` (so the assistant can always report bugs, which
surface in the error-log and diagnostics endpoints above) and for `read_text_attachment` and
`jq_query` (so the assistant can always read back tool results that exceeded the large-result
threshold and were auto-converted to attachments).

### Gemini Computer Use (visual browser profile)

The `browser_visual_profile` drives a browser via Gemini's native computer-use capability, enabled
per profile with `enable_computer_use: true` on `processing_config` (Google provider only; combining
it with a non-Google provider or `retry_config` is a startup error). When enabled:

- The `types.Tool(computer_use=...)` tool is attached to every request with prompt-injection
  detection always on (no waiver knob; detections surface as safety confirmations).
- The model's predefined action space (`click`, `type`, `scroll`, `drag_and_drop`, …) executes
  through the shared `BrowserBackend` (local Playwright or remote browser-server). Actions the
  browser-server REST API cannot faithfully execute (non-left buttons, multi-clicks, key down/up)
  fail with an explicit error rather than degrading silently; `computer_use_excluded_functions` on
  `processing_config` can exclude them from the action space.
- When the model attaches a `safety_decision` requiring confirmation (payments, messaging,
  terms-of-service, suspected prompt injection), the action pauses for user confirmation through the
  standard tool-confirmation flow and, once approved, the acknowledgement is returned to the API.
  Declines are reported back to the model without ending the turn.

See [docs/design/gemini-computer-use-native.md](docs/design/gemini-computer-use-native.md) for the
full design.

### Embedding Providers

The embedding generator is selected from `embedding_model`/`embedding_provider`. By default the
provider is inferred from the model name (`gemini/<model>` for Google Gemini, a path starting with
`/` for local sentence-transformer models, `mock-deterministic-embedder` for tests).

Set `embedding_provider` to `openai` to use any OpenAI-compatible embeddings endpoint (OpenAI,
OpenRouter, or a self-hosted inference server); `embedding_model` is then sent to the API verbatim.

- **`EMBEDDING_PROVIDER`** → `embedding_provider`. Set to `openai` for an OpenAI-compatible
  endpoint; leave unset to infer from `embedding_model`.
- **`EMBEDDING_BASE_URL`** → `embedding_base_url`. Endpoint base URL, only used when
  `embedding_provider=openai` (OpenRouter: `https://openrouter.ai/api/v1`). Leave unset for the
  official OpenAI API.
- **`EMBEDDING_API_KEY`** → `embedding_api_key`. Falls back to `openai_api_key` / `OPENAI_API_KEY`
  when unset. Secret, redacted from logged config.
- **`EMBEDDING_DIMENSIONS`** → `embedding_dimensions`. Only forwarded as the `dimensions` request
  parameter when explicitly set, so the model must support that output size (e.g. OpenAI's
  `text-embedding-3-*`). Leave it unset for models that reject the field (e.g.
  `text-embedding-ada-002`) to use the native size. When set, it must also match the vector storage
  column dimensionality.

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
5. **Complex Tasks Profile [BC]**: full tool access via OpenAI GPT-5.6-sol (`gpt-5.6-sol`) with a
   higher iteration limit (100) for deep multi-step reasoning. Used via `/complex` or delegation
   from the default assistant. **Not to be confused with `spawn_worker`**, which launches isolated
   coding agents (Claude Code / Gemini CLI) in sandboxed containers with NO access to Family
   Assistant tools or data. Use `complex_tasks` when the task needs FA context (notes, calendar,
   documents, Home Assistant, etc.); use `spawn_worker` for standalone coding or computing tasks.

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
- When completing a user-visible feature, update docs/user/USER_GUIDE.md and tell the assistant how
  it works in the system prompt in prompts.yaml or in tool descriptions. This is not optional.
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

### Adding New Tools

Tools must be registered in TWO places: in the code (`src/family_assistant/tools/__init__.py`) and
in the configuration (`defaults.yaml` / `config.yaml`). See
[src/family_assistant/tools/CLAUDE.md](src/family_assistant/tools/CLAUDE.md) for complete tool
development guidance.

### Adding New UI Endpoints

Create your router in `src/family_assistant/web/routers/`, and add the new endpoint to the
appropriate test files in `tests/functional/web/` so it's covered for basic functionality. See
[src/family_assistant/web/CLAUDE.md](src/family_assistant/web/CLAUDE.md) for web API development
guidance.

UI pages are handled entirely by the React frontend — add new views as React components in
`frontend/`, not as server-side endpoints.

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

- **Database Access Pattern**: Use the repository pattern via DatabaseContext:

  ```python
  from family_assistant.storage.context import DatabaseContext

  async with DatabaseContext() as db:
      await db.notes.add_or_update(title, content)
      tasks = await db.tasks.get_pending_tasks()
  ```

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
`poe dev`), and **claude** (claude-code-webui on port 8080 with MCP servers configured).

### Building and Deploying

`.devcontainer/build-and-push.sh [tag]` builds the container with podman and pushes it to the
registry. Without an argument the tag defaults to a `YYYYMMDD_HHMMSS` timestamp.

### Automatic Git Synchronization

The dev container runs `git fetch` and `git pull --rebase` when Claude is invoked, stashing and
restoring local uncommitted changes. If conflicts occur it reverts to the original state rather than
breaking the workspace.
