# Processing Cleanup Backlog

## Purpose

This document captures architecture and code-quality issues in `src/family_assistant/processing`,
prioritized for incremental execution.

It now incorporates external analysis from:

- PR #677 (Gemini agent): commit `c2d130ef2`, doc `docs/development/processing_cleanup_ideas.md`
- PR #678 (Claude agent): commit `faac7e88d`, doc `docs/design/processing-module-cleanup.md`

## Source Tags

- `[Cdx]`: Identified in the local Codex audit on branch `cleanup-processing-logic`
- `[G677]`: Reported in PR #677 (Gemini)
- `[C678]`: Reported in PR #678 (Claude)
- `Added from PR`: Not in the original local backlog; incorporated from PR analysis

## Priority Definitions

- `P0`: Correctness/security/reliability risk; likely user-visible breakage or hidden data loss.
- `P1`: High-value maintainability and behavior-consistency improvements.
- `P2`: Technical-debt cleanup and contract hardening.

## Backlog

### P0

- **01. Stream path drops `user_id` and `subconversation_id` before LLM processing.**

  - Impact: Wrong tool context/permissions and broken delegated-subconversation behavior.
  - Status (2026-03-10): Complete in merged code. Stream interactions now pass both `user_id` and
    `subconversation_id` through the shared turn-preparation path into `process_message_stream` and
    `LLMStreamingLoop.run_stream(...)`, so tool execution receives the correct context.
  - References: `service.py:961`
  - Sources: `[Cdx]`

- **02. Sync path drops `chat_interfaces` before LLM processing.**

  - Impact: Sync/stream mismatch for cross-interface tool behavior.
  - Status (2026-03-10): Complete in merged code. `handle_chat_interaction(...)` now passes
    `chat_interfaces` into `process_message(...)`, and the sync path forwards them into
    `LLMStreamingLoop.run(...)`.
  - References: `service.py:580`
  - Sources: `[Cdx]`

- **03. Reply threading logic differs between sync and stream paths.**

  - Impact: Different thread context depending on endpoint used.
  - Status (2026-03-10): Complete in merged code. Sync and stream now share reply-thread resolution
    and initial-message construction through `_prepare_turn_messages_for_llm(...)`.
  - References: `service.py:330`, `service.py:749`
  - Sources: `[Cdx]`

- **04. History fetch errors are swallowed with `raw_history_messages = []`.**

  - Impact: Silent context loss and confusing downstream behavior.
  - Status (2026-03-10): Complete in merged code. History loading now calls
    `db_context.message_history.get_recent(...)` directly with no broad catch/fallback to `[]`.
  - References: `service.py:399`, `service.py:828`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **05. Thread fetch errors are swallowed and processing continues with partial context.**

  - Impact: Hidden failure with inconsistent conversation grounding.
  - Status (2026-03-10): Complete in merged code. Full-thread loading now calls
    `db_context.message_history.get_by_thread_id(...)` directly with no catch-and-continue path.
  - References: `service.py:458`, `service.py:865`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **06. System prompt formatting still tolerates unknown placeholders by keeping them as literals.**

  - Impact: Prompt template mistakes can still pass through as literal text instead of failing fast,
    even though sync/stream divergence has been removed.
  - Status (2026-03-10): Complete in merged code. `_render_system_prompt(...)` now rejects unknown
    placeholders with a `ValueError` and requires literal braces to be escaped explicitly with `{{`
    and `}}`.
  - References: `service.py:303`, `service.py:500`, `service.py:832`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **07. Attachment content-part processing can drop user input on exceptions.**

  - Impact: Silent loss of user-provided attachments.
  - Status (2026-03-10): Complete in merged code. Attachment content-part processing now fails fast
    on missing registry, missing attachment IDs, missing metadata, or missing content.
  - References: `attachments.py:91`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **08. URL-to-data-URI conversion swallows failures and keeps internal URLs.**

  - Impact: External providers cannot fetch internal attachment URLs.
  - Status (2026-03-10): Complete in merged code. Internal attachment URLs now raise on invalid
    format or missing files instead of silently remaining as internal URLs.
  - References: `attachments.py:223`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **09. Stream error-type mapping does not align with provider metadata values.**

  - Impact: Typed provider errors (rate limit/auth/context) are lost.
  - Status (2026-03-10): Complete in merged code. Stream error mapping now normalizes canonical and
    provider-style snake_case values such as `rate_limit`, `model_not_found`, and `context_length`.
  - References: `processing/utils.py:28`, `processing/utils.py:159`,
    `llm/providers/openai_client.py:591`, `llm/providers/anthropic_client.py:908`
  - Sources: `[Cdx]`

- **10. Final-iteration tool calls are ignored.**

  - Impact: Side-effecting tool calls may be silently skipped on last iteration.
  - Status (2026-03-10): Complete in merged code. Final-iteration tool calls now emit explicit
    non-executed tool-result messages instead of disappearing silently.
  - References: `llm_loop.py:505`
  - Sources: `[Cdx]`

- **11. Defensive invalid-tool-call fallback still fabricates a placeholder tool-call ID.**

  - Impact: If an invalid tool call without an ID ever reaches `ToolExecutor`, the generated
    `ToolMessage` still uses a placeholder ID that may not match provider correlation semantics.
  - Status (2026-03-10): Complete in merged code. `ToolExecutor.execute(...)` now enforces non-empty
    tool-call IDs and function names as fail-fast invariants, and `_build_error_result()` only
    accepts validated call IDs instead of fabricating placeholders.
  - References: `tool_execution.py:203`, `tool_execution.py:508`
  - Sources: `[C678]` (Added from PR #678)

- **12. Context provider aggregation catches broad exceptions and continues silently.**

  - Impact: Broken providers are hidden; context quality degrades invisibly.
  - Status (2026-03-10): Complete in merged code. `ContextPreparer.aggregate_context()` now raises a
    contextual `RuntimeError` instead of catching and continuing.
  - References: `context.py:103`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **13. Errors are persisted as `AssistantMessage` rather than `ErrorMessage`.**

  - Impact: History cannot clearly distinguish failures from normal assistant replies.
  - Status (2026-03-10): Complete in merged code. Processing errors are now persisted via
    `_persist_error_history_message(...)` as `ErrorMessage`.
  - References: `service.py:648`
  - Sources: `[C678]` (Added from PR #678)

### P1

- **14. `select_for_response` fails open to "last N" attachments on parse/LLM errors.**

  - Impact: Can return irrelevant attachments instead of explicit failure handling.
  - Status (2026-03-08): Phase 7b complete on branch
    `processing-phase7b-attachment-fail-open-cleanup` (attachment-selection failures now use an
    explicit warning path with deterministic, capped auto-queued ordering instead of open-ended
    fallback selection).
  - References: `attachments.py:478`, `attachments.py:483`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **15. `handle_large_result` fails open to inline large content.**

  - Impact: Context explosion and degraded reliability when attachment storage fails.
  - Status (2026-03-08): Phase 7b complete on branch
    `processing-phase7b-attachment-fail-open-cleanup` (large-result auto-conversion now fails fast
    when storage is unavailable or persistence fails, instead of returning fallback strings).
  - References: `attachments.py:543`, `attachments.py:593`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **16. `ToolExecutor.execute` mixes parsing, execution, attachment IO, enrichment, and error
  mapping.**

  - Impact: Hard to test and reason about; high change risk.
  - Status (2026-03-08): Phase 6b complete on branch `processing-phase6b-tool-executor-fail-fast`
    (execution path split into explicit stages; tool runtime errors are mapped separately from
    post-processing).
  - References: `tool_execution.py:58`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **17. Broad catches during attachment enrichment/storage in tool execution.**

  - Impact: Partial/incorrect metadata may leak through with weak observability.
  - Status (2026-03-08): Phase 6b complete on branch `processing-phase6b-tool-executor-fail-fast`
    (attachment enrichment/storage failures now propagate fail-fast; covered by unit tests).
  - References: `tool_execution.py:305`, `tool_execution.py:406`
  - Sources: `[Cdx][C678]`

- **18. Sync/stream feature drift in orchestration.**

  - Impact: Behavior inconsistencies and test blind spots.
  - Status (2026-03-09): Phase 8c complete on branch `processing-phase8c-orchestration-error-parity`
    (sync and stream orchestration now share a single history-write strategy via
    `ProcessingService._USE_ISOLATED_HISTORY_WRITES`, including user-trigger persistence and
    generated-message persistence).
  - References: `service.py:425`, `service.py:500`, `service.py:938`
  - Sources: `[Cdx][C678]`

- **19. `processed_trigger_parts` is computed but unused in both paths.**

  - Impact: Unclear contract and dead-return-path smell.
  - Status (2026-03-09): Phase 8d complete on branch `processing-phase8d-signature-and-cleanups`
    (shared turn-preparation path now returns only `typed_messages_for_llm`; no separate
    `processed_trigger_parts` value is produced or threaded through sync/stream orchestration).
  - References: `service.py:561`, `service.py:930`
  - Sources: `[Cdx][C678]` (corroborated)

- **20. Thought-signature detection logic is duplicated.**

  - Impact: Drift risk between history formatting and streaming loop behavior.
  - Status (2026-03-09): Phase 8d complete on branch `processing-phase8d-signature-and-cleanups`
    (conversation-level signature detection is now centralized in
    `messages_have_thought_signatures(...)` and reused by `LLMStreamingLoop`; helper behavior is
    covered by unit tests in `test_thought_signatures.py` and
    `test_processing_history_formatting.py`).
  - References: `context.py:134`, `llm_loop.py:228`
  - Sources: `[C678]` (Added from PR #678)

- **21. `attach_to_response` special handling is scattered across layers.**

  - Impact: Tool-specific behavior leaks into multiple pipeline stages.
  - Status (2026-03-08): Phase 8a complete on branch `processing-phase8a-attach-and-log-cleanups`
    (`ToolExecutionResult` now applies attachment queue updates directly, so `llm_loop` no longer
    contains separate explicit-vs-auto attachment update logic).
  - References: `llm_loop.py:558`, `tool_execution.py:378`
  - Sources: `[C678]` (Added from PR #678)

- **22. Tool execution logs full arguments.**

  - Impact: Sensitive values/PII may leak to logs.
  - Status (2026-03-08): Phase 8a complete on branch `processing-phase8a-attach-and-log-cleanups`
    (tool argument parse failures now log sanitized error summaries and store sanitized tracebacks
    instead of raw argument payloads).
  - References: `tool_execution.py:186`
  - Sources: `[Cdx]`

- **23. Done-event attachment payload hardcodes `"type": "image"`.**

  - Impact: Incorrect metadata for non-image attachments.
  - Status (2026-03-09): Complete on branch `processing-phase11-final-cleanup-tail`
    (`LLMStreamingLoop` now derives streamed attachment display types with
    `_infer_attachment_type(...)` instead of hard-coding `"image"`).
  - References: `llm_loop.py:466`
  - Sources: `[Cdx]`

- **24. Error persistence behavior is inconsistent between sync and stream.**

  - Impact: Divergent UX and history/audit trail behavior.
  - Status (2026-03-09): Phase 8c complete on branch `processing-phase8c-orchestration-error-parity`
    (sync and stream now both persist errors through shared helper `_persist_error_history_message`
    with consistent `ErrorMessage` payloads and write strategy).
  - References: `service.py:647`, `service.py:655`, `service.py:1019`
  - Sources: `[Cdx][C678]`

- **25. Processing layer contains database-dialect branching (`postgresql` vs others).**

  - Impact: Storage concerns leak into orchestration layer.
  - Status (2026-03-09): Phase 8b complete on branch
    `processing-phase8b-dialect-and-setter-cleanups` (`ProcessingService` now uses
    `DatabaseContext.supports_isolated_writes` and `create_isolated_context()`; dialect detection is
    owned by storage context instead of processing orchestration).
  - References: `service.py:780`, `service.py:982`
  - Sources: `[C678]` (Added from PR #678)

- **26. Complex confirmation callback type is duplicated across multiple method signatures.**

  - Impact: Hard to maintain; error-prone copy-paste types.
  - Status (2026-03-09): Phase 10 complete on branch `processing-phase10-type-boundary-hardening`
    (confirmation callback typing is now centralized as `RequestConfirmationCallback` in
    `tools/types.py`; processing/tool layers reuse that contract, and policy enforcement invokes
    callbacks by keyword arguments to prevent positional signature drift).
  - References: `service.py:148`, `service.py:206`, `service.py:266`, `service.py:687`,
    `llm_loop.py:67`, `tool_execution.py:70`
  - Sources: `[C678]` (Added from PR #678)

- **27. `ChatInteractionResult` success/error semantics are ambiguous (`text_reply` can be `None` on
  success).**

  - Impact: Callers must infer state from multiple nullable fields.
  - Status (2026-03-09): Phase 9 complete on branch `processing-phase9-types-and-config-cohesion`
    (`ChatInteractionResult.success(...)` now always carries string `text_reply` (empty string when
    no textual assistant reply), removing nullable success payload ambiguity while preserving
    explicit error typing via `status` and `error_traceback`).
  - References: `types.py:16`
  - Sources: `[C678]` (Added from PR #678)

- **37. Stream done-event attachment metadata lookup swallowed failures and continued with partial
  metadata.**

  - Impact: Hidden attachment-registry failures can silently degrade streamed attachment payloads.
  - Status (2026-03-08): Phase 7a complete on branch
    `processing-phase7-attachment-context-fail-fast` (missing attachment metadata in done-event
    enrichment now raises fail-fast).
  - References: `llm_loop.py:472`
  - Sources: `[Cdx]` (Added during implementation)

- **38. Thread attachment context extraction swallowed exceptions and returned empty context.**

  - Impact: Attachment context can disappear silently, degrading prompt grounding.
  - Status (2026-03-08): Phase 7a complete on branch
    `processing-phase7-attachment-context-fail-fast` (attachment-context extraction now propagates
    failures).
  - References: `attachments.py:285`
  - Sources: `[Cdx]` (Added during implementation)

### P2

- **28. Processing interfaces use `Any` where stronger types/protocols are available.**

  - Impact: Weaker static guarantees and easier contract drift.
  - Status (2026-03-09): Phase 10 complete on branch `processing-phase10-type-boundary-hardening`
    (processing callback typing now imports shared `RequestConfirmationCallback` protocol from tools
    typing instead of defining an ad-hoc `dict[str, Any]` callback signature in
    `processing/types.py`).
  - References: `service.py:70`, `llm_loop.py:87`, `tool_execution.py:89`
  - Sources: `[Cdx][G677][C678]` (corroborated by both PRs)

- **29. `event_sources` typing in processing does not match tool-context typing.**

  - Impact: Type inconsistency across layer boundary.
  - Status (2026-03-09): Phase 10 complete on branch `processing-phase10-type-boundary-hardening`
    (`TaskWorker` and processing runtime paths now use shared `EventSourcesById` typing, matching
    tool-context contracts).
  - References: `tool_execution.py:93`, `tools/types.py:243`
  - Sources: `[Cdx]`

- **30. `delegation_security_level` is unbounded string rather than literal/enum.**

  - Impact: Invalid config states are not caught early.
  - Status (2026-03-09): Phase 9 complete on branch `processing-phase9-types-and-config-cohesion`
    (`ProcessingServiceConfig` now enforces `DelegationSecurityLevel` at runtime via
    `__post_init__`; string callsites in tests were migrated to explicit enum values).
  - References: `processing/types.py:49`
  - Sources: `[Cdx]`

- **31. `ProcessingServiceConfig` aggregates many unrelated concerns.**

  - Impact: Poor cohesion; difficult configuration evolution.
  - Status (2026-03-09): Phase 9 complete on branch `processing-phase9-types-and-config-cohesion`
    (processing helpers now depend on focused config protocols (`ContextPreparerConfig`,
    `ToolExecutorConfig`, `LLMStreamingLoopConfig`) rather than the full concrete config contract,
    reducing cross-component coupling while preserving runtime behavior).
  - References: `processing/types.py:41`
  - Sources: `[C678]` (Added from PR #678)

- **32. `ProcessingService` setter wiring depends on `hasattr` and mutable post-construction
  state.**

  - Impact: Temporal coupling and fragile initialization sequencing.
  - Status (2026-03-09): Phase 8b complete on branch
    `processing-phase8b-dialect-and-setter-cleanups` (`Assistant` now constructor-injects profile
    runtime dependencies (`processing_services_registry`, Home Assistant client, camera backend)
    instead of post-construction setter wiring).
  - References: `service.py:114`, `service.py:80`
  - Sources: `[C678]` (Added from PR #678)

- **33. Internal helpers with leading underscore are exported via package `__all__`.**

  - Impact: Public API boundary ambiguity.
  - Status (2026-03-09): Complete on branch `processing-phase11-final-cleanup-tail`
    (\[processing/__init__.py\] now exposes only the intended public processing API; internal
    pruning helpers remain importable from `processing.utils` only).
  - References: `processing/__init__.py:6`
  - Sources: `[Cdx][C678]` (corroborated)

- **34. Hard-coded policy constants (for example, pruning turn count).**

  - Impact: Behavior tuning requires code edits instead of config.
  - Status (2026-03-09): Complete on branch `processing-phase11-final-cleanup-tail` (context pruning
    already uses configurable `context_pruning_min_turns`, and delegation confirmation now uses
    profile `tools_config.confirmation_timeout_seconds` instead of a local `60.0` constant).
  - References: `processing/utils.py:111`
  - Sources: `[Cdx][C678]`

- **35. MIME-type detection for large results relies on full `json.loads` attempt.**

  - Impact: Potentially expensive parsing used only for heuristic classification.
  - Status (2026-03-09): Complete on branch `processing-phase11-final-cleanup-tail`
    (`AttachmentProcessor.handle_large_result(...)` now uses `_looks_like_json_content(...)`
    heuristic detection instead of eager `json.loads(...)` parsing for MIME classification).
  - References: `attachments.py:531`
  - Sources: `[C678]` (Added from PR #678)

- **36. Minor cleanup noise (redundant `x if x is not None else None`, explicit role literals in
  constructors).**

  - Impact: Low-severity readability debt.
  - Status (2026-03-09): Complete on branch `processing-phase11-final-cleanup-tail` (remaining
    redundant `ToolMessage(role="tool", ...)` calls in processing code were removed; earlier phases
    had already eliminated the other noted no-op conditional noise).
  - References: `service.py:658`, `llm_loop.py:491`, `tool_execution.py:134`
  - Sources: `[C678]` (Added from PR #678)

## Recommended Execution Order

1. Fix core correctness parity and hidden-data-loss failures.

   - Items: 1, 2, 3, 4, 5, 7, 8, 12, 13

2. Normalize prompt and error semantics.

   - Items: 6, 9, 10, 11, 24

3. Stabilize attachment/tool execution behavior.

   - Items: 14, 15, 16, 17, 21, 22, 23

4. Reduce orchestration drift and complexity.

   - Items: 18, 19, 20, 25, 26, 27

5. Harden types and API boundaries.

   - Items: 28, 29, 30, 31, 32, 33, 34, 35, 36

## Suggested Working Method

For each selected backlog item:

1. Implement the smallest coherent change.
2. Add/adjust tests that fail before and pass after.
3. Run:
   - `scripts/format-and-lint.sh`
   - Relevant targeted tests
   - `poe test`
4. Land as a focused commit before moving to the next item.
