# Quiet turns: letting the assistant choose not to message the user

**Status:** Approved. Milestone 1 (quiet ends on automation wakes) implemented. Milestone 3
implemented for web and iOS; the Telegram reaction and a cancel button are not built yet. **Date:**
2026-09-28

## Where things stood

Every turn the app runs on its own ends in a message to the user, whether or not there is anything
to say.

- **Automation and scheduled wakes** (`handle_llm_callback`, `task_worker.py`): whatever the model
  returns is sent through `_deliver_llm_callback_reply`. An empty reply on a non-reminder callback
  raises `RuntimeError("LLM failed to generate response content for callback.")`. That fails the
  task and **retries the whole turn**. So the only way to say nothing is to fail, and failing
  re-runs the tools.
- **Delegation completion wakes** (`_wake_source_profile_for_delegation`): an empty reply raises
  `DelegationNotificationError("...produced no response...")`. That leaves the run un-notified, so
  the cleanup sweep keeps retrying it.
- **Async handoff** (`_delegation_reference_text`, `tools/services.py`): the tool result tells the
  model to "Let the user know the work is in progress, then end your turn". Every long delegation
  therefore costs two messages: a "working on it" message, then the result.
- **Reminder follow-ups** already cancel themselves before any LLM call when the user has written
  since the reminder. That is the one existing silent path, and it is decided in code, not by the
  model.

The upshot is that the model has no honest way to say "nothing to report", so prompts and
automations are written around making it say *something*.

## The rule

> **The model may end a turn without messaging the user only when nobody is owed a reply by that
> turn.**

The trigger lineage decides who is owed. The model's tool list follows that decision. The model does
not get to make it.

| Turn started by                                                                               | Owed a reply?                                        | Quiet option                                                                   |
| --------------------------------------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------ |
| A user message                                                                                | Yes                                                  | Only **defer**, and only to a delegation this turn handed off (see scenario 2) |
| An automation, event listener, script `wake_llm`, or scheduled callback                       | No                                                   | `end_turn_quietly`                                                             |
| The first firing of a reminder the user set                                                   | Yes. They asked to be reminded.                      | None                                                                           |
| A script failure notice                                                                       | Yes. It is the only report that an automation broke. | None                                                                           |
| A reminder follow-up                                                                          | No. The user was already told once.                  | `end_turn_quietly`                                                             |
| A delegation completion that carries a deferred reply                                         | Yes, but only once all its siblings are done         | `end_turn_quietly` while siblings from the same source turn are still pending  |
| A delegation completion whose source turn was itself quiet-eligible (an automation delegated) | No                                                   | `end_turn_quietly`                                                             |
| A `failed_forward` delivery-failure wake                                                      | Yes                                                  | None                                                                           |

Tying the choice to the trigger is the enforcement point. A user-initiated turn is never shown the
tool, so no prompt wording can make the assistant ignore a person.

## Mechanism

**1. A tool, not a sentinel string.** The tool is `end_turn_quietly(reason: str)`, plus
`defer_reply(delegation_id)` on user turns. A magic reply such as `NO_REPLY` breaks as soon as a
model adds a word around it. A tool call is explicit, its argument is typed, and it records a
reason. The loop ends after the call, the same way it does after any terminal tool.

**2. The turn is still recorded.** The closing row is persisted as an internal assistant row
carrying the reason. Internal rows are already hidden from user-facing history and still replayed to
the model, which is exactly the quiet semantics, so no new column is needed. That gives us three
things:

- The next turn's history shows "checked the washer at 15:02, still running". The model won't repeat
  itself or wonder whether it already acted.
- The web and iOS UIs can later show quiet turns as a collapsed "checked in, nothing to report" row,
  as the audit trail for side effects the user wasn't told about. Until then the rows are in the
  database and visible to the engineer profile.
- Delivery bookkeeping can tell the difference. The existing checkpoint,
  `get_undelivered_terminal_reply`, treats "no interface id" as "generated but never sent". A caller
  that finds an internal closing row treats the turn as settled rather than re-sending it or running
  it again. When delegation wakes can end quietly (milestone 4), `mark_notified` should point at the
  quiet row and skip the push notification the same way.

**3. Remove the failure paths for empty replies.** Once the model has an explicit way to be quiet,
an empty reply *without* the tool is still a bug. It should keep failing loudly, but once, not
through a retry that re-runs the tools. `end_turn_quietly` becomes the only successful way to say
nothing.

**4. No new review or confirmation step.** Staying quiet removes a channel; it doesn't add one. It
can't exfiltrate anything, and the state-changing tools used during the turn are already governed by
the taint policy and tool-call review. The worst a prompt injection can do with it is suppress a
message the user wanted. That is an availability loss, it shows up as a visible quiet row, and the
owed-reply rule prevents it wherever a person is waiting. Under the reduce-friction direction,
that's an acceptable bounded residual, not a reason to add review.

## Scenario 1: an automation woke the assistant for nothing

Examples: "the washer sensor flickered", "check the calendar for conflicts" when there are none,
"add the bin reminder to the list" when that is done and no one needs to hear about it.

- The wake trigger text, the automation-creation skill and the callback trigger say plainly that
  quiet is allowed: *if nothing here needs the user's attention, call `end_turn_quietly` with a
  one-line reason.*
- Automations can say how they want to be reported. The existing `callback_context` or automation
  description carries it ("only tell me if X"). No new field is needed; the model reads it.
- This is also a useful signal. If an automation is quiet on 100% of its runs for 30 days, the
  automation's wake is probably misconfigured. This needs no new mechanism; it's a query over
  outcomes. It could be surfaced later in the automations UI. (Out of scope for this change.)

## Scenario 2: a delegation is pending

The handoff message ("I've started looking into that…") exists only because the user otherwise sees
nothing. Surface the pending work first, then let the model skip the filler.

**Step A: show pending delegations without the model.** A run with `handed_off_at` set and no
`notified_at` is pending. `delegation_runs` already records `conversation_id` and `source_turn_id`,
so no new state is needed.

- Web and iOS: a "Working on it: research profile, started 2 min ago" chip under the user's message,
  driven by the existing live-updates stream. It clears when the result arrives. A cancel button on
  it could reuse `cancel_requested_at`.
- Telegram: a 👀 reaction on the user's message (Bot API `setMessageReaction`), replaced by the
  reply. This matches how the project chat works for you here.
- Voice: unchanged. Voice already tells the user where the result will appear, and speaking is the
  only surface a voice call has.

**Step B: `defer_reply(delegation_id)`.** On a user turn, the model may defer instead of writing a
filler message, but only to a run that this same turn handed off and that is still pending. The tool
validates that. The reply debt moves onto the run.

- If the run completes, the completion wake owes the reply. It can't go quiet unless a sibling run
  from the same `source_turn_id` is still pending. Fan-out ("research these three") then produces
  one combined answer, not three.
- If the run fails, times out or is cancelled, the existing failure wake and the
  `delegation-delivery-fail-forward` path already tell the user. Deferring never turns into a lost
  answer.
- If the model wants to say something useful now ("while that runs, here's the short answer…"), it
  just replies normally. `defer_reply` is for when the only content would be filler.

The wake trigger text for a completion should list sibling runs still pending from the same source
turn. That way the model knows whether to answer now or wait.

## Scenario 3: other places this fits

- **Reminder follow-ups** where the user acknowledged the reminder some other way: they ticked the
  task, the calendar event passed, Home Assistant shows the bins went out. The code-level cancel
  only notices chat replies; the model can notice these. A quiet follow-up also ends the follow-up
  chain, since the model judged the reminder no longer needs the user.
- **Fan-out completions**: covered above. All but the last completion go quiet.
- **Stale delegation results**: the user already got the answer another way, or said "never mind"
  and the run finished anyway. The completion wake is quiet with the reason "superseded". This is
  only allowed when the conversation shows the debt was settled, meaning a later assistant reply
  answered it. Otherwise it's owed.
- **Forwarded or intake email that needs no action** (newsletters, receipts that were only filed):
  file it and stay quiet. This is the untrusted-content case where the residual above matters most.
  The quiet row keeps it visible.
- **`spawn_worker` completions** that land through an event listener. These are automation-shaped,
  so quiet is allowed when the worker's result was only an intermediate step for something already
  delivered.
- **Watch automations that poll** ("tell me when the price drops below $X"): nearly every run is
  quiet by design. Today these have to be written as scripts to avoid waking the LLM. With quiet
  turns, an LLM-judged condition becomes viable.

Not proposed: quiet on ordinary user turns ("ok thanks"). The chat already handles that with a short
reply. A reaction-only acknowledgement could be a separate small feature (Telegram reaction, web
emoji), but it doesn't belong to this change.

## Deliberate simplifications

- **An empty reply without the tool fails the callback task and is not retried.** A retry would
  re-run every tool the turn already committed. This also applies to reminders, which used to drop
  an empty reply without saying so.
- **A quiet call batched with other tools ends the turn once they finish.** The model doesn't see
  their results first. The tool description asks for it to be called on its own; a model that
  batches it has already decided.
- **Quiet rows stay in LLM history as they are.** A frequently firing automation could crowd the
  history. If it does, exclude all but the latest quiet row per automation, or fold them into one
  line. Not worth building up front.
- **Owed is judged per trigger type, not by reading the conversation.** The one exception is the
  stale-result case, where "settled" means a later assistant reply exists. We won't try to judge
  whether that reply really answered the question.
- **The model is trusted on "nothing worth saying" in the not-owed cases.** A wrong call costs one
  missed FYI, which the quiet row records.

## Work plan

1. **Quiet outcome plumbing.** Add the outcome marker on the terminal row. Make the delivery
   checkpoint and `mark_notified` treat quiet as closed. Add `end_turn_quietly`, advertised only on
   not-owed triggers. Remove the retry-on-empty paths. *Verified by:* functional tests for a
   callback that ends quietly (no send, no retry, row recorded), a user turn that never sees the
   tool, and a delegation wake with a pending sibling.
2. **Prompts and docs.** Wake trigger texts, the automation-creation skill, `prompts.yaml`, and a
   line in the automations user guide. *Verified by:* an eval set of automation wakes with "nothing
   to report" and "must report" cases.
3. **Pending delegation surface.** Web/iOS chip and Telegram reaction, driven by `delegation_runs`.
   The read side is `GET /api/v1/chat/conversations/{id}/pending-delegations`, which lists runs from
   handoff until their result is delivered, with a child count for runs such as a council that are
   waiting on their own delegations. *Verified by:* API tests for which runs are listed, Playwright
   test that the chip appears on handoff and clears on the result, plus a Telegram interface test
   for the reaction.
4. **`defer_reply` plus the sibling-aware completion wake.** Build on PR #1295 (council of LLMs),
   which already makes a delegated run with children wait for all of them and wake once, so only its
   final turn reports back. Sibling-aware quiet wakes should reuse that rather than add a second
   mechanism. *Verified by:* functional tests for a fan-out of 3 producing one message, a failed
   deferred run still telling the user, and a deferral to another turn's run being rejected.

Milestones 1–2 cover scenario 1 on their own. Milestone 3 is useful without 4, because it gives
visible progress on long delegations even when the model still writes the handoff line.
