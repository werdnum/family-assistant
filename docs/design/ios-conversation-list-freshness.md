# Keeping the iOS conversation list fresh

## Problem

When a user starts a new chat on iOS and sends the first message, the new conversation does not
appear in the conversation list until the user manually pull-to-refreshes. More generally, the
conversation **list** (the sidebar of `ChatConversationSummary` rows) goes stale whenever a
conversation changes outside the thread the user is currently viewing:

- a brand-new conversation created by this device's first send,
- a delegated sub-turn or scheduled/callback turn finishing in the background,
- server-side processing completing for a conversation the user has navigated away from,
- (longer term) a conversation created or updated from another interface (Telegram, another device).

### Root cause

The list only refreshes on a handful of triggers:

- a turn the user is actively watching **completes/drops** in the *active* conversation
  (`ChatViewModel.runSendTurn` → `refreshRecentConversations`),
- the app **returns from background to foreground** (`ChatThreadView` `onChange(scenePhase)` →
  `reconnectLiveUpdates`),
- a **manual pull-to-refresh**.

The live-update SSE stream (`ConversationStreamHub`) is strictly **per-conversation**
(`/v1/chat/conversations/{id}/stream`), and the iOS follow loop only subscribes to the *currently
active* conversation. There is no account-level channel that tells a client "some conversation in
your list changed", so anything happening outside the open thread is invisible until one of the
coarse triggers above fires.

## Approach

Two complementary fixes (both shipped):

1. **Client-side optimistic insert (band-aid).** When the user sends a message, immediately upsert a
   `ChatConversationSummary` for the active conversation into `conversations` and move it to the
   top. A brand-new chat shows up instantly with the user's prompt as the preview; an existing chat
   bumps to the top. The authoritative server refresh that already runs on turn completion
   reconciles the optimistic row (same `conversationID` → replaced by the real summary), so there is
   no flicker and nothing disappears if the refresh races ahead of persistence.

2. **Global conversation-activity SSE channel (proper fix).** A new account-scoped stream broadcasts
   a lightweight "conversation X changed" ping whenever any conversation the user owns gets new
   visible activity. iOS subscribes to it while foregrounded and, on each ping, runs the existing
   incremental `refreshRecentConversations`. This covers background completions,
   delegation/scheduled turns, and (where the emitting code has hub access) cross-interface activity
   — none of which the per-conversation stream sees.

### Why the activity channel can be advisory

`GET /v1/chat/conversations` already filters to conversations the caller canonically owns
(`_caller_is_sole_canonical_owner`). The activity ping carries only an opaque `conversation_id` +
timestamp and never any message content, and the client reacts by re-fetching the authoritative,
ownership-filtered list. So the channel is a *refresh nudge*, not a source of truth:

- A missed ping degrades to the existing foreground/manual refresh — no correctness loss, just
  latency.
- A spurious ping (delivered to the wrong subscriber) at worst causes a redundant list fetch that
  surfaces nothing new.

This lets the hub filter activity subscribers by a simple exact `user_id` match (the same
`user_identifier` the turn was started under) without reimplementing the identity-resolver ownership
logic: exact match is correct for the dominant same-user case and safe (no conversation-id leak)
otherwise.

## Backend design

### `ConversationStreamHub` (in-process, single-worker — unchanged assumption)

Add a global activity fan-out alongside the existing per-conversation buffers. Activity events are
**ephemeral** (no ring buffer / no replay): a subscriber that connects late simply refreshes on its
next ping, and on connect the client refreshes once unconditionally.

- `subscribe_activity(user_id) -> ActivitySubscriptionHandle` — registers a fresh unbounded-ish
  queue (bounded like subscriber queues; overflow drops the subscriber, which reconnects).
- `unsubscribe_activity(queue)` — synchronous, lock-free (mirrors `unsubscribe`, safe from a
  generator `finally`).
- `publish_activity(conversation_id, *, user_id, reason)` — fan out a compact
  `ConversationActivity(conversation_id, reason, timestamp)` to every activity subscriber whose
  `user_id` matches.

The hub emits activity automatically from `end_turn` only (the reply is persisted by then).
`start_turn` deliberately does **not** emit: it runs before the caller persists the user message,
and the conversation-list endpoint only lists persisted messages — a ping there would make a client
refetch a list that doesn't yet include the conversation (and clobber an optimistic row). The
turn-start ping is instead emitted by the `/v1/chat/turns` endpoint via `publish_activity` *after*
the user-message commit. `publish_activity` is public so endpoints and non-turn producers can emit
it.

### Emit sites

- **Web/iOS streaming turns (`/v1/chat/turns`):** the endpoint calls `publish_activity` after the
  user-message commit (start), and the hub auto-emits from `end_turn` (reply persisted).
- **Non-streaming sends (`/v1/chat/send_message`, e.g. iOS App Intents/Siri):** no turn lifecycle,
  so the endpoint schedules `publish_activity` from `db_context.on_commit` (its request transaction
  commits at request end).
- **Delegation / scheduled completions:** `task_worker._tickle_stream_hub_on_commit` already
  publishes a per-conversation `message` nudge after commit; it additionally calls
  `hub.publish_activity(...)` with the run's `user_id`.
- **Telegram / other hub-less interfaces:** out of scope for this change (no hub reference). Push
  notifications + foreground refresh remain the fallback; a future change can route these through
  the hub or a shared activity bus.

### Endpoint

`GET /api/v1/chat/activity/stream` — authenticated, account-scoped SSE.

- Resolves `user_id = current_user["user_identifier"]`, subscribes to the hub's activity channel for
  that user.
- Emits `event: conversation_activity` frames (`{"conversation_id", "reason", "timestamp"}`), 30s
  `heartbeat` frames, and a `stream_dropped` frame on server shutdown or subscriber overflow —
  matching the per-conversation stream's wire conventions.
- Always-on lifetime (like `follow=true`).

## iOS client design

- `ChatConversationActivityEvent` model + `ChatAPIClient.connectActivityStream()` returning an
  `AsyncThrowingStream`.
- `ChatViewModel.startActivityStream()` runs a reconnect loop (shared backoff with the existing
  live-events loop): on connect it refreshes once, then on each `conversation_activity` ping calls
  `refreshRecentConversations`. Cancelled in `deinit`; restarted on foreground via the existing
  `reconnectLiveUpdates` path; started in `bootstrap`.

## Testing

- **iOS unit (`ChatViewModelTests`):** sending in a new conversation optimistically inserts a
  summary at the top before any server response; a later `refreshRecentConversations` reconciles it
  without duplication; a failed turn-start removes the optimistic row (no phantom); an activity ping
  triggers a list refresh.
- **Backend hub tests:** `end_turn`/`publish_activity` fan out to a matching-user activity
  subscriber and not to a different user; `start_turn` does **not** broadcast; overflow drops the
  subscriber.
- **Web Playwright (two-tab):** a chat started in one tab appears in a second tab's sidebar live,
  with no manual refresh — end-to-end coverage of the real endpoint over a real server. (An
  in-memory ASGI endpoint test was dropped: an always-on SSE stream plus a concurrent request on one
  httpx client deadlocks; the Playwright test is the better integration check.)

## Milestones

1. iOS optimistic insert + tests (ships first).
2. Backend activity channel: hub + endpoint + task_worker hook + tests.
3. iOS activity subscription + tests.
4. Web parity: optimistic insert + activity stream + two-tab Playwright test.
