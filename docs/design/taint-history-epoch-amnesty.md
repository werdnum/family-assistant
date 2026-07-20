# Taint History Epoch Amnesty

## Status

Implemented. Companion to [runtime-taint-machinery.md](runtime-taint-machinery.md), which describes
the runtime taint state, source trust tiers, and the taint-by-sink policy matrix that this design
unblocks in production.

## Problem

The production deployment (single family, `taint_policy.mode: observe`) cannot flip to `enforce`
because the observed taint state is dominated by legacy poison rather than genuine untrusted
content. A week of live taint-audit data showed:

- 85% of 4,939 taint events at `max_tier=unknown_external`.
- 75% of policy evaluations would require user confirmation under enforce mode (~200 would-enforce
  errors/day), dominated by `get_note` / `search_calendar_events` hitting the
  `sensitive_read_broadening` floor.
- The `legacy_missing_taint_metadata` label appeared 65,917 times in one week of events.
- 91% of July Telegram user messages classified as `unknown_external`.

The cause is not untrusted content reaching the assistant. It is two interacting mechanisms:

### 1. The legacy read-time fallback

Message-history rows created before the taint-metadata migration (2026-07-06) have no taint
metadata. At read time, `_message_history_taint_metadata()`
(`src/family_assistant/storage/repositories/message_history.py`) substitutes a synthetic
`unknown_external` source labeled `legacy_missing_taint_metadata`. This is the correct conservative
default in isolation — but a single such row escalates the entire turn to `unknown_external`.

### 2. Snapshot re-baking

The poison is not merely inherited from old rows; it is **re-baked** into new ones. Every
assistant/tool row persists the full merged turn snapshot (`taint_tracker.snapshot().to_metadata()`
in `src/family_assistant/processing/llm_loop.py`), so once a turn has been escalated by a legacy
row, each new row carries the escalated state forward in its *own* metadata. Even though the
Telegram prompt window is only 10 messages / 2 hours, the escalation propagates from row to row
indefinitely: threads never heal, and rows written months after the migration still read back as
`unknown_external`.

## Why not a data migration?

Two obvious alternatives were rejected because the stored data cannot support them:

- **Strip migration** (delete `legacy_missing_taint_metadata` sources from stored metadata and
  recompute): `TurnTaintState.to_metadata()` truncates to the last 12 sources and persists a bare
  `max_tier`. When `from_metadata()` finds `max_tier` exceeding the retained source summaries, it
  synthesizes *anonymous* `manual` sources with no labels and no source id
  (`src/family_assistant/security/taint.py`). In many stored rows the attribution (legacy poison vs.
  genuine email/web content) is therefore already destroyed — there is nothing precise left to
  strip. A migration would either under-strip (leaving poison) or over-strip (rewriting rows whose
  escalation was genuine), and it would permanently rewrite audit-relevant provenance either way.
- **Replay backfill** (recompute each row's taint from its turn's inputs): the inputs needed to
  replay classification (original email trust decisions, tool output tiers at the time, upstream
  turn state) are not durably stored per row. Replay would be a large, bespoke, one-shot pipeline
  reconstructing state that the system was never designed to reconstruct, with the same
  truncation-anonymization problem at every hop.

Read-time epoch filtering avoids both failure modes: stored rows are left untouched (fully
reversible by unsetting one config key), and the interpretation policy is explicit, testable, and
uniform across every read path.

## Design

### Config: `taint_policy.history_taint_epoch`

A new optional field on `TaintPolicyConfig` (`src/family_assistant/security/taint.py`):

- ISO-8601 timestamp; **must** be timezone-aware. A naive or unparseable value fails startup with a
  message telling the operator to quote the value (unquoted YAML timestamps parse as naive
  datetimes). Validated values are normalized to UTC.
- Unset (`null`, the default) preserves current behavior exactly — no amnesty.
- Profiles cannot set it (`merge_taint_policy_config` raises, mirroring `operator_minimum`): the
  epoch is a deployment-level statement about stored data, not a per-profile policy knob.

### Read-time filtering semantics

Rows with `timestamp < history_taint_epoch` (**pre-epoch** rows):

- **No metadata** (`taint_metadata_json` null): contribute **no taint**. The synthetic
  `legacy_missing_taint_metadata` fallback is skipped entirely.
- **`runtime_v1` metadata**: only explicitly attributed sources are kept. Two kinds of source are
  dropped: (a) any source labeled `legacy_missing_taint_metadata` (the read-time fallback, re-baked
  or not), and (b) anonymous manual escalation artifacts — `source_type=manual`, no labels, no
  `source_id` — which are exactly what `from_metadata()` synthesizes for truncated/omitted summaries
  and malformed entries. Kept: typed genuine sources — `email` (which carries the explicit
  `source_unknown_external` artifact label from email intake), `tool_output` (with call ids),
  `note`, `user_message`, `browser_snapshot`, etc. The row's taint contribution (`max_tier`,
  `history_high_taint_present`) is **recomputed from the kept sources only**; the persisted
  `max_tier` is deliberately not honored, because for pre-epoch rows it may encode nothing but
  re-baked poison whose attribution was truncated away.
- **Malformed metadata**: contributes no taint (it is pre-epoch legacy junk by definition; the same
  amnesty as null metadata).

Rows with `timestamp >= history_taint_epoch` (**post-epoch** rows) are **trusted as recorded**, with
one timestamp-independent exception described next. A post-epoch row with a taint-applicable role
(`user`/`assistant`/`tool`) and missing metadata still receives the worst-case
`legacy_missing_taint_metadata` fallback, but the event is now logged at **ERROR** level with
conversation id, role, tool name, and timestamp: with the epoch set, missing metadata on a new row
is a write-path regression, not an expected legacy condition. The alarm is deduplicated once per
conversation per process (a small LRU) so a single broken conversation does not flood the error log
on every history read. (The known metadata-less write paths are being fixed in a separate in-flight
PR; this alarm is the read-side tripwire that keeps them fixed.)

### Timestamp-independent legacy-echo filtering

A source labeled `legacy_missing_taint_metadata` found *inside* persisted `runtime_v1` metadata is,
by construction, a second-hand **echo** of some other row's read-time fallback: the label is only
ever stamped on the synthetic source that `_message_history_taint_metadata()` fabricates for a row
with *missing* metadata, which is then re-baked into that turn's snapshot. It is never a first-hand
attribution. `strip_legacy_labeled_echoes()` (`src/family_assistant/security/taint.py`) therefore
drops these echoes from every history row's stored `sources` **regardless of the row's timestamp**
(the pre-epoch path in `amnestied_history_taint_metadata()` already dropped them; this extends the
same drop to post-epoch rows). When an echo is dropped, the row's contribution is **recomputed from
the surviving sources** rather than honoring the stored `max_tier` — a row whose only source was the
echo contributes nothing.

This closes a self-healing gap: if an operator sets the epoch earlier than this feature's deploy,
rows written between the epoch and the deploy already carry re-baked echoes in their own snapshots.
Without this filter those rows read as post-epoch and unfiltered, so an active conversation keeps
re-seeding `unknown_external` from them and re-persisting poisoned snapshots — self-healing would be
blocked until a full prompt window turned over with no such row.

Two properties are deliberately preserved:

- **First-hand missing metadata is not weakened.** A post-epoch row that itself has *missing*
  metadata (null `taint_metadata_json`) still gets the synthetic labeled fallback (worst-case
  `unknown_external`) and still fires the ERROR write-path regression alarm. That path synthesizes
  the label fresh; it does not read it back from stored `sources`, so echo-stripping never touches
  it.
- **Anonymous manual artifacts in post-epoch rows are NOT dropped.** Only the explicitly labeled
  echoes are stripped. Anonymous `manual`/no-label/no-`source_id` escalation artifacts can
  legitimately represent a *truncated genuine* source in a post-epoch row, so they are read
  conservatively.

### Where the filtering runs

The semantics live in the security module (`amnestied_history_taint_metadata()` in
`src/family_assistant/security/taint.py`) next to `from_metadata()`/`merge_history_taint()`, and are
applied at the single materialization choke point `_message_history_taint_metadata()` in the
message-history repository — the one place that sees both the row timestamp and the raw stored
metadata, and which feeds every consumer (LLM turn preparation via `merge_history_taint()`, history
APIs, message-history search, A2A serialization). Filtering happens on the *raw metadata dict
before* `from_metadata()` runs, so the "max_tier exceeded retained source summaries" escalation path
cannot re-add what was filtered: the recomputed metadata's `max_tier` always equals the maximum of
its kept sources.

The epoch reaches the repository via an engine-scoped registry (`set_engine_history_taint_epoch()`
in `src/family_assistant/storage/context.py`, called once at startup in `assistant.py`;
`DatabaseContext.history_taint_epoch` reads it back). `DatabaseContext` objects are constructed from
a bare engine at dozens of call sites (web dependency, Telegram, task worker, A2A, scripting), so
attaching the epoch to the engine — like the engine itself, a deployment-scoped object wired at the
outer layer — gives uniform behavior on every read path without re-threading a parameter through
every creation site. Engines that never register an epoch (tests, scripts) keep the conservative
legacy behavior.

### Self-healing induction

The fix converges by induction, without touching stored rows:

1. The first post-deploy turn merges history in which every pre-epoch row's poison is filtered out,
   so the turn starts clean (absent genuine taint).
2. That turn's snapshot — persisted into its new (post-epoch) rows — is therefore clean.
3. Subsequent turns read those clean post-epoch rows as recorded, and remain clean.

The re-baking mechanism that made the poison self-perpetuating now propagates health instead: one
clean read is enough to stop the escalation chain for a thread forever.

### Audit endpoint

`GET /api/diagnostics/taint-audit` now surfaces the configured epoch (`history_taint_epoch`, ISO
string or null) and, when an epoch is set, splits the message-history inventory into
`pre_epoch_rows` / `post_epoch_rows`, adds a per-group `pre_epoch` flag, and reports
`post_epoch_missing_required_metadata_rows` — the number that must be (and stay) zero before
flipping enforce mode.

## Accepted residual risk

Pre-epoch rows whose *genuine* untrusted provenance was truncation-anonymized (an anonymous manual
escalation artifact standing in for, say, real email content that fell off the 12-source window) are
amnestied along with the poison — the stored data cannot distinguish them. This is:

- **One-time and bounded**: it applies only to rows persisted before the operator-chosen epoch;
  nothing after the epoch is ever amnestied.
- **Partially mitigated**: explicitly labeled sources are preserved. Email intake stamps
  `source_unknown_external` on its sources, and typed `tool_output`/`browser_snapshot` sources keep
  their tiers, so the highest-signal genuine provenance survives the filter.
- **Operator-accepted**: the operator sets the epoch knowingly, for a deployment where the pre-epoch
  window is dominated by known-benign family conversation, and accepts that a stale prompt injection
  hiding in an anonymized pre-epoch row would lose its taint marking. The alternative — permanent
  confirmation fatigue that trains rubber-stamping — is the greater practical risk (see the
  observe-mode numbers above).

Note that `get_note`/`list_notes` are `OUTPUT_TRUSTED` and do not re-taint turns; post-amnesty they
trigger `sensitive_read_broadening` confirmations only in genuinely tainted turns, which is the
intended behavior.

If an operator sets the epoch *earlier* than this feature's deploy (against the guidance below),
rows written in the gap between the epoch and the deploy may carry re-baked poison as an **anonymous
truncation artifact** rather than a labeled echo. Timestamp-independent echo-stripping removes the
labeled form but reads the anonymous form conservatively, so such a row keeps contributing
`unknown_external` until the thread's prompt window turns over past it. This residual is bounded (it
never touches rows written after the deploy) and is moot when the epoch is set to the deploy time or
later, as prescribed below.

## Rollout plan

1. **Deploy** with `taint_policy.history_taint_epoch` set in operator config to the instant this
   feature is **deployed** (or later) — quoted ISO-8601 with offset, e.g.
   `"2026-08-01T00:00:00+00:00"`. **Never set it earlier** (in particular, not the taint-metadata
   migration date `2026-07-06`): rows written before the deploy may contain re-baked legacy poison
   that read-time amnesty only partially neutralizes (labeled echoes are dropped regardless of
   timestamp, but anonymized truncation artifacts in those rows are read conservatively), so an
   earlier epoch treats them as post-epoch and keeps re-seeding the poison. Setting the epoch at (or
   after) deploy makes every gap row a genuine post-deploy write with clean metadata.
2. **Watch `GET /api/diagnostics/taint-audit` for ~1 week**: `unknown_external` share and
   `legacy_missing_taint_metadata` occurrences should collapse;
   `post_epoch_missing_required_metadata_rows` must be zero (any nonzero value or
   `post_epoch_missing_taint_metadata` ERROR log means a write path regressed — fix before
   proceeding).
3. **Flip `taint_policy.mode: enforce`** and remove the
   `google_integration.require_taint_enforcement: false` waiver if one was set.
4. **Add the Gmail/Drive write scopes** (`gmail.compose`, `drive.file`), which the enforcement floor
   was gating.

Rollback at any step is config-only: unsetting the epoch restores the previous conservative
read-time behavior unchanged.
