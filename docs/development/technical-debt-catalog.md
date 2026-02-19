# Technical Debt Catalogue (2026-02)

This catalogue identifies high-impact technical debt currently visible in the repository and
proposes concrete projects to resolve it.

## How this catalogue was assembled

I reviewed architecture and contributor docs, scanned TODO/FIXME markers, and sampled code hotspots.
I also measured the largest files to identify concentration risk.

### Hotspot size snapshot

Command used:

```bash
python - <<'PY'
from pathlib import Path
files=[]
for p in Path('src').rglob('*.py'):
    files.append((sum(1 for _ in p.open()), str(p)))
for n,p in sorted(files, reverse=True)[:10]:
    print(f"{n:5d} {p}")
PY
```

Top results at time of analysis:

- 2803 lines: `src/family_assistant/processing.py`
- 2635 lines: `src/family_assistant/llm/__init__.py`
- 1624 lines: `src/family_assistant/llm/providers/google_genai_client.py`
- 1551 lines: `src/family_assistant/web/routers/chat_api.py`
- 1550 lines: `src/family_assistant/web/routers/asterisk_live_api.py`

A similar measurement in `frontend/src` shows `frontend/src/chat/ToolUI.jsx` at 4286 lines.

## Debt register

## 1) Monolithic core modules (backend and frontend)

- **Evidence**
  - Very large backend modules (`processing.py`, `llm/__init__.py`, multiple routers).
  - Extremely large frontend rendering/controller file (`frontend/src/chat/ToolUI.jsx`, 4286 lines).
- **Why this matters**
  - High merge-conflict frequency and slower review cycles.
  - Elevated regression risk from cross-cutting edits in single files.
  - Harder onboarding and weaker component boundaries.
- **Resolution project: “Modularization Program”**
  - Split by bounded contexts: orchestration, provider adapters, tool rendering, transport APIs.
  - Introduce strict layer boundaries with import rules and ownership docs.
  - Add guardrails: max-file-size lint rule and architectural tests.
- **Definition of done**
  - No production source file above an agreed threshold (for example 800–1000 lines, excluding test
    fixtures/generated files).
  - All extracted modules have focused tests and stable public interfaces.

## 2) ~~Integration gap in Google tool-calling path~~ (RESOLVED)

- **Status**: Resolved (2026-02-15)
- **Resolution**: The Google GenAI client has full tool-calling support. The
  `skip_if_google_tool_calling()` function was dead code (defined but never called) and has been
  removed. All three providers (OpenAI, Google, Anthropic) now have complete tool-calling
  integration tests with VCR cassettes, and these tests run in CI via replay mode. The invalid
  Anthropic model ID (`claude-haiku-3-5-20241022`) was corrected to `claude-haiku-4-5-20251001` and
  all cassettes recorded.

## 3) Streaming/resumption reliability debt

- **Evidence**
  - TODO in Google client: resumption logic using `chunk.event_id` is not implemented
    (`src/family_assistant/llm/providers/google_genai_client.py`).
  - Asterisk Live API marks tool execution parallelism/non-blocking execution as TODO
    (`src/family_assistant/web/routers/asterisk_live_api.py`).
- **Why this matters**
  - Degraded UX under transient network failures.
  - Voice/live sessions are sensitive to latency and head-of-line blocking.
- **Resolution project: “Real-time Reliability Hardening”**
  - Add resumable stream cursor handling and replay-safe idempotency model.
  - Introduce bounded parallel tool execution with explicit cancellation/timeouts.
  - Add chaos tests for dropped streams, delayed tool responses, reconnect storms.
- **Definition of done**
  - Stream interruption tests prove deterministic recovery.
  - Median/95th percentile latency targets documented and met for live sessions.

## 4) Security authorization gap in attachments

- **Evidence**
  - TODO states missing conversation-level access validation in attachment tool flow
    (`src/family_assistant/tools/attachments.py`).
- **Why this matters**
  - Potential data boundary violations across conversations/users.
  - Security risk is amplified because attachments can include sensitive content.
- **Resolution project: “Attachment Access Control Enforcement”**
  - Enforce ownership and conversation scoping in repository and tool layers.
  - Add negative security tests (cross-conversation ID injection attempts).
  - Add audit logging for attachment fetch/attach operations.
- **Definition of done**
  - All attachment operations require verified principal + conversation scope.
  - Security tests cover authorization failures and pass in CI.

## 5) Configuration/documentation drift

- **Evidence**
  - Documentation heavily references `config.yaml`, while repository root only has `defaults.yaml`
    and no committed `config.yaml`.
  - README prerequisites claim Python 3.10+, but project metadata requires Python 3.13.x
    (`pyproject.toml`).
- **Why this matters**
  - New contributor setup friction and failed first-run experiences.
  - Increased operational error rate from outdated deployment assumptions.
- **Resolution project: “Configuration Surface Cleanup”**
  - Add/refresh `config.example.yaml` and document merge semantics vs `defaults.yaml`.
  - Align README/runtime docs with actual Python requirement and startup paths.
  - Add CI doc-consistency checks for key version/config claims.
- **Definition of done**
  - Fresh clone can follow docs exactly without corrective guesswork.
  - Doc consistency checks fail PRs when requirements drift.

## 6) ~~Packaging and metadata hygiene debt~~ (RESOLVED)

- **Status**: Resolved (2026-02-18)
- **Resolution**: Placeholder metadata in `pyproject.toml` replaced with correct author info and
  GitHub URLs. Duplicate dependency entries removed (`passlib[bcrypt]`, `llm`, `asyncpg`, `httpx`).
  Stale TODO comments cleaned up.

## 7) UI test stability debt (timing-based waits)

- **Evidence**
  - Playwright page object contains TODOs to replace `wait_for_timeout` polling with explicit waits
    (`tests/functional/web/pages/chat_page.py`).
- **Why this matters**
  - Timing-based polling causes flaky tests and longer CI cycles.
  - Flakiness hides regressions and increases rerun overhead.
- **Resolution project: “Deterministic Web Test Synchronization”**
  - Replace hard waits with state/event-based synchronizers.
  - Expose test-friendly backend hooks/markers for save and stream completion.
  - Track and publish test flake rate over time.
- **Definition of done**
  - No raw timeout polling in page objects except justified low-level helpers.
  - Flake rate for functional web tests reduced below agreed threshold.

## 8) ~~Path fragility and environment coupling~~ (RESOLVED)

- **Status**: Resolved (2026-02-19)
- **Resolution**: All deep `pathlib.Path(__file__).parent...` traversals (up to 5 levels) across the
  web layer and storage module have been replaced with a centralized `family_assistant.paths`
  module. This module derives all paths (`PROJECT_ROOT`, `PACKAGE_ROOT`, `FRONTEND_DIR`,
  `STATIC_DIR`, `STATIC_DIST_DIR`, `TEMPLATES_DIR`, `WEB_RESOURCES_DIR`) from a single anchor point
  with a `validate_paths_at_startup()` function for early feedback. Unit tests verify all path
  constants.

## Prioritized project portfolio

Recommended execution order:

1. **Attachment Access Control Enforcement** (security risk reduction)
2. **Provider Capability Parity** (core feature completeness)
3. **Deterministic Web Test Synchronization** (delivery reliability)
4. **Configuration Surface Cleanup** (developer/operator productivity)
5. **Real-time Reliability Hardening** (resilience and UX)
6. **Modularization Program** (sustained velocity and maintainability)
7. ~~**Build Metadata Normalization** (packaging hygiene)~~ RESOLVED
8. ~~**Path & Runtime Contract Consolidation** (deployment robustness)~~ RESOLVED

## Governance recommendation

Create a standing technical debt board with:

- A named owner per debt project.
- A measurable KPI per project (for example flake rate, stream recovery success, max file size).
- A “debt budget” in each iteration (fixed capacity slice reserved for debt reduction).
