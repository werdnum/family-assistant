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
- No multi-subscriber backpressure hardening / `stream_dropped` client handling beyond the event
  existing in the protocol. (Milestone 2.)

## Key files

- `src/family_assistant/web/conversation_stream_hub.py` — the broker.
- `src/family_assistant/web/turn_producer.py` — runs a turn, publishes events, fires the push.
- `src/family_assistant/web/routers/chat_api.py` — `POST /turns`, `GET …/stream`, `POST /ack`,
  `active_turns` on `GET …/messages`.
- `frontend/src/chat/useStreamingResponse.js` — two-step send + stream consumption.
- `frontend/src/chat/useLiveMessageUpdates.ts` — live updates over `…/stream?follow=true`.
- `ios/.../Chat/ChatAPIClient.swift`, `ChatViewModel.swift`, `SSEParser.swift` — iOS equivalent.
