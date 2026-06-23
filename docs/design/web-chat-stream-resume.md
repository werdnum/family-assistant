# Web chat: resume a dropped reply stream instead of erroring

## Problem

The web chat UI surfaced **"Sorry, I encountered an error after running tools"** (or "the connection
was interrupted") for turns that actually **succeeded**.

Diagnosed from production: a turn ran two tool calls over ~19s, the browser's SSE stream dropped (an
edge-proxy idle cut) before the final assistant text + `turn_ended` reached the client, but the
backend completed and **persisted** the reply. The client treated the dropped stream as a failure
and rendered an error.

The original `useStreamingResponse` resume logic resubscribed **once** on a `stream_dropped` event,
did **not** resume on a plain interrupted EOF (the proxy-cut case), and on give-up called
`onError(...)` — so a perfectly good reply showed as an error.

## Why the first approach was wrong

An initial fix suppressed the optimistic error bubble while a history reload was pending and
restored it if the reload failed. This is a band-aid on the wrong layer: it accepts "show an error
on drop, then try to take it back," which is inherently racy. Five rounds of review found
progressively deeper edges (reload-fails, partial-text-clobber, aborted-reload, concurrent-send,
reload-beats-persist). Each was a symptom of the same root cause: **the client shows an error for a
recoverable condition.**

## The model (from the iOS client)

The recently-built iOS client (`ios/.../Chat/ChatViewModel.swift`) treats a drop as a **resumable,
durable-turn condition**, not an error. The backend contract already supports this on web too:

- `POST /api/v1/chat/turns` is idempotent on a client `turn_id` and returns `first_seq` /
  `already_complete`.
- `GET /stream?from_seq&ack_seq` with `follow=false` **drains the hub buffer and streams the
  in-flight turn through its `turn_ended`, then closes** — i.e. it holds the connection open while
  the turn is live (the edge proxy is what cuts it mid-turn). The hub replays events with
  `seq >= from_seq`.

So a drop is recovered by **resuming the subscription from `lastSeq + 1`** — the hub replays the
remaining tokens (final text + `turn_ended`) and the turn finishes cleanly. The client never
fabricates a completion or a failure for a turn it merely lost sight of; the final state always
comes from server truth.

## Implementation (`frontend/src/chat/useStreamingResponse.js` and `ChatApp.tsx`)

Replace the single-resubscribe + interrupt-error with a resume loop:

- Treat `dropped`, `interrupted`, and `subscribe_failed` (5xx) as **resumable**.
- Resume from `lastSeq + 1` (the hub's `from_seq` is inclusive).
- **Liveness-aware give-up**: a resume that advances **our turn's ack cursor** (not the
  conversation-wide `lastSeq`, which a concurrent turn also moves) resets the no-progress streak. A
  leg that merely stayed open for at least `STREAM_RESUME_LIVENESS_MS` is _ambiguous_ — the backend
  holds a `follow=false` stream open while **any** of the user's turns in the conversation is
  running (`_has_running_turn` is user+conversation scoped), so a held-open leg can belong to a
  concurrent tab's turn rather than ours. So a held-open leg that delivered nothing for our turn
  counts as liveness **only if** `onCheckTurnActive` confirms the server still reports _our_ turn
  running (a tiny `/messages?limit=1` `active_turns` check); otherwise it counts toward
  `MAX_NO_PROGRESS_RESUMES`. This keeps a long, quiet tool call of ours streaming while still
  bounding a gone turn — even one whose stream is held open by an unrelated concurrent turn. A slow
  `subscribe_failed` (5xx) is never liveness.
- On give-up there is no durable completion signal, so reconcile from persisted history and surface
  a "couldn't confirm the reply" marker **only if** the reconciled turn has no terminal reply
  (`onReloadHistory(convId, { errorIfNoReply: true, turnId })`). A `410` (buffer evicted) routes
  through the same path — it usually means the reply persisted, but can also fire while the turn is
  still running, so the unified reconcile handles both.
- The marker is **self-healing**, not a one-shot bubble. A give-up/`410` records the turn in
  `unconfirmedTurnsRef` (`turnId → convId`), and the single history reconcile
  (`loadConversationMessages`) **re-derives** the marker on *every* reload: it clears the turn once
  its reply lands, re-appends the marker while the turn is finished with no reply, and shows nothing
  while the server still reports the turn `running` (`active_turns`) — its reply is still coming. So
  a reply that arrives later (via the always-on follow stream's reload, a 410-while-running turn
  finishing, or any navigation) **replaces** the marker, and a reload can never silently **erase**
  it. The give-up also drops the turn's follow-stream self-skip so that turn's eventual `turn_ended`
  triggers the reconciling reload instead of being swallowed as "our own turn". Because the follow
  stream tails future-only and may be disconnected (so it can miss the `turn_ended`), a turn left
  `running` after a reconcile also drives a **bounded fallback poll** (`reconcilePollTuning`) that
  re-reloads history until it resolves — the reconcile is never solely dependent on the follow
  stream. Only the `failed` (history fetch itself failed) branch appends the marker on optimistic
  state immediately, since there is no server truth yet to reconcile; the turn stays unconfirmed so
  the next successful reload self-heals. This replaced an earlier approach that armed/re-armed the
  self-skip per outcome and special-cased spinner replacement and dedup — two Codex reviewers gave
  directly opposing P2s on that approach (preserve the error vs. let `turn_ended` reconcile the
  reply); the re-derive resolves both because it does whichever the latest authoritative history
  warrants. This is stricter than the iOS client, whose give-up reconcile is unconditionally silent.
- `onError` is otherwise reserved for genuine failures surfaced during the stream: a `turn_ended`
  carrying `status=failed` / an explicit `error` event (handled inside `consumeStream`), or a start
  failure / fatal (non-5xx) subscribe error.

The earlier suppress/restore subsystem in `ChatApp.tsx` is replaced by a single reconciliation path:
`useStreamingResponse` resumes the stream and asks for a history reconcile only when it cannot
observe durable completion, while `ChatApp.tsx` re-derives the self-healing unconfirmed-reply marker
from persisted history and `active_turns`.

## Tuning

| Constant                         | Value | Rationale                                                                                                                                   |
| -------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `STREAM_RESUME_INITIAL_DELAY_MS` | 250   | backoff seed between no-progress closes                                                                                                     |
| `STREAM_RESUME_MAX_DELAY_MS`     | 1000  | backoff cap                                                                                                                                 |
| `STREAM_RESUME_LIVENESS_MS`      | 1500  | held-open ≥ this ⇒ turn still live (resets streak)                                                                                          |
| `MAX_NO_PROGRESS_RESUMES`        | 4     | a still-running turn resets the streak, so the bound is only hit by a gone turn whose reply is already persisted ⇒ quick give-up is correct |

## Not adopted (yet)

iOS also renders cross-device turns into `local_follow_` bubbles reconciled by `turn_id`, and
advances the ack cursor only for its own turn. The web already has an always-on follow stream
(`useLiveMessageUpdates`); this change does not touch it. Bringing the web follow stream fully in
line with iOS's live-follow bubble reconciliation is possible future work but is out of scope here.
