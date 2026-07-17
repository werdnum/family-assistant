# Saved Browser Sessions: Family Assistant Adapter — Implementation Scoping

## Status

Scoping. This document turns milestones 5–7 of [browser-cookie-jars.md](browser-cookie-jars.md) into
a concrete work breakdown, written against the browser-server mechanism **as actually shipped**
(browser-server `main` @ `fd2289b`, PR #27; mechanism doc `cookie-jar-design.md` in that repo). The
design doc was written against a proposed API; several assumptions no longer hold, and the deltas
change the shape of the FA work — mostly by *shrinking* it. Policy decisions from the design doc are
not re-litigated here; where the shipped mechanism makes a designed control moot or impossible, that
is called out explicitly.

## What shipped (browser-server), in one paragraph

Jars are AEAD-encrypted at rest (`BROWSER_JAR_KEY`, fail-closed 503 without it), save/load/probe/
invalidate/delete are audited, no cookie material or probe internals appear in any API response or
log, load happens only at session creation, `exec` is default-deny in jar-loaded sessions, and
exact-origin navigation confinement (documents/forms in all frames, redirects aborted pre-request,
off-scope popups closed, service workers blocked) is implemented and on-by-default for jar-loaded
agent sessions. Delete and invalidate terminate live sessions seeded from (or producing) the jar,
cross-pod, including a noVNC watchdog. The design doc's two acceptance prerequisites for enabling
`load_saved_session` — origin confinement and `exec` default-deny — **are both in place**.

## Reconciliation: shipped API vs. design-doc assumptions

### Deltas that shrink FA scope

1. **Agent saves cannot widen scope — the elevated off-site-origin confirmation is moot for the tool
   path.** This is an *effective-scope guarantee*, not input rejection: `SaveJarRequest` accepts
   `origins`, `nav_allowlist`, probe `url`, and `logged_out_url_prefix` from any caller, but for a
   service-token (agent) save the registry overrides or discards them. For a **new** agent save the
   captured scope is the live page's origin only and `nav_allowlist` is dropped. For an agent
   **refresh** with omitted `origins`, `resolve_refresh_scope()` preserves the existing jar's
   (possibly multi-origin) scope and stored allowlist — a refresh does *not* collapse the jar to one
   origin; caller-supplied `origins` can only narrow, and a widening set is **rejected**
   (`JarValidationError` ⇒ 400), while agent-supplied `nav_allowlist` and probe overrides are
   silently discarded rather than rejected. On either path the probe target is derived server-side
   (`origin + "/"`), and an agent-supplied `logged_in_selector` is kept only if it passes a
   server-side logged-out discrimination check (otherwise silently dropped — the jar reads
   `uncertain`, never fake-`fresh`). So don't build 4xx handling or tests around these fields being
   rejected; FA simply never sends them (test-asserted omission). The design doc's FA-wrapper
   machinery for "enumerate every captured origin, deny-by-default on off-site origins" has nothing
   to gate on the agent path — widening inputs are inert there. `save_browser_session` simplifies to
   `(label, logged_in_selector, jar_id=None)`: no `origins` parameter at all. Multi-origin scope
   (SSO IdP origins, `nav_allowlist` entries) can only enter via the **human** save path — but note
   the shipped handover form cannot express it yet either (gap #13). FA keeps a plain confirm on the
   tool (it still persists a credential-equivalent), but the elevated/deny origin-set logic is
   deleted from scope.
2. **Probe-target validation is server-side.** Sanitization (query/fragment stripping), rejection of
   action-like and token-shaped URLs, in-scope constraint, and the authenticated-only-indicator
   discrimination check all happen in browser-server (`build_probe`, `_selector_discriminates`). FA
   does not re-implement any of it.
3. **Kill-switch semantics are server-side.** Forget/invalidate terminating live sessions is
   enforced by browser-server; FA's `forget_saved_session` is a thin DELETE + confirm.

### Deltas that change how FA code must be written

04. **The rebinding fingerprint is `generation`, not `version`, and there is no server-side
    precondition.** `CookieJarMeta.generation` (monotonic, AAD-bound, tombstone-referenced) is the
    authoritative identity fingerprint; `version` is a display counter. Neither
    `CreateSessionRequest` nor `DELETE /v1/jars/{id}` accepts an expected-generation precondition,
    so the design doc's "abort if the jar changed since approval" must be done FA-side:
    - **load**: compare the create response's `jar_generation` + `jar_origins` + `jar_nav_allowlist`
      against the metadata shown at confirmation; on mismatch, close the just-created session and
      error. (The server itself closes the revoked-during-load race via a post-start recheck and
      per-command rechecks; the FA check covers only "refreshed-to-newer-generation between confirm
      and create", which is benign-but-must-be-shown — origins can't have widened, since refresh
      cannot widen scope.)
    - **forget**: re-`GET /v1/jars/{id}` immediately before DELETE and abort on generation mismatch.
      A tiny GET→DELETE race remains; acceptable because delete is recoverable by re-login and the
      jar id (not label) is what was approved.
05. **`invalidated` is `invalidated_at: datetime | null`** (with server-side `annotate_revocation`
    folding in tombstones and session-cookie TTL expiry), and freshness is the triple `has_probe` /
    `last_probe_at` / `last_probe_result` (`fresh|stale|uncertain|error`). `list_saved_sessions`
    renders from these fields.
06. **`confine_navigation` is a caller-controllable opt-out.** `CreateSessionRequest` accepts
    `confine_navigation: bool | None`; `None` means "confine iff jar loaded". FA must never send
    `false`. Tests must assert the field is omitted (or `None`) on every jar-loaded create — this is
    the one place FA could silently disable the load-enablement prerequisite.
07. **Jar-loaded sessions cannot be resumed or handed back.** `allowed_resume` must be `"never"` for
    a handoff from a jar-loaded session, and handover-to-agent is refused entirely. FA's existing
    `browser_request_handoff` must force `allow_resume="never"` when the session is jar-loaded (not
    just default it), and `browser_claim_handback` must surface a clear "start a fresh saved-login
    session instead" error.
08. **Save-time freshness is unverified** (deliberately deferred in browser-server, PR #27 round
    15): a save can capture a logged-out state and still succeed. FA should treat a fresh save as
    unverified — the cheap mitigation is for `save_browser_session` to call
    `POST /v1/jars/{id}/probe` immediately after a successful save (refresh resets probe state, so
    the 15-minute rate limit does not block it) and report the result in the tool output.
09. **DELETE returns 200 + final `CookieJarMeta`**, not 204. Probe returns
    `{"result": ..., "final_origin": ...}` with no request body. Probe rate limit is 15 minutes, 409
    on conflict.
10. **Session-cookie jars expire on a bounded TTL** (`contains_session_cookies=true` ⇒ loadable only
    within `BROWSER_JAR_SESSION_TTL_HOURS`, default 12h, of `session_ttl_anchor`). FA doesn't
    implement anything, but `list_saved_sessions` should surface "needs re-login" from
    `invalidated_at != null` and the prompt guidance should mention that some sites' logins are
    short-lived by nature.

### Gaps to resolve (not blocking the first milestones)

11. **Human-save-in-place *refresh* is not reachable from the shipped handover form.** The built-in
    "Save this login" form posts `{token, label, probe: {}}` with no `jar_id`, so a human re-login
    save always creates a **new** jar — the design doc's re-login flow ("human-first session → human
    logs in → human-save-in-place refresh of the existing jar id") cannot preserve the jar id or its
    approved multi-origin scope today. Options, in preference order: (a) browser-server follow-up:
    let the handoff request carry a `refresh_jar_id` hint the form includes in its POST (server
    already supports human refresh with ownership checks); (b) FA-relayed human save using the
    optional save-authorization gate (`BROWSER_JAR_REQUIRE_SAVE_AUTHORIZATION`), where FA renders
    its own save flow; (c) interim: re-login creates a new jar and FA offers to forget the stale one
    (loses human-approved multi-origin scope for SSO jars — they'd need the human path again).
    browser-server is ours to change, so (a) is the plan: a small, additive browser-server change
    (handoff request carries `refresh_jar_id`; the form includes it in its save POST; existing
    ownership/subset-refresh checks apply unchanged). It gates only the re-login *orchestration*
    milestone (M6), not M1–M5.
12. **No API-exposed audit trail** (`jar-audit.jsonl` is file-only) and jar events arrive on the
    per-session SSE stream (`jar_loaded`, `jar_saved`, `session_closed` with
    `reason: jar_revoked|jar_deleted|jar_invalidated`). FA does not currently consume the SSE
    stream; mid-conversation "your session was revoked" feedback is deferred (the next tool call
    fails cleanly anyway via the per-command revocation recheck).
13. **No consent path exists yet for creating multi-origin/SSO jars.** The shipped handover form
    accepts only a label and posts `{token, label, probe: {}}` — it neither displays nor submits
    `origins`/`nav_allowlist`, so a human save also captures the current origin only, and it does
    not show the human what a save captures. The API supports human saves with multi-origin scope
    and allowlists, but no UI exposes it, so SSO jars (app origin + IdP in scope) cannot be created
    at all today. Resolution is companion change #3 below (extend the handover save form to display
    the captured scope and let the human include additional origins visited during the session /
    allowlist entries). Until it lands, all jars are single-origin; this blocks only the
    multi-origin/SSO use case, not M1–M5.

## Work breakdown

Ordered so each milestone is independently shippable and testable. All tools stay behind a new named
default-off flag; nothing is reachable until an operator flips it.

### M1 — Config + backend jar client

- `config_models.py`: add `saved_sessions_enabled: bool = False` to `BrowserHandoffConfig`
  (`config_models.py:211`).
- `config_loader.py`:
  `EnvVarMapping("BROWSER_SAVED_SESSIONS_ENABLED", "browser_handoff_config.saved_sessions_enabled", bool)`
  alongside the existing `BROWSER_HANDOFF_*` mappings (`config_loader.py:170`).
- `defaults.yaml`: `saved_sessions_enabled: false` in the `browser_handoff_config` block.
- `tools/browser_backend.py`: jar client methods on `RemoteBrowserBackend` —
  `list_jars() -> list[JarMeta]`, `get_jar(jar_id)`,
  `save_jar(label, logged_in_selector, jar_id=None)` (POSTs to the conversation's active session),
  `delete_jar(jar_id)`, `invalidate_jar(jar_id)`, `probe_jar(jar_id)`, and a jar-loaded session
  create path (`_ensure_session` currently hardcodes a plain create body, `browser_backend.py:502`;
  add a variant that sends `jar_id` and never sends `confine_navigation` or `allow_exec`). A
  `JarMeta` dataclass/TypedDict mirrors `CookieJarMeta` fields FA consumes (`jar_id`, `label`,
  `origins`, `nav_allowlist`, `generation`, `saved_by`, `invalidated_at`, `has_probe`,
  `last_probe_at`, `last_probe_result`, `earliest_cookie_expiry`, `contains_session_cookies`).
- Tests: extend `_make_mock_browser_server()` (`tests/unit/tools/test_browser_backend.py:43`) with
  the six jar endpoints, including 409/404/503 error shapes; assert the create body for a jar-loaded
  session contains `jar_id` and omits `confine_navigation`/`allow_exec` (delta #6).

### M2 — Inventory + revocation tools: `list_saved_sessions`, `forget_saved_session`

- New `tools/browser_jars.py` (keeps `browser_dom.py` from growing further): tool definitions +
  implementations, following the `browser_request_handoff` precedent for "errors cleanly when the
  remote backend is inactive" (`HandoffUnavailableError`) and additionally when
  `saved_sessions_enabled` is false.
- **Jar reference resolution helper** (shared by forget/load, M3): exact-match against id and label
  across both namespaces; >1 match ⇒ ambiguous ⇒ generic error with no candidate inventory (design
  doc: no disclosure side channel).
- Registration in `tools/__init__.py` with metadata: `list_saved_sessions` tagged
  `READ_ONLY + SENSITIVE_DATA + OUTPUT_UNTRUSTED` (the tag triple that both raises turn taint on
  planted labels via `derive_tool_result_taint_source()` and classifies as the
  `sensitive_read_broadening` sink); `forget_saved_session` tagged `STATE_CHANGING`.
- Policy in `defaults.yaml`: both confirm-by-default, placed only in `handoff_capable_profiles`
  (same gate as `browser_request_handoff`), never in untrusted-input profiles. `list_saved_sessions`
  keeps its **interim static confirm** until the deployment's taint policy is verified enforcing for
  this sink (M6).
- `forget_saved_session` confirmation binds to the resolved `jar_id` + `generation` and names the
  jar's origins; re-GET before DELETE per delta #4.
- Functional tests: fake browser-server; assert confirmation fires, ambiguous label errors
  generically, no cookie-material-shaped fields ever reach the transcript, tags present on the
  definitions.

### M3 — The chokepoint: `load_saved_session`

- Resolves label/id → confirms with label + `jar_id` + the full reachable set (`origins` ∪
  `nav_allowlist`) → creates the jar-loaded session → post-create rebinding check (delta #4) →
  navigates to an in-scope start page and returns its snapshot. The create response alone is a blank
  page (`about:blank` — `CreateSessionRequest` takes no landing URL), so the tool must explicitly
  `goto` before snapshotting: default to the jar's sole origin's root, and accept an optional
  in-scope `start_url` argument for multi-origin jars (validated against `origins` ∪ `nav_allowlist`
  FA-side; confinement blocks it server-side anyway).
- Registration metadata: `load_saved_session` returns live web content, so it carries the same
  taint-relevant tags as `browser_open` (`BROWSER`, `STATE_CHANGING`, `EXTERNAL_COMM`,
  `OUTPUT_UNTRUSTED`). (`derive_tool_result_taint_source()` already defaults an *untagged* result to
  `UNKNOWN_EXTERNAL` with a warning, so the tag is explicit metadata rather than the only thing
  preventing clean-output treatment — but the explicit tag is required, not the fallback.)
  Test-asserted alongside the M2 tag assertions.
- Conversation-session conflict: `_remote_backends` keys one session per conversation
  (`browser_backend.py:765`), and FA exposes **no** browser-close tool (`close_browser_backend()` is
  not called from production code) — so a bare "close it first" error would strand the conversation
  until the remote session expires. `load_saved_session` therefore **closes-and-recreates under the
  load confirmation**: when a session already exists, the (always required) confirmation
  additionally states that the current browser session will be closed, and on approval the tool
  closes it and creates the jar-loaded one. Never attach a jar to an existing session (the API
  cannot anyway); the point is the user approves the close explicitly rather than it happening
  silently.
- Handoff interplay (delta #7): `browser_request_handoff` forces `allowed_resume="never"` on
  jar-loaded sessions; `browser_claim_handback` returns a clear error for them.
- Functional tests (design-doc acceptance list, adjusted): (a) confirmation fires before any
  jar-loaded session exists; (b) rebinding — a generation bump between confirm and create closes the
  session and errors; (c) existing-session conflict: the confirmation names the close, the old
  session is closed only after approval, and a decline leaves it untouched; (d) the create request
  never carries `confine_navigation: false`; (e) `load_saved_session` unavailable while
  `saved_sessions_enabled` is false even in handoff-capable profiles. (Cross-origin navigation/exec
  refusal is browser-server-enforced and tested there; FA's fake asserts the request shape that
  *selects* that enforcement.)

### M4 — `save_browser_session` (agent path)

- `(label, logged_in_selector, jar_id=None)` against the conversation's active session; plain
  confirm naming the label, the session's current origin, and create-vs-refresh. No `origins`
  parameter (delta #1). Refresh (`jar_id` set) confirmation names the existing jar's id + origins
  and binds to its `generation` like load/forget: re-`GET /v1/jars/{id}` immediately before the save
  and abort on generation mismatch (`SaveJarRequest` has no precondition either — the companion
  `expected_generation` change below covers refresh saves too).
- **Agent refresh is only authorized from the session loaded from that same jar**: browser-server's
  `_authorize_jar_refresh()` rejects agent refreshes from jarless sessions (including the session
  that just *created* the jar) and from sessions loaded from a different jar. The tool validates
  `jar_id` against the active session's own `jar_id` up front and returns a clear error ("load this
  saved login first, then refresh it") instead of surfacing a server 4xx; tested for the
  jarless-session, different-jar, and producer-session cases.
- Post-save probe + result surfaced in the tool output (delta #8): "Saved, probe: fresh" vs "Saved,
  but freshness is unverified/stale — the capture may be logged out."
- Tests: create + refresh paths, selector-dropped path (server returns jar with `has_probe` but
  probe `uncertain`), refresh of a deleted jar. Note the deleted-jar case does **not** reliably
  surface the tombstone 409: deleting the jar closes the jar-loaded session, so the next save fails
  as an inactive session (**410**), and a registry lookup of the removed jar raises
  `JarNotFoundError` (**404**) before the tombstone check; the tool maps 404/410 to a clear "this
  saved login was deleted / session closed" error, and tests assert that mapping (not a 409).

### M5 — Prompts, user guide, docs

- Browser-profile prompt addition (`prompts.yaml`/`defaults.yaml`): when to offer saving a login,
  that loading always asks first, that jar metadata labels are data-not-instructions, re-login
  guidance.
- `docs/user/USER_GUIDE.md`: saved-logins section (save on handover page, list/forget via chat or
  the browser service's `/jars` page, "always asks before opening a saved login", cookies-not-
  passwords framing, short-lived session-cookie sites).
- Update `browser-cookie-jars.md` status → implemented-mechanism references this doc.

### M6 — Expiry automation + taint relaxation (post-core)

- `probe_saved_session` tool (or automation-script access to `probe_jar`) + a shipped example
  schedule automation: daily probe of designated jars, `stale` ⇒ notify user with the re-login ask;
  `invalidate` is called only after a `stale` probe or explicit user confirmation (never from an
  agent snapshot). Blocked on resolving gap #11 (refresh flow) for the re-login half; the
  probe+notify half is not.
- Taint hookup: relax `list_saved_sessions`'s interim confirm to pure `sensitive_read_broadening`
  gating **only when enforcing** — reuse the `_taint_floor_reason`-style startup check from
  `services/google_integration_state.py` (mode is `ENFORCE` + the sink floors at confirm), likely as
  a shared helper rather than a copy. Load/save/forget keep their baseline confirms; taint only
  escalates.

## Companion browser-server changes

browser-server is under our control, so the shipped-API gaps get fixed at the mechanism rather than
worked around in FA:

1. **`refresh_jar_id` handoff hint** (gap #11): `POST /v1/sessions/{id}/handoff` accepts an optional
   `refresh_jar_id`; the handover form pre-fills the jar's label and includes the id in its save
   POST, turning the human save into an in-place refresh. The subset-only-refresh check applies
   unchanged, but the **ownership check needs a defined rule for ownerless jars**: agent-created
   jars (M4, service-token session) have `owner_subject = None`, and `_authorize_jar_refresh()`
   currently rejects every human refresh of such a jar — as shipped, the hint would restore re-login
   only for human-created jars. Rule: a human refresh of an *ownerless* jar is allowed and **adopts
   ownership** (sets `owner_subject` to the refreshing human) — ownerless jars were created by the
   household's own agent, and adopting on first human touch is strictly scope-narrowing-or-equal
   under the subset rule. Restores the designed re-login flow for both jar kinds. Prerequisite for
   M6's re-login half only.
2. **`expected_generation` precondition** (delta #4): optional field on `CreateSessionRequest` (jar
   loads), `DELETE /v1/jars/{id}`, and `SaveJarRequest` when `jar_id` is set (refresh saves mutate
   an approved generation just like delete removes one); mismatch ⇒ 409. Closes the confirm→execute
   races server-side instead of via FA's compare-and-close/re-GET workarounds. FA still shows
   `jar_id` + `generation` + origins at confirmation and passes the generation through. Nice-to-have
   before M3; the FA-side checks in delta #4 and M4 remain the fallback if it lags.
3. **Scope-aware human save form + visited-origin enforcement** (gap #13): two halves, both
   currently missing. *Server*: track the set of origins the session has actually visited on
   `BrowserSession` and validate human-submitted `origins`/`nav_allowlist` against it — today the
   human save path passes `req.origins` straight through (`JarStore._save_locked()` only
   normalizes), so a human save could name arbitrary never-visited origins. *UI*: the handover save
   form displays exactly what a save would capture and lets the human include additional
   visited-during-session origins and allowlist entries. This is the only path that can create
   multi-origin/SSO jars, so it gates that use case; single-origin jars work everywhere without it.

All are small and additive; none touches the human/sanitize guards or the crypto envelope.

## Open questions (none block M1–M4)

1. **`storage: "cookies_only"`**: expose to the agent save tool, or leave it human-path-only?
   Recommendation: leave it out of the tool surface initially.
2. **Sequencing of the browser-server changes above**: land `expected_generation` before M3 (so the
   load/forget confirmations bind server-side from day one), or ship M3 with the FA-side fallback
   and swap in the precondition later? Recommendation: before M3 — it deletes the fallback code path
   rather than deprecating it.

## Out of scope (unchanged from the design doc)

Credential broker; page-JS subresource egress (egress-proxy/CSP milestone); SSE-driven
mid-conversation revocation notices (gap #12); remoting the visual/Computer-Use profile; kube-config
wiring for `BROWSER_JAR_*` (browser-server-side ops, separate repo).
