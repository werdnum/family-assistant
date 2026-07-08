# Browser Cookie Jars: Family Assistant Integration

## Status

Proposed. Companion to `cookie-jar-design.md` in the
[browser-server](https://github.com/werdnum/browser-server) repository, which specifies the
mechanism (encrypted storage, save/load/probe API, audit events). This document specifies the
Family Assistant side: tools, policy gating, and taint integration.

Origin: [project-assessment-2026-07](project-assessment-2026-07.md), capability roadmap milestone 4
("persistent per-origin browser contexts, human-performed login via the existing warm-session
handoff, and session-expiry detection"), resequenced from PR #833. The credential broker remains
deferred; cookie jars capture post-login session state only, never credentials.

## Division of responsibility

browser-server enforces mechanism invariants regardless of what this repo does: jar contents are
never model-visible (metadata only, everywhere), jars are encrypted at rest and fail closed without
a key, load happens only at session creation so a session's authenticated origins are immutable and
truthfully reported, `exec` is denied in jar-loaded sessions unless explicitly enabled, and every
operation is audited.

Family Assistant owns **all policy**: which profiles see the tools, which operations require
confirmation, how authenticated sessions interact with taint, and when to probe/re-login. Adding or
tightening policy here must require zero browser-server changes — that is the acceptance test for
the mechanism design.

## Tool surface

Four new tools in the browser tool family, all thin wrappers over the `RemoteBrowserBackend`
client (they are meaningless without `browser_handoff_config.enabled`, and error cleanly when the
remote backend is not active, like `request_browser_handoff`):

- `list_saved_sessions()` → renders `GET /v1/jars` metadata: label, origins, saved-by, freshness
  (last probe result, earliest cookie expiry, invalidated flag). Safe to show the model verbatim —
  the service guarantees no cookie names/values appear in metadata.
- `save_browser_session(label, origins=None, probe=None)` → `POST
  /v1/sessions/{id}/save-jar` against the conversation's active session. Also used with an existing
  jar's label/id to refresh after re-login. Default scope is the current origin (the service's
  minimization default keeps IdP cookies out; capturing an IdP origin requires listing it
  explicitly, which policy should treat as a bigger ask).
- `load_saved_session(jar_label_or_id)` → creates the conversation's browser session with
  `jar_id` set (and `allow_exec` false). This is **the** policy chokepoint: after this call the
  session acts as the logged-in user at the jar's origins.
- `forget_saved_session(jar_label_or_id)` → `DELETE /v1/jars/{id}`.

Supporting behavior (not separate tools): when the agent hits a login wall in a jar-loaded session
it calls the service `invalidate` endpoint via the backend and proposes the re-login flow
(handoff with reason `credentials`, then `save_browser_session` refresh).

## Policy gating

Via the existing tool-policy engine and durable confirmations — no new enforcement machinery:

- `load_saved_session`: **confirm** by default in every profile that has it. The confirmation
  message names the jar label and origins ("Open a browser logged in to woolworths.com.au as
  Andrew?"). Durable confirmations make this work across interfaces and background turns.
- `save_browser_session`: confirm by default. A human-initiated save (the checkbox on the
  browser-server handover form, recorded as `saved_by: "human"`) already carries explicit consent
  at save time and needs no FA-side confirmation; the tool path is the agent asking on its own.
- `forget_saved_session`: allow (destructive but user-recoverable by re-login; revocation should be
  cheap).
- `list_saved_sessions`: allow.
- Profile placement: the browser/handoff-capable profiles only (same
  `handoff_capable_profiles` gate as `request_browser_handoff`). Notably **not** in
  untrusted-input profiles.

When the taint machinery ([runtime-taint-machinery](runtime-taint-machinery.md)) lands, these
per-tool confirmations become taint-matrix rules instead: `load_saved_session` maps to a
high-sensitivity sink whose outcome depends on turn taint, and navigation/form-submit inside a
jar-loaded session is the "browser cell" — `attacker_addressable_egress` evaluated against the
session's `jar_origins` (navigation within jar origins is the authenticated-browsing use case;
cross-origin navigation from an authenticated session after high-tier taint is where blocking
concentrates). The session metadata (`jar_id`, `jar_origins`, `jar_registrable_domains` on every
session response) exists precisely so those rules have ground truth to evaluate against.

Snapshots from jar-loaded sessions remain `UNKNOWN_EXTERNAL` taint like all web content —
being logged in makes the fetched content more sensitive, not more trustworthy.

## Expiry and re-login orchestration

browser-server provides three signals (cookie-expiry metadata, an on-demand rate-limited probe
endpoint, and agent-reported invalidation); it never acts on its own. Family Assistant owns the
loop, as userspace configuration in keeping with primitives-in-code / behavior-in-configuration:

- A schedule automation probes household-critical jars (e.g. daily) and, on `stale`, notifies the
  user with the re-login ask.
- Re-login is the existing warm-handoff flow: `load_saved_session` (stale) → handoff with reason
  `credentials` → human logs in → `save_browser_session` refresh. No new primitives.

## User-visible behavior

To document in `docs/user/USER_GUIDE.md` and the browser-profile prompt when implemented:

- "Save this login" appears on the browser handover page; saved logins are listed and revocable in
  the browser service UI (`/jars`) and via chat ("forget my Woolworths login").
- The assistant always asks before opening a browser with a saved login.
- Saved logins keep the site's session data (cookies and browser storage) only — never passwords —
  encrypted on the browser server.

## Milestones

1–4 are browser-server milestones (JarStore; save path; load path; expiry) — see the mechanism doc.

5. **FA adapter (this repo)**: the four tools on `RemoteBrowserBackend`, registration in code +
   `config.yaml`, policy defaults above, prompts + user guide, functional tests against a fake
   browser-server asserting (a) confirmation fires before a jar-loaded session exists and (b) no
   cookie material ever reaches the model transcript.
6. **Expiry automation**: shipped example schedule automation for probing + re-login notification.
7. **Taint hookup** (after #992/#993 land): replace static confirms with taint-matrix rules keyed on
   `jar_origins`.
