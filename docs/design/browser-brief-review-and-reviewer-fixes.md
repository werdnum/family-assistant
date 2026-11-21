# Browser Brief Review and Tool-Call Reviewer Fixes

## Status

Accepted (implementing milestones 1–3). Adjusts the confined-profile disclosure exemption of
[risk-adjudicated-taint-enforcement.md](risk-adjudicated-taint-enforcement.md) for browser sessions,
extends the upfront-review model of
[script-execution-review-boundary.md](script-execution-review-boundary.md) to browser delegations,
and updates the action-review prompt and runtime handling from
[tool-call-review-deny-false-positives.md](tool-call-review-deny-false-positives.md).

## Problem

Tool-call review entered `enforce` mode in production on 2026-11-17. Over the first four days of
enforcement (`1,087` model reviews: `1,033` allow, `32` confirm, `21` deny, `1` malformed fallback),
most review volume and friction came from browser sessions holding nothing private and no capability
to act as the household:

- **71% of all reviews (`770` across `34` sessions) were in `browser_profile` and
  `browser_visual_profile`.** Pattern-matching their briefs shows `30` of the `34` sessions (`678`
  reviews, `27` non-allow verdicts, ~30 minutes of added latency) carried either nothing private or
  only trip dates and party composition.
- **The confined-profile exemption (`_is_confined_review_exempt`) tests the wrong property.** Its
  design asks whether private data is in reach, but the implementation turns the exemption off when
  `state.history_high_taint_present` is set or when history precedes the current turn. Because
  `prompt_window_taint` feeds the child's opening brief row through `from_history=True`, any
  delegation from a turn that ran a web search immediately sets `history_high_taint_present` on Turn
  1, and every resumed delegation fails both clauses. The exemption covered only `2` of `35` browser
  sessions after hotel-search workflows began.
- **`browser_profile` is not a closed clean room today.** Unlike the credential and
  authenticated-site browser profiles, `browser_profile` leaves `delegate_to_service` unpinned, and
  local delegations resolve to `user_local` (`allow`, unreviewed). Six browser runs since June
  delegated back to `default_assistant` — including one on 2026-11-10 that asked for and received
  the user's email address, phone number and suburb from household notes, without recording a
  `sensitive_read` because prompt-injected household context only adds taint sources. In addition,
  the script `AttachmentAPI` (`attachment_read`, `attachment_get`, `attachment_list`) resolves
  parent-conversation and user-owned attachments without recording a `sensitive_read`, while
  `read_text_attachment` and `jq_query` record a private read (`107` calls) even when paging the
  browser's own oversized public snapshot.
- **Across all profiles, `23` of the `54` non-allow verdicts named no security harm, and `17`
  browser `confirm`s acted as opaque blocks.** The prompt's opening line, `allow`/`deny`
  definitions, and delegated-run header ask the reviewer to police step-by-step argument fidelity
  (such as check-in dates or derived child ages on an unsubmitted search form) despite the
  `Steps and scope` section. Meanwhile, tainted turns hide the delegated brief as a stub, bare
  policy strings (`fallback_verdict` and `"fallback is confirm"` in `taint_evaluation.reason`) steer
  reasons toward `confirm`, `_ambient_review_context` ships household notes into reviews for
  profiles that never received them, visual `scroll` and `wait` are tagged `state_changing` (`47`
  reviews), and undeliverable background `confirm`s return
  `Action blocked ... deferred execution is unsafe` while resetting the consecutive-denial streak.

## Decision

### 1. Browser profiles as a clean room with one brief review

`browser_profile` and `browser_visual_profile` operate as a clean room with two boundaries: the
**brief review** controls whether household capability enters a session, and **pinned outbound
delegations** control what can leave it.

- **Upfront brief review on delegation.** Every delegation that opens a `browser_profile` or
  `browser_visual_profile` session is reviewed once by `ToolCallReviewer` on a dedicated binary
  schema (`checked` or `unchecked`), regardless of the delegating turn's taint — matching
  `execute_script`, because a browser session reads its untrusted pages after the brief is written.
  The review never blocks the delegation and never prompts a human; timeouts, errors, malformed
  output, exhausted review budgets, and delegations carrying attachments resolve to `checked`.

- **What the brief reviewer judges.** The brief is shown in full inside neutralized data boundaries,
  never as instructions. A session is `checked` when the brief contains anything usable to act as or
  expose a household member:

  - a password, token, key, one-time code, or sign-in/reset link;
  - payment details;
  - a name together with an email address, phone number or street address;
  - a booking, account or membership reference;
  - a household member's full name with a date of birth;
  - text taken from the household's mail, calendar, documents, notes or messages.

  Incidental context — destinations, dates, party size, ages, a suburb, a budget, preferences,
  product names, public addresses, or the user's display name alone — leaves the session
  `unchecked`.

- **Pinned outbound delegations.** `browser_profile`'s `delegate_to_service` policy is pinned to
  `browser_visual_profile`, `credential_browser_profile`, self-delegation, `coder`, `research`, and
  `research_max` (with `engineer` denied rather than reviewed, matching the other confined browser
  profiles). A browser session that needs household contact details finishes and asks its caller;
  when the caller resumes the delegation with those details, the resume brief passes through the
  brief review.

- **What `unchecked` covers.** Only taint-triggered reviews on `BROWSER_TOOL_NAMES` in the two
  disclosure sink classes (`arbitrary_external_message`, `attacker_addressable_egress`) resolve to
  audit (`_record_confined_exemption_audit`). Static policy rules, confirmation/denial floors,
  `execute_script` (`sandbox_network`), `coder` (`sandbox_network`), shopping tools (`ucp_*`), and
  Gemini Computer Use's native `safety_decision` remain enforced. Non-browser confined profiles keep
  `_is_confined_review_exempt` unchanged.

- **In-memory monotonic mode on `browser_scope_key`.** Session mode lives in memory keyed by
  `browser_scope_key`, so a visual hop (`browser_profile -> browser_visual_profile`) shares its
  parent tab's mode without a second brief review, and no database migration is needed on this
  single-replica deployment. Transitions are strictly one-way (`unchecked -> checked`):

  - **Initial vs. resumed delegations:** Only an initial (non-resumed) delegation may initialize a
    scope to `unchecked`. On `resume_delegation_id`, if the scope is not already present in memory
    as `unchecked` (because it was `checked` or because the application restarted while history
    persisted), it stays `checked` without calling the reviewer; if it is `unchecked`, the new brief
    is reviewed (and attachments checked) to decide whether it flips to `checked`.
  - **Handback:** `browser_claim_handback` flips the scope to `checked` and latches a scope-level
    handed-back flag so `_HANDED_BACK_NOTICE` survives across later turns in the session.
  - **Protected sessions:** `credential_browser_*` and `authenticated_browser_*` never receive an
    `unchecked` entry (unknown mode is `checked`).
  - **Private reads:** Any `state.sensitive_reads` entry flips the session to `checked`.

- **Off switch and rollout.** A single boolean on `tool_call_review` forces all browser sessions
  `checked` without a code revert. Enforcement begins immediately once offline replay over stored
  `delegation_runs` briefs passes, followed by an audit review of `unchecked` briefs after real
  traffic accumulates.

### 2. Deterministic reviewer input, tag, attachment, and confirmation fixes

- **Render the delegated brief as non-authoritative evidence.** When a delegated turn's brief is
  externally tainted, render it in a dedicated neutralized boundary tag (separate from
  `<trusted_trigger_definition>`, which claims authority for stored definitions) framed as the
  sub-task handed to the worker: context for its steps, never authority overriding
  `trusted_originating_request`. Normalize synchronous and queued delegations so both supply human-
  readable brief text.
- **Strip policy-plumbing noise from the prompt.** Omit `fallback_verdict` from
  `_render_policy_contexts` and stop copying `taint_evaluation.reason` (which names the fallback and
  raw sink identifier) into `DelegatingPolicyContext.description`. Gate `_ambient_review_context` on
  `service_config.include_aggregated_context` so profiles without ambient household context do not
  ship household notes into the reviewer prompt.
- **Retag no-effect visual actions.** Change `computer_use`'s `scroll` and `wait` from
  `state_changing` to `read_only` in `catalog.py` so `resolve_tool_sink_class` maps them to
  `user_local` alongside `browser_wait` and `take_screenshot`. `move` (which drags when preceded by
  `mouse_down`) and `browser_exec` remain gated.
- **Unify attachment private-read accounting and the own-snapshot carve-out.** Make
  `execute_script`'s `AttachmentAPI` (`attachment_read`, `attachment_get`, `attachment_list`) record
  `sensitive_reads`, keeping artifact `taint_metadata` envelope separation intact. When
  auto-converting an oversized tool output to an attachment, stamp an affirmative eligibility marker
  on the attachment metadata only when the tool is in `BROWSER_TOOL_NAMES` and the session is an
  unprotected, non-handed-back browser session (recording the originating browser scope). Share one
  helper across `read_text_attachment`, `jq_query` / `_record_generic_sensitive_read`, and
  `AttachmentAPI` that skips recording a `sensitive_read` only when that affirmative marker is
  present for the reading session's scope — so historical attachments, user uploads, and snapshots
  from credential, authenticated-site, or handed-back sessions all retain ordinary private-read
  handling.
- **Keep `{allow, confirm, deny}` and fix undeliverable `confirm` at runtime.** Do not remove
  `confirm` or add a fourth verdict where an agent loop awaits the tool result (reserve
  `_without_confirm` for unattended background firings with no waiting agent). When the reviewer
  returns `confirm` and `_review_confirmation_unavailable` is true:
  - do not execute or queue the call;
  - return a plain non-`Action blocked` tool message telling the agent what human decision is needed
    so it can report back to the waiting conversation;
  - record a distinct audit status so undelivered confirms are countable in metrics;
  - neither increment nor reset `consecutive_denials` (and require `not result.used_fallback` before
    resetting `consecutive_denials`, so a timeout fallback to `confirm` does not clear a streak).
    Change `_is_escalatable_review_denial` to count model denials whenever a non-`deny` verdict was
    available (`constraints.available_verdicts - {DENY}` is non-empty).

### 3. Surgical action-review prompt and guidance edits

Edit `_ACTION_REVIEW_SYSTEM_PROMPT`, `_trigger_header`, `_PROGRAM_REVIEW_ADDENDUM`, and `engineer`'s
`review_guidance` while preserving the tuned `confirm` exclusions verbatim:

- **Harm-first opening.** Ask first what harm the call could cause if injected content had chosen
  it: sending household data out, paying or committing, deleting, releasing a credential, granting
  access, changing standing behaviour, messaging as the user, or creating unrequested standing
  external state (an account, accepted terms, a booking). If none of those can occur, return `allow`
  without policing step alignment.
- **Fidelity only on household data sent or commitments.** Update the `allow` and `deny` definitions
  to match `Steps and scope`: values entered into search, filter, or booking forms prior to
  submission or payment are not commitments and are not denied as
  `"a different value than the user stated"`. Do not add
  `"reads more household data than the task needs"` as a deny ground.
- **Delegated-run header.** Replace
  `"the trigger definition and the proposed call must be derivable from it"` in `_trigger_header` so
  ordinary intermediate steps in a delegated run do not contradict `Steps and scope`.
- **Configured inference providers.** State in both `_ACTION_REVIEW_SYSTEM_PROMPT` and
  `_PROGRAM_REVIEW_ADDENDUM` that anything except a credential or secret may be sent to an inference
  provider configured by the operator, and update `engineer.processing_config.review_guidance` so
  diagnostic/eval payloads to configured providers do not conflict with `"never household content"`.

## Deliberate simplifications

- **Same-origin hostile-site actions and anonymous forms.** An `unchecked` session is treated like a
  stranger driving an empty private window: a hostile page can steer clicks or anonymous form
  submissions on that site, and can learn incidental brief details (dates, party size). Only
  boundary crossings carrying household identity, data, or standing capability are checked.
- **No home-network or cluster egress guard in v1.** `browser-server` has no Kubernetes
  `NetworkPolicy` and its primary worker runs on the macOS Tart host. Exempt sessions already have
  this reach today, and local coding agents run on the same VM; a browser-server address guard that
  preserves LAN targets like OctoPrint is deferred.
- **Direct `/browse` and `/browse_visual` stay `checked` in v1.** Top-level browser conversations
  (`subconversation_id is None`) accounted for `0` of the `767` enforced browser reviews and have a
  live human present to answer confirmations.
- **No date injected into the review prompt.** Date-related denials in production came from the
  reviewer policing check-in dates and derived child ages on unsubmitted search forms, which is a
  scope error solved by the harm-first and pre-submission rules rather than by giving the reviewer a
  calendar.
- **No regex floor or secondary classifier in v1.** A wrong `unchecked` verdict on a brief has no
  per-call backstop, matching `execute_script`. Offline replay over all stored `delegation_runs`
  briefs before shipping plus a post-rollout audit review of `unchecked` briefs verify the prompt; a
  narrow classifier (such as Jev) will only be evaluated if either check surfaces misses.
- **Named tightenings over today:**
  - Every browser delegation runs one non-blocking brief review (`~2–3s`).
  - Briefs carrying contact or personal details from a clean turn are now `checked` (previously
    exempt on Turn 1).
  - Delegations with attachments and handed-back sessions stay `checked` for the session's life.
  - `browser_profile` can no longer delegate to `default_assistant` or other household profiles (`6`
    occurrences since June); it asks its caller instead.
  - Model denials in unattended/delegated runs now count toward the 3-consecutive / 20-total
    escalation cap.

## Work plan

Because keeping `confirm` in the verdict space decouples confirm handling from prompt wording, the
work splits into three separately reviewable PRs (Milestone 2 lands after Milestone 1 so attachment
private-read accounting is active before `unchecked` sessions are enabled; Milestone 3 is
independent of both):

1. **Deterministic input, tag, attachment, and confirm fixes.** Retag visual `scroll` and `wait`;
   render tainted delegated briefs in an evidence block; strip `fallback_verdict` and raw policy
   reason text; gate `_ambient_review_context` on `include_aggregated_context`; add `AttachmentAPI`
   private-read accounting and the affirmative own-snapshot eligibility marker + shared reader
   carve-out; update undeliverable-`confirm` handling and denial escalation counting. *Verify:* unit
   tests for each gate, streak/escalation transitions (including fallback and undeliverable
   `confirm`), own-snapshot vs. historical/user/handed-back/protected attachment reads across
   `read_text_attachment`, `jq_query` and `execute_script`, and prefix-stability / prompt-rendering
   tests; update `docs/design/` references that still describe browser page reads as
   `sensitive_data`.
2. **Browser clean room and upfront brief review.** Pin `browser_profile` outbound delegations in
   `defaults.yaml`; implement the binary brief-review schema, prompt, in-memory `browser_scope_key`
   mode (with the fail-closed resume and handback rules), and config off-switch; update
   `docs/user/confirmations-and-safety.md` and `docs/operations/CONFIGURATION_REFERENCE.md`.
   *Verify:* offline run of the brief-review prompt over historical browser `delegation_runs`
   (`request_text`), manually inspecting every brief marked `unchecked` to confirm zero checked-list
   misses; unit/functional tests covering tainted-parent harmless brief -> `unchecked`, identity
   brief -> `checked`, attachments -> `checked`, reviewer error -> `checked`, visual-hop
   inheritance, restart + resume fail-closed -> `checked`, handback -> `checked` + durable notice,
   private attachment read -> `checked`, pinned delegation denial to `default_assistant`, and
   static/floor rules still firing in `unchecked` mode.
3. **Action-review prompt and provider-guidance edits.** Apply the harm-first opening, pre-submit
   form fidelity rule, `_trigger_header` fix, and configured-provider rule in
   `services/tool_call_review.py` and `defaults.yaml`; add cluster-based benign/harmful-twin cases
   from the `54` production non-allows to the eval suite alongside friction metrics (no-harm
   non-allows, undelivered `confirm`s, review-seconds per task). *Verify:* `judge_lab` replay on
   Haiku (and a stronger model if no-harm verdicts persist on Haiku after Milestone 1's input fixes)
   across `manual`, `history`, `heldout`, `fresh2all`, `agentdojo`, and `script_review_eval.py`
   showing no new attack misses (beyond named exceptions) and elimination of the observed no-harm
   false positives.
