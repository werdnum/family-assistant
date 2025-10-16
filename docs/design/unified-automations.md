# Unified Automations Design

## Status

**QA Stabilization** – Unified automations now span backend, API, and the new React experience.
Legacy event-listener assets have been removed, and Playwright coverage exists for the automations
UI. The remaining work is focused on documentation/prompt updates and the last round of performance
polish (database pagination).

- **Phase 1–5 Complete**: Database, repositories, tools, task worker hooks, REST API, and the
  Automations React experience (list/detail/create) are shipping together.
- **Phase 6 In Progress**: Documentation and system prompt updates; this document is being refreshed
  as part of that work.
- **Phase 7 In Progress**: Final QA items (database-level pagination optimisation, additional E2E
  coverage for creation flows) are queued.

## Progress Summary

### What's Complete

**Phase 1: Database Layer**

- Added the `schedule_automations` table with RRULE support and `next_scheduled_at`
- Landed `ScheduleAutomationsRepository` and `AutomationsRepository` plus registration in
  `DatabaseContext`
- Repository tests cover creation, updates, rescheduling, cancellation, and stats queries

**Phase 2: Tool Layer**

- Consolidated tools into the new `automations.py` module (`create_automation`, `list_automations`,
  `update_automation`, `enable_automation`, etc.)
- Registered the unified tools and removed the legacy event listener functions from the tool surface
  area and system configuration
- Validated behaviour with integration coverage against both automation types

**Phase 3: Task Worker**

- Hooked `after_task_execution` for schedule automations so recurring instances reschedule
  automatically
- Ensured payloads carry `automation_id`/`automation_type` and that execution metrics stay in sync

**Phase 4: Web API**

- Exposed unified endpoints under `/api/automations` (list, detail, create, update, enable/disable,
  delete, stats)
- Enforced cross-type name uniqueness, conversation scoping, and strict validation for action and
  trigger payloads
- Removed the deprecated `/api/event-listeners` router now that consumers have migrated
- Added basic pagination (page/page_size) with in-memory slicing pending DB optimisation

**Phase 5: Frontend & Tests**

- Introduced the `frontend/src/pages/Automations/` app (list with filters + inline enable/disable,
  detail view with delete, creation forms for event and schedule automations)
- Wired navigation updates so "Automations" appears in the product menu and routes resolve via the
  Vite router
- Added Playwright coverage in `tests/functional/web/test_automations_ui.py` for list rendering,
  navigation, and filter UX
- Removed the obsolete Event Listeners React app, CSS, and Playwright tests
- Updated both the primary navigation menu and navigation sheet (`frontend/src/shared/Layout.tsx`,
  `frontend/src/shared/NavigationSheet.tsx`, `frontend/src/chat/NavHeader.jsx`,
  `src/family_assistant/templates/base.html.j2`) so "Automations" appears alongside existing product
  sections

### What's Remaining

**Phase 6: Documentation** (in progress)

- Refresh `docs/user/USER_GUIDE.md` with automations walkthroughs and screenshots
- Update `prompts.yaml` (and related system prompts) so the LLM understands the new tool surface
- Add tool usage examples and migration notes here and in the user-facing docs

**Phase 7: Final Testing & Polish** (in progress)

- Expand Playwright coverage to exercise creation flows end-to-end once deterministic fixtures are
  in place
- Implement database-level pagination (likely via UNION querying) so the API scales beyond the
  current in-memory slice
- Capture additional performance telemetry (e.g., load time with 250+ automations) and adjust page
  defaults if required

### Known Limitations

1. **In-memory pagination**: `AutomationsRepository.list_all` still fetches everything before
   slicing. That is acceptable for typical (\<100) automations but needs a UNION-based query in the
   follow-up polish work.

2. **Conversation selection stub**: The React UI sends `conversation_id=web` for now. Wiring the UI
   into the real conversation picker is required before we expose automations broadly.

3. **Disabled-only filtering gap**: `ScheduleAutomationsRepository.list_all` only understands an
   `enabled_only` flag. Getting “disabled automations” still requires loading everything and
   filtering in Python, which prevents efficient pagination for that slice.

## Overview

This document proposes unifying event listeners and recurring actions under a single "automations"
abstraction, reducing tool complexity while providing a cleaner mental model for both LLMs and
users.

## Historical Context (Pre-unification)

### Event Listeners (legacy)

Prior to the unified automations work we exposed dedicated event-listener tooling. The components
below have now been replaced but are kept here as context for ongoing migration notes.

**Storage**: `event_listeners` table (src/family_assistant/storage/events.py)

- Persistent entity with name, description, enabled status
- Trigger: External events (Home Assistant, indexing, webhooks)
- Action: wake_llm or script execution
- Features: Rate limiting, execution stats, Starlark conditions

**Management**: 6 tools (formerly in `src/family_assistant/tools/event_listeners.py` – now deleted)

- `create_event_listener`
- `list_event_listeners`
- `delete_event_listener`
- `toggle_event_listener`
- `validate_event_listener_script`
- `test_event_listener_script`

**UI**: Dedicated page at `/event-listeners` (frontend/src/pages/EventListeners/) – removed in
favour of the new automations React app

- List view with filters
- Detail view with execution stats
- Form for creation/editing

### Recurring Actions (pre-existing gap)

PR #312 added instance-level management for scheduled tasks. Before unification this was the best
available option:

**Storage**: `tasks` table (src/family_assistant/storage/tasks.py)

- Tasks have optional `recurrence_rule` (RRULE)
- No persistent "recurring action" entity
- Tasks disappear after execution

**Management**: 3 tools (src/family_assistant/tools/tasks.py - from PR #312)

- `list_pending_actions` - View pending task instances
- `modify_pending_action` - Modify individual instance
- `cancel_pending_action` - Cancel individual instance

**Gap**: No entity-level management

- Can't list "all my recurring actions" after execution
- Can't name recurring actions
- Can't enable/disable without canceling all instances
- Can't view execution history over time
- No UI for management

## Problem Statement

Event listeners and recurring actions are conceptually identical:

| Feature             | Event Listener     | Recurring Action      |
| ------------------- | ------------------ | --------------------- |
| Has name            | ✓                  | ✗ (missing)           |
| Has description     | ✓                  | ✗ (missing)           |
| Action type         | wake_llm, script   | wake_llm, script      |
| Action config       | ✓                  | ✓                     |
| Enable/disable      | ✓                  | ✗ (missing)           |
| Execution stats     | ✓                  | ✗ (missing)           |
| **Only difference** | **Trigger: event** | **Trigger: schedule** |

Creating separate tool sets (6 + 4 = 10 tools) for nearly identical functionality:

- Confuses LLMs with too many similar tools
- Creates separate mental models for the same concept
- Duplicates UI/API patterns
- Misses optimization opportunities

Users naturally think: "I have automations that run when X happens" - where X is either an event or
a time.

## Proposed Solution

### Core Concept

**Automation**: A named, configurable action that executes automatically based on a trigger.

**Trigger Types**:

- **Event**: External system event (Home Assistant, webhook, indexing)
- **Schedule**: Time-based RRULE pattern

Both automation types share:

- Name, description, conversation scoping
- Action configuration (wake_llm or script)
- Enable/disable functionality
- Execution tracking and statistics
- Validation and testing

### Architecture Principles

1. **Separate storage, unified interface**: Keep event_listeners and schedule_automations as
   separate tables for type safety, but present a unified "automations" API
2. **Type-specific creation**: Different parameters needed (event conditions vs RRULE)
3. **Unified management**: By ID, operations work identically regardless of type
4. **No breaking changes**: Existing event_listeners continue working, migration is additive

## Database Schema

### Event listeners (legacy table)

- Table: `event_listeners` (`src/family_assistant/storage/events.py`)
- Primary key: auto-incrementing integer; names are unique per conversation via
  `uq_name_conversation`.
- Key columns: `source_id` (enum), `match_conditions` (JSON/JSONB), `action_type`, optional
  `condition_script`, `conversation_id`, `interface_type`, `one_time`, `enabled`, and execution
  counters (`daily_executions`, `daily_reset_at`, `last_execution_at`). All timestamps are stored as
  timezone-aware values.

### Schedule automations

- Table: `schedule_automations` (`src/family_assistant/storage/schedule_automations.py`)
- Primary key: auto-incrementing integer; names are unique per conversation via
  `uq_sched_name_conversation`.
- Key columns: `recurrence_rule` (RRULE string), `next_scheduled_at`, `action_type`,
  `action_config`, `conversation_id`, `interface_type`, `enabled`, `execution_count`, and
  timestamps. Created/updated timestamps are stored in UTC.

### Task integration

- Tasks created for schedule automations include `automation_id` (string version of the integer
  primary key) and `automation_type` in the payload so the worker can route callbacks correctly.
- Wake-LLM payloads include `callback_context` while script payloads carry
  `script_code`/`task_name`.

## Repository Layer

### AutomationsRepository (`src/family_assistant/storage/repositories/automations.py`)

- Wraps `EventsRepository` and `ScheduleAutomationsRepository` so callers interact with a single
  abstraction.
- `list_all` returns `(automations, total_count)` and accepts `automation_type`, `enabled`, `limit`,
  and `offset` parameters. Pagination is still performed in memory, which is why database-level
  pagination remains on the Phase 7 checklist.
- `get_by_id`, `update_enabled`, `delete`, and `get_execution_stats` delegate to the correct
  repository based on the type passed in. The method signatures mirror the REST path structure
  (`/api/automations/{type}/{id}`) so we avoid extra table probes.
- `check_name_available` enforces cross-type uniqueness before any creation or rename proceeds,
  preventing a schedule automation from shadowing an event automation with the same name.

### ScheduleAutomationsRepository (`src/family_assistant/storage/repositories/schedule_automations.py`)

- Continues to accept `_UNSET` sentinel values but now defends updates with `isinstance(...)`
  checks, ensuring that falsy-but-valid inputs are not discarded. RRULE updates recalculate
  `next_scheduled_at` and reschedule pending work.
- Cancels pending tasks by casting the JSON payload column with `sa_cast(..., String)` rather than
  relying on `.astext`, keeping the code compatible with both PostgreSQL and SQLite backends.
- Action updates rebuild wake-LLM payloads so that callback context, script code, and supporting
  arguments stay consistent with the current automation definition.

### DatabaseContext integration

- `DatabaseContext.automations` and `.schedule_automations` cache repository instances per request
  and serve both the API layer and the tool runtime.

## Tool Interface

### Unified tool definitions (`src/family_assistant/tools/automations.py`)

- `create_automation` – create either an event or schedule automation based on the `automation_type`
  and `trigger_config` payload.
- `list_automations` – list automations with optional `automation_type` and `enabled_only` filters.
- `get_automation` – fetch a single automation by ID and type.
- `update_automation` – patch trigger/action/description fields for the specified automation.
- `enable_automation` / `disable_automation` – convenience wrappers that toggle the enabled flag.
- `delete_automation` – remove an automation and cancel its scheduled workload.
- `get_automation_stats` – surface execution counts and recent history details back to the LLM.

### Behaviour notes

- Tool implementations validate the `automation_type` string via `_validate_automation_type`, so the
  LLM receives immediate feedback for invalid values.
- `create_automation_tool` checks name availability before dispatching to
  `events.create_event_listener` or `schedule_automations.create`, then augments the confirmation
  message with source-specific hints (e.g., next run time for schedules).
- `list_automations_tool` surfaces the total count along with basic metadata. It currently relies on
  in-memory pagination and mirrors the API's behaviour.
- Update/delete helpers re-use the repository layer, guaranteeing that conversation scoping and name
  conflicts stay consistent across surfaces.

## Web API

### Endpoint summary (`src/family_assistant/web/routers/automations_api.py`)

- `GET /api/automations` – list automations with optional `automation_type`, `enabled`, `page`, and
  `page_size` filters. Returns an `AutomationsListResponse` payload containing the automations,
  total count, and pagination metadata. Requires an explicit `conversation_id` query parameter.
- `GET /api/automations/{automation_type}/{automation_id}` – fetch a single automation. The path
  encodes the automation type (`event` or `schedule`) so the repository can target the correct table
  without probing.
- `POST /api/automations/event` / `POST /api/automations/schedule` – create new automations. Both
  endpoints validate `action_type`, enforce name uniqueness across types, and raise detailed errors
  when required fields (e.g., `script_code` for script actions) are missing.
- `PATCH /api/automations/{type}/{id}` – update automation metadata. Request bodies are parsed into
  type-specific models (`UpdateEventAutomationRequest`, `UpdateScheduleAutomationRequest`) that
  leverage the `_UNSET` sentinel to differentiate "not provided" from explicit nulls.
- `PATCH /api/automations/{type}/{id}/enabled` – toggle enabled state via a lightweight query string
  parameter.
- `DELETE /api/automations/{type}/{id}` – delete an automation after verifying conversation access
  and name uniqueness constraints. Schedule deletes also cancel future tasks.
- `GET /api/automations/{type}/{id}/stats` – returns execution counters and recent run metadata from
  the underlying repositories.

### Validation and security

- Every endpoint requires a `conversation_id` (query parameter) so we can confirm ownership before
  mutating data.
- Creation paths call `AutomationsRepository.check_name_available` to enforce cross-type uniqueness
  and surface helpful error messages.
- Event automation creation validates `source_id` and `action_type` against allow lists and ensures
  script actions include `script_code`.
- Schedule automation updates recompute `next_scheduled_at` and cancel/reschedule tasks whenever the
  RRULE changes.

### Pagination status

- The list endpoint exposes `page`/`page_size` knobs now. Under the hood we still rely on in-memory
  pagination (see Known Limitations) until the UNION-based query lands in Phase 7.

## Migration Path

### Phase 1 – Schedule automation infrastructure ✅ Completed

- Added the `schedule_automations` table, repository, and Alembic migration.
- Introduced `AutomationsRepository` and wired it into `DatabaseContext`.
- Landed repository test coverage (creation, rescheduling, stats).

### Phase 2 – Unified tools ✅ Completed

- Shipped the consolidated `automations.py` tool module and registered it with the default profile.
- Removed the legacy event-listener tool definitions after verifying LLM workflows continued to
  pass.

### Phase 3 – Task worker integration ✅ Completed

- Ensured automation IDs/types are propagated through task payloads.
- Added `after_task_execution` hooks so schedule automations reschedule themselves after each run.

### Phase 4 – REST API ✅ Completed

- Exposed CRUD + stats endpoints under `/api/automations`.
- Removed the deprecated `/api/event-listeners` router once consumers migrated.

### Phase 5 – Frontend and surface cleanup ✅ Completed

- Delivered the new Automations React app (list, filters, detail, create forms) and updated
  routing/nav.
- Added Playwright smoke tests for page load, navigation, and filter interactions.
- Deleted the legacy Event Listeners React stack, CSS, and Playwright coverage.

### Phase 6 – Documentation and prompts 🔄 In progress

- Refresh `docs/user/USER_GUIDE.md`, update system prompts, and add tool usage examples.
- Capture migration notes for any lingering references to event listeners.

### Phase 7 – Final QA and performance 🔄 In progress

- Replace in-memory pagination with a UNION query.
- Expand Playwright coverage to exercise creation flows end-to-end once deterministic fixtures are
  ready.
- Gather performance metrics for large automation sets and tune defaults if needed.

## Trade-offs and Alternatives

### Why Not Single Table?

**Considered**: One `automations` table with JSONB for trigger config

**Rejected because**:

- ❌ Loses type safety for trigger-specific fields
- ❌ Complex queries with JSONB parsing
- ❌ Requires migrating existing event_listeners
- ❌ Harder to maintain indexes
- ❌ Database schema doesn't encode domain logic

**Chosen approach** (separate tables, unified interface):

- ✅ Type-safe storage
- ✅ No migration needed
- ✅ Efficient queries
- ✅ Clear separation of concerns
- ✅ Easy to understand and maintain

### Why Not Keep Separate?

**Considered**: Keep event listeners and recurring actions as separate systems

**Rejected because**:

- ❌ More tools (10 vs 8)
- ❌ Separate mental models for same concept
- ❌ Duplicate UI patterns
- ❌ Confusing for users/LLM

**Chosen approach** (unified interface):

- ✅ Fewer, clearer tools
- ✅ Single mental model: "automations"
- ✅ Consistent UI experience
- ✅ Better discoverability

### Edge Cases and Limitations

1. **Name uniqueness**: Names are unique per conversation, across both types

   - User can't have event automation "Daily Summary" and schedule automation "Daily Summary"
   - Database unique constraints only enforce within each table
   - Resolution: Application layer checks both tables before creating, returns clear error message
   - Implementation: `AutomationsRepository.check_name_available` queries both tables prior to
     creation or rename

2. **Different execution tracking by design**: Event vs schedule automations have different tracking
   needs

   - Event listeners: `daily_executions` (resets at midnight) - for rate limiting against
     misconfigured match conditions
   - Schedule automations: `execution_count` (lifetime counter) - for statistics/history
   - Rationale: Event listeners need rate limiting because incorrect `match_conditions` can match
     too many events (e.g., matching every motion sensor event). Schedule automations can't have
     this problem - RRULE explicitly controls frequency.
   - Resolution: Unified API shows appropriate metric per type with clear labels
   - UI implication: Display "Executions today (rate limit: 5/day)" for events vs "Total executions"
     for schedules

3. **Timezone normalisation**: Both tables store timezone-aware timestamps now, but the repository
   keeps guards in place for legacy naive rows.

4. **ID collision**: Automation IDs are integers scoped per table.

   - The API always requires callers to specify both `automation_type` and `automation_id`, so the
     combination remains unique.
   - Legacy references (e.g., tasks) carry both pieces of information.

5. **Migration of existing recurring tasks**: Tasks created before this feature won't have
   automation entities

   - Resolution: Continue working as before (just instances)
   - Optional: Migration script to create entities for active recurring tasks

6. **Rate limiting**: Event listeners have daily rate limits, schedule automations don't

   - Resolution: Keep rate limiting specific to event listeners
   - Document the difference

## Success Criteria

### Must Have

✅ Users can create both event and schedule automations through unified tools ✅ Single "Automations"
page shows both types ✅ Enable/disable works identically for both types ✅ Execution statistics
tracked for both types ✅ Schedule automations persist after execution ✅ Existing event listeners
continue working unchanged ✅ All tests pass (unit, integration, E2E)

### Should Have

✅ LLM prefers unified tools over old tools ✅ Clear documentation for when to use each automation
type ✅ Migration path from old tools clearly documented ✅ UI provides good filtering and search

### Nice to Have

✅ Migration script for existing recurring tasks ✅ Execution history charts/visualization ✅ Bulk
operations (enable/disable multiple) ✅ Export/import automation definitions

## Implementation Checklist

### Phase 1: Database Layer ✅ Completed

- [x] Create `schedule_automations_table` schema
- [x] Create Alembic migration
- [x] Implement `ScheduleAutomationsRepository`
- [x] Implement `AutomationsRepository` abstraction
- [x] Add to `DatabaseContext`
- [x] Unit tests for repositories

### Phase 2: Tool Layer ✅ Completed

- [x] Create `src/family_assistant/tools/automations.py`
- [x] Implement 8 automation tools
- [x] Update `schedule_recurring_action` to create entities
- [x] Register tools in `__init__.py`
- [x] Update `config.yaml`
- [x] Integration tests for tools
- [x] Remove old event listener tools

### Phase 3: Task Worker ✅ Completed

- [x] Update script execution handler
- [x] Update LLM callback handler
- [x] Call `after_task_execution` for schedule automations
- [x] Tests for automation lifecycle

### Phase 4: Web API ✅ Completed

- [x] Create `automations_api.py` router
- [x] Implement REST endpoints (GET, POST, PATCH, DELETE)
- [x] Add Pydantic models
- [x] Register router in app
- [x] Remove deprecated `listeners_api.py` once the frontend migrated
- [x] Fix Union request body validation issue
- [x] Implement sentinel pattern for nullable fields
- [x] Add conversation_id security verification
- [x] API tests (covered by existing test suite)

### Phase 5: Frontend ✅ Completed

- [x] Create `pages/Automations/` directory
- [x] Implement `AutomationsList` component
- [x] Implement `AutomationDetail` component
- [x] Implement `CreateEventAutomation` component
- [x] Implement `CreateScheduleAutomation` component
- [x] Update navigation and Vite router entries
- [x] Add Playwright smoke coverage in `tests/functional/web/test_automations_ui.py`
- [x] Remove legacy Event Listeners React views, CSS, and tests

### Phase 6: Documentation 🔄 In progress

- [ ] Update `docs/user/USER_GUIDE.md` with automations guide
- [ ] Update system prompt in `prompts.yaml` to explain automations
- [ ] Add tool usage examples
- [ ] Create migration guide for users
- [ ] Update architecture diagram

### Phase 7: Final Testing & polish 🔄 In progress

- [x] Unit tests for all new repositories
- [x] Integration tests for tool workflows
- [x] Test both PostgreSQL and SQLite
- [ ] Frontend component tests (targeted coverage for Automations forms)
- [ ] Expand Playwright coverage to exercise creation and deletion flows
- [ ] Replace in-memory pagination with database-level UNION queries
- [ ] Performance testing for pagination at scale & final verification

## Future Enhancements

### Automation Templates

Pre-built automation templates users can quickly instantiate:

- "Daily summary at 8am"
- "Notify when door opens"
- "Weekly calendar review"

### Conditional Actions

Support for multiple actions with conditions:

```json
{
  "conditions": [
    {"if": "temperature > 75", "then": {"action": "notify"}},
    {"if": "temperature > 85", "then": {"action": "turn_on_fan"}}
  ]
}
```

### Automation Groups

Group related automations for bulk operations:

- "Morning routine" group
- "Security alerts" group
- Enable/disable groups together

### Automation History Dashboard

Dedicated page showing:

- Execution timeline across all automations
- Success/failure rates
- Performance metrics
- Trend analysis

### Import/Export

Export automations as JSON/YAML for:

- Backup and restore
- Sharing between conversations
- Version control

## Related Work

- **PR #312**: Instance-level management for scheduled tasks (foundation for this work)
- **Task Queue**: Underlying execution infrastructure
- **Actions System**: Unified action execution (wake_llm, script)

## References

- Automations tools: `src/family_assistant/tools/automations.py`
- Automations repository: `src/family_assistant/storage/repositories/automations.py`
- Schedule automations repository:
  `src/family_assistant/storage/repositories/schedule_automations.py`
- Automations API router: `src/family_assistant/web/routers/automations_api.py`
- Automations React app: `frontend/src/pages/Automations/`
- Playwright coverage: `tests/functional/web/test_automations_ui.py`
- Task worker: `src/family_assistant/task_worker.py`
- RRULE spec: https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html
