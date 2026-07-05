# Profile-Confined Note Writes and Automation Approvals

## Problem

Unattended maintenance automations need to do work such as:

1. Read operational diagnostics, such as recent error logs.
2. Extract distinct issues.
3. Persist the result somewhere durable, usually a note.
4. Escalate to a human, or file a ticket, when severity crosses a threshold.

The current default security split intentionally makes this awkward:

- The `engineer` profile can read diagnostics, source, and database state, but is read-only.
- Trusted/default profiles can write notes, schedule tasks, and communicate, but should not receive
  broad engineering diagnostic access by default.
- Automation scripts execute under the profile that created the automation, preserving provenance
  and policy.
- Note visibility is profile-scoped, but `default_note_visibility_labels` is only a default. A
  caller with `add_or_update_note` can explicitly pass `visibility_labels=[]` and create an
  unrestricted note.

That last point means a restricted automation profile cannot currently rely on
`default_note_visibility_labels` as a write confinement boundary.

## Threat Model

In Rule-of-Two terms, this automation is an **[A]+[B] agent**: it processes untrustworthy input (log
messages and tracebacks can contain attacker-influenced content — errors triggered by external
email, web content, or API responses) and it reads sensitive diagnostics. The design's job is
therefore to make its **[C] as narrow and auditable as possible**:

- Unattended writes go only into quarantined sinks: label-confined notes, and a deployment-private
  tracker that sits inside the trust boundary.
- LLM output derived from log content is treated as data, never as control flow.
- Anything that genuinely leaves the quarantine (free-form outbound messages, shared destinations)
  goes through a human.

The goal is risk reduction, not an iron-clad guarantee. Residual risks are listed explicitly at the
end.

## Goals

- Reuse existing profile provenance, tool policy, durable confirmations, and note visibility.
- Make note visibility labels usable as a real write-confinement boundary, enforced at the storage
  layer rather than in a single tool.
- Support unattended maintenance profiles without bespoke tools for every report type.
- Allow unattended escalation (notification, ticket filing) without a per-action human confirmation,
  by confining the destination instead.
- Preserve current default behavior for existing profiles unless they opt into stricter semantics.

## Non-Goals

- Replacing note visibility labels with per-note ACLs.
- Making `engineer` a state-changing profile.
- Allowing generalized unattended delegation to privileged profiles.
- Target execution profiles (creating an automation from one profile that runs as another). This was
  previously Part 4 of this design; it is deferred to Future Work below because the delegation-based
  setup path covers the current need without new machinery.
- A concrete ticket-tracker integration. The escalation destination is deployment-specific (the
  reference deployment uses an internal Vikunja instance); this design only states the constraints
  such a tool must satisfy.
- Solving arbitrary exfiltration through every possible state-changing tool. This design covers note
  writes and the diagnostics escalation path.

## Current Semantics

Visibility labels are restrictions. A profile can read a note when:

```text
note.visibility_labels subset-of profile.visibility_grants
```

No labels means unrestricted. More labels means more restricted. The read-side subset check is
enforced in the repository (`NotesRepository._apply_visibility_filter`) on both SQLite and
PostgreSQL.

Current write behavior:

- `add_or_update_note` accepts optional `visibility_labels`.
- If `visibility_labels` is omitted for a new note, `exec_context.default_note_visibility_labels` is
  applied.
- If `visibility_labels=[]` is supplied, the note is explicitly unrestricted.
- Updates preserve labels when `visibility_labels` is omitted.
- Updates can explicitly clear labels with `visibility_labels=[]`.
- The tool refuses to update an existing note the caller cannot see.

Crucially, all of this write-side behavior lives in the **tool layer only** (`tools/notes.py`). The
repository (`NotesRepository.add_or_update`, `rename_and_update`) performs no visibility
enforcement. Known write paths that bypass the tool guard today:

- The workspace-file import path (`tools/workspace_files.py`) writes notes with no see-before-write
  check and no default labels.
- `rename_and_update` has no visibility gating.
- The web notes API is intentionally unfiltered (admin management surface).
- The asterisk call-transcript writer applies default labels but no other policy.

This is useful for full-trust profiles, but too loose for unattended restricted profiles: label
confinement cannot be a boundary while enforcement is a property of one tool.

## Design Part 1: Note Write Confinement

Add explicit write-confinement settings separate from the existing defaulting setting, and enforce
them in the repository so every write path is covered.

```yaml
service_profiles:
  - id: "ops_automation"
    visibility_grants: ["ops_diagnostics"]
    processing_config:
      default_note_visibility_labels: ["ops_diagnostics"]
      required_note_visibility_labels: ["ops_diagnostics"]
      allowed_note_visibility_labels: ["ops_diagnostics"]
```

### New Config Fields

Add these optional fields to `ProcessingConfig`:

- `required_note_visibility_labels: list[str] | None = None`
- `allowed_note_visibility_labels: list[str] | None = None`

Semantics:

- `default_note_visibility_labels` remains what it is today: labels used when the caller omits
  `visibility_labels` for a new note.
- `required_note_visibility_labels` is a write floor. These labels are always unioned into note
  writes performed by the profile.
- `allowed_note_visibility_labels`, when set, is a write ceiling. The final labels on the note must
  be a subset of this list.

This avoids changing the meaning of `default_note_visibility_labels` for existing profiles while
giving restricted profiles a real boundary.

### Enforcement Layer: `NoteWritePolicy` in the Repository

Enforcement moves into the repository, carried by a new value object:

```python
@dataclass(frozen=True)
class NoteWritePolicy:
    visibility_grants: set[str] | None      # for the see-before-overwrite check
    default_labels: list[str] | None        # applied when a new note omits labels
    required_labels: list[str] | None       # write floor (unioned in)
    allowed_labels: list[str] | None        # write ceiling (subset check)

    UNCONSTRAINED: ClassVar["NoteWritePolicy"]  # all fields None
```

`NotesRepository.add_or_update` and `rename_and_update` take `write_policy: NoteWritePolicy` as a
**required** parameter. There is no default: every caller must make a visible decision, enforced by
the type checker. Trusted admin surfaces pass `NoteWritePolicy.UNCONSTRAINED` explicitly, with a
one-line justification comment at the call site. This makes opt-outs greppable and obvious in
review, and flips the failure mode from "forgot to opt in → silent hole" to "must consciously opt
out → visible in diff."

A `NoteWritePolicy` is derived once from `ToolExecutionContext` (a small helper such as
`exec_context.note_write_policy()`) and passed by every tool-layer writer: `add_or_update_note`, the
workspace-file import path, and the asterisk transcript writer. The web notes API passes
`UNCONSTRAINED` (it is the admin management surface and bypasses visibility by design).

Add an ast-grep conformance rule restricting `NoteWritePolicy.UNCONSTRAINED` to approved locations
(e.g. `web/routers/`), so a future caller cannot quietly opt out.

### Write Algorithm

Performed inside the repository for `add_or_update` (and analogously for `rename_and_update`):

1. Resolve the existing note. If `write_policy.visibility_grants` is set and a note with the title
   exists but is not visible under those grants, deny the write. (This check exists in the tool
   today; it moves down so bypass paths get it too.)
2. Compute base labels:
   - Existing note + omitted `visibility_labels`: preserve existing labels.
   - New note + omitted `visibility_labels`: use `write_policy.default_labels` if set, otherwise
     `[]`.
   - Explicit `visibility_labels`: use the explicit value.
3. If the note already exists and the policy is confined (has `required_labels` or
   `allowed_labels`), the existing note's current labels must already satisfy the confinement: every
   required label present, no label beyond the ceiling. Otherwise deny. Without this, a title
   collision with an unrestricted note (empty labels, so visible to everyone and passing step 1)
   would append the required labels and silently pull an unrelated user note into the quarantine
   space — a confined writer may only update notes already inside its confinement.
4. Compute final labels:
   - `final_labels = base_labels union write_policy.required_labels`
5. Validate final labels:
   - If `write_policy.allowed_labels` is set, every final label must be included in it.
   - If validation fails, raise; the tool layer surfaces this as a tool error. Do not write.
6. Persist `final_labels`.

Steps 1 and 3 are also re-asserted **atomically with the write** (as the WHERE of the
`ON CONFLICT DO UPDATE` on PostgreSQL, and of the conflict-recovery UPDATE on SQLite): the preflight
reads cannot see a same-title row inserted by another transaction in the meantime, so without this a
title race would overwrite a row the preflight would have rejected. A conflict update excluded by
the policy WHERE (rowcount 0) raises instead of writing.

Examples:

| Profile Config                                              | Tool Args     | Final Labels      | Result  |
| ----------------------------------------------------------- | ------------- | ----------------- | ------- |
| `required=["ops_diagnostics"]`                              | omitted       | `ops_diagnostics` | allowed |
| `required=["ops_diagnostics"]`                              | `[]`          | `ops_diagnostics` | allowed |
| `required=["ops_diagnostics"], allowed=["ops_diagnostics"]` | `["default"]` | `default, ops...` | denied  |
| no required/allowed labels                                  | `[]`          | `[]`              | allowed |

### Context Threading

The two new `ProcessingConfig` fields flow along the same path as `default_note_visibility_labels`
today, and must reach all three places that build a tool execution context:

1. `ToolExecutionContext` (normal interactive turns).
2. The script-execution context rebuild in `task_worker.py` (`handle_script_execution` re-points the
   context at the automation's stored profile and already threads `default_note_visibility_labels`;
   the new fields ride along).
3. Deferred-confirmation execution in `task_worker.py`, which rebuilds context from the stored
   confirmation row's `processing_profile_id`.

Without (2) and (3), automation scripts and confirm-then-run tools would silently escape the
confinement.

### Why Not Reinterpret `default_note_visibility_labels`?

The existing repository and tests treat explicit `visibility_labels=[]` as meaningful.
Reinterpreting `default_note_visibility_labels` as mandatory would silently change existing
behavior, including profiles that use it only as a convenience default. The safer move is to add new
fields for mandatory labels.

### Tool Schema Wording

Update the `add_or_update_note` schema:

- Keep `visibility_labels` available.
- Add wording that profile policy may add required labels or reject label values.
- Make clear that `[]` only means unrestricted when the active profile permits unrestricted note
  writes.

### Confirmation Rendering

When a confirm-gated note write is displayed, show both:

- Requested visibility labels.
- Effective visibility labels after profile write policy.

This prevents a user approving a misleading payload where the model asked for one label set but the
runtime applies another. Implementation note: confirmation renderers (`tools/confirmation.py`)
currently receive only tool arguments, so the effective labels must be computed before rendering
(the same `NoteWritePolicy` derivation, applied to the requested labels) and passed to the renderer,
or the renderer signature must gain context.

The renderer also mirrors the repository's see-before-overwrite check: when the target title belongs
to a note the active profile cannot see, the prompt reports the rejection up front instead of
showing effective labels for a write that will be refused after approval anyway.

## Design Part 2: Maintenance Profile

Define a narrow profile for unattended operational summaries:

```yaml
service_profiles:
  - id: "ops_automation"
    description: "Unattended operational diagnostics automation."
    visibility_grants: ["ops_diagnostics"]
    processing_config:
      default_note_visibility_labels: ["ops_diagnostics"]
      required_note_visibility_labels: ["ops_diagnostics"]
      allowed_note_visibility_labels: ["ops_diagnostics"]
      allow_wake_llm: false
    tools_policy:
      default_decision: "deny"
      rules:
        - match:
            names:
              - "create_automation"
            argument_equals:
              action_type: "wake_llm"
          decision: "deny"
          priority: 20
          description: "Script actions only; this profile opts out of waking the LLM (allow_wake_llm=false)."
        - match:
            names:
              - "read_error_logs"
            argument_equals:
              include_extra_data: true
          decision: "deny"
          priority: 20
          description: "message+traceback only; the freeform extra_data field can carry sensitive/bulky data."
        - match:
            names:
              - "read_error_logs"
              - "add_or_update_note"
              - "create_automation"
          decision: "allow"
          priority: 10
```

This profile can:

- Read bounded diagnostic logs (message + traceback; no `extra_data`).
- Write notes only into `ops_diagnostics`.
- Create its own script automations.

It cannot:

- Read general family notes unless it receives those grants.
- Write unrestricted notes.
- Create `wake_llm` automations, or wake the LLM from a script (see below).
- Read the freeform `extra_data` field via `read_error_logs(include_extra_data=True)` (denied by
  policy).
- Enumerate or read other profiles' automations: `get_automation`/`list_automations` are **not**
  granted, because `get_automation` fetches with `conversation_id=None` and returns inline script
  bodies, which is broader than this profile's bounded access.
- Delegate to `engineer` unless separately configured.
- Send messages unless explicitly granted.

### wake_llm Profile Routing (F0 fix)

Historically `wake_llm` actions never threaded a profile: `execute_action` threaded
`processing_profile_id` into the `script_execution` payload only, and `handle_llm_callback`'s single
profile switch was a hardcoded `reminder` lookup. So **every** `wake_llm` turn ran under the task
worker's default trusted profile — including event-listener wakes triggered by attacker-influenced
email/webhook content, which is exactly the injectable [ABC] case the docs claimed `event_handler`
protected against.

`wake_llm` now honors a threaded execution profile:

- Every `llm_callback` enqueue point stamps `processing_profile_id`: `execute_action`,
  `schedule_automations` (create / enable / reschedule / recurring advance), the script built-in
  `_process_script_wake_llm`, and `schedule_future_callback`.
- `handle_llm_callback` resolves it via `_resolve_execution_service` (shared with script execution):
  a stamped non-default profile that cannot be resolved raises rather than silently downgrading to
  the default. Reminders keep switching to the `reminder` profile.
- **Routing** (see "wake_llm routing" decision):
  - **Event listeners → `event_handler`** (restricted). Event triggers are untrusted and the woken
    LLM is the injectable component, so `EventProcessor` routes event `wake_llm` to `event_handler`
    regardless of who created the listener. (Event `script` actions still run under the creating
    profile so their validated tool set matches execution.)
  - **Schedule automations and `schedule_future_callback` → the originating (creating) profile** —
    trusted, user-set-up triggers.
  - **Reminders → `reminder`** (unchanged).
  - **Script failure notifications → the failed script's profile.** The error-summary `llm_callback`
    embeds the failed script, error, and (untrusted) event data, so
    `_enqueue_script_error_notification` stamps it with the script's stored profile. If that profile
    sets `allow_wake_llm: false` (or can no longer be resolved), the LLM notification is skipped
    entirely — the fixed-template push notification still fires — so a crashing confined script
    cannot wake the trusted default profile.

Because routing sends event-triggered wakes to `event_handler`, that profile's note writes are
quarantined too: `event_handler` carries `event_logs` as its default/required/allowed note labels,
and no other shipped profile is granted `event_logs`. A prompt-injected event can therefore not
plant or overwrite a note that the trusted assistant would later load into its prompt context — the
same reader-side quarantine pattern as `ops_diagnostics`.

The `allow_wake_llm` guard is enforced at **every** wake trigger point, not just automation
creation: `create_automation(action_type="wake_llm")`, `update_automation` of an existing `wake_llm`
automation (which would otherwise let a confined profile keep or reschedule a same-profile/legacy
wake), `execute_action`, `schedule_action`, `schedule_future_callback`, and the script built-in
`wake_llm()`. It is also re-checked at **execution time** for wakes the creation guards cannot
cover: `handle_llm_callback` refuses an already-enqueued wake whose stamped profile now disallows
waking, and the `EventProcessor` refuses a `wake_llm` listener whose *origin* profile disallows
waking before routing it to `event_handler` (pre-existing or admin-created listeners never passed a
creation guard).

### Script Actions Only (for `ops_automation`)

`ops_automation` additionally sets `allow_wake_llm: false` so it cannot wake the LLM at all — not
because wake_llm is broken (it now honors the profile), but because this profile's whole point is to
produce *data* (a confined note), never a conversational turn. `script` + `llm_json` is the right
Rule-of-Two shape: the script author controls all tool calls deterministically, and LLM output
derived from untrusted log content lands only in data (the note body), never in control flow.

Enforcement is a per-profile capability plus belt-and-braces policy:

- **`allow_wake_llm` capability (`ProcessingConfig` field, default `True`).** Confined profiles set
  `allow_wake_llm: false`. A shared `assert_wake_llm_allowed(action_type, allow_wake_llm)` guard
  runs at **every** point a wake can be triggered:

  - `create_automation` (refuses creating a `wake_llm` automation),
  - `execute_action` (refuses enqueuing a `wake_llm` action — one-time schedules and event
    listeners),
  - **`_process_script_wake_llm`** — the script built-in `wake_llm()`. This one matters: an allowed
    `action_type="script"` can still call Monty's `wake_llm()`, so the guard runs before the
    script's accumulated wakes are enqueued.

  The capability (rather than a "non-default profile" heuristic) is used because full-capability
  non-default profiles (`event_handler`, `complex_tasks`) legitimately wake the LLM — only profiles
  that opt into confinement are blocked.

- **Policy** (above), belt-and-braces: deny `create_automation` with
  `argument_equals: {action_type: "wake_llm"}`. This works with the existing matcher because
  `action_type` is a required argument, so it is always present. (`update_automation` cannot change
  an automation's action type.)

### Triage Script Shape

For the daily log-triage use case, the script shards LLM work rather than piping all logs into a
single call:

1. Page through `read_error_logs` using level/logger filters and the time window.
2. Group deterministically first (logger + exception type + normalized message) — no LLM needed.
3. Make one bounded `llm_json` call per group (or per batch of small groups) to summarize.
4. A final small `llm_json` reduce call ranks and rolls up the day's report.
5. Write the report to the confined note; escalate per Part 4 if severity warrants.

Map-reduce over deterministic shards also degrades gracefully: one oversized shard fails, the rest
of the report still lands.

### Reader-Side Quarantine

Do **not** grant `ops_diagnostics` to the default assistant profile. Label confinement then protects
readers too: a triage note whose content was influenced by injected log text can never enter the
trusted assistant's prompt context automatically. Humans read the report via the web UI, and
anything acted on crosses the trust boundary through them.

This binary quarantine — the label is simply never granted to the default assistant — is the
coarsest available setting, and the right one today. Once taint-aware sink policy exists (see
"Relationship to the Provenance/Taint Roadmap" below), a graduated alternative becomes possible:
labelled content may enter the trusted assistant's context, but its presence escalates
external-communication/egress tools to confirm for the remainder of the turn. That keeps the
diagnostics report usable in conversation without opening an unattended exfiltration path.

## Design Part 3: Bounded Diagnostic Read Tool

`read_error_logs` currently supports `level`/`logger_name`/`limit` only. Add a bounded time filter
and an `extra_data` toggle:

```python
read_error_logs(
    level: str | None = None,
    logger_name: str | None = None,
    limit: int = 50,
    since_hours: int = 168,
    include_extra_data: bool = False,
)
```

The field worth gating is **`extra_data`**, not the traceback. A Python traceback is call stack,
source lines, and file/function names — code *structure*, not data, and the least likely field to
carry sensitive content; it is also the most useful for triage, so it is **always included**. The
`message`/`exception_message` are developer-authored and are the point of the log (they cannot be
stripped without gutting the tool). `extra_data`, by contrast, is arbitrary JSON attached at log
time (request bodies, tokens, whole objects) — the field most likely to leak sensitive or bulky
content — so it is omitted unless `include_extra_data=True`.

`include_extra_data` defaults to **`False` globally**, not per-profile. Per-profile argument
defaults are not enforceable: the policy matcher fails on *missing* keys, so a deny rule on
`include_extra_data: true` cannot catch a call that simply omits the argument. A global-safe default
makes the passive behavior the safe one; the `engineer` profile — an interactive, human-supervised
context — passes `include_extra_data=True` explicitly when needed.

The recency window is bounded for the same reason: `since_hours` defaults to 168 (7 days), is capped
at 720 (30 days — the error-log retention default), and rejects non-positive or non-integer values,
so an unattended profile cannot widen its diagnostics surface by simply omitting the argument (which
no deny rule could catch). The recommended triage window is `since_hours=24, limit=200`.

## Design Part 4: Escalation Beyond the Note

The confined note is the unattended sink. Two escalation paths exist beyond it, with different trust
requirements.

### Unattended Escalation to a Quarantined Destination

Per-action human confirmation for routine ticket filing is a significant usability downgrade and
trains rubber-stamping. Instead, unattended filing is acceptable when the **destination is
quarantined**: deployment-configured, inside the deployment's trust boundary — for the reference
deployment, an internal Vikunja instance — and readable only by the operators who are entitled to
the diagnostics anyway. One rule is non-negotiable regardless of destination: **no automation may
act on tickets authored by the diagnostics automation**. An issue tracker that feeds coding bots
turns a poisoned ticket into an injection relay with write access; that edge must not exist.

With a private, human-read destination, the two residual failure modes are ticket *content* skewed
by injected log text and ticket *volume* under a log storm — the same risks already accepted for the
confined note, just in a second quarantined store. So filing can be a plain tool call; no extra
machinery is required initially.

If a deployment ever points escalation at a shared tracker, or volume proves noisy in practice, the
hardening ladder is known and can be added incrementally: templated server-side ticket bodies built
from referenced error-log rows (LLM chooses *which* errors, not the outbound text, with the summary
in one fenced untrusted field), fingerprint-based dedupe so recurring errors update one ticket, and
daily rate caps. None of these are prerequisites for the internal-tracker deployment.

A fixed-template push notification on critical severity ("N new critical diagnostics, see note X")
may likewise run unattended.

The concrete tracker tool is out of scope for this design (deployment-specific backend).

### Human-Gated Escalation

Anything that genuinely leaves the quarantine — free-form outbound messages, filing into a shared
destination without the hardening above — is set to `decision: "confirm"` in the profile policy.
Confirm-gated tool calls inside automation scripts already produce **durable confirmations addressed
to `created_by_user_id`** (the automation owner); the tool does not run until the owner approves.
The confirmation itself doubles as the notification. No new mechanism is needed for this path.

## Setup Path

No new machinery is needed for a human to set this up:

1. The user (from a trusted interactive profile) delegates:
   `delegate_to_service(target_service_id="ops_automation", user_request="set up daily log triage…")`.
   Existing delegation policy rules gate this per target (deny/confirm/allow), so the human approval
   boundary — "may work be handed to this profile?" — is already enforced where it belongs.
2. The `ops_automation` profile creates its own schedule automation with its own `create_automation`
   tool. Provenance stamps `processing_profile_id` and `created_by_user_id` from its exec context;
   the script validates against its own tools provider; runtime resolution already fails loudly if
   the stored profile disappears.

## Update Hardening

Today, any `action_config` change via `update_automation` re-stamps `processing_profile_id` and
`created_by_user_id` to the *updating* profile. That means an edit from the default trusted profile
silently moves the automation — and whatever script was submitted — to full-trust execution.

New rule: when the updater's profile differs from the automation's stored `processing_profile_id`,
the tool-path `update_automation` **denies with a clear error** directing the caller to delegate to
the owning profile instead. The web admin API may stay permissive, consistent with the notes API
exception. This preserves the invariant that an automation's execution profile only changes
deliberately.

## Future Work: Target Execution Profiles

An earlier revision of this design proposed `execution_profile_id` on
`create_automation`/`update_automation`, letting one profile author an automation that runs as
another, gated by the same policy decision as `delegate_to_service(target_service_id=...)`. The
delegation-based setup path above covers the current need, so this is deferred. Prerequisites
discovered during review, for whenever it is picked up:

- **Confirmation rendering for inline scripts.** There is no attachment-backed confirmation
  mechanism today; rendered confirmation values are truncated (1,200 chars) and oversized
  confirm-gated delegations are refused outright (3,000-char cap). Approving a script the approver
  cannot fully read is exactly the misleading-approval risk this design warns about, so
  cross-profile automation approval needs either attachment-backed rendering or a web-UI-complete
  flow first.
- **The second delegation gate.** Besides the policy engine, `delegate_to_service` honors the target
  profile's `allowed_delegation_sources`. A delegation-equivalent check that consults only the
  policy engine would let automation creation bypass that gate.
- **Provenance of the creating profile.** Reusing `processing_profile_id` as the target profile
  loses the record of which profile authored the automation; audit likely wants a separate
  `created_by_profile_id`.
- **The update re-stamp collision.** The current "re-stamp on action_config change" logic must be
  removed/replaced before the column can mean "target execution profile" (partially addressed by
  Update Hardening above).
- `wake_llm` now honors a threaded execution profile (see "wake_llm Profile Routing"), so
  cross-profile execution is no longer blocked on that; `event_handler`'s own tool set (F1) is a
  separate follow-up.

## Relationship to the Provenance/Taint Roadmap

This design implements confinement **statically**: each confined profile declares its
required/allowed labels by hand, and reader-side quarantine is a manual grant decision. That is the
right V1, but it should be read as the manual precursor of a more general mechanism — automatic
provenance propagation, where artifacts (notes, tickets, automations) inherit labels derived from
the provenance/taint of the turn context that wrote them, and profiles shrink to declarations of
label *policy*: which labels they may read, and which they must stamp on writes.

`NoteWritePolicy` is deliberately the chokepoint that propagation will attach to. When turn-level
provenance state exists, the policy derivation moves from static profile config to computed turn
taint, with no repository or call-site changes required.

Related dormant substrate: tools already carry `OUTPUT_UNTRUSTED` tags (`tools/metadata.py`) with no
runtime consumer. A turn-level taint state that consumes them — escalating high-bandwidth,
attacker-addressable egress sinks to confirm when untrusted output is in context — is the read-side
counterpart of this design's write-side confinement.

The acceptance test for that future work: the next confined ambient agent (for example, a mailbox
digester) must cost only a YAML profile stanza. If it requires new Python beyond a data connector,
the propagation layer is not finished.

## Residual Risks (Accepted)

- The web notes and automations APIs remain unfiltered admin surfaces; `UNCONSTRAINED` is explicit
  and conformance-checked but still a bypass.
- A future note writer that passes `UNCONSTRAINED` inappropriately reopens the gap; the ast-grep
  rule and review are the backstop, not the type system.
- The triage note's *content* is untrusted by construction. Confinement guarantees it stays
  quarantined, not that it is true; severity labels and summaries can be skewed by injected log
  content. The same applies to tickets filed in the internal tracker.
- Ticket filing is initially unconstrained in content and volume; a log storm or runaway prompt can
  file noisy or duplicate tickets. Accepted while the destination is private and human-read; the
  hardening ladder in Part 4 is the remedy if it bites.
- Escalation destination quarantine (private, and no bots acting on filed tickets) is a deployment
  configuration property, not enforceable from this codebase. Artifact-level provenance propagation
  (see "Relationship to the Provenance/Taint Roadmap" above) is what would make the "no bots act on
  these tickets" rule enforceable in code — a ticket stamped with `ops_automation` provenance can be
  refused as input to any automation.

## Recommended Rollout

### Phase 1: Repository-Level Note Write Policy

- Add `required_note_visibility_labels` / `allowed_note_visibility_labels` to `ProcessingConfig`.
- Introduce `NoteWritePolicy` with the `UNCONSTRAINED` sentinel; make it a required parameter of
  `NotesRepository.add_or_update` and `rename_and_update`; move the see-before-overwrite check down
  into the repository.
- Update all callers: tools derive the policy from `ToolExecutionContext`; web API passes
  `UNCONSTRAINED` with justification.
- Thread the new fields through `ToolExecutionContext`, the script-execution context rebuild, and
  deferred-confirmation execution.
- Add the ast-grep conformance rule for `UNCONSTRAINED`.
- Update confirmation rendering to show effective labels.
- Tests:
  - required labels are added when omitted and when `visibility_labels=[]`,
  - allowed-label ceiling rejects unexpected labels,
  - existing unrestricted profiles keep current behavior,
  - the workspace-file import path honors the write policy,
  - see-before-overwrite is enforced at the repository for a bypass-path caller,
  - automation scripts inherit the creating profile's write policy,
  - deferred confirm-then-run execution inherits the write policy.

### Phase 2: Maintenance Profile, Log Window, wake_llm Guard

- Add `since_hours` to `read_error_logs`; gate the freeform `extra_data` field behind
  `include_extra_data` (default `False`; engineer passes `True` explicitly). Tracebacks stay
  included.
- Add the `allow_wake_llm` capability and route `wake_llm` under the intended profile (see wake_llm
  sections above).
- Add the `ops_automation` example profile (script-only policy) to docs/config examples.
- Create the daily issue-triage automation (sharded `llm_json` script) under that profile via the
  delegation setup path.
- Add the cross-profile `update_automation` denial.
- Tests:
  - `create_automation(action_type="wake_llm")` is denied for the profile by policy,
  - the `allow_wake_llm` guard fails loudly for a confined profile (create/execute/script paths),
  - event-listener wakes route to `event_handler`; scheduled wakes carry the originating profile,
  - cross-profile tool-path updates are denied,
  - `read_error_logs` respects `since_hours` and omits `extra_data` by default.

### Phase 3: Escalation Tool (When a Deployment Needs It)

- Add a ticket-filing tool backed by a deployment-configured internal tracker (e.g. Vikunja),
  allowed unattended for `ops_automation` when the destination is configured; confirm-gated or
  absent otherwise.
- Add the fixed-template critical-severity notification.
- Hardening (templated server-side bodies, fingerprint dedupe, rate caps) is deferred until a
  deployment needs a shared destination or volume misbehaves; see Part 4.

## Recommendation

Phases 1 and 2 fully deliver the stated goal — crawl logs on a schedule, triage with sharded LLM
judgment, persist to a quarantined note, escalate critical findings to a human — using existing
provenance, policy, and confirmation machinery. The only genuinely new enforcement code is the
repository-level write policy, which is also the single biggest risk reduction: it turns label
confinement from a property of one tool into a property of the storage layer.
