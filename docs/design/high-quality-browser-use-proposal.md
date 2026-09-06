# High-Quality Browser Use Proposal (2026)

## Context

Family Assistant currently supports two browser/web patterns:

1. **Image-based browser automation** in `browser_profile` via local tools (`open_web_browser`,
   `navigate`, `click_at`, `type_text_at`, etc.), where each action returns a screenshot.
2. **Scraper MCP** via `scrape_url`, optimized for content extraction and markdown conversion.

This has worked, but recent industry direction suggests a better default: **agent-ergonomic browser
interfaces** that reduce turn count, token volume, and error-prone action/observation loops.

Recent AXI benchmark claims show strong outcomes for browser tasks with a CLI layer designed for
agents (combined operations, query-focused output, contextual next steps) versus both raw MCP and
traditional browser CLIs. See:

- https://axi.md/
- https://github.com/kunchenguid/axi
- https://github.com/kunchenguid/chrome-devtools-axi

______________________________________________________________________

## Problem Statement

### Current strengths

- Screenshot-first browser control is robust for visual tasks (forms, auth flows, unpredictable UI).
- Scraper path is efficient for extraction-heavy tasks.
- Existing security/profile boundaries are already in place.

### Current friction

1. **High interaction tax for browser_profile**

   - Actions and observations are effectively separated across repeated steps.
   - Coordinate-based operations are brittle when layout shifts.
   - Screenshots can consume token/context budget quickly.

2. **Tool sprawl and discoverability cost**

   - Many low-level browser actions are exposed directly.
   - The model must plan multi-step mechanics instead of expressing user intent.

3. **Split mental model between “scrape” and “browse”**

   - There is no unified task-level browser contract like:
     - “open page + find target info”
     - “fill form + submit + verify success”
     - “extract table rows as structured data”

4. **No first-class trajectory benchmark in this repo**

   - We do not currently measure browser success/cost/latency/turns across representative tasks.

______________________________________________________________________

## Design Goals

1. **Maximize task success** on real websites (including JS-heavy flows).
2. **Reduce turns and token cost** for common browsing/extraction tasks.
3. **Preserve visual fallback reliability** where DOM/text-only interaction fails.
4. **Make tool usage more intent-level** and less mechanical.
5. **Create measurable quality gates** with reproducible benchmarks.

______________________________________________________________________

## Proposed Architecture: Hybrid “AXI-Inspired Browser Stack”

### Summary

Adopt a **hybrid architecture**:

- **Primary path:** task-level, agent-ergonomic browser interface (AXI-style semantics).
- **Fallback path:** existing image-based computer-use tools for hard visual interactions.
- **Extraction fast path:** keep scraper for simple fetch/convert workloads.

Instead of replacing everything, we add a new **Browser Orchestrator layer** that routes requests to
one of three execution modes.

### Execution modes

1. **Mode A — Structured Browse (default for most browse tasks)**

   - New higher-level operations such as:
     - `browser_open_and_query(url, query, options)`
     - `browser_click_and_query(target, query, options)`
     - `browser_fill_and_submit(field, value, options)`
     - `browser_extract_table(url|selector, schema_hint)`
   - Returns concise structured text plus minimal metadata (AXI principle alignment).
   - Should support “combined operations” so one call does action + observation.

2. **Mode B — Visual Computer Use (fallback)**

   - Reuse current tools in `computer_use.py` for:
     - CAPTCHAs, difficult custom widgets, anti-bot oddities, unknown selectors.
   - Keep screenshot attachments, but only when needed.

3. **Mode C — Scrape/Convert (fast path)**

   - Use `scrape_url` when the task is extraction-only and does not require interactive navigation.

### Routing policy (Orchestrator)

- If user asks to *extract/read/summarize page content*: start with Mode C or Mode A.
- If user asks to *interact* (click/fill/login/multi-page navigation): start with Mode A.
- Auto-fallback to Mode B when repeated action failures occur or confidence drops.

______________________________________________________________________

## AXI Principles to Adopt Directly

We should adopt the AXI ideas as **interface design requirements** (not dogma):

1. **Combined action + observation**

   - Eliminate “do action, then separately inspect” loops where possible.

2. **Minimal default output schemas**

   - Return only fields needed for next reasoning step.

3. **Content truncation with explicit escape hatch**

   - Example: `truncated=true`, `total_chars`, and `--full`/`full=true` equivalent.

4. **Definitive empty states and structured errors**

   - Avoid ambiguous empty outputs.

5. **Contextual disclosure**

   - Each result should include likely next actions (machine-readable hints).

6. **Content-first defaults**

   - Default return should be actionable content, not verbose help text.

______________________________________________________________________

## Implementation Plan (Incremental)

### Phase 0 — Baseline & instrumentation

- Add browser trajectory telemetry:
  - task success/failure,
  - turns/tool calls,
  - token usage,
  - wall-clock duration,
  - fallback rate (A→B).
- Define a small benchmark task suite (10–20 tasks) reflecting Family Assistant usage:
  - finding facts from pages,
  - extracting tables,
  - multi-click navigation,
  - simple form completion,
  - error recovery.

### Phase 1 — New high-level browser tools

- Introduce a new tool module (e.g., `browser_orchestrator.py`) with 4–6 task-level tools.
- Keep current low-level tools but remove them from default exposure where possible.
- Update `browser_profile` tool set to prioritize high-level tools.

### Phase 2 — Smarter fallback and retries

- On structured-mode failure, fallback to visual mode with bounded retries.
- Add failure classifiers (selector-not-found, nav-timeout, auth-wall, anti-bot).
- Return explicit recovery guidance in tool output.

### Phase 3 — Output/token optimization

- Introduce compact output format inspired by TOON for repetitive lists/tables.
- Add field selection (`fields`) and truncation controls (`max_chars`, `full`).

### Phase 4 — Policy and prompt integration

- Update browser profile system prompt with mode selection guidance.
- Add docs for operators/users clarifying when browse vs scrape vs visual modes are expected.

### Phase 5 — Benchmark gate for regressions

- Add a CI-friendly benchmark smoke run (small subset).
- Track trend dashboard in docs or generated report artifact.

______________________________________________________________________

## Concrete Changes to Existing Configuration

1. **`defaults.yaml`**

   - Keep `scrape` MCP server.
   - Keep existing browser tools available as fallback.
   - Introduce new high-level browser tools in `browser_profile` and make them primary.

2. **Tooling layer (`src/family_assistant/tools/`)**

   - Add orchestrator module.
   - Keep `computer_use.py` as fallback implementation.
   - Potentially add adapter around Chrome DevTools MCP or AXI CLI semantics.

3. **Observability**

   - Add per-browser-task execution metadata logging for mode, fallback, and outcome.

______________________________________________________________________

## Buy vs Build Recommendation

### Recommendation

**Build an internal AXI-inspired abstraction**, while keeping compatibility with existing stack.

### Why

- We can tailor interfaces to Family Assistant’s profiles, trust boundaries, and tool policies.
- We avoid tight coupling to one external CLI’s command contract.
- We can still borrow the best ideas and, where helpful, integrate external components as adapters.

### Optional acceleration path

Prototype an adapter that shells out to `chrome-devtools-axi` in an isolated worker environment,
then compare against internal orchestrator mode on the same benchmark tasks.

______________________________________________________________________

## Risks & Mitigations

1. **Risk: Higher complexity from three modes**

   - Mitigation: strict routing policy + telemetry + bounded fallback.

2. **Risk: New abstractions hide useful low-level control**

   - Mitigation: retain low-level tools behind explicit fallback/debug paths.

3. **Risk: Benchmark mismatch with real usage**

   - Mitigation: include production-derived task samples and periodically refresh suite.

4. **Risk: Regression in visual-only scenarios**

   - Mitigation: do not remove existing computer-use path until benchmark parity is proven.

______________________________________________________________________

## Success Criteria

After rollout, compared to today’s browser_profile baseline:

- **Task success**: non-inferior or better.
- **Average turns/tool calls**: reduced by at least 20%.
- **Average token usage per browser task**: reduced by at least 20%.
- **Median time-to-completion**: reduced.
- **User-visible retries/failures**: reduced.

______________________________________________________________________

## Decision

Proceed with the **hybrid AXI-inspired browser stack** using an internal orchestrator and measured
incremental rollout. Keep scraper and visual automation as complementary execution modes rather than
competing systems.
