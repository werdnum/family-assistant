# Resumable Streaming (Milestone 0)

## Problem

When a user sent a chat message and then closed the tab or backgrounded the iOS app, an earlier fix
(PR #879) kept the in-flight turn running in the background and delivered the final reply via push
notification. That stopped data loss, but the experience was still poor: the client briefly saw an
error, and on reopen the user only saw the final result (or had to wait for the push) with no sense
of continuity. There was no way to re-attach to the live event stream.

This milestone reshapes the chat streaming API so a client can **subscribe to a conversation's event
stream and resume it from any point**, which is the foundation for "it loads up and continues where
it left off."

## Design

### Two-step API: send vs. subscribe

The old single endpoint `POST /api/v1/chat/send_message_stream` (which both started processing and
streamed the response over the same connection) is replaced by two endpoints:

- `POST /api/v1/chat/turns` — starts a turn. The client supplies a `turn_id` (UUIDv4). The server
  persists the user message, registers the turn in the hub, **synchronously seeds a `turn_started`
  event into the buffer**, then kicks off the background producer. Returns
  `{turn_id, conversation_id, first_seq}`. **Idempotent**: a retried POST with the same `turn_id`
  returns the existing turn instead of starting a second producer — so a network-timed-out send is
  safe to retry.

  Idempotency is enforced at two layers, because the hub is in-memory and a restart wipes its turn
  registry:

  1. **In-memory fast path** — `hub.get_turn` recognizes a turn that is still live (or recently
     completed and not yet pruned). `start_turn` additionally reserves the `turn_id` under a
     per-conversation lock, so two near-simultaneous POSTs can't both spawn a producer.
  2. **Durable fallback** — before starting a producer the endpoint consults the database
     (`get_user_row_by_turn_id`). The persisted user message is the durable record that "this turn
     was already started", so a `turn_id` retried after a backend restart (or after the completed
     turn was pruned from the hub) returns the existing identity instead of persisting a second user
     message and re-driving the LLM. The lookup is scoped to the caller and conversation (404 on
     mismatch) so it can't echo back a foreign turn. Note `message_history.turn_id` is intentionally
     **not** unique — a turn owns several rows (user message, assistant reply, tool messages) — so
     this is an application-level check, not a DB constraint.

- `GET /api/v1/chat/conversations/{conversation_id}/stream?from_seq=<n>&ack_seq=<n>&follow=<bool>` —
  subscribes to the conversation's event stream. Replays buffered events from `from_seq`, then tails
  live events. This is the **only** SSE endpoint; the original sender and any later reconnect use
  the same shape.

Because the producer is decoupled from any HTTP request, a client disconnecting (closing the tab,
backgrounding the app) does not affect processing — and a fresh `GET …/stream?from_seq=0` replays
the whole turn including `turn_started`.

### ConversationStreamHub

`src/family_assistant/web/conversation_stream_hub.py` is an in-memory broker. Per conversation it
holds:

- a **monotonic `seq`** counter and a bounded **ring buffer** of typed `StreamEvent`s (default 5000
  events) — late subscribers replay from any `seq` still in the buffer;
- a set of **subscriber queues** — `publish` assigns a seq, appends to the buffer, and fans out via
  `put_nowait`;
- a `dict[turn_id, TurnRecord]` tracking running/completed turns, including a **strong reference to
  the producer task** (replacing the old anonymous `app.state.background_chat_tasks` set).

The wire events are: `turn_started`, `text`, `tool_call`, `tool_result`,
`tool_confirmation_request`, `tool_confirmation_result`, `attachment`, `turn_ended`, `heartbeat`,
`stream_dropped`. Each carries `seq` (and `turn_id` where applicable) in its payload.

`turn_ended` carries `status` (`complete`/`failed`). `follow=false` (the default, used by the
send-and-watch and resume flows) drains the buffer and any in-flight turn through `turn_ended`, then
closes once no turn is running. `follow=true` stays open with heartbeats for always-on live updates
(e.g. surfacing a reply triggered from another device); clients that prefer a reconnect loop can
keep using `follow=false`.

### Push-notification suppression

The disconnect push (still delivered by `WebChatInterface`'s notifier) is now suppressed only when a
subscriber has **acknowledged** receiving the `turn_ended` seq — either implicitly by consuming the
stream through `turn_ended`, or explicitly via `POST /api/v1/chat/ack`. Enqueuing is not treated as
delivery; the default is to fire the push, so an offline recipient always gets it.

### Producer-side normalization

LaTeX normalization now runs in the producer (`turn_producer.py`) before events are published, so
every subscriber — original or resumed — sees identical normalized text. The hub buffers typed
events, not pre-serialized SSE bytes, so the wire format can evolve without invalidating buffers.

### Authorization: conversations are single-user on the hub

On the web/iOS surface a conversation is owned by exactly one user: each client mints its own
client-side UUID `conversation_id`, and `_ensure_user_owns_conversation` is the **sole**
authorization boundary (404, not 403, on mismatch). Because of that, the hub does **no**
per-subscriber filtering: every registered subscriber is entitled to every event. The check enforces
that invariant:

- Create/read endpoints (`POST /turns`, `POST /send_message`, `GET /messages`) require the caller be
  *an* owner, or the conversation to be brand-new.
- Subscribe/ack endpoints (`GET /stream`, `POST /ack`) require the caller be the **sole** owner of a
  non-empty conversation. A genuinely multi-author conversation (e.g. a Telegram group `chat_id`,
  which `get_conversation_owner_ids` legitimately returns several owners for) is refused — the
  unfiltered hub would otherwise leak co-owners' turns to each other.
- An empty (brand-new) conversation is allowed to subscribe: the always-on live-update stream
  attaches to the user's own new conversation before any message is sent. `conversation_id`s are
  unguessable UUIDs, so this is not a meaningful way to wait on someone else's future conversation.

### One turn at a time, on every turn-producing endpoint

A conversation runs one turn at a time. Two LLM loops over one history interleave their writes, and
the newcomer rebuilds history mid-flight: it sees the running turn's unanswered tool call and fills
it with the "abandoned" placeholder while the real result is still coming.

The reservation lives in the hub, not in the endpoint: `start_turn(reject_if_running=True)` refuses
registration under the same lock that performs it, so two concurrent kickoffs cannot both find the
conversation idle. Every endpoint that drives a turn takes it, which is what makes the invariant
hold across surfaces rather than within one of them:

- `POST /turns` reserves before launching its producer task; the producer's terminal `end_turn`
  (with the hub's done-callback as the safety net) releases it. Its record outlives the turn — the
  buffer replay, the ack and the push-suppression window all read it — and is aged out by
  `_prune_completed_turns`.
- `POST /send_message` reserves for the duration of `handle_chat_interaction` and ends the turn with
  a terminal status in a `finally`, so an exception, a 500, or a client hanging up mid-turn releases
  the conversation rather than wedging it at `running` (neither pruning nor eviction touches a
  running turn). It then **discards** the record: a non-streaming turn has no task, no deltas to
  replay and no ack to wait for, and keeping the record would burn its `turn_id` for a retry of a
  turn that failed without persisting a reply.

Rivals in either direction get the same 409 naming the running turn. A streaming turn is steerable,
so its rival can hand its prompt to the running turn instead; a `/send_message` turn is not — it
publishes no stream to carry the steer echo — so a client refused by one waits and resends. That is
tolerable because such a turn is short-lived and holds the reservation only while the request is in
flight; making it steerable would mean giving a headless caller an event stream it never reads.

`/send_message`'s idempotency is unaffected: it is settled from the persisted reply, not the hub
record, so a retry that already produced a reply returns it (`already_complete: true`) without
reaching the reservation at all.

## Deployment constraints (load-bearing)

This is a **single-process** FastAPI deployment serving 1–2 users. The hub is **in-memory only**:

- A reconnect must land on the same process. With one worker this is automatic.
- On a backend restart mid-turn, the live buffer is lost, but the user message is durably persisted
  and the next history reload shows the turn as interrupted — the same failure mode as before this
  change.

If the deployment grows (multiple workers, many users, public install), the hub must be promoted to
a durable, shared broker (e.g. Redis Streams or Postgres `LISTEN`/`NOTIFY`) and the resume URL must
be routed to the owning process (sticky sessions) or made broker-backed. The public API (`turn_id` +
per-conversation `seq`) is already shaped to allow that without a client-visible change.

## What this milestone does NOT yet do

- No "assistant is still thinking…" placeholder on reload driven by an `active_turns` surface — the
  `active_turns` field is returned by `GET …/messages` and the 410-out-of-buffer body, but the
  web/iOS UIs do not yet render a dedicated placeholder/affordance from it. (Milestone 1.)
- No live token-by-token resume wired through the clients on background→foreground (the plumbing
  exists; the iOS/web UX that re-attaches with a stored cursor is Milestone 2.)
- No multi-subscriber backpressure hardening. The server emits `stream_dropped` when it drops a
  subscriber whose queue overflowed (the SSE generator notices the dropped subscription on its next
  heartbeat tick and closes). The send-and-watch clients (web `useStreamingResponse.js` and the iOS
  `ChatViewModel`) now react to a mid-turn disconnect: `stream_dropped` or a transient 5xx on the
  subscribe GET resubscribes once from the last applied seq; a stream that closes without
  `turn_ended`, or a `410` (events rotated out of the buffer), reloads persisted history instead of
  surfacing an error, because the durable turn keeps running regardless of the client. The remaining
  Milestone 2 work is live token-by-token resume on background→foreground.

## Key files

- `src/family_assistant/web/conversation_stream_hub.py` — the broker.
- `src/family_assistant/web/turn_producer.py` — runs a turn, publishes events, fires the push.
- `src/family_assistant/web/routers/chat_api.py` — `POST /turns`, `GET …/stream`, `POST /ack`,
  `active_turns` on `GET …/messages`.
- `frontend/src/chat/useStreamingResponse.js` — two-step send + stream consumption.
- `frontend/src/chat/useLiveMessageUpdates.ts` — live updates over `…/stream?follow=true`.
- `ios/.../Chat/ChatAPIClient.swift`, `ChatViewModel.swift`, `SSEParser.swift` — iOS equivalent.
