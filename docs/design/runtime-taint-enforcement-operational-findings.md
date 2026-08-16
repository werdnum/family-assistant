# Runtime Taint Enforcement Operational Findings

## Status

Proposed operational correction and follow-up design.

## Summary

Runtime taint tracking is collecting useful provenance in production, but enabling the shipped
enforcement matrix today would impose enough false-positive friction that the deployment remains in
`observe` mode. That is not a neutral outcome: a policy too disruptive to enable provides no runtime
protection at all.

The production audit identified one immediate systemic cause. Two notes carrying `unknown_external`
provenance are also marked `include_in_prompt=true`. Prompt context assembly restores their stored
provenance before every turn, so every profile that receives those notes starts at the highest taint
tier even when the current request has no external input. One note was recently re-stamped from the
other, demonstrating a live artifact-to-artifact feedback loop.

The minimal correction is ambient prompt-admission control:

- A stored note at or above `taint_policy.high_taint_tier` must not contribute content, titles, or
  skill-catalog metadata to ambient prompt context unless an authenticated user has explicitly
  reviewed that exact prompt-visible material.
- The note remains stored, searchable, and explicitly retrievable. Explicit retrieval restores its
  provenance normally, so subsequent sinks remain protected.
- Review state is derived from the existing `include_in_prompt` intent and provenance metadata. The
  product does not need a review queue, note revision system, or artifact-wide amnesty.

This correction should be paired with changing interactive `unknown_external -> sandbox_network`
from hard denial to confirmation. When confirmation is unavailable, the existing fail-closed
behavior still denies execution. This makes enforcement deployable without pretending that an
authenticated operator cannot perform the same coding task out of band with less provenance and
auditing.

## Goals

- Make enforcement usable enough to leave observe mode.
- Prevent high-tier stored artifacts, including database-backed skills, from creating an ambient
  injection path or deployment-wide taint floor.
- Preserve durable provenance and explicit-read enforcement.
- Keep user review discoverable without building a workflow subsystem.
- Preserve operator choice for interactive sandbox work while unattended execution fails closed.
- Distinguish measured operational friction from raw tool-call counts.

## Non-goals

- Per-token or sentence-level information-flow tracking.
- Automatic expiration or amnesty for artifact provenance.
- A note revision, diff, notification, or review-queue subsystem.
- Immediate per-origin browser grants or a full sandbox egress proxy.
- Treating all lower-trust content as unsafe for ambient context. The admission rule applies at the
  configured high tier, currently `unknown_external`; `known_contact` and `recognized_machine`
  remain eligible.

## Production evidence

The following snapshot was produced by a read-only engineer-profile audit on 2026-08-16. The raw
table contains `policy_evaluation` and `result_taint` events recorded under observe mode. The audit
re-applied the current deployed matrix, including the production override
`unknown_external.sensitive_read_broadening: audit`, instead of assuming that historical requested
outcomes represented current policy.

| Window  | Would-gate tool calls | Distinct turns | Conversations | Profiles |
| ------- | --------------------: | -------------: | ------------: | -------: |
| 7 days  |                   337 |             52 |            10 |        4 |
| 30 days |                 2,122 |            383 |            51 |        8 |

The call-to-turn compression is material, especially for browser workflows that commonly perform
many navigation, snapshot, click, wait, and extraction calls in one turn. It does **not** prove that
one blanket approval per turn is safe: later calls can carry different code, recipients, URLs, or
destinations. Distinct turns are a lower bound on approval episodes, not an authorization scope.

The same audit found:

- 82 seven-day and 369 thirty-day `sandbox_network` decisions would be hard denials under the
  current default matrix.
- Exactly two regular notes were both high-tier and prompt-included; twelve other high-tier notes
  were not prompt-included and therefore did not raise the ambient *taint state*. Their titles may
  still have appeared in the excluded-note catalog, which is a separate ambient-content gap.
- `known_contact` and `recognized_machine` did not occur in the audit data. The production email
  sender allowlists for those tiers are empty, so the middle tiers are not calibrated by live data.
- Argument summaries intentionally retain keys and types rather than values. The audit cannot
  estimate per-origin, recipient, or destination approval reuse from existing records.

These figures come from a privileged raw-event analysis and cannot be reproduced from the aggregate
diagnostics endpoint alone. Future operational decisions should preserve the analysis query or add
the non-sensitive rollups described below.

## Corrected interpretation of MCP metadata

An initial audit attributed most tool-output taint to an active MCP metadata-resolution defect. That
causal claim was wrong.

Production config intentionally marks `brave_web_search`, `execute_python`, and `execute_shell` as
`output_untrusted`. Both `output_untrusted` and the conservative `output_unspecified` fallback raise
the result tier to `unknown_external`. Correcting those output tags would not have prevented the two
notes from inheriting high-tier provenance.

There was a historical sink-resolution cutover for Brave and code execution: missing configured tags
caused calls to fall back to `arbitrary_external_message`, while the configured tags resolve them to
`low_bandwidth_external` and `sandbox_network`. The audit places the last wrong Brave event on
2026-07-17 and the first correct event later that day; code execution similarly cut over by
2026-07-20. This is no longer the dominant current friction source.

Trino metadata calls may still have a low-volume sink-resolution defect. The deployment config gives
the server wildcard `read_only` and `output_untrusted` tags, which should resolve closed-world reads
to `sensitive_read_broadening`, but twelve observed calls fell back to `arbitrary_external_message`,
most recently on 2026-08-04. That warrants a focused correctness investigation, but it does not
explain artifact poisoning because the result remains intentionally untrusted either way.

## Issue 1: an unusable policy remains unenforced

Observe mode downgrades gating outcomes to audit, so the current deployment has provenance and
counterfactual logs but no runtime blocking or confirmation. The relevant comparison is therefore
not maximum strictness versus a weaker enabled policy. It is a usable enabled policy versus no
enforcement.

Confirmation fatigue is itself a security failure mode: it trains indiscriminate approval and makes
operators likely to revert enforcement. Operational readiness must be evaluated in user-visible
approval episodes and failed tasks, not only matrix-cell strictness.

## Issue 2: hard sandbox denial removes an in-band safer choice

The original design maps `unknown_external -> sandbox_network` to `deny` because arbitrary code can
choose destinations dynamically and a generic per-command prompt may not convey the real egress.
That is a sound default for unattended, unmediated network execution.

It is too broad as the only interactive outcome:

- An authenticated operator can copy the same request into a coding agent manually.
- The out-of-band path may lose Family Assistant's provenance, audit trail, scoped credentials, and
  result-taint propagation.
- Brokered HTTP is currently classified in the same cell as arbitrary sandbox networking even when
  the broker can constrain destination, method, credential, use count, and lifetime.

The near-term policy should request confirmation for interactive high-tier sandbox calls. A missing
confirmation callback or an unattended profile still fails closed. Restricted profiles may
strengthen the deployment-level confirmation outcome to denial.

This requires changing both the matrix cell and the deployment's `operator_minimum`. The original
machinery design presents `unknown_external.sandbox_network: deny` as an unrelaxable example, and
the evaluator clamps a weaker matrix outcome back to that minimum. Changing only the matrix would
therefore appear to succeed in configuration while remaining a denial at runtime. The original
design document and configuration guidance must be updated with the policy change.

A later design should split arbitrary network execution from brokered, capability-scoped egress.
That refinement should not block enabling the rest of enforcement.

## Issue 3: stored notes have multiple ambient injection paths

The current behavior follows from two individually intentional mechanisms:

1. Note writes persist the current turn's maximum taint and source summaries as artifact provenance.
2. The notes context provider restores provenance from every prompt-included note before the turn
   executes.

Together they create disproportionate behavior. An unrelated web search earlier in a note-writing
turn can make an evergreen preference note high-tier forever. If that note is prompt-included, every
future turn begins high-tier. Editing another note in that ambient context copies the first note's
taint into the second, creating a feedback loop.

This is not a legacy-message problem and should not use `history_taint_epoch`. The stored provenance
is accurate about the writing context; the mistake is treating high-tier artifact content as
eligible for unconditional ambient injection.

The existing amnesty design also contains a misleading statement: it says `get_note` and
`list_notes` are `OUTPUT_TRUSTED` and do not re-taint turns. Their static tool output does not add a
new generic source, but explicit note reads restore artifact provenance, and prompt-note context
assembly restores it before tool execution. Documentation must distinguish those behaviors.

Prompt-included regular notes are not the only ambient path:

- The prompt lists titles of regular notes excluded from full inclusion. A high-tier title can
  therefore reach every prompt without contributing corresponding provenance.
- A note becomes a database-backed skill when its frontmatter declares skill metadata. Every such
  skill's name and description is included in the ambient skill catalog, regardless of
  `include_in_prompt`, while skill provenance is not restored by the current context-taint path. A
  tainted turn can therefore create attacker-influenced ambient skill metadata that appears trusted.

The admission invariant must cover every database-derived string placed into ambient context, not
only the body of `include_in_prompt` regular notes. File-based skills are outside this artifact
provenance rule because they are deployment-controlled files rather than database artifacts.

## Minimal prompt-admission design

### Effective status

Derive a regular note's user-visible prompt status without adding a database column:

| Stored intent and provenance                                          | Effective status   |
| --------------------------------------------------------------------- | ------------------ |
| `include_in_prompt=false`                                             | `excluded`         |
| `include_in_prompt=true`, tier below `high_taint_tier`                | `included`         |
| `include_in_prompt=true`, high-tier, no valid user review             | `blocked_by_taint` |
| `include_in_prompt=true`, high-tier, valid review for current content | `included`         |

`include_in_prompt` remains the user's intent. Admission is computed separately so the system does
not silently forget that the user wanted the note available as evergreen context.

Database-backed skills have an equivalent derived catalog status: `included` when below the high
tier or covered by a valid review, and `blocked_by_taint` otherwise. Their catalog intent is
implicit because database-backed skills are currently always advertised.

### Context assembly

The notes context provider must omit all prompt-visible material from `blocked_by_taint` artifacts:

- regular-note title and content from the included-notes section;
- blocked regular-note titles from the excluded-notes section; and
- database-skill name and description from the skill catalog.

Admission must be computed once from a single repository snapshot used for both prompt fragments and
context taint. The current separate `get_prompt_notes` reads can otherwise observe different
versions of a note. Malformed provenance must fail conservatively as high-tier rather than being
silently skipped. These read-time invariants cover existing rows, repository writes, imports, and
future alternate write paths.

The note remains available to `get_note`, search, and the Notes UI. Explicit retrieval merges the
stored provenance into the active turn exactly as it does today. The model therefore encounters
untrusted content only after choosing an explicit read, and downstream egress remains gated.

Prompt metadata may expose only a count such as "2 notes are blocked pending review"; it must not
inject their titles, skill metadata, or content. The authenticated Notes UI is the primary discovery
surface. A chat request to enumerate blocked artifacts is an explicit read and should restore their
provenance normally; a status-only tool response must not smuggle attacker-controlled titles through
an untainted channel.

### Write feedback and discoverability

When a write leaves an ambient-intended note or database skill blocked, the tool result must say so
directly. The message must distinguish current-turn taint from preserved stored provenance. For
example:

> Note saved, but it is not included in ambient context because this turn contains untrusted
> external content. Ask to review and trust the note before including it.

or:

> Note saved, but its earlier untrusted provenance is still present, so the changed content needs
> review before it can return to ambient context.

`list_notes` and note-detail responses should expose the derived prompt or skill-catalog status.
This supports the ordinary question "Which notes need my review?" without a new review queue,
notification system, or background scheduler. Because `list_notes` returns artifact-controlled
metadata and currently restores listed-note provenance, using it for discovery can legitimately
raise the active turn's tier; the Notes UI avoids contaminating an unrelated chat turn.

Only the transition of an ambient-intended note to `blocked_by_taint` needs prominent feedback.
High-tier notes that were deliberately created with `include_in_prompt=false` remain ordinary
on-demand notes and do not create review noise.

### Explicit review

Provide one authenticated operation equivalent to `trust_note_for_prompt(note_id)`. It must require
a real user decision; the model cannot approve content on its own. The review surface should be the
authenticated Notes UI, where the user can inspect the complete artifact in trusted application
chrome. A chat or push confirmation must not render attacker-controlled note content as if it were
part of the application's own instruction.

The review can be stored inside `provenance_metadata_json` and bound to a canonical hash of every
prompt-visible field: title and content for a regular note, or skill name and description for a
database-backed skill. If attachments or other fields later become ambient, they join the hash.
Original source provenance remains available for audit. Prompt admission treats a matching review as
the user's attestation that the exact material is safe for ambient use. Any prompt-visible change
invalidates the review automatically. An authenticated Notes UI save may combine editing and review
when the user explicitly selects ambient inclusion for the resulting content.

The attestation is repository-managed security metadata. No model-influenced write path, import, or
generic provenance mapping may set or preserve a review for changed content. The repository must
compute and validate the hash and authenticated reviewer identity rather than trusting caller-
supplied review fields.

This design deliberately does not retain an earlier prompt-approved revision while a new revision
awaits review. Temporarily omitting the note is correct. Because clean-turn writes currently
preserve earlier stored provenance, frequently edited evergreen notes may need repeated review; the
UI-save shortcut above reduces that friction without adding revision management.

### Policy ownership

Ambient admission uses the effective taint policy, but a profile must not be able to weaken the
deployment threshold. `high_taint_tier` currently merges without the tighten-only validation used by
other security-sensitive policy fields. Either admission must use the deployment-level threshold
directly or profile merging must enforce that a profile can only lower the threshold toward more
trusted tiers, thereby blocking more artifacts.

## Issue 4: confirmation counts lack a safe reuse scope

Raw gates overstate interruptions, but `(turn_id, sink_class)` is not sufficient authorization. One
browser turn can visit several origins, and one sandbox turn can run materially different code after
receiving new external input.

Confirmation reuse should eventually bind to an enforceable capability, for example:

- exact tool and argument fingerprint;
- browser origin;
- external destination or recipient;
- brokered credential capability and method;
- an explicit user-approved task-wide sandbox grant.

Concurrent calls requesting the same capability should coalesce into one pending confirmation.
Broader grants are a product choice that the confirmation UI must state plainly, not an implicit
cache optimization.

This capability-scoped reuse is useful but is not a prerequisite for the prompt-admission fix. A
blanket turn-scoped cache must not be introduced as a supposedly lossless deduplication.

## Issue 5: some entry points cannot confirm

Some API and voice execution contexts still construct tool execution with no confirmation callback.
Under enforcement, a requested confirmation correctly becomes a denial there, but diagnostics do not
record confirmation availability. Profile descriptions and code inspection are currently needed to
infer user-visible failures.

The same gap affects delegated runs whose originating interface has no live confirmation manager and
no deferred fallback. In addition, static-policy confirmation can remove unavailable tools from
advertisement, while taint confirmation is evaluated only at dispatch. A no-channel context may
therefore advertise a tool that is guaranteed to fail when called.

The enforcement rollout must enumerate interactive, deferred, unattended, API, and voice entry
points. Each must either provide a durable owner-addressed confirmation path or explicitly document
that confirm-gated calls fail closed. Where possible, guaranteed failures should be reflected in
tool advertisement rather than discovered only after the model attempts a task.

## Issue 6: confirmation layers can prompt twice

Static tool policy and runtime taint policy authorize sequentially. If both independently require
confirmation for the same call, changing sandbox taint from denial to confirmation can produce two
prompts. Existing design intent calls for one merged confirmation payload, but the implementation
does not yet provide it.

The rollout must measure and eliminate duplicate prompts. A single user decision may satisfy both
layers only when its payload clearly presents both reasons and the grant scope is no broader than
either layer would allow independently.

## Issue 7: source tiers are not calibrated

The live audit contained only `trusted_user` and `unknown_external`. Empty known-contact and
recognized-machine sender configuration means the graduated matrix is not reducing friction for
authenticated family, school, receipt, and notification sources as designed.

Source-tier configuration should be populated only from authenticated connector evidence. It is a
separate operational tuning task, not a reason to weaken unknown external handling.

## Observability gaps

The next audit should add privacy-preserving fields or rollups for:

- distinct turns with at least one requested gate;
- `can_confirm` at evaluation time;
- a stable hashed destination/origin/recipient fingerprint where the sink resolver has such a value;
- the resolved descriptor tags and resolution source (exact config, wildcard config, MCP
  annotations, or fallback);
- prompt-admission status counts, especially `blocked_by_taint`;
- high-tier artifact writes and transitions into or out of prompt eligibility;
- confirmation reason layers, so duplicate static-policy and taint prompts can be detected;
- count of no-channel tools advertised and subsequently denied; and
- provenance source on resumed turns, to distinguish current ambient artifacts from persisted
  message-history state.

Raw URLs, recipients, arguments, note content, and source identifiers must remain absent from the
aggregate diagnostics surface.

## Rollout sequence

1. Add one read-time ambient-admission snapshot covering included regular notes, excluded-note
   titles, database-skill catalog entries, derived status, and context taint.
2. Apply the same rule at note and skill write-feedback boundaries and add explicit content-bound
   review in the authenticated Notes UI.
3. Correct the amnesty documentation and user-facing note guidance.
4. Let the two currently affected notes become `blocked_by_taint`; review them individually rather
   than deleting their provenance.
5. Change both the deployment matrix and any `operator_minimum` clamp for high-tier sandbox access
   from denial to confirmation. Update the runtime machinery design, whose example currently calls
   the sandbox denial unrelaxable. Keep no-confirmation contexts fail-closed and allow restricted
   profiles to strengthen the outcome. Merge duplicate static-policy and taint confirmations before
   enabling profiles where both layers gate the same tools.
6. Investigate the Trino descriptor mismatch separately.
7. Re-run the operational audit after representative traffic no longer starts from an ambient
   high-tier floor. Existing post-epoch conversation history has already persisted note-derived
   high-tier snapshots, so resumed threads remain tainted until their configured history windows
   roll over; verify provenance sources rather than assuming the floor disappears immediately.
   Compare tool-call gates, distinct gated turns, duplicate prompts, failed no-channel calls, and
   blocked prompt artifacts.
8. Enable enforcement when the corrected data shows tolerable approval episodes, then pursue
   capability-scoped confirmation reuse and brokered-network sink refinement based on actual
   remaining friction.

## Acceptance criteria

- A high-tier database artifact without a valid review contributes no title, content, skill name,
  skill description, or taint to initial turn context.
- The same note remains explicitly discoverable and retrievable; retrieval restores its provenance.
- A note or skill write that becomes blocked tells the user whether current or stored provenance is
  responsible.
- `list_notes` and the Notes UI distinguish `included`, `excluded`, and `blocked_by_taint`.
- An authenticated review admits only the canonical prompt-material hash; modifying any covered
  field invalidates it, and model-influenced paths cannot create the attestation.
- Lower tiers remain ambient-eligible unless operator policy says otherwise.
- Profiles cannot relax the deployment's ambient-admission threshold.
- Interactive high-tier sandbox use can request confirmation; contexts without a confirmation path
  fail closed.
- The effective sandbox outcome is not silently strengthened by `operator_minimum`, and one call
  does not produce separate static-policy and taint confirmations.
- No blanket per-turn approval cache authorizes calls with materially different capabilities.
- Diagnostics can report turn-level friction and prompt-admission counts without exposing private
  content or destinations.
