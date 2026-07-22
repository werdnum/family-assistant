# iOS auth-session handling: deliberate simplification

**Status:** Decided 2026-07-21. Supersedes the distributed auth-recovery machinery that accreted
across M1 review rounds on `feat/ios-sync-m1-sync-coordinator` (PR #1041).

## Why this note exists

Across several code-review rounds, the iOS client's auth-session handling grew a large defensive
apparatus: per-channel refresh-attempt flags reset per reconnect, a tri-state stream-connect auth
result, double-401 terminal latching duplicated across the follow and activity stream loops, a
stream-start guard, and `authEpoch` "fencing" threaded through `ChatAPIClient`, `ChatViewModel`, and
both stream loops. Each review round then found new edge cases *inside that machinery* ("the attempt
flag never resets", "the activity loop doesn't break", "streams start while authRequired"), fixed by
adding more state.

Two independent senior assessments (the maintaining agent and an external `codex gpt-5.6-sol`
architecture review, 2026-07-21) reached the same conclusion: this is an over-engineering spiral.
The auth overlay is now more complicated than the user-facing guarantee it provides, and review is
finding defects in the defensive machinery itself rather than in real behavior.

## Trust model (the load-bearing fact)

The iOS client is **trusted**; the **server enforces all authentication and authorization on every
request** (OIDC bearer tokens — the server rejects any invalid, expired, or revoked token). It is
overwhelmingly a **personal, single-user device**.

Consequences:

- A rejected/expired/revoked token lingering in the keychain is **not a security vulnerability** —
  the server rejects it, so replaying it gains nothing. It is at most cleanup/UX debt (pointless
  requests, confusing startup). The only scenario where retained client auth state would be a
  security concern is a **shared device between mutually untrusted users** with a *still-valid*
  token surviving logout — which is neither this app's model nor the scenario the machinery was
  defending. There is **no client-side security boundary here to harden**; the server is the
  boundary.

## The actual requirement

1. Keep the user signed in through ordinary token expiry (silent refresh).
2. If silent refresh cannot restore a server-accepted session, **stop retrying** and present one
   clear "Sign in again" affordance — never a generic error modal.
3. Logout stops authenticated use and clears local credentials best-effort.

That is the whole guarantee. Single-flight refresh (avoiding a refresh stampede when the app
foregrounds and fires many requests at once) is a worthwhile implementation detail in service of #1.

## Target design (centralize the policy)

`AuthManager` is the **sole auth authority**. No caller-managed epochs; no per-channel auth-attempt
state; the stream loops do not implement an authentication protocol.

- **State:** `authenticated | refreshing | signInRequired` (surfaced to the coordinator as the
  existing `AuthStateSignal`; drives the derived `.authRequired` presentation).
- **Every request** obtains its token from `AuthManager`.
- **On a 401**, `AuthManager` performs **one single-flight forced refresh**. If it succeeds, retry
  once where replay is safe (idempotent GETs, and the turn-start path reconciled by `turn_id`). If
  it fails/rejects, **atomically** clear credentials and enter `signInRequired`.
- **Stream connectors** back off only on transport errors and stop on `signInRequired`. On a connect
  401/403 a loop performs **one** forced refresh and retries the connect once; a second 401/403 is
  terminal (latch `signInRequired`, stop). To bound that to a single attempt per consecutive-401
  episode, each loop keeps one local `authRefreshAlreadyAttempted` bool that **resets on a
  successful connect** (so a later legitimate expiry can refresh again). This is the minimum needed
  for loop termination — *not* the deleted cross-layer machinery (the tri-state
  `StreamConnectAuthResult`, the `forceAuthRefreshForStreamConnect`/`markStreamConnectAuthRejected`
  delegate protocol, per-channel attempt state threaded through the view model). Review rounds
  confirmed some bound is genuinely required: without it a persistently-rejecting stream whose
  refresh returns 200 hot-loops forever and never surfaces sign-in.
- **Successful login restarts the streams centrally** (via the existing auth-state observer), and
  clears `signInRequired`.
- **The terminal auth latch is epoch-fenced.** Clearing credentials + latching `signInRequired` is
  destructive, so it must not fire from a stale operation whose session has since been superseded by
  a logout/re-login. Each auth-sensitive operation (turn start, each stream loop, the GET retry)
  captures `AuthManager.authEpoch` at its start and latches via
  `markAuthRequiredIfCurrent(capturedEpoch:)`, which no-ops unless that epoch still owns the auth
  state. This is a single captured `Int` at the terminal path only — the minimum for correctness,
  not the full per-request epoch threading that was deleted. Refresh-completion fencing (a stale
  refresh overwriting a newer login) stays private to `AuthManager` and additionally guards the
  WebKit session-cookie bridge.

## What gets deleted vs kept

**Delete:** the tri-state `StreamConnectAuthResult`; the `forceAuthRefreshForStreamConnect` /
`markStreamConnectAuthRejected` / `getCurrentAuthEpochForStreamConnect` delegate plumbing; the
duplicated ~120-line inline turn-start retry that reimplemented the whole registration flow; the
full per-request epoch threading (every request/refresh/clear passing a caller-managed epoch).

**Keep** (and, where review showed it necessary, a deliberately minimal form of):

- single-flight refresh;
- the `authRequired`/`AuthStateSignal` state and its observer registry;
- `authEpoch` inside `AuthManager` for the stale-refresh / WebKit-bridge race;
- a one-line per-loop `authRefreshAlreadyAttempted` bool (reset on connect) to bound stream 401
  recovery to a single refresh;
- a start-captured epoch checked via `markAuthRequiredIfCurrent` at each terminal-latch site (turn
  start, both stream loops, the GET retry), so a stale rejection can't erase a newer login;
- a transparent single retry wrapping the turn-start POST (one call site, one refresh, one replay by
  `turn_id`, cancellation-checked before the replay).

These are the minimum for loop termination, terminal-latch correctness, and silent recovery — not
the deleted distributed machinery.

## Disposition of the open review findings (PR #1041)

- **#1 (attempt flag reset per reconnect)** and **#5 (streams start while authRequired)** —
  dissolved by construction: there is no per-loop attempt flag, and loops consult the central
  `signInRequired` state before/while running.
- **#2 (activity loop doesn't stop on double rejection)** — dissolved: the loop stops on
  `signInRequired` from the central authority; the duplicated terminal-latch branch is gone.
- **#4 (turn-start 401 → generic modal)** — fixed: the turn-start path routes a
  `ChatAPIError.server(401/403)` into the central `signInRequired` path (no modal), like every other
  request.
- **#3 (empty launch draft shows "degraded")** — fixed independently: the presentation derivation
  does not require follow-stream health when no conversation is selected yet.

## Verification

This re-opens a verified-green M1 for a refactor that **net-removes** code. It ships only after a
full `FamilyAssistantTests` run to completion (with `-test-timeouts-enabled` so a hang aborts and
names itself) and a local `codex exec review` pass with its findings addressed.
