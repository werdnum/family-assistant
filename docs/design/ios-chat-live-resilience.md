# iOS Chat Live Resilience (Resumable Streaming, Milestones 1–2)

## Problem

The reported symptom: send a chat message on device, streaming stops right after the first tool
call, a push notification arrives when the turn finishes, and the full conversation only appears
after the app is reopened — leaving a stuck "disconnected" (`wifi.slash`) indicator in the meantime.

Root cause is the production HTTP front door buffering / severing long-lived SSE (see
[[ios-chat-stream-drop-is-proxy-sse-buffering]] in agent memory), **not** an app bug. The backend
never voluntarily closes the stream and its 30s heartbeats never reach the client. The durable fix
is at the proxy (disable SSE buffering); this document covers the **client-side resilience** so the
app self-heals regardless of a hostile front door.

PR #923 fixed the **send path**: while *this device* is actively sending, `runSendTurn` resumes the
token stream across mid-turn drops (`.dropped`/`.interrupted`) from `lastSeq + 1`, treating the
server's held-open behaviour as an active-turn liveness signal, and gives up to a history reload
only after `maxConsecutiveStreamResumes` instant no-progress closes.

Two gaps remain, both observed in practice (the user still has to pull-to-refresh the conversation
list to recover):

### Gap A — passive recovery is gated behind a working SSE connection

Every recovery path *other than an active send* runs through the passive live-follow loop
(`ChatViewModel.startLiveEvents`). That loop only pulls finished-turn content (`mergeNewMessages`,
a plain `GET …/messages`) **after `connectEvents` successfully opens the follow SSE**
(`handleLiveReconnect`). When the proxy makes SSE unusable, `connectEvents` keeps throwing
(`URLError -1017 cannotParseResponse` from mangled framing); the `catch` only sets the disconnected
indicator and backs off. The plain-HTTP catch-up never runs, so a turn that completed while the
follow stream was down never surfaces — until the user manually refreshes the list and re-opens the
conversation (both plain, short HTTP requests the proxy handles fine). That asymmetry — manual
plain-HTTP reload works, automatic SSE-gated reload does not — is exactly the reported symptom.

There is also no catch-up on **app foreground**: `scenePhase == .active` only gates chat-thread
layout, so a turn that finishes while the app is backgrounded strands until manual refresh.

### Gap B — the live-follow stream carries no tokens

The follow stream subscribes with `event_types=["message","turn_ended"]`, so it never renders
live tokens. A conversation **opened while a turn is already running** — a turn started on another
device, or one whose send task already gave up to a history reload — shows nothing live; the reply
only appears when the turn ends and history reloads. There is no live "assistant is typing"
continuity outside the single device that started the send.

## Design

### Milestone 1 — resilience that survives a broken front door

Make passive recovery independent of SSE health.

1. **SSE-independent history catch-up.** In `startLiveEvents`, on every involuntary disconnect
   (the `connectEvents` throw / clean-EOF path, before the backoff sleep) run a plain-HTTP
   `mergeNewMessages` + `refreshRecentConversations` catch-up (guarded by `!isStreaming`, as the
   reconnect merge already is). Now even a permanently-mangled SSE stream self-heals: a finished
   turn surfaces within one backoff interval, no manual refresh. This is plain HTTP, so it works
   precisely when SSE does not.

2. **Catch up on foreground.** When `scenePhase` transitions to `.active`, kick
   `reconnectLiveUpdates()` (which clears the disconnected flag, restarts the follow loop, and —
   with change 1 — catches up history even if the follow reconnect itself fails). Recovers the
   "turn finished while backgrounded" case.

These changes are small, low-risk, and valuable as defense-in-depth even after the proxy is fixed.

### Milestone 2 — unify live-follow to carry tokens

Collapse the two SSE consumers (send-path token firehose + passive lifecycle-only follow) into a
single always-on, token-carrying live stream per open conversation.

1. **Token-carrying follow.** Drop the `event_types` filter on the follow subscribe so `follow=true`
   streams every event type, including `text`/`tool_call`/`tool_result` tokens, for whichever turn
   is live. The server already supports this: `connectEvents` passes `eventTypes` through verbatim
   and `follow=true` holds the connection open across turns, replaying from `from_seq`.

2. **`turn_id → assistant bubble` mapping.** Incoming token events carry a `turn_id`. The follow
   consumer maintains a map from `turn_id` to the assistant message bubble it is rendering into,
   creating a placeholder bubble on the first token of a previously-unseen running turn. This lets a
   conversation opened mid-turn (turn started elsewhere, or after the local send task gave up) stream
   live into the correct bubble.

3. **Collapse the send-path resume loop into the always-on consumer.** "Send" becomes "POST the
   turn, then let the always-on live stream render it" — removing the dual-consumer split and the
   bespoke resume loop in `runSendTurn`. The ack-cursor ownership that currently distinguishes the
   two paths (`isStreaming` gating in `handleLiveEvent`) folds into the single consumer.

Milestone 2 delivers live mid-turn streaming but pays off most once SSE is healthy through the
proxy, so it follows Milestone 1.

## Testing

iOS unit tests use `ChatMockBackendURLProtocol` (in `ChatViewModelTests.swift`) to script backend
responses. New red-green coverage:

- **M1:** a follow loop where `connectEvents` always throws but `GET …/messages` has a newly-finished
  turn → assert the content surfaces with no manual reload and the plain-HTTP merge count ≥ 1.
- **M1:** `scenePhase → .active` triggers a history catch-up.
- **M2:** opening a conversation with a turn already running streams tokens live into a freshly
  created assistant bubble; a turn started on another device renders live; the unified consumer still
  acks `turn_ended` and suppresses the disconnect push.

## Key files

- `ios/.../Chat/ChatViewModel.swift` — `startLiveEvents`, `handleLiveReconnect`, `handleLiveEvent`,
  `runSendTurn`, the `turn_id → bubble` map.
- `ios/.../Chat/ChatViews.swift` — `scenePhase` foreground hook.
- `ios/.../Chat/ChatAPIClient.swift` — `connectEvents` (drop the `event_types` filter for follow).
- `ios/.../FamilyAssistantTests/ChatViewModelTests.swift` — mock-backend regression tests.

## Known limitations

- **Mid-turn open shows only the tail.** The follow stream subscribes with the default
  `from_seq = -1` (tail from the current head), so a turn already part-way through generating its
  reply when you open the conversation streams in only from the connect point — you may see it
  mid-sentence. The full reply reconciles from persisted history on `turn_ended`. Replaying the
  in-progress turn from its start would need the running turn's first seq, which `GET …/messages`
  does not currently expose (only the `active_turns` count). Wiring an active-turn replay cursor
  through that endpoint is deferred; subscribing from an arbitrary lower seq is unsafe because the
  hub would replay already-persisted turns and double-render them.
- **Passive turns don't drive global UI.** `.error` and tool-confirmation frames carried on the
  follow stream are deliberately ignored — they would otherwise pop a modal "Chat Error" alert or a
  tool-approval sheet for a turn this device is not driving (started elsewhere / by a schedule). A
  failed passive turn still surfaces via its `turn_ended` + history reload.

## What this does NOT change

- The server-side hub, wire protocol, and ack semantics are unchanged; this is a pure client
  resilience / UX change over the existing resumable-streaming API.
- The proxy / front-door SSE buffering fix is tracked separately (infra); these changes make the app
  correct regardless of whether that lands.
