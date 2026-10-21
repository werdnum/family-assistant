# Automations

**What's here:** making things happen automatically — when an event occurs, or on a schedule.

An automation has a **trigger** (an event, or a time) and an **action**. There are two kinds of
action:

- **Wake the assistant** — good when the response needs judgement, because the assistant reads the
  situation and decides what to do.
- **Run a script** — good when the response is fixed. A script that only reads state and writes a
  note runs quickly, costs nothing per run, and behaves identically every time, which makes it ideal
  for logging, simple notifications, and data collection. A script can also call the model or reach
  external services, and then those guarantees no longer hold.

When the assistant is woken and finds nothing worth your attention — the washer is still running,
the price hasn't dropped, the check came back clean — it can finish without messaging you. It still
does whatever the automation asked; it just doesn't report routine results. So say what you want to
hear about: "check the calendar every evening and only tell me if there's a clash" works as written.
Reminders you set are different: the first time a reminder fires, you always get it.

Just ask in an ordinary conversation. The assistant validates the trigger against real data and
tests the action before creating anything, so say what you want and it will check its own work.

## Event automations

- "Let me know when Alex arrives home."
- "Alert me if the garage door opens after 10pm."
- "Watch for when the washing machine finishes."
- "Notify me when any new documents are indexed."
- "Run a script to log all motion events to a note when motion is detected."
- "Send me a Telegram message when the temperature goes above 25°C during business hours."

### Building one that works

Explore before you commit:

1. **See what's actually happening:** "show me recent events from Home Assistant."
2. **Test a condition against real events:** "test if `person.alex` state changes to 'Home' would
   have triggered in the last day."
3. **Use exact field names** from the event data, with dot notation for nested fields
   (`new_state.state`).

Simple matching handles equality conditions (`entity_id` is `person.alex`). For anything more
involved — detecting a genuine state transition, a numeric threshold, a pattern across entities —
use a condition script, which receives the event and returns a boolean:

- Arriving home:
  `event.get('old_state', {}).get('state') != 'home' and event.get('new_state', {}).get('state') == 'home'`
- Above a threshold:
  `event.get('new_state', {}).get('state') not in (None, 'unavailable', 'unknown') and float(event['new_state']['state']) > 25`
  — screen out the non-numeric readings Home Assistant sends, then compare as a float so `25.9`
  counts as above 25
- Any motion sensor: `event.get('entity_id', '').startswith('binary_sensor.motion')`

## Scheduled automations

For anything recurring, use a scheduled automation rather than a chain of one-off reminders:

- "Send me a daily briefing every morning at 8am."
- "Run a cleanup script every Sunday at midnight."
- "Execute a script every hour to check if any tasks are overdue."

Scheduling supports arbitrary recurrence rules — daily, weekly, monthly, and more complex patterns.
See [scheduling.md](scheduling.md) for the full picture, including one-off reminders and callbacks,
which are simpler than automations and often what you actually want.

## Where the assistant replies

If your household has threads turned on (the web chat opens on **Home** — see
[Home and threads](interfaces.md#home-and-threads)), an automation you create in the web or iOS app
replies in your Home, whichever conversation you were in when you set it up. Reminders and callbacks
work the same way, and so do a script's `wake_llm()` hand-offs and the note you get when a script
automation fails. If you don't have a Home yet, the first reply creates one. You get a notification
as with any other reply.

There are two exceptions:

- **Created inside a thread:** the automation, reminder or callback stays with that thread, so it
  arrives alongside the work it belongs to.
- **Created in Telegram:** it replies in the chat where you created it.

To keep one in the conversation where you created it, say so: "remind me here tomorrow at 9", or
"check back in this conversation in an hour". Automations you set up before Home existed keep
replying where they always have.

In the web app you can also ask for each run to start a **new conversation**: "every morning at 7am,
start a new conversation with my daily briefing." Each run then becomes a thread of its own off your
Home, named after the automation and the day it ran, so it appears as a card in Home and in the
thread list; you can reply in it, and it can be finished like any other thread. Where threads aren't
in use, each run appears as its own conversation in the sidebar instead. On the Automations page
this is the "Start a new conversation each time" checkbox. A run started this way has no earlier
history, so the automation's prompt has to say everything the assistant needs. This option isn't
available in Telegram, where a chat is a single conversation.

## Managing automations

By conversation:

- "List all my automations."
- "Disable the garage door alert."
- "Delete the washing machine automation."
- "Show me the script for my temperature alert."
- "Convert my garage door automation to use a script instead."

Or in the web interface, on the **Automations** page: create automations with a visual form and live
script validation, filter by type and status, open one to review its configuration and execution
history, and enable, disable, or delete it.

To change an existing automation's conditions or script, ask the assistant — the web form creates
and inspects automations, but doesn't edit them in place.

### Automatic tidying

Some automations finish for good. The assistant removes them once they can no longer fire — a
listener waiting on a background worker that has already finished or died, or a schedule that has
run out of occurrences, such as one you gave an end date. A listener waiting on a worker that
overran the time it was given goes soon after that becomes clear, and within a week at the outside.

A schedule that repeats indefinitely is never removed for you, and neither is an automation you have
disabled — delete those yourself when you are done with them. If you want to keep a record of one
that has finished, save what matters to a note before its last run.

## Scripts in automations

Scripts are Python, running in a sandbox, and can call the assistant's tools. See
[scripting.md](scripting.md) for the language and API reference.

Two things worth knowing:

- **Test before you deploy.** Ask the assistant to test a script with simulated tools, or with a
  sample event: "test this script with a sample temperature event". The test harness uses the real
  tool registry, keeps read-only tools real, simulates the tools that change state or send messages,
  and returns a transcript of which calls were real and which were simulated.
- **Tool availability is checked when the automation is saved,** so a script that validates will
  have the tools it needs when it later runs.

If a script calls an action that would normally need your approval — deleting a calendar event, say
— it doesn't run silently in the background. A confirmation reaches you wherever you approve things
— Telegram, the web UI, or the iOS app — and the action happens once you approve.

A script can also call `wake_llm()` to hand off to the assistant conditionally, with context you
choose. That's a good middle ground: cheap filtering in the script, judgement only when it's
actually needed. Pass `new_conversation=True` to have that hand-off start afresh — a new thread of
your Home, or a new web conversation where threads aren't in use — rather than continue the current
one.

## Events from outside your household

An event trigger can carry content from outside — a webhook, an email — so the assistant turn it
wakes runs in a restricted event-handler mode. That includes a `wake_llm()` hand-off from a script
an event triggered. Notes it saves are quarantined under the `event_logs` label and are not pulled
into your main assistant's context; you can read them in the web interface. If you want an
event-driven result in a normally visible note, have a **script** automation write the note rather
than the woken assistant.

## Scheduled health checks

You can have the assistant watch its own error logs on a schedule and write up what it finds — ask
from a normal chat, for example "set up a daily log-triage automation". You'll be asked to confirm
before it's created.

The resulting reports are written as notes you read in the web interface. They are kept out of your
main assistant's context on purpose: error logs can contain text from outside your household, so
quarantining the reports stops that text reaching a trusted conversation. Ask your operator if you
want these reports readable in chat as well.

## Troubleshooting

**An event automation isn't firing:**

1. Check what events are actually arriving: "show me recent events from Home Assistant."
2. Test the condition against them: "test if `entity_id` equals `person.alex` would match recent
   events."
3. Confirm the field names match the event data exactly, using dot notation for nested fields.
4. Confirm the automation is enabled on the **Automations** page.
5. For a condition script, test it directly: "test this condition script with a sample event: …" —
   and remember it must return a boolean.

A common trap: Home Assistant emits `state_changed` events even when only an attribute changed. Use
a condition script comparing `old_state` and `new_state` to catch real transitions.

**A script isn't behaving:**

- Validate it before scheduling: "validate this script syntax: …"
- Check the **Tasks** page for execution logs and error messages, and the **Automations** page for
  run history.
- Handle missing or malformed inputs explicitly — event payloads vary more than you expect.
- Scripts time out after 10 minutes.
