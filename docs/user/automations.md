# Automations

**What's here:** making things happen automatically — when an event occurs, or on a schedule.

An automation has a **trigger** (an event, or a time) and an **action**. There are two kinds of
action:

- **Wake the assistant** — good when the response needs judgement, because the assistant reads the
  situation and decides what to do.
- **Run a script** — good when the response is fixed. Scripts run instantly, cost nothing per run,
  and behave identically every time. Ideal for logging, simple notifications, and data collection.

Use the `/automate` command for a mode focused on building and validating automations, or just ask
in an ordinary conversation.

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
- Above a threshold: `int(event.get('new_state', {}).get('state', '0').split('.')[0]) > 25`
- Any motion sensor: `event.get('entity_id', '').startswith('binary_sensor.motion')`

## Scheduled automations

For anything recurring, use a scheduled automation rather than a chain of one-off reminders:

- "Send me a daily briefing every morning at 8am."
- "Run a cleanup script every Sunday at midnight."
- "Execute a script every hour to check if any tasks are overdue."

Scheduling supports arbitrary recurrence rules — daily, weekly, monthly, and more complex patterns.
See [scheduling.md](scheduling.md) for the full picture, including one-off reminders and callbacks,
which are simpler than automations and often what you actually want.

## Managing automations

By conversation:

- "List all my automations."
- "Disable the garage door alert."
- "Delete the washing machine automation."
- "Show me the script for my temperature alert."
- "Convert my garage door automation to use a script instead."

Or in the web interface, on the **Automations** page: create automations with a visual form and live
script validation, review each one's configuration and execution history, edit conditions and
scripts, switch between action types, enable or disable, delete, and filter by type and status.

## Scripts in automations

Scripts are written in Starlark and can call the assistant's tools. See [scripting.md](scripting.md)
for the language and API reference.

Two things worth knowing:

- **Test before you deploy.** Ask the assistant to test a script with simulated tools, or with a
  sample event: "test this script with a sample temperature event". The test harness uses the real
  tool registry, keeps read-only tools real, simulates the tools that change state or send messages,
  and returns a transcript of which calls were real and which were simulated.
- **Tool availability is checked when the automation is saved,** so a script that validates will
  have the tools it needs when it later runs.

If a script calls an action that would normally need your approval — deleting a calendar event, say
— it doesn't run silently in the background. A confirmation lands in Telegram or the web UI and the
action happens once you approve.

A script can also call `wake_llm()` to hand off to the assistant conditionally, with context you
choose. That's a good middle ground: cheap filtering in the script, judgement only when it's
actually needed.

## Events from outside your household

An event trigger can carry content from outside — a webhook, an email — so the assistant turn it
wakes runs in a restricted event-handler mode. Notes it saves are quarantined under the `event_logs`
label and are not pulled into your main assistant's context; you can read them in the web interface.
If you want an event-driven result in a normally visible note, have a **script** automation write
the note rather than the woken assistant.

## Unattended operational diagnostics

A confined `ops_automation` profile exists for standing up a scheduled job that crawls recent error
logs, triages them, and records a summary note — without giving an unattended job broad access.

Set it up by asking from a trusted chat ("set up a daily log-triage automation"); handing work to
this profile asks for your confirmation first. The profile is deliberately narrow: it reads bounded
diagnostics, writes **only** notes labelled `ops_diagnostics` (enforced in storage, so it cannot
write elsewhere even if asked), and can create only script automations, never ones that wake the
full assistant.

Its triage notes are quarantined too, so log text that might contain injected content can't reach a
trusted conversation. You read the reports yourself in the web interface. Using it requires the
deployment to grant the `ops_diagnostics` label to whichever profiles should read the reports; see
[docs/design/profile-confined-note-writes-and-automation-approvals.md](../design/profile-confined-note-writes-and-automation-approvals.md).

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
