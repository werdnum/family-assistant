# Failing forward when a chat delivery cannot succeed

## Status

Proposed.

## The problem

A delegation run that finishes but cannot be delivered is retried by `handle_delegation_run_cleanup`
every hour, forever, with no attempt limit, no backoff, and nothing user-visible. For a transient
failure that is correct. For a permanent one it is a silent hole: the run stays
`notified_at = NULL`, one warning per hour accumulates in the error log, and the person who asked
for the work never learns it finished.

This was reached in production by a delegation whose result was 5,589 characters —
`TelegramChatInterface.send_message` had no chunking, so Telegram refused it. That specific cause is
fixed separately (see the Telegram chunking change), but the retry loop it exposed is generic: any
permanently undeliverable notification behaves the same way.

Two things make the loop unable to resolve itself:

1. **The failure reason is discarded.** `ChatInterface.send_message` returns `str | None`; every
   implementation catches its own exceptions, logs them, and returns `None`. Callers know only
   *that* delivery failed, so no caller can distinguish "Telegram is down, try later" from "this
   message will never be accepted as written".
2. **Retrying is the only response.** `_force_notify_delegation` swallows
   `DelegationNotificationError` and leaves the run for the next hourly pass. There is no other
   outcome — not giving up, not adapting, not telling anyone.

## Approach: deliver the failure to the controlling agent

The delegating profile is a model with tools that already runs a full turn over the delegation
result (`_wake_source_profile_for_delegation`) and decides what to say. When its reply cannot be
delivered, the natural owner of that problem is the same model: tell it the delivery failed and why,
and let it decide what to do — restate the result briefly, save it as a note and link to it, or
accept that the channel is unusable.

That only works if the failure reason survives the transport layer, so the protocol change comes
first and is useful on its own.

## Milestone 1: make delivery failures speak

`ChatInterface.send_message` returns `str` and raises `ChatDeliveryError` on failure, instead of
returning `None`:

```python
class ChatDeliveryError(RuntimeError):
    """A chat interface could not deliver a message."""

    def __init__(self, message: str, *, transient: bool) -> None: ...
```

`transient` is the interface's judgement about whether the identical send could succeed later:

- **Telegram**: `NetworkError`, `TimedOut`, `RetryAfter` and 5xx are transient; `BadRequest`
  (message too long, chat not found, bot blocked, invalid parse) is not.
- **Web**: a failed database write is transient; a conversation that does not resolve is not.
- **Email**: SMTP 4xx transient, 5xx not.

Per the project's no-backwards-compatibility rule the `| None` return goes away entirely, and the
six call sites are updated to catch `ChatDeliveryError` where they currently test for `None`
(`task_worker` ×4, `tools/communication.py`, `email_intake/actions.py`). Their existing wrapper
errors — `DelegationNotificationError`, `ConfirmationNotificationError` — carry the reason and the
transient flag through.

Behaviour is otherwise unchanged by this milestone: everything that retried before still retries. It
is independently testable and independently useful (the hourly warning finally says *why*).

## Milestone 2: fail forward on permanent failures

In `_notify_delegation_if_needed` / `_deliver_delegation_wake_response`:

- **Transient failure** — unchanged. Leave `notified_at` NULL; the hourly cleanup retries the same
  text. Waking the model to rewrite a message because the network blipped would burn a turn and
  change a reply that was fine.
- **Permanent failure** — run one more source-profile turn whose trigger says the reply could not be
  delivered, names the interface and the reason, and states that the text is already saved in the
  conversation history. Its reply is delivered like any other. The model has its normal tools, so
  "save it as a note and send a one-line pointer" is available to it.

Bounding, so a failing channel cannot spin:

- **One fail-forward turn per delegation run.** A new `notify_attempts` column on `delegation_runs`
  (mirroring the existing `poll_attempts`), incremented per delivery attempt, with an Alembic
  migration.
- The wake checkpoint (`get_undelivered_terminal_reply`) is keyed on a turn id derived from the
  delegation id, so the fail-forward turn derives its own id from `(delegation_id, attempt)` —
  otherwise it would resume the undelivered first reply instead of running.
- **Floor.** If the fail-forward reply also fails permanently, send the short canned completion
  notice (a length- and formatting-safe pointer to the conversation), mark the run notified, and
  record the delivery failure as a technical problem. If even that fails, mark the run notified with
  the error recorded rather than retrying forever — the content is in message history and visible in
  the web UI either way.

Net effect: at most three sends and one extra LLM turn per undeliverable run, and no run that
retries indefinitely.

## Milestone 3: back off transient retries

Transient retries stay hourly today because the cleanup job is hourly. Once `notify_attempts`
exists, stretch them (1h, 2h, 4h, capped daily) so a long outage does not produce one failure log
per hour per stuck run. Optional; separable from the milestones above.

## What this does not change

- Delivery still happens before the recording transaction, for the reasons in
  `_notify_delegation_if_needed` — the ambient-transaction guard rejects a handle operation inside a
  transaction, and interfaces use their own handle while sending.
- The wake turn is still checkpointed so a retry never re-runs the source profile's tools.
- Confirmation and LLM-callback deliveries get the better error from milestone 1, but keep their
  current retry behaviour; fail-forward is scoped to delegation notifications, where a controlling
  agent exists to hand the failure to.

## Testing

- Interface-level: each implementation raises `ChatDeliveryError` with the right `transient` value
  for its characteristic failures (bot double for Telegram, as in the chunking change).
- Delegation-level, against the fake chat interfaces already used in
  `tests/functional/automations/`: a transient failure leaves the run unnotified for retry; a
  permanent failure runs exactly one fail-forward turn and delivers its reply; a permanent failure
  on the fail-forward reply delivers the canned notice and marks the run notified; no path attempts
  more than the bounded number of sends.
