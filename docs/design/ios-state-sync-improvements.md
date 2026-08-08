# iOS State Sync: Current Issues, Redesign, and Verification Plan

**Status:** Draft v2 — revised after external review (codex `gpt-5.6-sol`, 2026-07-20; see §9)
**Scope:** iOS app connection/state synchronization (`ios/FamilyAssistant`), with supporting backend
changes in `src/family_assistant/web`.

## 1. Problem statement

State sync between the iOS app and the backend works under ideal conditions but degrades around app
suspend/resume and flaky networks. Observed symptoms:

1. **Stuck "no connection" icon** — the live-updates indicator shows disconnected even when
   connectivity is fine, particularly after resuming.
2. **Error popups on resume** — foregrounding the app frequently raises a "Chat Error" modal even
   though nothing actionable went wrong.
3. **Stale conversation list** — new chats and background activity don't appear until the user
   manually refreshes.

### 1.1 Production evidence

Diagnostics query (reproducible; token per ops setup):

```bash
source ~/.config/agent-sandbox/diagnostics-readonly-token.env
curl -s -H "Authorization: Bearer $DIAGNOSTICS_READONLY_TOKEN" \
  "https://assistant.andrewgarrett.dev/api/errors/?limit=100&page=N"
# Paginate pages 1..6 for the full 600-record sample.
# iOS records: logger_name == "frontend.javascript" AND
# user_agent starts with "FamilyAssistant-iOS/"
```

Sample: 600 most recent records, 2026-07-18 → 2026-07-20, one installation (build 41, TestFlight).
33 iOS-originated reports, arriving in tight bursts aligned with app-resume times, clustered as
(clusters overlap: the 22:07 server-incident burst contributes the 503 entries counted in F/G):

| Cluster | n   | Signature                                                                              |
| ------- | --- | -------------------------------------------------------------------------------------- |
| A       | 7   | `Chat.liveStreamDrop error=timedOut`, phase `live-follow`, `is_streaming=false`        |
| B       | 6   | `Chat.liveStreamDrop error=networkConnectionLost`                                      |
| C       | 4   | `Chat.streamDrop` outcome `interrupted`, `error=none`, `sawToolCall=true`, idle 5–96 s |
| D/E     | 6   | `Chat.streamDrop error=networkConnectionLost` (send-subscribe phase)                   |
| F       | 3   | `Chat.recentConversations` REST failure (timedOut, networkConnectionLost, 503)         |
| G       | 2   | pending-approvals poll failure (timedOut, 503)                                         |
| H       | 5   | 503s at 2026-07-19T22:07 across stream + REST — a server incident, not client          |
| I       | 1   | `streamDrop timedOut` after 133 s with only `turnStarted` received                     |

Caveat (from review): burst clustering shows correlation with resume, not proven causation; §7.4
adds telemetry (`chatAlertPresented`) so the popup rate becomes directly measurable rather than
inferred from breadcrumbs. Cluster C's ~96 s idle drops are *suggestive* of front-door idle-timeout
behavior (≈100 s) and motivate the M0 audit; they are not yet proof.

## 2. Current architecture

### 2.1 Client

Sync state lives in `ChatViewModel` (`ios/.../Chat/ChatViewModel.swift`), an
`@Observable @MainActor` singleton, with transport in `ChatAPIClient.swift` (`URLSession.bytes` +
hand-rolled `SSEParser`). Three streams:

- **Live follow stream** (`startLiveEvents`, ~2209):
  `GET /api/v1/chat/conversations/{id}/stream?follow=true`; infinite reconnect loop, exponential
  backoff 2 s → 30 s cap. On successful connect and on failed connect (when not `isStreaming`) it
  runs a history + recent-list catch-up.
- **Send/turn stream** (`runSendTurn` → `subscribeToTurn`, ~1102): `?follow=false`; *bounded* resume
  budget (5 consecutive no-progress resumes), then falls back to a history reload. It already
  classifies errors locally: 410 → history reload, 5xx → resume, transport drop →
  resume/interrupted, recovered drops deliberately avoid `errorMessage` (~1209, ~1256).
- **Activity stream** (`startActivityStream`, ~2123): `GET /api/v1/chat/activity/stream`; same
  backoff loop; refreshes the recent list on connect and on every ping.

Connection indicator: a single `liveUpdatesConnected: Bool` (initialized `true`), written **only by
the follow-stream path** — set `true` on successful connect (~2317), set `false` by
`markLiveUpdatesDisconnectedIfActive` (~2486), which is suppressed while `isStreaming`. The
`.connected` SSE event case (~2394) is dead code: the server never emits such an event; connect
success is inferred when `URLSession.bytes(for:)` returns headers (`ChatAPIClient.swift:458`).

Errors: a single `errorMessage` property drives the "Chat Error" modal (`ChatViews.swift` ~80). It
is written with raw `localizedDescription` by ~a dozen unrelated paths: conversation refresh (~344),
recent-list refresh (~387), message load/merge (~619–784), profile load, attachment ops, send
failures, and the **15-second pending-approvals poll** (~2092), which runs independently of the
streams.

Lifecycle: `ChatScenePhaseObserver` (`ChatViews.swift` ~299) calls `reconnectLiveUpdates()` on real
background→active (not `.inactive` blips), restarting the follow and activity streams. On
backgrounding nothing is torn down; stream tasks are suspended by the OS and their sockets die while
suspended. There is no `NWPathMonitor` input. Turn state (`turnID`, `lastSeq`, ack cursor,
steer/stop controls) lives partly in task-local variables of `runSendTurn` (~982). Token refresh in
`AuthManager` has no single-flight guard, and response-time 401s are not refreshed-and-retried
(`AuthManager.swift:342, 518`; `ChatAPIClient.swift:606`).

### 2.2 Server

`ConversationStreamHub` (`src/family_assistant/web/conversation_stream_hub.py`) is a single-process
in-memory broker:

- Per-conversation ring buffer (5000 events, monotonic `seq`); clients resume with `?from_seq=`;
  evicted cursors get **HTTP 410** with `min_available_seq` + `active_turns`. No SSE
  `id:`/`Last-Event-ID`; no persistence across restarts.
- Activity stream has **no replay** (hub ~448): pings missed while disconnected are gone; clients
  must refetch on reconnect.
- 30 s heartbeats on both streams; `X-Accel-Buffering: no` is set, but the production front door
  (Cloudflare/ingress) has a history of buffering/killing long-lived SSE (see memory of the prod SSE
  incident), so end-to-end heartbeat delivery is unverified.
- APNs push fires only when no SSE subscriber acks `turn_ended` within a 2 s grace window; alert
  push only — **no `content-available`** (`services/apns.py:68, 323`), and the app declares only the
  `audio` background mode, so pushes cannot wake the suspended app to refresh.
- `GET /.../messages` returns `active_turns` (`chat_api.py:464`) with `latest_seq` — but **not
  `first_seq`**, and the iOS `ChatConversationMessagesResponse` doesn't decode `active_turns` at all
  (`ChatModels.swift:343`).
- `GET /conversations` has a pagination correctness bug: ownership filtering happens *after* the DB
  page is fetched while `count` is the unfiltered total (`chat_api.py:2070`), and the iOS client
  stops paginating when a filtered page comes back empty (`ChatAPIClient.swift:15`).

## 3. Diagnosis

### 3.1 Error popups on resume — a shared modal error channel

The dominant cause is not the streams: it is that **any REST failure anywhere sets the modal
`errorMessage`** with a raw `localizedDescription`. On resume, several REST calls fire at once
(follow-connect catch-up merges, recent-list refresh, and the always-running 15 s approvals poll)
exactly when the radio and sockets are still waking up, so transient `timedOut` /
`networkConnectionLost` failures become modals. Production clusters F and G (`recentConversations`,
`pendingApprovals`) are this pathway directly; the send path, by contrast, already recovers most
transport errors silently.

### 3.2 The foreground reconnect hook is likely inert on normal resume

`shouldReconnectOnForeground(cameFromBackground:isNowActive:)` (`ChatViewModel.swift:2174`) returns
true only when a **single** scene-phase `onChange` firing goes directly `.background → .active`. A
normal iOS resume delivers two firings — `.background → .inactive` (fails `isNowActive`), then
`.inactive → .active` (fails `cameFromBackground`) — so `reconnectLiveUpdates()` never runs on the
common resume path. The gate was written to ignore Control-Center/app-switcher blips, but as a pure
function of one transition pair it cannot distinguish "blip" from "real resume that passed through
background"; that needs a latched came-from-background bit. The only other trigger is the
indicator's manual tap-to-reconnect (`ChatViews.swift:875`).

Consequences on resume-into-a-conversation: the thread does not resume live updates until the
suspended stream tasks' dead sockets error out on their own (URLSession idle timeout ≈ 60 s —
matching cluster A's `timedOut` drops) and the backoff loop reconnects; or until the user forces
fresh connections by navigating out and re-selecting the conversation (`selectConversation` restarts
the follow stream; list pull-to-refresh itself only refetches the list via REST). This matches the
reported experience directly: "foreground into a conversation → it doesn't resume updating →
refreshing/re-entering fixes it." The dimension model in §4.1 fixes this structurally: lifecycle is
**latched state** owned by the coordinator (background observed → resync on next active), not a
predicate over a single transition.

### 3.3 Stuck "no connection" icon — a laggy single boolean

The indicator is one boolean owned by the follow stream:

- On resume it stays `false` until a follow connect *succeeds*; the reconnect loop's backoff (up to
  30 s) is not reset on foreground, so a single failed first attempt (cluster A: `timedOut` while
  idle) leaves the icon stuck for the remainder of the backoff even though REST traffic is working.
- The `isStreaming` suppression guard means drops during a send never mark disconnected, so the
  flag's meaning is inconsistent ("follow-stream health, except during sends").
- No reachability input: Wi-Fi↔cellular transitions surface only as eventual socket errors, in both
  directions (slow to show offline, slow to clear).
- The `.connected`-event arm is dead code against this backend, so indicator truth depends solely on
  connect-call success, which a buffering front door can fake (headers arrive, events never do).

(An earlier draft attributed the stuck icon to a zombie-task race between a suspended old loop task
and its replacement. Review found current cancellation checks make that unproven; generation fencing
is retained in the design as defense-in-depth, not as the established root cause.)

### 3.4 Stale conversation list — refresh gated behind connections

List refresh is not solely activity-stream-dependent (follow connect/fail paths also refresh), but
every refresh trigger is **contingent on a stream attempt completing**: foreground refresh happens
only as a side effect of the follow/activity loops progressing, follow catch-up is skipped while
`isStreaming`, activity pings have no replay, and a failed first reconnect defers everything by the
backoff interval. There is no unconditional "foreground ⇒ refetch" step and no push-receipt refresh
hint.

### 3.5 Cross-cutting structural issues

- No single owner of sync state: three loops + N REST paths each decide error surfacing and refresh
  independently.
- Active-turn state is trapped in task-locals; nothing can reattach to a running turn after the
  owning task dies (and the client doesn't even decode `active_turns`).
- Auth refresh is not single-flighted and 401s aren't retried — a resume that triggers several
  concurrent requests can race token rotation.
- Verification blind spot: there is no telemetry distinguishing "breadcrumb logged" from "modal
  actually shown to the user."

## 4. Design

### 4.1 Sync model: independent dimensions, derived presentation (M1)

A `@MainActor` `SyncCoordinator` becomes the owner of the stream tasks and their lifecycles from the
first milestone (a report-only shim was considered and rejected in review: a component that doesn't
own cancellation/restart can't be the single owner). It tracks **separate dimensions**:

- lifecycle: foreground / background
- reachability *hint* from `NWPathMonitor` (`.unsatisfied` pauses connect attempts and shows offline
  immediately; `.satisfied` is only a hint to retry, never proof of health)
- auth: ok / refreshing / authRequired
- per-channel health: follow-stream, activity-stream (connected / reconnecting / down / idle).
  `idle` is the never-attempted initial state and is deliberately distinct from `down` ("tried,
  waiting to retry"): only `down` maps to the wifi-bad `degraded` warning, so the launch window
  before `bootstrap` opens the streams reads as the `syncing` spinner rather than announcing a
  connection failure that has not happened. The streams are opened at the *top* of `bootstrap`,
  before its awaited profile/conversation-list fetches, so that window is milliseconds rather than
  the several seconds those fetches take.
- reconciliation phase: idle / syncing

and derives a small presentation state for the UI:
`live | syncing | degraded | offline | authRequired | suspended`. The indicator renders this derived
state only. All stream callbacks carry a generation number; events from a stale generation are
dropped.

Auth hardening in the same milestone: single-flight token refresh, one bounded refresh-and-retry for
idempotent requests on response-time 401, and an explicit `authRequired` terminal state (never a
generic error modal).

### 4.2 Durable `ActiveTurnSession` (M1)

Extract the in-flight turn state out of `runSendTurn` task-locals into a persistent-for-the-session
object: conversation ID, turn ID, optimistic bubble ID, original payload (for retry), last applied
seq, ack cursor, and steer/stop control state. The session survives its transport task. This is the
precondition for both background teardown and foreground reattachment. Client decodes `active_turns`
from `/messages`; reattachment is **tail-only** (subscribe from `latest_seq`) followed by canonical
history replacement when the turn ends — this matches existing server semantics and avoids needing a
new `first_seq` field. (If tail-only proves insufficient in practice, add `first_seq` to
`ActiveTurnInfo` server-side then.)

**Single event owner.** Per conversation there is exactly one stream consumer (the coordinator's
follow stream) and one ack cursor. "Reattaching" a session never opens a second subscription: the
coordinator's event router dispatches events for the session's `turnID` to the `ActiveTurnSession`
and everything else to passive rendering. Cursors are precise:

- Known session: resume with `from_seq = lastAppliedSeq + 1` (the server replays `seq >= from_seq`
  from its ring buffer, filling the gap; 410 → history reload as today).
- Newly discovered turn (server reports it in `active_turns` but there is no local session):
  subscribe at the tail (`from_seq = -1`) and render progressively; the canonical history
  replacement at `turn_ended` supplies the missed prefix. No mid-turn prefix reconstruction is
  attempted.

### 4.3 Background policy (M2)

On real background (not `.inactive` blips):

1. Bump generation; stop scheduling new advisory work.
2. Cancel the **follow and activity** streams deliberately (they are advisory; push notifications
   correctly take over delivery when no subscriber acks).
3. **Preserve the send.** Policy (decided, not conditional): on real background the send's transport
   task **is cancelled** by the coordinator through a dedicated suspend path that must not run the
   user-facing `cancelStream()` semantics ("Response stopped", control discard). The
   `ActiveTurnSession` (state, cursors, retry payload) survives. On foreground, resync **awaits
   termination of the old consumer task** (the generation fence rejects any late events it emits)
   before establishing the new follow stream, then reattaches per §4.2 — the turn continues
   rendering or is reconciled from history if it finished server-side.

### 4.4 Foreground reconciliation (M2)

A single idempotent, coalesced resync, ordered to avoid the lost-wakeup race the review identified
(activity has no replay, so snapshot-then-subscribe can drop anything committed in between):

1. Bump generation; cancel obsolete advisory tasks; reset backoffs.
2. Complete one auth gate (single-flight refresh if near expiry).
3. **Establish and buffer** the activity stream and the selected conversation's follow stream first.
   "Established" means response headers received (the existing connect signal). Buffered events are
   held in a bounded queue (a few hundred entries); overflow — which should not happen during a
   snapshot fetch — aborts and restarts the resync rather than dropping events silently.
4. Fetch authoritative snapshots: full conversation list (full replacement semantics, so deletions
   converge) and selected-conversation messages + `active_turns`.
5. Apply snapshots only if generation and selection still match, merging around the live
   `ActiveTurnSession`.
6. Drain buffered stream events; reattach to running turns.
7. Hand off to the steady-state stream loops and merge one bounded recent-conversation page to close
   the activity stream's no-replay reconnect window when step 4 succeeded. If its authoritative
   snapshot failed, retry the full replacement instead so a recent-page success cannot hide stale
   older rows or clear the list failure state prematurely.
8. Publish `live` or `degraded` from per-channel health.

The same resync runs on `NWPathMonitor` recovery and (as a hint) on push receipt while foregrounded.

### 4.5 Operation-aware error taxonomy (M3)

Replace scattered `errorMessage = error.localizedDescription` with a central classifier keyed on
**operation × error**, not error type alone:

- Background/advisory reads (list refresh, approvals poll, profile load, catch-up merges): failures
  are *never* modal. Feed coordinator health + breadcrumb; persistent failure (N consecutive, or
  sustained 5xx) surfaces as the `degraded` presentation state, so silent-forever failure is
  impossible.
- User-initiated actions (send, stop, confirm tool, attachment ops): show failure **inline at the
  point of action** (retry affordance on the bubble), not a modal. A send timeout after
  `POST /turns` was accepted is retried / reconciled with the same `turnID`, never double-sent.
- 401 → auth flow; 403 is endpoint-dependent (on a conversation it means access changed — surface as
  such, don't re-auth); 404 on conversation → treated as deleted, not transport; 429/`Retry-After`
  honored; clean EOF classified by context (expected for `follow=false` after `turn_ended`;
  suspicious mid-turn).
- Classification includes whether the user initiated the operation: the same list-refresh failure is
  silent when triggered by resync but shows inline feedback when the user pulled to refresh.
- The modal remains only for truly unrecoverable states.

Every classification decision emits a `chatAlertPresented`-style telemetry event when it does
surface UI, making popup rates measurable (§7.4).

### 4.6 List freshness (M2/M3)

- Unconditional list refetch inside every foreground resync (§4.4 step 4), with full-replacement
  semantics.
- Activity pings remain the low-latency foregrounded path.
- Push receipt while foregrounded triggers a targeted refresh of the referenced conversation + list.
  **Suspended-state refresh via silent push is explicitly out of scope** (requires
  `content-available`, the remote-notification background mode, and best-effort semantics we don't
  need yet).

### 4.7 Server work (M0 + M4)

- **M0 — front-door SSE audit:** verify heartbeat frames traverse assistant.andrewgarrett.dev
  end-to-end (timestamped `curl -N` through the front door), since cluster C timing is consistent
  with ~100 s idle kills. Fix ingress config if buffering is confirmed. Run this first: if the front
  door is eating heartbeats, no client-side lifecycle work can keep streams healthy while
  foregrounded.
- **M4 — pagination correctness:** make conversation-list ownership filtering happen in the query
  (or fix `count` and empty-filtered-page handling) — `chat_api.py:2070` / `ChatAPIClient.swift:15`.
- **Deferred:** `updated_since` incremental list sync. Review verdict: timestamp-based incremental
  sync without tombstones/pagination-cursor semantics is not a safe protocol, and family-sized lists
  don't need it. Full replacement first; revisit with a proper revision/change-feed design only if
  measurement shows full refetch matters.
- **Deferred:** event-log persistence / cross-restart resume / SSE `id:` lines (410-plus-reload
  already handles restarts correctly).

### 4.8 Non-goals

- No transport change (SSE stays; lifecycle handling is the problem).
- No multi-process broker (single-worker assumption stands).
- No offline mutation queue.
- No silent-push background refresh (for now).

## 5. Milestones

Reordered per review: evidence and guardrails first; scenario tests ship *with* each milestone, not
at the end.

- **M0 — Evidence & guardrails** (small, immediate): `chatAlertPresented` telemetry (reason-tagged)
  so popup rates are directly measurable; front-door SSE heartbeat audit; characterization tests
  pinning current recover-silently behaviors we must not regress.
- **M1 — Ownership & fencing:** `SyncCoordinator` owning stream tasks; dimension model + derived
  presentation state; generation fencing; `NWPathMonitor` (behind a protocol for tests); auth
  single-flight + 401-retry + `authRequired`; `ActiveTurnSession` extraction; `active_turns`
  decoding. Fixes the stuck indicator.
- **M2 — Lifecycle policy:** deliberate background teardown (advisory streams only, send session
  preserved); subscribe-then-fetch foreground resync; unconditional list refresh in resync. Fixes
  most of symptoms 2–3.
- **M3 — Error UX:** operation-aware taxonomy; inline retry for user-initiated actions; `degraded`
  surfacing for persistent background failure; push-receipt foreground refresh hints. Eliminates the
  popup class.
- **M4 — Server:** pagination-ownership fix; backend test gaps (activity endpoint HTTP-level,
  injectable-interval heartbeat test); further server work only as measurement justifies.

## 6. Testing strategy

### 6.1 Current state (summary)

Client coverage is real but misses the failing scenarios. Existing: SSE parsing (`SSEParserTests`),
transport-level request shape and hanging/dropped-stream behavior (`ChatAPIClientTests` via a
`URLProtocol` mock), and substantial `ChatViewModelTests` coverage including follow reconnect after
EOF, disconnected indicator after failed connect (~4529), foreground catch-up (~4606),
scene-transition predicate (~5124), activity ping → list refresh (~1035), and transport-drop
suppression (~6535). Missing: real scene wiring, activity-stream reconnect, stale-generation
rejection, indicator recovery (false→true), resume-failure error suppression, and any
true-suspension or adverse-network scenario. Backend: hub unit tests and resumable-streaming
functional tests exist; heartbeat emission and the activity endpoint have no HTTP-level test. CI UI
tests retry ×3, so lifecycle regressions need a deterministic non-retried unit gate.

### 6.2 New tests per milestone

**M0:** characterization tests for existing silent-recovery paths; telemetry-event unit tests.

**M1:**

- Coordinator reducer tests: invariants plus representative transition traces (not exhaustive
  input×state), including emitted effects and stale-generation rejection. Inject a path-monitor
  protocol and use the existing injectable delays (add a clock seam only if backoff-schedule
  assertions need it).
- Indicator: `false` on clean EOF drop; back to `true` after successful reconnect; unaffected by
  stale-generation callbacks.
- Latched foreground detection (regression for §3.2): the realistic scene-phase sequence
  `.background → .inactive → .active` (two firings) triggers exactly one resync; an
  `.inactive → .active` blip with no prior background does not.
- Auth: concurrent refresh single-flight, expiry mid-resync, response-time 401 retry-once, refresh
  rejection → `authRequired`, logout epoch change.

**M2 (race traces, deterministic):**

- Background during: turn POST in flight; post-accept pre-first-event; mid-stream. Foreground resync
  racing: an in-flight send; conversation switch; logout; a second resync request; backgrounding
  mid-resync.
- Lost-wakeup: activity event committed between snapshot fetch and subscription (must be caught by
  subscribe-then-fetch ordering).
- Send whose transport died while the turn kept running server-side: resync shows the completed
  message, no alert (regression for clusters C–E).
- Activity-stream drop → backoff → reconnect → refresh.

**M3:** table-driven taxonomy tests (operation × error → surface), incl. 408, 429/`Retry-After`,
persistent 5xx → `degraded`, 404-as-deleted, clean EOF pre/post `turn_ended`, long quiet tool calls;
approvals poll under suspend/resume never modals.

**M4 (backend):** HTTP-level activity-stream test (connect/ping/disconnect); heartbeat emission with
injectable interval on both stream endpoints; `active_turns` contract test (client-decodable shape);
pagination-ownership fix tests.

**UI tests (with M2):** extend `UITestBackendURLProtocol` with scriptable stream behavior (hang,
drop-after-N, 503 burst). XCUITest covers scene *wiring* (background/foreground → no alert, no stuck
disconnected identifier, list row added while backgrounded appears). Known limitation (from review):
simulator home-press does not faithfully reproduce suspension/socket death — true-suspension checks
happen on device (§7.4).

## 7. Verification

1. **Unit/CI:** all of §6.2 in the deterministic (non-retried) unit lane.
2. **Front-door audit artifact (M0):** recorded evidence that heartbeats arrive through production
   ingress at 30 s cadence, or the ingress fix.
3. **Scenario lane:** scriptable-mock UI tests in `ios-tests.yml`.
4. **Production, measured against M0 telemetry baseline** (per foreground transition, per build):
   modal-alert presentation rate → ~0 for transport-class causes; resync completion rate and p50/p95
   duration (`Chat.resyncCompleted` breadcrumb); time in `degraded`; list convergence after
   foreground (row added while backgrounded is present without manual refresh); transport
   breadcrumbs may persist but tagged `expected=true`.
5. **Dogfood:** several days of TestFlight use across suspend/resume, Wi-Fi↔cellular, and
   poor-signal conditions; re-pull `/api/errors/` clusters and compare against the §1.1 baseline.

## 8. Open questions

1. Inline-retry UX for failed sends: reuse failed-bubble styling or a dedicated component? (M3
   decision.)
2. Should background teardown wait a short grace period (fast app switches) before cancelling
   advisory streams, at the cost of occasionally leaving a suspended socket? Proposal: no grace
   initially — measure resync cost first; it's cheap.
3. Does tail-only turn reattachment (no `first_seq`) render acceptably when resuming mid-turn with a
   long missed prefix? If not, add `first_seq` server-side (small change, M4).

## 9. Review record

Reviewed by codex `gpt-5.6-sol` (2026-07-20). Material corrections adopted:

- Popup diagnosis recentered on the shared modal `errorMessage` channel used by ~a dozen REST paths
  (incl. the 15 s approvals poll), matching prod clusters F/G; send path already recovers silently.
- Indicator diagnosis corrected: single follow-stream-owned boolean with backoff-latency and
  suppression-guard problems; zombie-task race downgraded to unproven (fencing kept as
  defense-in-depth); `.connected` event arm identified as dead code.
- State machine split into independent dimensions + derived presentation state; NWPath demoted to a
  hint.
- Resync reordered subscribe-then-fetch to close a lost-wakeup race against the no-replay activity
  stream.
- `ActiveTurnSession` extraction made a prerequisite; background teardown scoped to advisory streams
  only, never the send session.
- Auth single-flight/401-retry/`authRequired` added.
- APNs suspended-refresh cut (no `content-available` support today).
- `updated_since` deferred (unsafe without tombstones; list sizes small); pre-existing
  pagination-ownership bug queued instead.
- Milestones reordered with telemetry/audit first; scenario tests ship per milestone;
  alert-presentation telemetry added because "alerts drop to zero" was otherwise unverifiable.

A second codex pass on v2 confirmed the above resolved and flagged remaining gaps, all addressed in
this revision: single event owner + precise reattachment cursors (`lastAppliedSeq + 1` vs tail; the
server replays `seq >= from_seq`, so "tail from `latest_seq`" was wrong), a decided background send
policy (cancel transport via suspend path, await old consumer, preserve session), bounded resync
buffering with overflow-restarts, endpoint-aware 403 handling and user-initiated-operation awareness
in the taxonomy, and corrected production cluster counts (F/G/H overlap from the 22:07 server
incident).

### 9.1 M2 review disposition (codex `gpt-5.6-sol`, 2026-07-22)

A local codex pass on the M2 branch (after rebasing onto M1's centralized-auth refactor) raised four
findings about the new foreground-resync interacting with active sends. Disposition, applying the
threat-model / cost-benefit / behaviour-altitude gates in `REVIEW_GUIDELINES.md`:

- **[P1] Active-send resync erases the streaming placeholder — FIXED.** A foregrounded
  reachability/auth recovery ran `applyMessagesSnapshot` → `mergeNewMessages` while a send was
  actively streaming; `mergeNewMessages` drops every `local_` row, deleting the in-flight assistant
  placeholder the send is rendering into. Common scenario (network blip during a streaming reply).
  Fixed by guarding `applyMessagesSnapshot` on `isSendActivelyStreaming`, completing the pattern the
  other passive-resync steps (`reconcileSuspendedSession`, `catchUpPersistedHistory`, the follow
  merge) already follow. Covered by `testActiveSendResyncPreservesOptimisticPlaceholder`.
- **[P1] Overlapping send during the suspend-reconcile window — FIXED, then REVERTED (see the fifth
  pass).** Between foreground and the resync's `reconcileSuspendedSession`, a preserved suspended
  session had `isStreaming == false` and `canSendDraft == true`, so a normal send could overwrite
  `activeTurnSession` and overlap the still-running durable turn (losing stop/steer control). This
  was first fixed by deriving a send block from existing state (`canSendDraft` excluding the
  `activeTurnSession != nil && !isStreaming` window). The fifth pass showed the block's own failure
  mode (a failed reconcile bricking the composer) outweighed the rare, recoverable overlap it
  prevented, so the block was removed — see §9.1's fifth-pass entry.
- **[P2] Never-registered suspended turn loses the composed message — ACCEPTED (documented).** If
  the app backgrounds in the sub-second window while the initial `POST /turns` is in flight and its
  cancellation prevents server registration, the foreground snapshot legitimately shows no
  `active_turns` entry and the session is cleared, dropping the optimistic `local_` messages. The
  window is narrow and the "proper" mitigations codex proposed (reissue the idempotent turn, or
  verify a terminal persisted reply) are disproportionate machinery; the pragmatic alternative
  (restore the draft) needs finished-vs-never-registered disambiguation that reintroduces the same
  cost. Per behaviour-altitude an uncommon scenario warrants reasonable, not ideal, behaviour, so
  this is accepted as a known limitation. Revisit with a minimal draft-restore only if it is
  observed in practice.
- **[P2] Stalled follow handshake delays snapshots — ACCEPTED (documented).** A blackholed follow
  proxy that stalls (rather than fails) the HTTP handshake can delay the activity stream and both
  snapshots behind the sequential establish, bounded by the `URLSession` request timeout (not
  unbounded). Establishing the channels concurrently risks regressing the carefully ordered
  subscribe-then-buffer sequence (§4.4 steps 3/5/6) for an uncommon degraded case, so the bounded
  status quo is accepted.

A second local codex pass (after the two P1 fixes above) confirmed them resolved and found four more
interleavings, all fixed in the same branch — each a completion of the same guard pattern rather
than new machinery:

- **[P1] Push-hint merge erases the streaming placeholder — FIXED.** `targetedRefresh` (the
  push-hint path, §4.6) called `mergeNewMessages` for the selected conversation without the
  `isSendActivelyStreaming` guard — the same class as the resync fix, on the push path. Guarded
  identically.
- **[P1] Buffered events drained into a switched-to thread — FIXED.**
  `ResyncOrchestrator.drainBuffer` had no selection recheck, so a conversation switch mid-drain
  (during a per-event await) could route the old thread's buffered follow events at the new thread —
  the follow generation cannot be bumped during a resync (the coordinator's task is nilled). Added
  the per-iteration selection-supersede check the pre-drain steps already use.
- **[P2] Overlapping turn via the mutation path — FIXED, then REVERTED with the block (see the fifth
  pass).** `sendDraft` and the queued-follow-up-steer drain were also made to enforce the same
  suspended-send block. Removed together with the block in the fifth pass.
- **[P2] Stale enqueue after overflow-restart — FIXED.** On the `@MainActor`, a cooperatively
  cancelled buffering task could deliver one already-produced element after `stopBuffering`/
  `resetBuffer`, appending it into the next attempt's buffer (drained + re-rendered on replay).
  Added a `Task.isCancelled` check immediately before `enqueue` in both buffering loops; it is
  atomic with element receipt (no await between) and does not affect natural stream-finish tail
  draining.

A third local codex pass confirmed the above and raised four more, disposed as follows:

- **[P1] Re-auth resync leaked across the view model's lifetime — FIXED.** The auth-observer re-auth
  path calls `resyncOrchestrator.request()` fire-and-forget (does not await the returned task), and
  `deinit` tore down the coordinator's streams but not the orchestrator, so a discarded model could
  leave a resync running (holding open SSE sockets and performing its handover) — which also leaked
  the async work across test boundaries (an XCTest "multiple calls made to fulfill" under some suite
  orderings). Added `ResyncOrchestrator.cancelInFlight()` (nonisolated, mirroring
  `SyncCoordinator.cancelOwnedStreams`) and called it from `ChatViewModel.deinit`.
- **[P1] Invalidate buffered resyncs on mid-resync auth rejection — DECLINED (threat model).** If
  the token is revoked *during* a resync (after the initial gate), the buffering tasks can drain a
  few more events from sockets opened with then-valid credentials before the restarted streams get
  401 and latch `authRequired`. Per the trust model this feature operates under — a trusted client
  on a personal single-user device, with the server enforcing auth on every request — those events
  are the user's *own* already-authorized data rendered to their *own* screen; there is no
  exfiltration, cross-user leak, or state corruption, and the re-auth resync reconciles
  authoritative state afterward. The system already converges to `authRequired` correctly. Adding
  generation-invalidation machinery to fence a sub-second, harmless-under-the-threat-model race is
  the client-side auth over-engineering the `REVIEW_GUIDELINES.md` threat-model gate exists to
  filter, so it is declined.
- **[P2] Background-send tests' cancellation await was a no-op — FIXED, and uncovered a real bug.**
  The three new background-send tests read `sendTaskForTesting` *after* `scenePhaseChanged` had
  already nilled `streamTask` (the comment even said "before"), so `await sendTask?.value` awaited
  nil and the aftermath assertions could run before the async cancellation handler. Moved the
  capture before `scenePhaseChanged` — which then exposed a genuine defect the no-op await had
  masked: backgrounding while the initial turn POST is in flight tears down the URLSession request,
  which throws `URLError(.cancelled)` (not a Swift `CancellationError`), so it fell through the
  suspend-aware `catch is CancellationError` into the generic catch and surfaced a spurious "Chat
  Error" modal, marked the bubble failed, and rolled back the optimistic row. Fixed by checking
  `isSuspendCancelled` (the authoritative signal, independent of error type) at the top of the
  generic turn-start catch, taking the same silent suspend-preserve path.
- **[P2] Push hint pending at mount was never consumed — FIXED.** `onChange(of: pendingPushHint)`
  does not fire for a value already set when the view mounts, so a push delivered during auth
  bootstrap (or before `ChatRootView` appeared) was dropped. Factored the observer body into
  `consumePendingPushHint()` and also call it once from the mount `.task`.

A fourth local codex pass (all findings P2 — the P1s had converged) raised four more:

- **[P2] Orphaned `running` placeholder after an unregistered suspend — FIXED.** Refines the
  never-registered case above: on foreground the incremental merge returns an empty delta, and
  `mergeNewMessages`'s empty-delta early return fires *before* the `local_` drop, so the optimistic
  assistant bubble was stranded spinning in `running` forever (a broken outcome, not mere
  degradation). `reconcileSuspendedSession` now removes the orphaned placeholder when it retires a
  pure suspended session that is not running server-side (scoped to exclude the reattached case,
  which finalizes via its live-follow bubble). Covered by
  `testForegroundReconcileRemovesOrphanedPlaceholderForUnregisteredTurn`.
- **[P2] Queued follow-up stranded after a reattached turn ends — FIXED.** A steer that resolved
  `.finished` before the follow-stream `turn_ended` is queued, but its immediate drain no-ops while
  `isStreaming` is still true; the terminal event then reached `clearReattachedSession`, which left
  streaming mode without scheduling the drain, so the cleared composer text stayed queued forever.
  `clearReattachedSession` now schedules `sendNextQueuedFollowUpSteerIfReady`, mirroring
  `finishStreaming` (whose normal-path drain is covered by
  `testFinishedSteerQueuesFollowUpUntilTurnCompletes`).
- **[P2] Final list snapshot races the activity handover — MITIGATED.** After `restartStreams()`
  spawns the coordinator loops, the resync's final recent-page refetch races the activity
  connection's connect-time refresh; a specific interleaving (final request snapshots old state → a
  mutation lands → the no-replay activity stream connects and refreshes *after* it → the delayed old
  response arrives last) can leave one list row stale until the next activity event or resync. It
  remains transient and self-healing. After a successful authoritative replacement, the final
  fallback is deliberately bounded to the recent page; repeating complete pagination caused
  foreground resyncs to exceed their deadline under real database load. A failed authoritative
  replacement still receives a full retry after handoff so older deletions remain recoverable.
- **[P2] Resync retains its host across awaits, delaying teardown — ACCEPTED (documented).** The
  `deinit` `cancelInFlight()` hook (added in the third pass) cannot preempt a resync while it is
  suspended *inside* a host method call, because invoking a method on the (weak) `host` retains it
  for that call's duration, so a discarded screen's resync runs to its next await boundary before
  teardown proceeds. There is no permanent leak — `host` is weak, each step's await is bounded by
  the `URLSession` timeout, and the resync releases the host and completes on its own — so
  preempting mid-await (a broad refactor to stop calling host methods across awaits) is
  disproportionate to a bounded, rare (discard-mid-resync) delay.

A fifth local codex pass raised four findings; the two P1s were addressed, and — notably — its
second P1 was the trigger to unwind machinery from the second/third passes:

- **[P1] Active-send placeholder erased by a resync started while idle — FIXED.** The
  `isSendActivelyStreaming` entry guards (on `applyMessagesSnapshot` / `targetedRefresh` /
  `catchUpPersistedHistory`) are evaluated *before* `mergeNewMessages` awaits its HTTP fetch, so a
  send that STARTS during that await would have its fresh `local_` placeholders dropped out from
  under the still-rendering send task (a TOCTOU on the earlier fix). Added the companion re-check
  inside `mergeNewMessages` after the fetch, before the `local_` drop — one central guard covering
  every caller.
- **[P1] Failed reconcile bricked the composer → the suspended-send block REMOVED.** The second-pass
  `hasUnreconciledSuspendedSend` block (which blocked sends while
  `activeTurnSession != nil && !isStreaming`) had a worse failure mode than the race it guarded: if
  the foreground reconcile *failed*, the session stayed unreconciled and the composer was blocked
  permanently with no reconnect affordance. This is the machinery-edge-case spiral the
  `REVIEW_GUIDELINES.md` cost/benefit gate exists to stop. Reassessed against behaviour-altitude:
  the overlap the block prevented (a send in the sub-second foreground-reconcile window overwriting
  the preserved session) is uncommon and *recoverable* — the original durable turn still completes
  server-side; only client-side stop/steer of it is lost. That is "reasonable, non-broken" behaviour
  for a rare scenario, so the block (and its `sendDraft` / steer-drain enforcement and two tests)
  was removed outright rather than gated. The active-send placeholder protection above is
  independent and stays.
- **[P2] Never-registered turn leaves a "sent"-looking user row — ACCEPTED (documented).** The
  fourth-pass fix removes the orphaned *assistant* placeholder (the broken stuck-spinner); the
  unsent optimistic *user* row lingers looking sent until the next non-empty merge drops it.
  Distinguishing this from a finished-while-backgrounded turn (whose user row is legitimate) needs
  the same disambiguation declined in the first pass; the residue is transient and self-correcting,
  so accepted per behaviour-altitude for this uncommon window.
- **[P2] Two foreground push hints coalesce lossily — ACCEPTED (documented).** `pendingPushHint` is
  a single value, so a second foreground push arriving before SwiftUI consumes the first overwrites
  it; if the first named the selected conversation and the second did not, the selected thread's
  targeted refresh is skipped. It requires two pushes within one render cycle *and* the follow
  stream being down (else the thread updates live regardless), and self-corrects on the next
  event/foreground — a queue is disproportionate to that multi-condition, self-healing window.

A sixth local codex pass raised four findings, all P2 (no P1s — convergence held). Two cheap guards
for genuinely wrong-state outcomes were fixed, one test-correctness gap closed, one accepted:

- **[P2] Wrong-thread merge after a queued-control flush — FIXED.** `attachDiscoveredActiveTurns`
  (inside `mergeNewMessages`) can itself await — a suspended turn's queued Stop/steer flush issues
  HTTP — so a thread switch during that await could merge the old thread's delta into the newly
  selected one, even though the pre-`attachDiscoveredActiveTurns` selection check had passed. Added
  a selection re-check after it, before the delta merge.
- **[P2] Stale reattachment marker on re-suspend — FIXED.** `suspendActiveSend` left
  `reattachedRunningTurnID` populated when an already-reattached turn backgrounded again, so a
  normal send started before foreground reconciliation inherited the stale marker (making
  `isSendActivelyStreaming` false and letting passive resync/follow handling drop the new send's
  placeholder or duplicate output). It now clears the marker on suspend; foreground reconcile
  re-establishes it if the turn is still running.
- **[P2] Two remaining background-send tests' cancellation await was a no-op — FIXED.** The
  third-pass reordering missed `testBackgroundAfterAcceptBeforeFirstStreamEventSuspendsSend` and
  `testBackgroundMidStreamSuspendsSendAndPreservesCursor` (their capture block lacked the comment
  the bulk edit matched on). Moved the `sendTaskForTesting` capture before `scenePhaseChanged` in
  both; both still pass with the aftermath now genuinely awaited (no masked defect, unlike the
  POST-in-flight test).
- **[P2] Queued foreground resync can reopen streams in the background — ACCEPTED (documented).** A
  foreground effect dispatched as an unstructured `Task` could start after an immediate
  re-background (bumped generations + cancelled streams), then capture the new generations, pass
  every fence, and reopen the follow/activity streams while backgrounded. The trigger is a
  foreground→background toggle within the microseconds before the queued task starts; the
  consequence is briefly-open streams (wasteful, not corrupt), self-corrected by the next real
  background teardown — degraded, not broken, so accepted per behaviour-altitude over adding a
  lifecycle re-check to the effect dispatch.

### 9.2 M3 review disposition (codex `gpt-5.6-sol`, 2026-07-22)

A local codex pass on the M3 branch — after rebasing its error-taxonomy / inline-retry work onto the
merged M1+M2 main — raised one P1 and four P2s about the new retry and 429 paths. Disposition per
the same gates:

- **[P1] Retry targeted the user bubble, not the assistant bubble — FIXED.** The user and assistant
  messages of a turn share the same `turnID`, and the user row is appended first, so
  `retryFailedSend`'s `messages.first { $0.turnID == turnID }` selected the *user* bubble — tapping
  Retry cleared the prompt and rendered the resumed reply into it, leaving the actual failed bubble
  untouched. `failedSend(for:)` likewise exposed the affordance on the user row. Both now filter for
  `role == .assistant`. A common outcome (any Retry tap), so ideal behaviour is required. Covered by
  `testRetryAffordanceIsRestrictedToTheAssistantBubble`.
- **[P2] Delayed message-load retry could brick the composer — FIXED.** `loadMessages` set
  `isLoadingMessages = true` but reset it only at the function tail; the stale-selection guards (a
  conversation switch during the fetch await) returned early, leaking the flag and permanently
  disabling the composer on the thread the user moved to — newly reachable via M3's delayed advisory
  retry. Reset with a `defer` so every exit path clears it (also closes the latent pre-existing
  version). Covered by `testStaleMessageLoadDoesNotLeakLoadingFlag`.
- **[P2] User-initiated 429 read went to the modal — FIXED.** The classifier returned `.retryAfter`
  for any advisory-read 429, but `handleUserReadFailure` maps `.retryAfter` to the generic "Chat
  Error" modal, so a throttled pull-to-refresh popped a modal instead of the promised inline
  rate-limit feedback (§4.5). The 429 branch now schedules the silent retry only for *background*
  advisory reads and routes user-initiated reads (and actions) to `.inlineFeedback(.rateLimited)`.
  Covered by `test429UserInitiatedAdvisoryReadStaysInline`.
- **[P2] Approvals poll ignores `Retry-After` — ACCEPTED (documented).** A 429 on the ~15 s
  pending-approvals poll routes `.retryAfter` with no retry closure, so the honored-delay retry is a
  no-op and the loop re-polls on its fixed 15 s cadence rather than the server-requested delay. The
  endpoint returning 429 is uncommon, and the existing behaviour (re-poll at 15 s) already
  self-recovers — at worst one or two extra throttled requests before the delay elapses. Making the
  fixed-cadence poll suppress itself until `Retry-After` is coordination machinery disproportionate
  to that bounded, self-healing inefficiency, so accepted per the cost/benefit gate.
- **[P2] Shared advisory-retry task cancels cross-operation retries — ACCEPTED (documented).** A
  single `advisoryRetryTask` is deliberately coalesced (a burst of throttled polls must not stack
  retries), so a 429 from one operation cancels another operation's scheduled retry. The worst case
  (a one-shot profile-load retry lost to a concurrent list/message 429) degrades to the default
  profile — reasonable, non-broken behaviour — and self-heals on the next foreground resync or
  interaction. It requires two distinct advisory operations both throttled within one retry window;
  keying retries per operation (up to N concurrent retry tasks) trades the deliberate coalescing for
  a rare, recoverable degradation, so accepted per behaviour-altitude / cost-benefit.
